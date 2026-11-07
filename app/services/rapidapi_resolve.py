"""RapidAPI fallback for link resolve (Instagram / TikTok / ...).

Standalone module — wired into the live bot as the last-resort
fallback in ``app.services.instagram.resolve_instagram_media``
(2026-11-08): call it when the four anonymous strategies in
``app.services.instagram.resolve_instagram_media`` all return "none".

Provider (listing verified alive 2026-11-08):
    "Auto download all in one" by coder2077
    https://rapidapi.com/coder2077/api/instagram-tiktok-youtube-downloader/details
    host: instagram-tiktok-youtube-downloader.p.rapidapi.com
    GET /fetch?url=<link>

Confirmed /fetch response shape (from the listing's docs page):
    {
      "ok": true,                    // read this, not the HTTP status
      "id": "DRsmm9UjKfH",
      "source": "instagram.com",
      "type": "video",               // "video" | "image" | "album" | "gif"
      "download_url": "https://scontent.cdninstagram.com/...",
      "width": 1080, "height": 1920,
      "thumbnail_url": "https://scontent.cdninstagram.com/...jpg",
      "duration": 46,
      "caption": "Ferrari 296 GTS top speed run"
    }
    - type "video"/"image"/"gif": file is the top-level ``download_url``.
    - type "album" (carousel / TikTok photo post): NO top-level
      ``download_url``; each ``items[n]`` has its own ``type`` and
      ``download_url``. This module takes the FIRST video item.
    - TikTok responses also carry ``music_url`` and a ``music`` object.
    - ``download_url`` is a signed platform CDN link: download it right
      after the call, do not store it for hours.
    - Failure: ``ok: false`` + short ``detail`` (400 private/deleted/
      unsupported, 422 missing param, 429 rate limit/credits, 5xx
      temporary — retry after a second, never billed).
    - YouTube is NOT served by /fetch (separate POST /youtube/download,
      15 credits). A YouTube link sent to /fetch is expected to fail
      with 400 "Invalid URL".

Credits (Basic plan, free): 1,000/month; Instagram post/reel = 2
credits (= 500 reels/month), Instagram story = 5, TikTok = 2.
Failed fetches cost 0. Rate limit: 10 requests/minute.

ASSUMPTION TO VERIFY (see test_rapidapi.py): for type "video" the
platform's direct file is a single merged progressive mp4 WITH audio,
so ``audio_url == video_url``. The docs do not state this explicitly
for Instagram /fetch (they do for YouTube downloads); the harness
verifies it with ffprobe (video+audio streams) on every returned URL.

SECRET HANDLING (hard rule):
- The API key comes ONLY from the environment variable RAPIDAPI_KEY.
  It is never hardcoded, never written to a file, never logged, and
  never appears in any error string. Log/error output carries only the
  HTTP status and the API's short ``detail`` field.
- A missing key raises RapidAPIKeyMissing before any network call.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date as _date

import httpx

log = logging.getLogger(__name__)

ENV_VAR = "RAPIDAPI_KEY"
RAPIDAPI_HOST = "instagram-tiktok-youtube-downloader.p.rapidapi.com"
RAPIDAPI_BASE = f"https://{RAPIDAPI_HOST}"
FETCH_PATH = "/fetch"
STRATEGY = "rapidapi"

# The API waits up to 30 s on the platform; the docs ask for >= 35 s.
DEFAULT_TIMEOUT = 35.0
_MAX_ATTEMPTS = 2  # one retry, only on 5xx / transport errors (never billed)

# Quota accounting (2026-11-08, user order): Basic free plan = 1,000
# credits/month; IG reel/post = 2, IG story = 5, TikTok = 2. Failed
# fetches cost 0, so only successful video fetches count. In-memory
# only — the counter resets on restart (Render free restarts wipe it);
# the monthly total is reconstructible from the per-fetch log lines.
# Warn at 80% (400 reel-credits) so the cap is never hit silently.
_QUOTA_MONTHLY_LIMIT = 1000
_QUOTA_WARN_AT = 400
_quota_used: dict[str, int] = {}  # "YYYY-MM" -> credits spent


def _quota_note(url: str) -> None:
    """Charge one successful video fetch and log the monthly total."""
    month = _date.today().strftime("%Y-%m")
    cost = 5 if "/stories/" in url else 2
    total = _quota_used.get(month, 0) + cost
    _quota_used[month] = total
    if total >= _QUOTA_WARN_AT:
        log.warning("rapidapi quota warning: %d/%d credits used in %s",
                    total, _QUOTA_MONTHLY_LIMIT, month)
    else:
        log.info("rapidapi quota: %d/%d credits used in %s",
                 total, _QUOTA_MONTHLY_LIMIT, month)


class RapidAPIKeyMissing(RuntimeError):
    """RAPIDAPI_KEY is not set in the environment — no call was made."""


class RapidAPIError(RuntimeError):
    """The /fetch call failed. Carries status + detail only — never a key."""

    def __init__(self, status: int, detail: str = ""):
        self.status = status
        self.detail = detail or ""
        super().__init__(f"rapidapi /fetch failed: HTTP {status}"
                         + (f" ({self.detail})" if self.detail else ""))


def _redact(text: str, key: str | None) -> str:
    """Defense in depth: scrub the key out of any string before logging."""
    if not text or not key:
        return text
    return text.replace(key, "***")


async def resolve_via_rapidapi(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    client: httpx.AsyncClient | None = None,
) -> tuple[str | None, str | None, str]:
    """Resolve one link through RapidAPI /fetch.

    Returns ``(audio_url, video_url, strategy)`` where strategy is always
    "rapidapi". On any failure (private/deleted/unsupported link, rate
    limit, transport error) returns ``(None, None, "rapidapi")`` and logs
    the HTTP status + short detail only. Raises RapidAPIKeyMissing when
    RAPIDAPI_KEY is unset — before any network call is made.

    For type "video" the single ``download_url`` is returned as BOTH
    audio_url and video_url (merged progressive mp4 assumption — verified
    by test_rapidapi.py with ffprobe). For type "album" the first video
    item's download_url is used. Image/gif types return (None, None).
    """
    key = os.environ.get(ENV_VAR)
    if not key:
        raise RapidAPIKeyMissing(
            f"{ENV_VAR} not set — set the {ENV_VAR} environment variable "
            "with your RapidAPI key (never put the key in code or chat)."
        )

    headers = {"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": key}
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)

    try:
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await client.get(
                    RAPIDAPI_BASE + FETCH_PATH,
                    params={"url": url},
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(1.0)
                    continue
                log.warning("rapidapi fetch transport error: %s",
                            type(exc).__name__)
                return None, None, STRATEGY
            if resp.status_code >= 500 and attempt < _MAX_ATTEMPTS:
                # 5xx: temporary, retry after a second, never billed.
                await asyncio.sleep(1.0)
                continue
            result = _parse_fetch_response(resp, key)
            if result[0] and result[1]:
                _quota_note(url)
            return result
        # Unreachable in practice (loop always returns), kept for safety.
        log.warning("rapidapi fetch transport error: %s",
                    type(last_exc).__name__ if last_exc else "unknown")
        return None, None, STRATEGY
    finally:
        if own_client:
            await client.aclose()


def _parse_fetch_response(
    resp: httpx.Response, key: str | None
) -> tuple[str | None, str | None, str]:
    """Parse one /fetch response. Logs status + detail only — never the key."""
    try:
        data = resp.json()
    except Exception:
        log.warning("rapidapi fetch: HTTP %s, non-JSON body", resp.status_code)
        return None, None, STRATEGY
    if not isinstance(data, dict):
        log.warning("rapidapi fetch: HTTP %s, unexpected JSON shape",
                    resp.status_code)
        return None, None, STRATEGY
    if not data.get("ok"):
        detail = _redact(str(data.get("detail") or ""), key)
        if resp.status_code == 429:
            log.warning("rapidapi quota/rate-limit: HTTP 429, detail=%s",
                        detail)
        log.info("rapidapi fetch: HTTP %s, ok=false, detail=%s",
                 resp.status_code, detail)
        return None, None, STRATEGY

    media_type = data.get("type")
    if media_type == "album":
        for item in data.get("items") or []:
            if isinstance(item, dict) and item.get("type") == "video" \
                    and item.get("download_url"):
                dl = item["download_url"]
                return dl, dl, STRATEGY
        log.info("rapidapi fetch: album with no video item")
        return None, None, STRATEGY
    if media_type != "video":
        # image / gif / anything else: no audio track to identify.
        return None, None, STRATEGY
    dl = data.get("download_url")
    if not dl:
        log.info("rapidapi fetch: type=video but no download_url")
        return None, None, STRATEGY
    return dl, dl, STRATEGY


__all__ = [
    "ENV_VAR",
    "RAPIDAPI_HOST",
    "STRATEGY",
    "RapidAPIError",
    "RapidAPIKeyMissing",
    "resolve_via_rapidapi",
]
