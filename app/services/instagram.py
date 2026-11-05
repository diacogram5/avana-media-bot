"""Multi-strategy anonymous Instagram media resolver.

Why: Instagram's anonymous post page is usually a login-walled shell
with no media file, so a single fetch strategy fails on most reels.
Measured 2026-10-29 on reels DfDa4Epo8nr / DdEQzlco5lF / Ddayo2btjPI:
the desktop-UA page, /embed/ and /embed/captioned/ variants all return
the same ~510KB shell with zero video references, but the SAME page
fetched with a search-engine crawler user-agent embeds a DASH manifest
whose audio AdaptationSet <BaseURL> is a direct public .mp4 — real
audio bytes (216KB/27.8s, 151KB/18.9s, 63KB/6.6s), all fingerprintable.

All strategies are legitimate public no-login methods: ordinary page
fetches (the same requests any browser or search engine makes), the
public oEmbed-style embed endpoints, and the manifest Instagram itself
advertises in the page. No login cookies, no personal accounts, no
credential use, no ToS-breaking scraping.

Each attempt is logged with the strategy name so per-post success is
visible in the logs.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import httpx

from ..logging_config import get_logger, log_event
from .link_media import (
    extract_dash_audio_url,
    extract_dash_video_url,
    extract_media_url,
    extract_page_text,
)

log = get_logger(__name__)

DESKTOP_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
GOOGLEBOT_UA = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"}
_STRATEGY_TIMEOUT = 12

_OEMBED_ENDPOINT = "https://www.instagram.com/oauth/oembed/?url="
# Real gating markers an age/region-restricted reel's public oEmbed
# response carries (measured 2026-10-29 on reel Dd23bq7vkPr).
_GATING_MARKERS = ("geoblock_required", "MIN_AGE_ACCOUNT", "gating_type")


async def check_instagram_gating(page_url: str) -> str | None:
    """Classify an Instagram post's access restriction from its public
    oEmbed response.

    Returns "age_gated" | "private_or_deleted" | "timeout" |
    "network_error", or None when there is no restriction signal.
    Only explicit signals classify: an oEmbed JSON carrying gating
    markers (geoblock_required / gating_type / MIN_AGE_ACCOUNT) is
    age-gated; oEmbed 400/404 is private/deleted; a transport timeout
    and any other transport failure are reported distinctly. Anything
    ambiguous returns None so the caller falls back to its honest
    generic branch. Never raises.
    """
    endpoint = _OEMBED_ENDPOINT + quote(_base(page_url), safe="")
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(endpoint, headers=DESKTOP_UA)
    except httpx.TimeoutException:
        return "timeout"
    except Exception:
        return "network_error"
    if resp.status_code in (400, 404):
        # Instagram puts the age/region-gate signals INSIDE the 400 body
        # (measured 2026-10-29 on reel Dd23bq7vkPr: HTTP 400 with
        # geoblock_required / MIN_AGE_ACCOUNT in the JSON). Check the body
        # first — a gated post is never "private/deleted".
        try:
            _data = resp.json()
            _blob = json.dumps(_data, ensure_ascii=False) \
                if isinstance(_data, (dict, list)) else ""
        except Exception:
            try:
                _blob = resp.text or ""
            except Exception:
                _blob = ""
        if any(marker in _blob for marker in _GATING_MARKERS):
            return "age_gated"
        return "private_or_deleted"
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    blob = json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else ""
    if any(marker in blob for marker in _GATING_MARKERS):
        return "age_gated"
    return None


async def _get(client: httpx.AsyncClient, url: str, headers: dict) -> str:
    try:
        resp = await client.get(url, headers=headers)
    except Exception:
        return ""
    if resp.status_code != 200:
        return ""
    return resp.text[:2_000_000]


def _base(page_url: str) -> str:
    return (page_url or "").split("?")[0].split("#")[0].rstrip("/") + "/"


async def resolve_instagram_media(page_url: str) -> tuple[str | None, str | None, str, str]:
    """Return (audio_url, video_url, page_text, strategy).

    Strategies, in order: "page", "embed", "embed_captioned",
    "crawler_dash". strategy is "none" when nothing yielded media.
    page_text is the best title/caption found (for the channel query).

    - page/embed strategies: the advertised media URL is a progressive
      .mp4, so it serves as both the watchable video and the audio
      source for identification.
    - crawler_dash: the DASH manifest's audio AdaptationSet gives the
      audio bytes Shazam needs; the video AdaptationSet (best
      representation) gives the watchable video file to send the user.
    """
    base = _base(page_url)
    page_text = ""
    import time as _time
    _t0 = _time.time()
    try:
        async with httpx.AsyncClient(timeout=_STRATEGY_TIMEOUT,
                                     follow_redirects=True) as client:
            for name, url, headers in (
                ("page", base, DESKTOP_UA),
                ("embed", base + "embed/", DESKTOP_UA),
                ("embed_captioned", base + "embed/captioned/", DESKTOP_UA),
            ):
                page = await _get(client, url, headers)
                if not page:
                    continue
                if not page_text:
                    page_text = extract_page_text(page)
                media_url = extract_media_url(page)
                if media_url:
                    log_event(log, "instagram_resolve", strategy=name,
                              ms=int((_time.time() - _t0) * 1000))
                    return media_url, media_url, page_text, name
            # Last resort: crawler view embeds the DASH manifest with
            # separate audio/video streams.
            page = await _get(client, base, GOOGLEBOT_UA)
            if page:
                if not page_text:
                    page_text = extract_page_text(page)
                audio_url = extract_dash_audio_url(page)
                video_url = extract_dash_video_url(page)
                if audio_url or video_url:
                    log_event(log, "instagram_resolve",
                              strategy="crawler_dash",
                              ms=int((_time.time() - _t0) * 1000))
                    return audio_url, video_url, page_text, "crawler_dash"
    except Exception as exc:
        log_event(log, "instagram_resolve_error", error=str(exc)[:100])
    log_event(log, "instagram_resolve", strategy="none",
              ms=int((_time.time() - _t0) * 1000))
    return None, None, page_text, "none"


async def fetch_merged_video(video_url: str, audio_url: str | None = None,
                             timeout: int = 40,
                             max_mb: int = 50) -> tuple[bytes, bool]:
    """Download an Instagram DASH video stream and, when available, its
    separate audio stream, then mux both into a single MP4 with ffmpeg.

    Instagram's crawler-DASH manifest serves video and audio as SEPARATE
    streams; fetching only the video URL yields a SILENT video file.
    This helper fixes that at the single point where the sent video is
    built.

    Returns (bytes, has_audio). The download NEVER fails because of the
    audio step: a missing/unreachable audio stream or a failed mux falls
    back to the video-only bytes (previous behavior), so the healthy
    video path is preserved.
    """
    import asyncio as _asyncio
    import subprocess as _sp
    import tempfile as _tf
    import time as _time

    headers = {"User-Agent": "Mozilla/5.0",
               "Referer": "https://www.instagram.com/"}
    t0 = _time.time()

    async def _dl(url: str) -> bytes:
        """Download one stream. Returns b"" on any failure."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.content
                if data and len(data) <= max_mb * 1024 * 1024:
                    return data
        except Exception:
            pass
        return b""

    # Download video and audio IN PARALLEL (they're independent streams).
    # Sequential download was the 35s bottleneck.
    if audio_url:
        video_data, audio_data = await asyncio.gather(
            _dl(video_url), _dl(audio_url))
    else:
        video_data = await _dl(video_url)
        audio_data = b""

    if not video_data:
        log_event(log, "instagram_merge", stage="video_download",
                  ms=int((_time.time() - t0) * 1000), error="empty_or_failed")
        return b"", False

    if not audio_data:
        # No audio stream: return video only (silent-video behavior).
        return video_data, False

    def _mux() -> bytes:
        with _tf.TemporaryDirectory() as tmp:
            vp, ap, op = (f"{tmp}/v.mp4", f"{tmp}/a.m4a", f"{tmp}/out.mp4")
            open(vp, "wb").write(video_data)
            open(ap, "wb").write(audio_data)
            r = _sp.run(
                ["ffmpeg", "-y", "-v", "error",
                 "-i", vp, "-i", ap,
                 "-c:v", "copy", "-c:a", "aac",
                 "-shortest", "-movflags", "+faststart", op],
                capture_output=True, timeout=120)
            if r.returncode != 0 or not __import__("os").path.exists(op):
                return b""
            # Validate: the muxed file must really carry an audio stream.
            p = _sp.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type", "-of", "csv=p=0", op],
                capture_output=True, timeout=30)
            types = p.stdout.decode(errors="replace")
            if "audio" not in types or "video" not in types:
                return b""
            return open(op, "rb").read()

    try:
        merged = await _asyncio.to_thread(_mux)
    except Exception as exc:
        log_event(log, "instagram_merge", stage="mux",
                  ms=int((_time.time() - t0) * 1000), error=str(exc)[:80])
        merged = b""
    if merged:
        log_event(log, "instagram_merge", stage="done",
                  ms=int((_time.time() - t0) * 1000),
                  bytes=len(merged), has_audio=True)
        return merged, True
    return video_data, False
