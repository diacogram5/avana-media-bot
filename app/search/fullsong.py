"""Full-version-first: for an identified song with no catalog file, find the
FULL same-version recording from existing public music sites — before
falling back to the user's own clip.

Web parity (findFullSongFile): anchored post search, version gate (a
different remix's file is never passed off as this recording), and a real
duration probe (>= 85 s of audio) so a short clip is never presented as
the "full version". No new dependencies.
"""
from __future__ import annotations

import re
import time

import httpx

from ..bot.logic import (
    carries_version_tokens,
    token_overlap_ratio,
)

MUSIC_SITES = (
    "https://musics-fa.com",
    "https://minevisam.ir",
    "https://musicviral.ir",
)

FULL_SONG_MIN_SECONDS = 85
FULL_SONG_MAX_MB = 30
_UA = {"User-Agent": "Mozilla/5.0 (compatible; AvanoMediaBot/1.0)"}

# Small process-local cache: key -> (data, ext, duration_sec, source, ts).
# Render restarts clear it; correctness never depends on it.
_CACHE: dict[str, tuple[bytes, str, float, str, float]] = {}
_CACHE_TTL = 30 * 60
_CACHE_MAX = 3

_FILE_RE = re.compile(r"https?://[^\s\"'<>]+\.(?:mp3|m4a)", re.IGNORECASE)


def _cache_get(key: str):
    row = _CACHE.get(key)
    if row and time.time() - row[4] < _CACHE_TTL:
        return row[:4]
    _CACHE.pop(key, None)
    return None


def _cache_put(key: str, data: bytes, ext: str, duration: float, source: str) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][4])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (data, ext, duration, source, time.time())


def _fingerprint_duration(data: bytes) -> float:
    """Lower-bound duration of real audio in the bytes, via landmarks."""
    try:
        from .landmark import fingerprint_bytes
    except Exception:
        return 0.0
    try:
        fp = fingerprint_bytes(data)
    except Exception:
        return 0.0
    if fp.get("status") != "ok":
        return 0.0
    return float(fp.get("duration_sec") or 0.0)


async def find_full_song_file(artist: str, title: str) -> dict | None:
    """Return {"data", "ext", "duration_sec", "source"} or None.

    Only the same recording: the version gate rejects other-remix files,
    and the duration probe rejects short clips.
    """
    key = f"fullsong:{artist}|{title}".lower()
    hit = _cache_get(key)
    if hit:
        data, ext, duration, source = hit
        return {"data": data, "ext": ext, "duration_sec": duration, "source": source}

    queries = [q for q in (f"{artist} {title}".strip(), title.strip()) if len(q) >= 2]
    candidates: list[tuple[float, str, str]] = []  # (rank, file_url, site)
    try:
        async with httpx.AsyncClient(timeout=15, headers=_UA, follow_redirects=True) as client:
            for q in queries:
                for site in MUSIC_SITES:
                    try:
                        resp = await client.get(
                            f"{site}/wp-json/wp/v2/search",
                            params={"search": q, "per_page": 5})
                    except Exception:
                        continue
                    if resp.status_code != 200:
                        continue
                    try:
                        data = resp.json()
                    except Exception:
                        continue
                    if not isinstance(data, list):
                        continue
                    for p in data[:5]:
                        if not isinstance(p, dict):
                            continue
                        pid, ptitle = p.get("id"), p.get("title") or ""
                        if not isinstance(pid, int) or not isinstance(ptitle, str):
                            continue
                        if ("آهنگ" not in ptitle and "ریمیکس" not in ptitle):
                            continue
                        if "بهترین" in ptitle:
                            continue
                        if not (token_overlap_ratio(artist, ptitle) >= 0.5
                                or token_overlap_ratio(title, ptitle) >= 0.5):
                            continue
                        if not carries_version_tokens(title, ptitle):
                            continue
                        # Post page -> real file links.
                        try:
                            pr = await client.get(
                                f"{site}/wp-json/wp/v2/posts/{pid}",
                                params={"_fields": "title,content"}, timeout=15)
                            if pr.status_code != 200:
                                continue
                            content = ((pr.json() or {}).get("content") or {}).get("rendered") or ""
                        except Exception:
                            continue
                        for m in _FILE_RE.finditer(content):
                            url = m.group(0)
                            fname = url.rsplit("/", 1)[-1]
                            if not carries_version_tokens(title, fname):
                                continue
                            rank = token_overlap_ratio(f"{artist} {title}", fname)
                            candidates.append((rank, url, site.replace("https://", "")))
                    if candidates:
                        break
                if candidates:
                    break
            candidates.sort(key=lambda c: c[0], reverse=True)
            for _rank, url, source in candidates[:3]:
                ext = "m4a" if url.lower().endswith(".m4a") else "mp3"
                try:
                    resp = await client.get(url, timeout=45)
                except Exception:
                    continue
                if resp.status_code != 200:
                    continue
                data = resp.content
                if not (100_000 <= len(data) <= FULL_SONG_MAX_MB * 1024 * 1024):
                    continue
                duration = _fingerprint_duration(data)
                if duration < FULL_SONG_MIN_SECONDS:
                    continue
                _cache_put(key, data, ext, duration, source)
                return {"data": data, "ext": ext, "duration_sec": duration, "source": source}
    except Exception:
        return None
    return None
