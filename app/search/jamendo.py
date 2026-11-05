"""Jamendo catalog source (https://developer.jamendo.com/v3.0/docs).

Real capability: search Creative-Commons music; tracks carry `audio`
(streaming) and `audiodownload` (full MP3 download) URLs. No key ->
functions return [] (never fake results). Failures never raise to the
caller: they log and return [].

Env: JAMENDO_CLIENT_ID (free registration at developer.jamendo.com).
"""

from __future__ import annotations

import httpx

from ..logging_config import get_logger, log_event

log = get_logger("jamendo")

API = "https://api.jamendo.com/v3.0/tracks/"
_UA = {"User-Agent": "AvanoMediaBot/1.0"}


async def search_tracks(query: str, client_id: str, limit: int = 5,
                        client: httpx.AsyncClient | None = None) -> list[dict]:
    """Return [{title, artist, audio_url, download_url, jamendo_url}] or []."""
    if not client_id or len(query.strip()) < 2:
        return []
    params = {
        "client_id": client_id,
        "format": "json",
        "limit": limit,
        "search": query.strip(),
        "include": "musicinfo",
    }
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=20, headers=_UA) as c:
                resp = await c.get(API, params=params)
        else:
            resp = await client.get(API, params=params)
        if resp.status_code != 200:
            log_event(log, "jamendo", stage="http_error",
                      status=resp.status_code)
            return []
        items = (resp.json().get("results") or [])
        out = []
        for it in items:
            out.append({
                "title": (it.get("name") or "").strip(),
                "artist": (it.get("artist_name") or "").strip(),
                "audio_url": it.get("audio") or "",
                "download_url": it.get("audiodownload") or "",
                "jamendo_url": it.get("shareurl") or "",
            })
        log_event(log, "jamendo", stage="search", query=query[:40],
                  results=len(out))
        return out
    except Exception as exc:
        log_event(log, "jamendo", stage="error",
                  error=f"{type(exc).__name__}: {str(exc)[:60]}")
        return []
