"""Audio fingerprint identification (AudD-style API + ACRCloud, env token only).

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

ACRCloud endpoint: POST https://{host}/v1/identify with HMAC-SHA1 signature.
Requires ACRCLOUD_HOST, ACRCLOUD_ACCESS_KEY, ACRCLOUD_ACCESS_SECRET env vars.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
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
    # Machine-readable failure detail for the "error" status, so an
    # extraction/validation failure is never misreported as "not_found".
    # Values: "" | silent | undecodable | too_short | lookup_error |
    #         service_error | no_ffmpeg
    detail: str = ""


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


# --- ACRCloud --------------------------------------------------------------

def _acrcloud_signature(access_secret: str, timestamp: str) -> str:
    """Build HMAC-SHA1 signature for ACRCloud identify request."""
    sign_str = "\n".join(["POST", "/v1/identify", access_secret,
                          "audio", "1", timestamp])
    # Note: ACRCloud docs use: method\nuri\naccess_key\ndata_type\nsignature_version\ntimestamp
    # Actually the correct format per docs is:
    # POST\n/v1/identify\n{access_key}\naudio\n1\n{timestamp}
    # signed with access_secret as key
    return base64.b64encode(
        hmac.new(access_secret.encode("utf-8"),
                 sign_str.encode("utf-8"),
                 hashlib.sha1).digest()
    ).decode("utf-8")


def parse_acrcloud_payload(payload: dict) -> AudioIdResult:
    """Parse ACRCloud identify response."""
    try:
        status = payload.get("status", {})
        if status.get("code") != 0:
            return AudioIdResult(status="not_found")
        metadata = payload.get("metadata", {})
        music = metadata.get("music", [])
        if not music:
            return AudioIdResult(status="not_found")
        # Take the highest-score result
        best = max(music, key=lambda m: m.get("score", 0))
        title = (best.get("title") or "").strip()
        artists = best.get("artists", [])
        artist = (artists[0].get("name") if artists else "").strip()
        album = (best.get("album", {}).get("name") or "").strip()
        if not title:
            return AudioIdResult(status="not_found")
        return AudioIdResult(
            status="identified", title=title, artist=artist,
            album=album, raw=best,
        )
    except Exception:
        return AudioIdResult(status="error")


async def identify_acrcloud(data: bytes, host: str, access_key: str,
                            access_secret: str,
                            client: httpx.AsyncClient | None = None) -> AudioIdResult:
    """Identify audio via ACRCloud (150M tracks, MENA coverage, sped-up tolerant)."""
    if not (host and access_key and access_secret):
        return AudioIdResult(status="needs_token")
    if not data:
        return AudioIdResult(status="error")
    try:
        timestamp = str(int(time.time()))
        # ACRCloud signature: HMAC-SHA1 of
        # POST\n/v1/identify\n{access_key}\naudio\n1\n{timestamp}
        # keyed with access_secret
        sign_str = f"POST\n/v1/identify\n{access_key}\naudio\n1\n{timestamp}"
        signature = base64.b64encode(
            hmac.new(access_secret.encode("utf-8"),
                     sign_str.encode("utf-8"),
                     hashlib.sha1).digest()
        ).decode("utf-8")

        url = f"https://{host}/v1/identify"
        files = {"sample": ("sample.wav", data, "audio/wav")}
        payload = {
            "access_key": access_key,
            "data_type": "audio",
            "signature_version": "1",
            "signature": signature,
            "timestamp": timestamp,
        }
        if client is not None:
            resp = await client.post(url, data=payload, files=files, timeout=60)
        else:
            async with httpx.AsyncClient(timeout=60) as c:
                resp = await c.post(url, data=payload, files=files)
        resp.raise_for_status()
        return parse_acrcloud_payload(resp.json())
    except Exception:
        return AudioIdResult(status="error")
