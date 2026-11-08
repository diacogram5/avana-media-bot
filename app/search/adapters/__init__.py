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
    from .persian_music import PersianMusicAdapter
    return [
        RadioJavanAdapter(),  # Persian music, direct downloads, ~3-10s
        PersianMusicAdapter(),  # musicdel.ir etc., larger catalog, ~5-15s
        # WordPress REMOVED 2026-10-30: 5s timeout on every miss, zero hits
        # in production logs. Dead weight — just slows down the "not found"
        # path. RadioJavan alone is faster and more reliable.
        # YouTube removed 2026-10-30: "Sign in to confirm you're not a bot"
        # = complete IP block from Render. Wasted 3-4s per query.
    ]


# In-memory cache for full song files: "artist|title" -> file dict.
# Makes repeat requests instant (no re-download). HOTFIX 2026-11-08:
# capped at 2 entries + 10 min TTL to bound RAM on Render free tier
# (512MB) — 20 entries of full song bytes could reach ~300MB.
# Ephemeral (lost on restart) but speeds up repeated tests.
_FULLSONG_CACHE: dict[str, dict] = {}
_FULLSONG_CACHE_ORDER: list[str] = []
_FULLSONG_CACHE_MAX = 2
_FULLSONG_CACHE_TTL = 600
_FULLSONG_CACHE_TS: dict[str, float] = {}


def _fullsong_cache_key(artist: str, title: str) -> str:
    return f"{artist.strip().lower()}|{title.strip().lower()}"


async def find_full_song_file_v2(artist: str, title: str) -> dict | None:
    """Return {"data","ext","duration_sec","source","url","size"} or None.

    Same contract as find_full_song_file, but via parallel adapters.
    Checks in-memory cache first for instant repeat delivery.
    NOTE: No aggressive timeout — Persian sites need 5-15s. The 1.8s
    timeout from dc4aa2e4 broke all slow sources (always returned None).
    Reverted 2026-10-31.
    """
    import time as _time
    key = _fullsong_cache_key(artist, title)
    if key in _FULLSONG_CACHE:
        if _time.time() - _FULLSONG_CACHE_TS.get(key, 0) < _FULLSONG_CACHE_TTL:
            return _FULLSONG_CACHE[key]
        _FULLSONG_CACHE.pop(key, None)
        _FULLSONG_CACHE_TS.pop(key, None)
        try:
            _FULLSONG_CACHE_ORDER.remove(key)
        except ValueError:
            pass
    _t0 = _time.perf_counter()
    try:
        res: FileResult | None = await search_parallel(
            _adapters(), artist, title, first_win=True)
    except Exception:
        res = None
    _ms = int((_time.perf_counter() - _t0) * 1000)
    # Log timing
    try:
        from .base import log as _alog
        from ..logging_config import log_event as _le
        _le(_alog, "fullsong", stage="search_done", ms=_ms,
            artist=artist[:30], title=title[:30], found=bool(res))
    except Exception:
        pass
    if not res:
        return None
    out = {"data": res.data, "ext": res.ext,
           "duration_sec": res.duration_sec, "source": res.source,
           "url": res.url, "size": res.size}
    # Cache it (evict oldest if full; HOTFIX: max 2 entries + 10 min TTL)
    if key not in _FULLSONG_CACHE:
        if len(_FULLSONG_CACHE_ORDER) >= _FULLSONG_CACHE_MAX:
            old = _FULLSONG_CACHE_ORDER.pop(0)
            _FULLSONG_CACHE.pop(old, None)
            _FULLSONG_CACHE_TS.pop(old, None)
        _FULLSONG_CACHE_ORDER.append(key)
    _FULLSONG_CACHE[key] = out
    _FULLSONG_CACHE_TS[key] = _time.time()
    return out
