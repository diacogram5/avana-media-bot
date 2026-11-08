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
import os
import re as _re
from urllib.parse import quote

import httpx

from .. import timing as _timing
from ..logging_config import get_logger, log_event
from ..security import validate_url_for_fetch
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
    # P1-8: never fetch a page URL that fails the allowlist/DNS checks
    # (e.g. a host resolving to a private IP); no signal either way.
    if not await validate_url_for_fetch(page_url):
        return None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(endpoint, headers=DESKTOP_UA)
    except httpx.TimeoutException:
        return "timeout"
    except Exception:
        return "network_error"
    # P1-8: re-validate the URL actually fetched, after redirects, and
    # refuse the signal when the redirect target fails the checks.
    if not await validate_url_for_fetch(str(resp.url)):
        return None
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
    # P1-8: re-validate the URL actually fetched, after redirects — a
    # redirect to an unvalidated host yields "" (no body is used).
    try:
        if not await validate_url_for_fetch(str(resp.url)):
            return resp.status_code, "", len(resp.content)
    except Exception:
        pass
    nbytes = len(resp.content)
    if resp.status_code != 200:
        return resp.status_code, "", nbytes
    return resp.status_code, resp.text[:2_000_000], nbytes


def _base(page_url: str) -> str:
    return (page_url or "").split("?")[0].split("#")[0].rstrip("/") + "/"


async def _try_rapidapi(page_url: str, page_text: str, _t0: float
                        ) -> tuple[tuple[str, str, str, str, str] | None, str]:
    """RapidAPI /fetch fallback (2026-11-08).

    Returns (result_or_None, media_type). result is the full resolve
    5-tuple (audio_url, video_url, page_text, "rapidapi", media_type)
    on success (or on a shortcode-cache hit). media_type is "video" |
    "photo" | "carousel" | "unknown" — "unknown" when RapidAPI did not
    answer with a determinable type (key missing, transport, ok=false).
    Never raises.

    Behaviour (unchanged from the inline block it replaces): shortcode
    cache TTL 900 s, cap 128; quota accounting inside
    resolve_via_rapidapi (2 credits per reel, 5 per story); 35 s
    timeout; 1 retry on 5xx / transport only; fail-open. The signed
    CDN URL is never cached beyond the 15-min shortcode cache.

    LOUD (2026-11-08 fix): every failure path logs its reason, so a
    silent fallback is impossible. Reasons: key-missing (WARNING
    here); type image/gif, ok=false + detail, transport error (logged
    inside rapidapi_resolve.py at the point of failure, exactly one
    line each); any other exception (instagram_resolve_error,
    "rapidapi:" prefix). Never logs the key or its value.
    """
    import time as _time
    try:
        from .rapidapi_resolve import (
            RapidAPIKeyMissing,
            resolve_via_rapidapi,
        )
        _sc = _shortcode(page_url)
        _hit = _rapidapi_cache.get(_sc) if _sc else None
        if _hit and (_time.time() - _hit[3]) < _RAPIDAPI_CACHE_TTL:
            log_event(log, "instagram_resolve",
                      strategy="rapidapi", cached=True, media_type="video",
                      ms=int((_time.time() - _t0) * 1000))
            return (_hit[0], _hit[1], _hit[2] or page_text, "rapidapi",
                    "video"), "video"
        try:
            _ra_audio, _ra_video, _s, _mt = await resolve_via_rapidapi(
                page_url)
        except RapidAPIKeyMissing:
            log.warning("rapidapi key missing — fallback disabled")
            return None, "unknown"
        if _ra_audio and _ra_video:
            if _sc:
                if len(_rapidapi_cache) >= _RAPIDAPI_CACHE_MAX:
                    _rapidapi_cache.pop(next(iter(_rapidapi_cache)))
                _rapidapi_cache[_sc] = (
                    _ra_audio, _ra_video, page_text, _time.time())
            log_event(log, "instagram_resolve",
                      strategy="rapidapi", media_type=_mt,
                      ms=int((_time.time() - _t0) * 1000))
            return (_ra_audio, _ra_video, page_text, "rapidapi", _mt), _mt
        return None, _mt
    except Exception as exc:
        log_event(log, "instagram_resolve_error",
                  error=f"rapidapi:{str(exc)[:80]}")
        return None, "unknown"


def _rapidapi_test_chat_id() -> int | None:
    """Chat id for which RapidAPI is tried FIRST (2026-11-08).

    Env RAPIDAPI_TEST_CHAT_ID. Unset -> the owner's chat (OWNER_ID env,
    same source as Config.owner_id) so it is active for the owner as
    soon as this deploys. "0" disables it. Unparseable -> disabled.
    Read per call (cheap); never logs the value.
    """
    raw = os.environ.get("RAPIDAPI_TEST_CHAT_ID")
    if raw is None or raw == "":
        raw = os.environ.get("OWNER_ID", "")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None
    return val if val != 0 else None


async def _get_dash_page(client: httpx.AsyncClient, base: str, _t0: float,
                         cap_s: float = 3.0) -> tuple[int, str, int]:
    """Crawler fetch with early cutoff + ~3 s cap (2026-11-08 fail-fast).

    Streams the Googlebot page in 16 KB chunks and closes as soon as
    BOTH DASH URLs (audio + video AdaptationSet BaseURLs) are
    extractable — the proven Phase 2 cutoff (~40-55% faster in the
    sandbox). Returns (status, text, nbytes) like _get.

    - Stream completes without both URLs: the accumulated text IS the
      full page (identical to a full fetch) — no second fetch.
    - Cap hit (~3 s from resolve start): closes and returns
      (status, "", nbytes) — no usable page, so the caller moves on;
      the ms in the diag line shows the cap.
    - Stream error: one fast _get retry when <1 s elapsed, else
      (0, "", nbytes). The other strategies + RapidAPI still run.
    """
    import asyncio as _asyncio
    import time as _time
    _start = _time.time()
    try:
        async with client.stream("GET", base, headers=GOOGLEBOT_UA) as resp:
            status = resp.status_code
            if status != 200:
                return status, "", 0
            # P1-8: re-validate the URL actually fetched, after redirects,
            # BEFORE reading any body chunk.
            try:
                if not await validate_url_for_fetch(str(resp.url)):
                    return status, "", 0
            except Exception:
                pass
            buf = bytearray()
            _scanned = {"sets": -1}

            async def _read() -> bool:
                async for chunk in resp.aiter_bytes(16384):
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    n_sets = buf.count(b"AdaptationSet")
                    if n_sets >= 2 and n_sets != _scanned["sets"]:
                        _scanned["sets"] = n_sets
                        text = bytes(buf).decode("utf-8", errors="replace")
                        if (extract_dash_audio_url(text)
                                and extract_dash_video_url(text)):
                            return True
                return False

            try:
                _found = await _asyncio.wait_for(
                    _read(),
                    timeout=max(0.1, cap_s - (_time.time() - _t0)))
            except TimeoutError:
                # Cap hit: no usable page in budget — caller moves on.
                return status, "", len(buf)
            text = bytes(buf).decode("utf-8", errors="replace")
            return status, text, len(buf)
    except Exception:
        # Stream error: one fast full-fetch retry, else give up (the
        # other strategies + RapidAPI still run).
        if _time.time() - _start < 1.0:
            try:
                return await _get(client, base, GOOGLEBOT_UA)
            except Exception:
                pass
        return 0, "", 0


async def resolve_instagram_media(
        page_url: str, chat_id: int | None = None
) -> tuple[str | None, str | None, str, str, str]:
    """Return (audio_url, video_url, page_text, strategy, media_type).

    media_type is "video" | "photo" | "carousel" | "unknown": RapidAPI's
    /fetch type is the authority (video -> video; image/gif -> photo;
    album -> carousel); the anonymous strategies can only ever return
    video (a DASH manifest / media URL) or "unknown".

    Order (2026-11-08, reordered): (0) test-chat first try — when
    chat_id matches RAPIDAPI_TEST_CHAT_ID (default OWNER_ID), RapidAPI
    is tried FIRST, before any anonymous strategy; (1) crawler_dash
    FIRST among anonymous strategies (streamed with early cutoff,
    capped at ~3 s from resolve start); (2) on crawler HTTP 429,
    RapidAPI IMMEDIATELY (page/embed/embed_captioned skipped); (3)
    page, embed, embed_captioned — when page returns a login shell,
    the two middle strategies are SKIPPED (logged as skipped, reason
    login_shell); (4) RapidAPI at the end — skipped when the type is
    already determined (photo/carousel cannot become a video);
    (5) "none".

    A determined no-video type short-circuits the resolve to
    ("none", media_type) as soon as it is known — the owner's photo
    path finishes in ~1.5 s instead of ~10 s. A transient failure
    (media_type "unknown") still runs the full chain.

    Each attempt logs instagram_resolve_diag (status, bytes,
    login/dash/og_video booleans, ms); every instagram_resolve line
    carries media_type=... .
    """
    base = _base(page_url)
    page_text = ""
    media_type = "unknown"
    import time as _time
    _t0 = _time.time()
    # P1-8: refuse to fetch a page URL that fails the allowlist/DNS
    # checks at all; the per-response final-URL check lives in _get /
    # _get_dash_page. The RapidAPI branch below is unaffected (its host
    # is code-constant; only its returned media URLs are validated,
    # at the download call site).
    if not await validate_url_for_fetch(page_url):
        log_event(log, "instagram_resolve", strategy="none",
                  media_type="unknown",
                  ms=int((_time.time() - _t0) * 1000))
        return None, None, page_text, "none", "unknown"
    try:
        async with httpx.AsyncClient(timeout=_STRATEGY_TIMEOUT,
                                     follow_redirects=True) as client:
            # (0) Test-chat first try: the owner's chat tries RapidAPI
            # FIRST, without waiting for a 429.
            if chat_id is not None and chat_id == _rapidapi_test_chat_id():
                log_event(log, "instagram_resolve", strategy="rapidapi",
                          test_chat_first_try=True,
                          ms=int((_time.time() - _t0) * 1000))
                _ra, _mt = await _try_rapidapi(page_url, page_text, _t0)
                if _mt != "unknown":
                    media_type = _mt
                if _ra is not None:
                    return _ra
                if media_type in ("photo", "carousel"):
                    # Determined no-video post — no anonymous strategy
                    # can find a video; stop here (fail-fast).
                    log_event(log, "instagram_resolve", strategy="none",
                              media_type=media_type,
                              ms=int((_time.time() - _t0) * 1000))
                    return None, None, page_text, "none", media_type
            # (1) Crawler FIRST: Googlebot UA DASH manifest — the only
            # anonymous strategy returning media. Streamed with early
            # cutoff, capped at ~3 s.
            status, page, nbytes = await _get_dash_page(client, base, _t0)
            _login, _dash, _og = _diag_markers(page)
            log_event(log, "instagram_resolve_diag",
                      strategy="crawler_dash", status=status, bytes=nbytes,
                      login=_login, dash=_dash, og_video=_og,
                      ms=int((_time.time() - _t0) * 1000))
            # (2) Crawler 429 -> RapidAPI IMMEDIATELY (skip the
            # other anonymous strategies).
            if status == 429:
                _ra, _mt = await _try_rapidapi(page_url, page_text, _t0)
                if _mt != "unknown":
                    media_type = _mt
                if _ra is not None:
                    return _ra
                if media_type in ("photo", "carousel"):
                    log_event(log, "instagram_resolve", strategy="none",
                              media_type=media_type,
                              ms=int((_time.time() - _t0) * 1000))
                    return None, None, page_text, "none", media_type
            if page:
                if not page_text:
                    page_text = extract_page_text(page)
                audio_url = extract_dash_audio_url(page)
                video_url = extract_dash_video_url(page)
                if audio_url or video_url:
                    log_event(log, "instagram_resolve",
                              strategy="crawler_dash", media_type="video",
                              ms=int((_time.time() - _t0) * 1000))
                    return audio_url, video_url, page_text, \
                        "crawler_dash", "video"
            # (3) page, embed, embed_captioned — embed variants
            # skipped when page returns a login shell (fail-fast).
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
                              media_type="video",
                              ms=int((_time.time() - _t0) * 1000))
                    return media_url, media_url, page_text, name, "video"
                if name == "page" and _login:
                    # Fail-fast (2026-11-08): a login shell means the
                    # embed variants return the same shell — skip them.
                    for _sk in ("embed", "embed_captioned"):
                        log_event(log, "instagram_resolve_diag",
                                  strategy=_sk, status="skipped",
                                  bytes=0, login=False, dash=False,
                                  og_video=False, skipped=True,
                                  reason="login_shell",
                                  ms=int((_time.time() - _t0) * 1000))
                    break
            # (4) RapidAPI at the end (as before) — skipped when the
            # type is already determined (a re-fetch cannot turn a
            # photo/carousel into a video; transient failures stay
            # "unknown" and DO retry here).
            if media_type not in ("photo", "carousel"):
                _ra, _mt = await _try_rapidapi(page_url, page_text, _t0)
                if _mt != "unknown":
                    media_type = _mt
                if _ra is not None:
                    return _ra
    except Exception as exc:
        log_event(log, "instagram_resolve_error", error=str(exc)[:100])
    log_event(log, "instagram_resolve", strategy="none",
              media_type=media_type,
              ms=int((_time.time() - _t0) * 1000))
    return None, None, page_text, "none", media_type


# STEP 2 (2026-11-08): one video download/ffmpeg at a time — Render free
# tier (0.1 CPU, 512MB) cannot afford two concurrent merges stacking
# transients. Queue, never reject; the wait is log-only.
_VIDEO_DL_SEM = asyncio.Semaphore(1)


def _probe_streams(path: str) -> set[str]:
    """ffprobe stream types of a file on disk (e.g. {"video", "audio"})."""
    try:
        p = _timing.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, timeout=30)
        return {s.strip() for s in
                p.stdout.decode(errors="replace").splitlines() if s.strip()}
    except Exception:
        return set()


async def fetch_merged_video(video_url: str, audio_url: str | None = None,
                             timeout: int = 40,
                             max_mb: int = 50,
                             req_id: str | None = None
                             ) -> tuple[str | None, bool, object | None]:
    """Download an Instagram video (+ its separate audio stream when the
    manifest serves one) and return the merged file ON DISK.

    STEP 2 (2026-11-08): streams to disk in 64KB chunks — the merged
    bytes are NEVER held in RAM. Returns (path, has_audio, tmpdir);
    the CALLER owns tmpdir and must call tmpdir.cleanup() in a finally
    after the file is sent. (None, False, None) on any failure (the
    temp dir is cleaned up inside in that case).

    RapidAPI / crawler progressive shortcut: when audio_url == video_url
    (one single file) or there is no separate audio URL, the downloaded
    file is probed with ffprobe; if it already carries an audio stream
    it is sent directly and the mux is SKIPPED entirely. A genuine DASH
    pair (two different URLs) is still muxed with -c copy only.

    The download NEVER fails because of the audio step: a missing /
    unreachable audio stream or a failed mux falls back to the
    video-only file (previous behavior), so the healthy video path is
    preserved.
    """
    if _VIDEO_DL_SEM.locked():
        log_event(log, "instagram_merge", stage="queued", req_id=req_id)
    await _VIDEO_DL_SEM.acquire()
    try:
        return await _fetch_merged_video_locked(
            video_url, audio_url, timeout, max_mb, req_id)
    finally:
        _VIDEO_DL_SEM.release()


async def _fetch_merged_video_locked(
        video_url: str, audio_url: str | None, timeout: int,
        max_mb: int, req_id: str | None
) -> tuple[str | None, bool, object | None]:
    """fetch_merged_video body — runs under _VIDEO_DL_SEM. Never raises."""
    import asyncio as _asyncio
    import os as _os
    import tempfile as _tf
    import time as _time

    headers = {"User-Agent": "Mozilla/5.0",
               "Referer": "https://www.instagram.com/"}
    t0 = _time.time()
    tmp = _tf.TemporaryDirectory()
    try:
        async def _dl(url: str, path: str) -> int:
            """Stream one URL to path in 64KB chunks. Returns the byte
            count, or -1 on any failure (partial file left in tmp)."""
            total = 0
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("GET", url,
                                             headers=headers) as resp:
                        resp.raise_for_status()
                        with open(path, "wb") as f:
                            async for chunk in resp.aiter_bytes(65536):
                                if not chunk:
                                    continue
                                total += len(chunk)
                                if total > max_mb * 1024 * 1024:
                                    return -1
                                f.write(chunk)
                return total
            except httpx.HTTPStatusError as exc:
                # 2026-11-08: a 429 here means Render's IP is blocked on
                # the CDN too — log the status so it is reported at once.
                log_event(log, "instagram_merge", stage="stream_download",
                          status=exc.response.status_code,
                          ms=int((_time.time() - t0) * 1000))
                return -1
            except Exception:
                return -1

        same_url = bool(audio_url) and audio_url == video_url
        vp = f"{tmp.name}/v.mp4"
        ap = f"{tmp.name}/a.m4a"
        if same_url or not audio_url:
            # One file only: download it ONCE, probe it, skip the mux
            # when it already carries audio (progressive mp4).
            n = await _dl(video_url, vp)
            if n <= 0:
                log_event(log, "instagram_merge", stage="video_download",
                          ms=int((_time.time() - t0) * 1000),
                          error="empty_or_failed")
                tmp.cleanup()
                return None, False, None
            streams = _probe_streams(vp)
            if "video" not in streams:
                tmp.cleanup()
                return None, False, None
            log_event(log, "instagram_merge", stage="done",
                      ms=int((_time.time() - t0) * 1000),
                      bytes=n, has_audio="audio" in streams, mux="skipped")
            return vp, "audio" in streams, tmp

        # Genuine DASH pair: two different URLs, downloaded in parallel.
        vn, an = await _asyncio.gather(_dl(video_url, vp),
                                        _dl(audio_url, ap))
        if vn <= 0:
            log_event(log, "instagram_merge", stage="video_download",
                      ms=int((_time.time() - t0) * 1000),
                      error="empty_or_failed")
            tmp.cleanup()
            return None, False, None
        if an <= 0:
            # No audio stream: return video only (silent-video behavior).
            return vp, False, tmp
        op = f"{tmp.name}/out.mp4"
        r = _timing.run(
            ["ffmpeg", "-y", "-v", "error",
             "-i", vp, "-i", ap,
             "-c:v", "copy", "-c:a", "copy",
             "-shortest", "-movflags", "+faststart", op],
            req_id=req_id, logger=log, stage="ffmpeg_mux",
            capture_output=True, timeout=120)
        if r.returncode != 0 or not _os.path.exists(op):
            return vp, False, tmp
        # Validate: the muxed file must really carry both streams.
        if not {"video", "audio"} <= _probe_streams(op):
            return vp, False, tmp
        log_event(log, "instagram_merge", stage="done",
                  ms=int((_time.time() - t0) * 1000),
                  bytes=_os.path.getsize(op), has_audio=True, mux="copy")
        return op, True, tmp
    except Exception as exc:
        log_event(log, "instagram_merge", stage="mux",
                  ms=int((_time.time() - t0) * 1000), error=str(exc)[:80])
        try:
            tmp.cleanup()
        except Exception:
            pass
        return None, False, None
