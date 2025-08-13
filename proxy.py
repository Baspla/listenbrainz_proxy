# requirements: fastapi, uvicorn[standard], httpx
import os
from typing import Iterable, Any, List, Dict, Optional

from fastapi import FastAPI, Request, Response
import httpx

TARGET_BASE = os.getenv("TARGET_BASE", "http://localhost:8080")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")


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

app = FastAPI()

# Upstream client (trust_env False to avoid unexpected proxy/env effects)
upstream_client = httpx.AsyncClient(follow_redirects=False, timeout=build_timeout(), trust_env=False)

# Optional dedicated client for webhook (separate to customize trust_env if needed)
webhook_client = httpx.AsyncClient(follow_redirects=False, timeout=build_timeout(), trust_env=False)

async def post_to_webhook(payload: List[Dict[str, Any]]) -> Optional[int]:
    """
    Post the playing_now payload to a Home Assistant Webhook.
    Returns status code on success, or None on failure or if not configured.
    """
    if not WEBHOOK_URL:
        return None
    try:
        # Send the same structure ListenBrainz uses so HA automations can access trigger.json.*
        # logging the payload for debugging
        print("Posting to webhook")
        resp = await webhook_client.post(
            WEBHOOK_URL,
            json={"payload": payload},
            headers={"Content-Type": "application/json"},
            timeout=build_timeout(),
        )
        return resp.status_code
    except Exception:
        # Intentionally swallow errors to not affect upstream forwarding
        return None

# Your custom handler for playing_now
async def handle_playing_now(payload: List[Dict[str, Any]]) -> None:
    # Place custom logic here (logging, event bus, etc.)
    # Example minimal touch:
    for entry in payload:
         tm = (entry or {}).get("track_metadata", {})
         artist = tm.get("artist_name")
         track = tm.get("track_name")
         print(f"NOW PLAYING: {artist} - {track}")

    _ = await post_to_webhook(payload)
    return

@app.api_route("/{full_path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy(request: Request, full_path: str):
    import logging
    logger = logging.getLogger("proxy")
    # Special handling for ListenBrainz submit-listens
    if request.method.upper() == "POST" and request.url.path == "/1/submit-listens":
        logger.info("POST /1/submit-listens erkannt")
        body_bytes = await request.body()
        logger.debug(f"Request-Body: {body_bytes!r}")

        listen_type = None
        payload = None
        try:
            data = await request.json()
            logger.debug(f"JSON-Body: {data!r}")
            listen_type = (data or {}).get("listen_type")
            payload = (data or {}).get("payload")
        except Exception as e:
            logger.warning(f"Fehler beim Parsen des JSON-Bodys: {e}")
            data = None

        # If playing_now detected, run local handler and send webhook to HA
        if listen_type == "playing_now" and isinstance(payload, list):
            logger.info("playing_now erkannt, handle_playing_now wird aufgerufen")
            await handle_playing_now(payload)

        # Forward unchanged to upstream and return its response
        upstream_url = httpx.URL(TARGET_BASE).join(request.url.path)
        if request.url.query:
            upstream_url = upstream_url.copy_with(query=request.url.query)
        logger.info(f"Leite Anfrage weiter an Upstream: {upstream_url}")

        incoming_headers = filter_headers(request.headers.items())
        client_host = request.client.host if request.client else None
        headers = list(incoming_headers)
        if client_host:
            existing_xff = request.headers.get("x-forwarded-for", "")
            xff_val = f"{existing_xff}, {client_host}" if existing_xff else client_host
            headers = [(k, v) for (k, v) in headers if k.lower() != "x-forwarded-for"]
            headers.append(("x-forwarded-for", xff_val))
            logger.debug(f"X-Forwarded-For gesetzt: {xff_val}")

        upstream_resp = await upstream_client.request(
            request.method, upstream_url, headers=headers, content=body_bytes
        )
        logger.info(f"Antwort von Upstream erhalten: Status {upstream_resp.status_code}")
        resp_headers = filter_headers(upstream_resp.headers.items())
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=dict(resp_headers),
            media_type=upstream_resp.headers.get("content-type"),
        )

    # Default proxy path
    upstream_url = httpx.URL(TARGET_BASE).join(request.url.path)
    if request.url.query:
        upstream_url = upstream_url.copy_with(query=request.url.query)
    logger.info(f"Leite generische Anfrage weiter an Upstream: {upstream_url}")

    incoming_headers = filter_headers(request.headers.items())
    client_host = request.client.host if request.client else None
    headers = list(incoming_headers)
    if client_host:
        existing_xff = request.headers.get("x-forwarded-for", "")
        xff_val = f"{existing_xff}, {client_host}" if existing_xff else client_host
        headers = [(k, v) for (k, v) in headers if k.lower() != "x-forwarded-for"]
        headers.append(("x-forwarded-for", xff_val))
        logger.debug(f"X-Forwarded-For gesetzt: {xff_val}")

    body_bytes = await request.body()
    logger.debug(f"Request-Body: {body_bytes!r}")
    upstream_resp = await upstream_client.request(
        request.method, upstream_url, headers=headers, content=body_bytes
    )
    logger.info(f"Antwort von Upstream erhalten: Status {upstream_resp.status_code}")
    resp_headers = filter_headers(upstream_resp.headers.items())
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=dict(resp_headers),
        media_type=upstream_resp.headers.get("content-type"),
    )
