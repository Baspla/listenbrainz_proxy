# Maloja Now-Playing Proxy

A tiny FastAPI reverse proxy that sits between my Navidrome ListenBrainz scrobbler and a target service (e.g., Maloja), with one extra trick: it catches ListenBrainz-style playing_now submissions and forwards them to a webhook so now-playing can be used in automations and side projects.

- Navidrome can be pointed at a custom ListenBrainz base URL, which lets it scrobble to self-hosted services like Maloja.
- Majola ignores playing_now messages.
- This proxy inspects POST /apis/listenbrainz/1/submit-listens, mirrors the request upstream unchanged, and, if listen_type=playing_now, also posts the payload to a webhook.

## Why
Maloja is great for self-hosted scrobbling but doesn’t use now-playing internally; this proxy forwards those ephemeral events to a webhook (e.g., Home Assistant) so they’re useable in dashboards, lights, notifications, etc.

## How it works
- Acts as a transparent reverse proxy to TARGET_BASE.  
- Intercepts ListenBrainz submit-listens on /apis/listenbrainz/1/submit-listens.  
- If listen_type is playing_now, logs “NOW PLAYING: artist - track” and POSTs the original payload to WEBHOOK_URL as JSON: {"payload": [...]}.  
- Forwards the original request to the upstream without modification (headers filtered for hop‑by‑hop).

ListenBrainz JSON notes:
- playing_now must not include listened_at and is temporary by design.

## Configuration
Environment variables:
- TARGET_BASE: Upstream base URL (default http://localhost:8080). Point this to Maloja’s ListenBrainz API path root (e.g., http://maloja:42010).
- WEBHOOK_URL: Optional webhook to receive playing_now payloads (e.g., Home Assistant webhook).

Optional timeouts (seconds):
- PROXY_TIMEOUT_CONNECT (default 5.0)  
- PROXY_TIMEOUT_READ (default 30.0)  
- PROXY_TIMEOUT_WRITE (default 30.0)  
- PROXY_TIMEOUT_POOL (default 5.0)

## Run
There is a ready made image at ghcr.io/baspla/listenbrainz_proxy:latest

Example:
- TARGET_BASE=http://maloja:42010  
- WEBHOOK_URL=https://homeassistant.example/api/webhook/now_playing

## Point Navidrome at the proxy
In Navidrome, enable scrobbling to ListenBrainz and set the base URL to this proxy’s ListenBrainz path, for example:
- http://proxy:8000/apis/listenbrainz/1/  
Navidrome supports configuring a custom ListenBrainz BaseURL, which is how people direct scrobbles to Maloja’s /apis/listenbrainz endpoint.

## Notes
- The proxy preserves headers (minus hop‑by‑hop) and appends x-forwarded-for.  
- playing_now payloads follow ListenBrainz’s format: minimal track_metadata with artist_name and track_name, no listened_at.
- This doesn’t store anything; failures to post to the webhook won’t block upstream forwarding.  
