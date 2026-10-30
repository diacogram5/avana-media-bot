"""WordPress music sites adapter.

Wraps the existing MUSIC_SITES search (musics-fa, minevisam, musicviral,
musicsweb). These are Iranian catalog sites with real MP3 downloads.
"""

from __future__ import annotations

import httpx

from ..fullsong import (
    FULL_SONG_MAX_MB,
    FULL_SONG_MIN_SECONDS,
    MUSIC_SITES,
    _fingerprint_duration,
    _looks_like_audio,
    _probe_bitrate_kbps,
    _site_candidates,
)
from .base import FileResult, SourceAdapter

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


class WordPressAdapter(SourceAdapter):
    name = "wordpress"
    timeout_s = 5  # was 10 — WordPress is backup, don't wait long

    async def search(self, artist: str, title: str) -> FileResult | None:
        queries = [q for q in (f"{artist} {title}".strip(), title.strip())
                   if len(q) >= 2]
        async with httpx.AsyncClient(timeout=10, headers=_UA,
                                     follow_redirects=True) as client:
            for q in queries:
                cands = []
                for base, style in MUSIC_SITES:
                    try:
                        c = await _site_candidates(
                            client, base, style, q, artist, title)
                        cands.extend(c)
                    except Exception:
                        continue
                if not cands:
                    continue
                cands.sort(key=lambda c: c[0], reverse=True)
                for _rank, url, source in cands[:3]:
                    ext = "m4a" if url.lower().endswith(".m4a") else "mp3"
                    try:
                        resp = await client.get(url, timeout=30)
                    except Exception:
                        continue
                    if resp.status_code != 200:
                        continue
                    data = resp.content
                    if not (100_000 <= len(data)
                            <= FULL_SONG_MAX_MB * 1024 * 1024):
                        continue
                    if not _looks_like_audio(data):
                        continue
                    duration = _fingerprint_duration(data)
                    if duration < FULL_SONG_MIN_SECONDS:
                        continue
                    br = _probe_bitrate_kbps(data, ext) or 0
                    return FileResult(
                        data=data, ext=ext, duration_sec=duration,
                        source=f"wordpress:{source}",
                        bitrate_kbps=br, title=title, artist=artist)
        return None
