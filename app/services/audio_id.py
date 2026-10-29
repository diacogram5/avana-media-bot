"""Audio fingerprint identification (AudD-style API, env token only).

Honesty rule: this module NEVER invents an identification.
- No token  -> status "needs_token"
- API says no match -> status "not_found"
- API returns title/artist -> status "identified" (only then may the bot
  call the result identified; a catalog search result is never labelled
  "identified" by fingerprinting).

AudD endpoint (public API): POST https://api.audd.io/ with fields
api_token, return=apple_music,spotify and either `url` or a file upload.
For a sent Telegram file the caller passes raw bytes; for a link the
caller passes url. No key is ever logged.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

API_URL = "https://api.audd.io/"


@dataclass
class AudioIdResult:
    status: str  # identified | not_found | needs_token | error
    title: str = ""
    artist: str = ""
    album: str = ""
    raw: dict | None = None


def parse_audd_payload(payload: dict) -> AudioIdResult:
    result = payload.get("result") or {}
    if not result:
        return AudioIdResult(status="not_found")
    title = (result.get("title") or "").strip()
    artist = (result.get("artist") or "").strip()
    if not title:
        return AudioIdResult(status="not_found")
    return AudioIdResult(
        status="identified", title=title, artist=artist,
        album=(result.get("album") or "").strip(), raw=result,
    )


async def identify_bytes(data: bytes, filename: str, token: str,
                         client: httpx.AsyncClient | None = None) -> AudioIdResult:
    if not token:
        return AudioIdResult(status="needs_token")
    if not data:
        return AudioIdResult(status="error")
    try:
        if client is not None:
            resp = await client.post(
                API_URL, data={"api_token": token, "return": "apple_music,spotify"},
                files={"file": (filename, data)}, timeout=60)
        else:
            async with httpx.AsyncClient(timeout=60) as c:
                resp = await c.post(
                    API_URL, data={"api_token": token, "return": "apple_music,spotify"},
                    files={"file": (filename, data)})
        resp.raise_for_status()
        return parse_audd_payload(resp.json())
    except Exception:
        return AudioIdResult(status="error")


async def identify_url(url: str, token: str,
                       client: httpx.AsyncClient | None = None) -> AudioIdResult:
    if not token:
        return AudioIdResult(status="needs_token")
    try:
        payload = {"api_token": token, "url": url, "return": "apple_music,spotify"}
        if client is not None:
            resp = await client.post(API_URL, data=payload, timeout=60)
        else:
            async with httpx.AsyncClient(timeout=60) as c:
                resp = await c.post(API_URL, data=payload)
        resp.raise_for_status()
        return parse_audd_payload(resp.json())
    except Exception:
        return AudioIdResult(status="error")
