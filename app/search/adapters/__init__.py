"""Parallel orchestrator for full-song file search (v2 architecture).

Replaces the sequential find_full_song_file with modular adapters run in
parallel. First valid file wins — no waiting for slow sources.
"""

from __future__ import annotations

from .base import FileResult, search_parallel
from .radiojavan import RadioJavanAdapter
from .wordpress import WordPressAdapter
from .youtube import YouTubeAdapter


def _adapters() -> list:
    """All file-source adapters, fastest/most-reliable first."""
    return [
        RadioJavanAdapter(),  # Persian music, direct downloads, ~3-10s
        # WordPress REMOVED 2026-10-30: 5s timeout on every miss, zero hits
        # in production logs. Dead weight — just slows down the "not found"
        # path. RadioJavan alone is faster and more reliable.
        # YouTube removed 2026-10-30: "Sign in to confirm you're not a bot"
        # = complete IP block from Render. Wasted 3-4s per query.
    ]


# In-memory cache for full song files: "artist|title" -> file dict.
# Makes repeat requests instant (no re-download). Limited to 20 entries
# to avoid OOM on Render free tier (512MB). Ephemeral (lost on restart)
# but dramatically speeds up the user's repeated tests.
_FULLSONG_CACHE: dict[str, dict] = {}
_FULLSONG_CACHE_ORDER: list[str] = []
_FULLSONG_CACHE_MAX = 20


def _fullsong_cache_key(artist: str, title: str) -> str:
    return f"{artist.strip().lower()}|{title.strip().lower()}"


async def find_full_song_file_v2(artist: str, title: str) -> dict | None:
    """Return {"data","ext","duration_sec","source"} or None.

    Same contract as find_full_song_file, but via parallel adapters.
    Checks in-memory cache first for instant repeat delivery.
    """
    key = _fullsong_cache_key(artist, title)
    if key in _FULLSONG_CACHE:
        return _FULLSONG_CACHE[key]
    res: FileResult | None = await search_parallel(
        _adapters(), artist, title, first_win=True)
    if not res:
        return None
    out = {"data": res.data, "ext": res.ext,
           "duration_sec": res.duration_sec, "source": res.source}
    # Cache it (evict oldest if full)
    if key not in _FULLSONG_CACHE:
        if len(_FULLSONG_CACHE_ORDER) >= _FULLSONG_CACHE_MAX:
            old = _FULLSONG_CACHE_ORDER.pop(0)
            _FULLSONG_CACHE.pop(old, None)
        _FULLSONG_CACHE_ORDER.append(key)
    _FULLSONG_CACHE[key] = out
    return out
