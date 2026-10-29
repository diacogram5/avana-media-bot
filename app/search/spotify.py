"""Spotify Web API source (https://developer.spotify.com/documentation/web-api).

Real capability: track metadata, official Spotify links, 30-second
preview URLs. NO full-track downloads (by Spotify's design) — this module
never claims otherwise. Used for official links and previews when no full
file is reachable.

Auth: Client Credentials flow (free app at developer.spotify.com).
Env: SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET. No credentials -> [].
Token cached in-memory until expiry. Failures log and return [].
"""

from __future__ import annotations

import base64
import time

import httpx

from ..logging_config import get_logger, log_event

log = get_logger("spotify")

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SEARCH_URL = "https://api.spotify.com/v1/search"
_UA = {"User-Agent": "AvanoMediaBot/1.0"}

_token_cache: dict = {"token": "", "exp": 0.0}


async def _get_token(client_id: str, client_secret: str,
                     client: httpx.AsyncClient | None = None) -> str:
    now = time.time()
    if _token_cache["token"] and _token_cache["exp"] > now + 60:
        return _token_cache["token"]
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=20, headers=_UA) as c:
                resp = await c.post(
                    _TOKEN_URL, headers={"Authorization": f"Basic {basic}"},
                    data={"grant_type": "client_credentials"})
        else:
            resp = await client.post(
                _TOKEN_URL, headers={"Authorization": f"Basic {basic}"},
                data={"grant_type": "client_credentials"})
        if resp.status_code != 200:
            log_event(log, "spotify", stage="auth_error",
                      status=resp.status_code)
            return ""
        data = resp.json()
        _token_cache["token"] = data.get("access_token", "")
        _token_cache["exp"] = now + int(data.get("expires_in", 3600))
        return _token_cache["token"]
    except Exception as exc:
        log_event(log, "spotify", stage="auth_error",
                  error=f"{type(exc).__name__}: {str(exc)[:60]}")
        return ""


async def search_tracks(query: str, client_id: str, client_secret: str,
                        limit: int = 5,
                        client: httpx.AsyncClient | None = None) -> list[dict]:
    """Return [{title, artist, spotify_url, preview_url, album}] or []."""
    if not client_id or not client_secret or len(query.strip()) < 2:
        return []
    token = await _get_token(client_id, client_secret, client)
    if not token:
        return []
    try:
        params = {"q": query.strip(), "type": "track", "limit": limit}
        headers = {**_UA, "Authorization": f"Bearer {token}"}
        if client is None:
            async with httpx.AsyncClient(timeout=20, headers=headers) as c:
                resp = await c.get(_SEARCH_URL, params=params)
        else:
            # reuse caller's client but with auth header
            resp = await client.get(_SEARCH_URL, params=params, headers=headers)
        if resp.status_code != 200:
            log_event(log, "spotify", stage="http_error",
                      status=resp.status_code)
            return []
        items = ((resp.json().get("tracks") or {}).get("items") or [])
        out = []
        for it in items:
            artists = ", ".join(a.get("name", "") for a in it.get("artists", []))
            out.append({
                "title": (it.get("name") or "").strip(),
                "artist": artists.strip(),
                "spotify_url": ((it.get("external_urls") or {}).get("spotify") or ""),
                "preview_url": it.get("preview_url") or "",
                "album": ((it.get("album") or {}).get("name") or "").strip(),
            })
        log_event(log, "spotify", stage="search", query=query[:40],
                  results=len(out))
        return out
    except Exception as exc:
        log_event(log, "spotify", stage="error",
                  error=f"{type(exc).__name__}: {str(exc)[:60]}")
        return []
