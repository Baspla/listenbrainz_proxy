# requirements: fastapi, uvicorn[standard], httpx
import os
import time
import json
import logging
from contextlib import asynccontextmanager
from typing import Iterable, Any, List, Dict, Optional

from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import StreamingResponse
import httpx

# Configure Structured Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TARGET_BASE = os.getenv("TARGET_BASE", "http://localhost:8080")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Multi-worker safe configuration
CACHE_FILE = os.getenv("CACHE_FILE", "/tmp/lb_proxy_cache.json")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL", "900"))  # 15 minutes cache validation window

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade"
}

def filter_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(k, v) for (k, v) in headers if k.lower() not in HOP_BY_HOP]

def build_timeout() -> httpx.Timeout:
    def _f(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, default))
        except Exception:
            return default
    return httpx.Timeout(
        connect=_f("PROXY_TIMEOUT_CONNECT", 5.0),
        read=_f("PROXY_TIMEOUT_READ", 30.0),
        write=_f("PROXY_TIMEOUT_WRITE", 30.0),
        pool=_f("PROXY_TIMEOUT_POOL", 5.0),
    )

# File-backed cache management helper functions for Multi-Worker and TTL compliance
def _read_cache() -> dict:
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Cache file reading skipped or recovered from race condition: {e}")
    return {"last_track": None, "updated_at": 0}

def _write_cache(data: dict):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Failed writing state cache file: {e}")

def check_and_update_track(payload: List[Dict[str, Any]]) -> bool:
    if not payload:
        return False
    
    entry = payload[0] or {}
    tm = entry.get("track_metadata", {})
    artist = str(tm.get("artist_name", "")).strip().lower()
    track = str(tm.get("track_name", "")).strip().lower()
    
    if not artist and not track:
        return False

    current_key = f"{artist}||{track}"
    now = time.time()
    
    state = _read_cache()
    last_track = state.get("last_track")
    updated_at = state.get("updated_at", 0)

    # Trigger webhook if it is a brand new track OR the historical record has expired past the TTL
    if current_key == last_track and (now - updated_at) < CACHE_TTL_SECONDS:
        return False 
    
    _write_cache({"last_track": current_key, "updated_at": now})
    return True

def clear_track_cache():
    _write_cache({"last_track": None, "updated_at": 0})

# Instantiate clients globally
upstream_client = httpx.AsyncClient(follow_redirects=False, timeout=build_timeout(), trust_env=False)
webhook_client = httpx.AsyncClient(follow_redirects=False, timeout=build_timeout(), trust_env=False)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await upstream_client.aclose()
    await webhook_client.aclose()

app = FastAPI(lifespan=lifespan)

async def post_to_webhook(payload: List[Dict[str, Any]]) -> Optional[int]:
    if not WEBHOOK_URL:
        return None
    try:
        logger.info("Posting fresh track event to Home Assistant Webhook...")
        resp = await webhook_client.post(
            WEBHOOK_URL,
            json={"payload": payload},
            headers={"Content-Type": "application/json"},
            timeout=build_timeout(),
        )
        if resp.status_code != 200:
            logger.error(f"Webhook failed with status {resp.status_code}: {resp.text}")
        else:
            logger.info(f"Webhook successfully delivered: {resp.status_code}")
        return resp.status_code
    except Exception as e:
        logger.error(f"Error posting to webhook targets: {e}")
        return None

async def handle_playing_now(payload: List[Dict[str, Any]]) -> None:
    for entry in payload:
        tm = (entry or {}).get("track_metadata", {})
        artist = tm.get("artist_name")
        track = tm.get("track_name")
        logger.info(f"NOW PLAYING (INITIAL TRIGGER): {artist} - {track}")

    await post_to_webhook(payload)

@app.api_route("/{full_path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy(request: Request, full_path: str, background_tasks: BackgroundTasks):
    
    # 1. Secure URL Reconstruction (Fixes the sub-path stripping bug)
    target_stripped = TARGET_BASE.rstrip("/")
    path_stripped = request.url.path.lstrip("/")
    upstream_url = f"{target_stripped}/{path_stripped}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    # 2. Extract and format Proxy Headers
    incoming_headers = filter_headers(request.headers.items())
    client_host = request.client.host if request.client else None
    headers = list(incoming_headers)
    if client_host:
        existing_xff = request.headers.get("x-forwarded-for", "")
        xff_val = f"{existing_xff}, {client_host}" if existing_xff else client_host
        headers = [(k, v) for (k, v) in headers if k.lower() != "x-forwarded-for"]
        headers.append(("x-forwarded-for", xff_val))

    # Default payload containers for stream adjustments
    req_content: Any = None

    # 3. Handle ListenBrainz parsing or fall back to native Request Streaming
    if request.method.upper() == "POST" and request.url.path == "/apis/listenbrainz/1/submit-listens":
        try:
            # Explicit load required to parse JSON payload content
            body_bytes = await request.body()
            req_content = body_bytes
            
            data = json.loads(body_bytes) if body_bytes else {}
            listen_type = (data or {}).get("listen_type")
            payload = (data or {}).get("payload")
            
            if listen_type == "playing_now" and isinstance(payload, list):
                if check_and_update_track(payload):
                    background_tasks.add_task(handle_playing_now, payload)
                else:
                    logger.info("Duplicate 'playing_now' update suppressed via track TTL check.")
                    
            elif listen_type == "single":
                logger.info("Track completion scrobble recorded. Resetting track loop cache.")
                clear_track_cache()
                
        except Exception as e:
            logger.error(f"Error parsing ListenBrainz payload: {e}")
    else:
        # Optimization: Pass the incoming stream straight through if we don't need to intercept it
        has_body = "content-length" in request.headers or "transfer-encoding" in request.headers
        req_content = request.stream() if has_body else None

    # 4. Stream response back to client (Memory-Optimized)
    upstream_req = upstream_client.build_request(
        request.method, upstream_url, headers=headers, content=req_content
    )
    upstream_resp = await upstream_client.send(upstream_req, stream=True)
    
    async def stream_chunks():
        try:
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk
        finally:
            # Guarantees HTTP connection returns to the client pool safely after consumption
            await upstream_resp.aclose()

    resp_headers = filter_headers(upstream_resp.headers.items())
    return StreamingResponse(
        stream_chunks(),
        status_code=upstream_resp.status_code,
        headers=dict(resp_headers),
        media_type=upstream_resp.headers.get("content-type"),
    )