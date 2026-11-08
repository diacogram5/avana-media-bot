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
import re as _re
from urllib.parse import quote

import httpx

from .. import timing as _timing
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

# RapidAPI shortcode cache (2026-11-08, user order): a successful
# /fetch costs 2 credits (500 reels/month on the free plan), so a
# repeat of the SAME reel inside 15 min must NOT call /fetch again.
# In-memory only, TTL 900 s, cap 128 entries. The cached URL is a
# signed CDN link, so the TTL is deliberately short — it must never
# be served for hours. Keyed by shortcode (not the full URL) so a
# reel with different tracking params still hits.
_RAPIDAPI_CACHE_TTL = 900
_RAPIDAPI_CACHE_MAX = 128
_rapidapi_cache: dict[str, tuple[str, str, str, float]] = {}


def _shortcode(page_url: str) -> str:
    """Instagram shortcode from a reel/post URL, "" when unknown."""
    m = _re.search(r"instagram\.com/(?:reel|p|tv)/([A-Za-z0-9_-]+)",
                   page_url)
    return m.group(1) if m else ""


_OEMBED_ENDPOINT = "https://www.instagram.com/oauth/oembed/?url="
# Real gating markers an age/region-restricted reel's public oEmbed
# response carries (measured 2026-10-29 on reel Dd23bq7vkPr).
_GATING_MARKERS = ("geoblock_required", "MIN_AGE_ACCOUNT", "gating_type")

# Diagnostic markers for the per-strategy resolve log (booleans only —
# never page content). A logged-out / login-walled shell carries login
# links and "is_logged_in":false; a crawler page carrying media carries
# the DASH manifest; a progressive page carries og:video.
_LOGIN_SHELL_MARKERS = ("require_login", "/accounts/login/",
                        '"is_logged_in":false')


def _diag_markers(page: str) -> tuple[bool, bool, bool]:
    """(login-shell?, DASH-manifest?, og:video?) — booleans only."""
    return (any(m in page for m in _LOGIN_SHELL_MARKERS),
            "video_dash_manifest" in page or "AdaptationSet" in page,
            "og:video" in page)


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


async def _get(client: httpx.AsyncClient, url: str, headers: dict
               ) -> tuple[int, str, int]:
    """Fetch one strategy page: (HTTP status, body text, raw byte length).

    Diagnostic-only change: the old version swallowed the status
    ("" on any non-200 or transport error), which made a login shell,
    a 429/403 block and a timeout indistinguishable in the logs.
    Behaviour is unchanged: non-200 / error still yields "" text, and
    the body is still truncated to 2,000,000 chars. status 0 means no
    response arrived at all (transport error / timeout). No content,
    URL, token or caption is logged or stored here.
    """
    try:
        resp = await client.get(url, headers=headers)
    except Exception:
        return 0, "", 0
    nbytes = len(resp.content)
    if resp.status_code != 200:
        return resp.status_code, "", nbytes
    return resp.status_code, resp.text[:2_000_000], nbytes


def _base(page_url: str) -> str:
    return (page_url or "").split("?")[0].split("#")[0].rstrip("/") + "/"


async def resolve_instagram_media(page_url: str) -> tuple[str | None, str | None, str, str]:
    """Return (audio_url, video_url, page_text, strategy).

    Strategies, in order: "page", "embed", "embed_captioned",
    "crawler_dash". strategy is "none" when nothing yielded media.
    page_text is the best title/caption found (for the channel query).

    RapidAPI /fetch is the last-resort fallback when RAPIDAPI_KEY is
    set in the environment (2026-11-08); without the key the behaviour
    is exactly as before.

    - page/embed strategies: the advertised media URL is a progressive
      .mp4, so it serves as both the watchable video and the audio
      source for identification.
    - crawler_dash: the DASH manifest's audio AdaptationSet gives the
      audio bytes Shazam needs; the video AdaptationSet (best
      representation) gives the watchable video file to send the user.

    Diagnostic: every strategy attempt logs one "instagram_resolve_diag"
    line (HTTP status, response byte length, login-shell / DASH /
    og:video marker booleans) — log-only, no behaviour change.
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
                status, page, nbytes = await _get(client, url, headers)
                _login, _dash, _og = _diag_markers(page)
                log_event(log, "instagram_resolve_diag", strategy=name,
                          status=status, bytes=nbytes, login=_login,
                          dash=_dash, og_video=_og,
                          ms=int((_time.time() - _t0) * 1000))
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
            status, page, nbytes = await _get(client, base, GOOGLEBOT_UA)
            _login, _dash, _og = _diag_markers(page)
            log_event(log, "instagram_resolve_diag",
                      strategy="crawler_dash", status=status, bytes=nbytes,
                      login=_login, dash=_dash, og_video=_og,
                      ms=int((_time.time() - _t0) * 1000))
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
            # RapidAPI fallback (2026-11-08): only when the four
            # anonymous strategies all failed AND RAPIDAPI_KEY is set.
            # Fail-open: no key (or any API failure) leaves the
            # behaviour exactly as before (strategy "none"). The
            # signed CDN URL is never cached beyond the 15-min
            # shortcode cache — it expires.
            try:
                from .rapidapi_resolve import (
                    RapidAPIKeyMissing,
                    resolve_via_rapidapi,
                )
                _sc = _shortcode(page_url)
                _hit = _rapidapi_cache.get(_sc) if _sc else None
                if _hit and (_time.time() - _hit[3]) < _RAPIDAPI_CACHE_TTL:
                    log_event(log, "instagram_resolve",
                              strategy="rapidapi", cached=True,
                              ms=int((_time.time() - _t0) * 1000))
                    return _hit[0], _hit[1], _hit[2] or page_text, "rapidapi"
                try:
                    _ra_audio, _ra_video, _ = await resolve_via_rapidapi(
                        page_url)
                except RapidAPIKeyMissing:
                    _ra_audio = _ra_video = None
                if _ra_audio and _ra_video:
                    if _sc:
                        if len(_rapidapi_cache) >= _RAPIDAPI_CACHE_MAX:
                            _rapidapi_cache.pop(next(iter(_rapidapi_cache)))
                        _rapidapi_cache[_sc] = (
                            _ra_audio, _ra_video, page_text, _time.time())
                    log_event(log, "instagram_resolve",
                              strategy="rapidapi",
                              ms=int((_time.time() - _t0) * 1000))
                    return _ra_audio, _ra_video, page_text, "rapidapi"
            except Exception as exc:
                log_event(log, "instagram_resolve_error",
                          error=f"rapidapi:{str(exc)[:80]}")
    except Exception as exc:
        log_event(log, "instagram_resolve_error", error=str(exc)[:100])
    log_event(log, "instagram_resolve", strategy="none",
              ms=int((_time.time() - _t0) * 1000))
    return None, None, page_text, "none"


async def fetch_merged_video(video_url: str, audio_url: str | None = None,
                             timeout: int = 40,
                             max_mb: int = 50,
                             req_id: str | None = None) -> tuple[str, bool]:
    """Download an Instagram DASH video stream and, when available, its
    separate audio stream, then mux both into a single MP4 with ffmpeg.

    Instagram's crawler-DASH manifest serves video and audio as SEPARATE
    streams; fetching only the video URL yields a SILENT video file.
    This helper fixes that at the single point where the sent video is
    built.

    Phase 2 (2026-11-07, V6 design — prototype-verified): each stream is
    downloaded by its own fresh client (as before) but STREAMED in 64KB
    chunks straight to a temp file on disk — the full file is NEVER held
    in RAM (the old code held video + audio + merged bytes at once,
    ~100-150MB for a 50MB file on a 512MB instance). The mux copies
    BOTH streams (`-c:v copy -c:a copy`, no audio re-encode — DASH audio
    is already AAC) with `-movflags +faststart`.

    Returns (path, has_audio). ``path`` is the merged MP4 on disk (or
    the video-only file when there is no audio stream); the CALLER owns
    it and must delete the file and its parent temp dir after sending.
    Returns ("", False) when the video stream cannot be fetched. The
    download NEVER fails because of the audio step: a missing/unreachable
    audio stream or a failed mux falls back to the video-only file
    (previous behavior), so the healthy video path is preserved.
    """
    import asyncio as _asyncio
    import os as _os
    import shutil as _sh
    import tempfile as _tf
    import time as _time

    headers = {"User-Agent": "Mozilla/5.0",
               "Referer": "https://www.instagram.com/"}
    t0 = _time.time()
    tmp = _tf.mkdtemp(prefix="avano_ig_")
    vp, ap, op = (f"{tmp}/v.mp4", f"{tmp}/a.m4a", f"{tmp}/out.mp4")

    def _cleanup() -> None:
        try:
            _sh.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    async def _dl(url: str, path: str) -> int:
        """Stream one stream to disk. Returns bytes written, 0 on any
        failure or when the stream exceeds max_mb (aborts early)."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("GET", url,
                                         headers=headers) as resp:
                    resp.raise_for_status()
                    total = 0
                    with open(path, "wb") as fh:
                        async for chunk in resp.aiter_bytes(65536):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_mb * 1024 * 1024:
                                return 0
                            fh.write(chunk)
                    return total
        except httpx.HTTPStatusError as exc:
            # 2026-11-08: a 429 here means Render's IP is blocked on
            # the CDN too — log the status so it is reported at once.
            log_event(log, "instagram_merge", stage="stream_download",
                      status=exc.response.status_code,
                      ms=int((_time.time() - t0) * 1000))
            return 0
        except Exception:
            return 0

    # Download video and audio IN PARALLEL (they're independent streams).
    # Sequential download was the 35s bottleneck.
    if audio_url:
        video_bytes, audio_bytes = await _asyncio.gather(
            _dl(video_url, vp), _dl(audio_url, ap))
    else:
        video_bytes = await _dl(video_url, vp)
        audio_bytes = 0

    if not video_bytes:
        log_event(log, "instagram_merge", stage="video_download",
                  ms=int((_time.time() - t0) * 1000), error="empty_or_failed")
        _cleanup()
        return "", False

    if not audio_bytes:
        # No audio stream: return the video file as-is (silent-video
        # behavior). The caller owns the file + temp dir.
        try:
            _os.unlink(ap)
        except Exception:
            pass
        return vp, False

    def _mux() -> str:
        r = _timing.run(
            ["ffmpeg", "-y", "-v", "error",
             "-i", vp, "-i", ap,
             "-c:v", "copy", "-c:a", "copy",
             "-shortest", "-movflags", "+faststart", op],
            req_id=req_id, logger=log, stage="ffmpeg_mux",
            capture_output=True, timeout=120)
        if r.returncode != 0 or not _os.path.exists(op):
            return ""
        # Validate: the muxed file must really carry an audio stream.
        p = _timing.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", op],
            req_id=req_id, logger=log,
            capture_output=True, timeout=30)
        types = p.stdout.decode(errors="replace")
        if "audio" not in types or "video" not in types:
            return ""
        return op

    try:
        merged = await _asyncio.to_thread(_mux)
    except Exception as exc:
        log_event(log, "instagram_merge", stage="mux",
                  ms=int((_time.time() - t0) * 1000), error=str(exc)[:80])
        merged = ""
    if merged:
        log_event(log, "instagram_merge", stage="done",
                  ms=int((_time.time() - t0) * 1000),
                  bytes=_os.path.getsize(merged), has_audio=True)
        # Drop the intermediates; the caller owns the merged file.
        for _p in (vp, ap):
            try:
                _os.unlink(_p)
            except Exception:
                pass
        return merged, True
    return vp, False
