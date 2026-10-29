"""YouTube as a full-song fallback source.

Proven 2026-10-29: yt-dlp downloads real full audio (e.g. Tipsyferi - Parvaz,
176s AAC) even from sandboxed egress. Used ONLY as a fallback after the WP
sites, because it is slower (~10-20s) and typically 128k.

Honesty gates (same as WP path):
- video title must token-match artist+title (no wrong-song delivery)
- version tokens must agree (no remix sent as original)
- magic bytes + ffprobe duration >= FULL_SONG_MIN_SECONDS
- any failure -> None (never a fake result)
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from ..bot.logic import (
    carries_version_tokens,
    title_has_extra_tokens,
    token_overlap_ratio,
)
from ..logging_config import get_logger, log_event
from .fullsong import FULL_SONG_MIN_SECONDS, _looks_like_audio, _fingerprint_duration as _probe_duration_sec

log = get_logger("youtube_full")


from ..bot.logic import (
    _distinctive_tokens,
    carries_version_tokens,
    identified_version_tokens,
    token_overlap_ratio,
)
from ..logging_config import get_logger, log_event
from .fullsong import FULL_SONG_MIN_SECONDS, _looks_like_audio, _fingerprint_duration as _probe_duration_sec

log = get_logger("youtube_full")

# Extra words in a YouTube title that prove it is NOT the original version.
_VERSION_WORDS = {
    "remix", "cover", "live", "acoustic", "instrumental", "sped", "slowed",
    "edit", "flip", "bootleg", "mashup", "rework", "karaoke", "8d",
}


def _title_ok(video_title: str, uploader: str, artist: str, title: str) -> bool:
    """Strict YouTube match: title tokens all present, artist in title or
    uploader, and no version words (remix/cover/live...) beyond the wanted
    title. 'Artist - Title (Official Video)' is fine."""
    vt = (video_title or "").strip()
    if not vt or not (title or "").strip():
        return False
    # 1. every distinctive title token must be in the video title
    if token_overlap_ratio(title, vt) < 0.99:
        return False
    # 2. wanted version tokens must be present
    if not carries_version_tokens(title, vt):
        return False
    # 3. extra tokens (beyond title+artist+wanted-version) must not be version words
    title_toks = set(_distinctive_tokens(title))
    artist_toks = set(_distinctive_tokens(artist or ""))
    version_toks = set(identified_version_tokens(title))
    for t in _distinctive_tokens(vt):
        if (t not in title_toks and t not in artist_toks
                and t not in version_toks and t in _VERSION_WORDS):
            return False
    # 4. artist must appear in the video title or the uploader/channel
    a = (artist or "").strip().lower()
    if a:
        hay = f"{vt} {uploader or ''}".lower()
        if token_overlap_ratio(a, hay) >= 0.5:
            return True
        flat_hay = "".join(hay.split())
        flat_a = "".join(a.split())
        if flat_a and flat_a in flat_hay:
            return True
        return False
    return True


def _download(query: str, artist: str, title: str,
              timeout_s: int = 90) -> tuple[bytes, str, str] | None:
    """Blocking yt-dlp download. Returns (bytes, video_title, uploader).

    Tries up to 5 search candidates (was: only the top hit). A video that
    blocks datacenter IPs ("Failed to extract any player response") is
    skipped and the next candidate is tried. For each candidate, tries
    android/ios/web player clients.
    """
    import yt_dlp

    last_error = ""
    # Get up to 5 candidates first (fast, no download)
    candidates = []
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                               "socket_timeout": 20, "retries": 1}) as ydl:
            info = ydl.extract_info(f"ytsearch5:{query}", download=False)
            for e in ((info or {}).get("entries") or [])[:5]:
                url = e.get("webpage_url") or e.get("id") or ""
                if url:
                    candidates.append({
                        "url": url,
                        "title": e.get("title") or "",
                        "uploader": e.get("uploader") or e.get("channel") or "",
                    })
    except Exception as exc:
        log_event(log, "youtube_full", stage="search_failed",
                  error=f"{type(exc).__name__}: {str(exc)[:60]}")
        return None

    if not candidates:
        return None
    # Title gate BEFORE download: skip candidates that don't match
    # artist+title (saves 10-20s per wrong video).
    filtered = [c for c in candidates
                if _title_ok(c["title"], c["uploader"], artist, title)]
    log_event(log, "youtube_full", stage="candidates",
              count=len(candidates), title_ok=len(filtered))
    if not filtered:
        log_event(log, "youtube_full", stage="title_rejected_all")
        return None
    candidates = filtered

    for idx, cand in enumerate(candidates):
        for player_client in ("android", "ios", "web"):
            tmpdir = tempfile.mkdtemp(prefix="ytfull_")
            outtmpl = os.path.join(tmpdir, "audio.%(ext)s")
            opts = {
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "outtmpl": outtmpl,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 20,
                "retries": 1,
                "extractor_args": {"youtube": {"player_client": [player_client]}},
            }
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info2 = ydl.extract_info(cand["url"], download=True)
                    if not info2:
                        continue
                    for f in os.listdir(tmpdir):
                        p = os.path.join(tmpdir, f)
                        if os.path.isfile(p) and os.path.getsize(p) > 100_000:
                            with open(p, "rb") as fh:
                                data = fh.read()
                            _cleanup(tmpdir)
                            log_event(log, "youtube_full", stage="client_ok",
                                      player_client=player_client,
                                      candidate_idx=idx)
                            return data, cand["title"], cand["uploader"]
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:80]}"
                # "Failed to extract any player response" = this video blocks
                # the IP; try next candidate instead of next client.
                if "Failed to extract any player response" in str(exc):
                    log_event(log, "youtube_full", stage="candidate_blocked",
                              candidate_idx=idx)
                    _cleanup(tmpdir)
                    break
            finally:
                _cleanup(tmpdir)
    if last_error:
        log_event(log, "youtube_full", stage="all_candidates_failed",
                  error=last_error)
    return None


def _cleanup(tmpdir: str) -> None:
    import shutil
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass


async def find_youtube_audio(artist: str, title: str) -> dict | None:
    """Return {'data','ext','duration_sec','source'} or None."""
    query = f"{artist} {title}".strip()
    if len(query) < 2:
        return None
    t0 = asyncio.get_event_loop().time()
    try:
        res = await asyncio.to_thread(_download, query, artist, title)
    except Exception as exc:
        log_event(log, "youtube_full", stage="error",
                  error=f"{type(exc).__name__}: {str(exc)[:60]}")
        return None
    ms = int((asyncio.get_event_loop().time() - t0) * 1000)
    if not res:
        log_event(log, "youtube_full", stage="not_found", ms=ms)
        return None
    data, vtitle, uploader = res
    if not _title_ok(vtitle, uploader, artist, title):
        log_event(log, "youtube_full", stage="title_rejected", ms=ms,
                  video_title=vtitle[:60], uploader=uploader[:40])
        return None
    if not _looks_like_audio(data):
        log_event(log, "youtube_full", stage="bad_bytes", ms=ms)
        return None
    dur = _probe_duration_sec(data)
    if dur < FULL_SONG_MIN_SECONDS:
        log_event(log, "youtube_full", stage="too_short", ms=ms,
                  duration_s=round(dur, 1))
        return None
    log_event(log, "youtube_full", stage="found", ms=ms,
              duration_s=round(dur, 1), bytes=len(data),
              video_title=vtitle[:60])
    return {"data": data, "ext": "m4a",
            "duration_sec": dur, "source": "youtube"}
