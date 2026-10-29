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


def _download(query: str, timeout_s: int = 90) -> tuple[bytes, str, str] | None:
    """Blocking yt-dlp download of the top search hit.
    Returns (bytes, video_title, uploader)."""
    import yt_dlp

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
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            entries = (info or {}).get("entries") or []
            if not entries:
                return None
            video = entries[0]
            vtitle = video.get("title") or ""
            uploader = video.get("uploader") or video.get("channel") or ""
            url = video.get("webpage_url") or video.get("id") or ""
            if not url:
                return None
            info2 = ydl.extract_info(url, download=True)
            if not info2:
                return None
            # find the downloaded file
            for f in os.listdir(tmpdir):
                p = os.path.join(tmpdir, f)
                if os.path.isfile(p) and os.path.getsize(p) > 100_000:
                    with open(p, "rb") as fh:
                        return fh.read(), vtitle, uploader
            return None
    except Exception:
        return None
    finally:
        for f in os.listdir(tmpdir):
            try:
                os.remove(os.path.join(tmpdir, f))
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


async def find_youtube_audio(artist: str, title: str) -> dict | None:
    """Return {'data','ext','duration_sec','source'} or None."""
    query = f"{artist} {title}".strip()
    if len(query) < 2:
        return None
    t0 = asyncio.get_event_loop().time()
    try:
        res = await asyncio.to_thread(_download, query)
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
