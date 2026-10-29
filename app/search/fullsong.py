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
from ..logging_config import get_logger, log_event

log = get_logger("fullsong")

MUSIC_SITES = (
    # (base_url, api_style): "wp-json" uses /wp-json/wp/v2/... pretty URLs;
    # "rest_route" uses ?rest_route=/wp/v2/... for sites with pretty
    # permalinks disabled on the API (musicsweb.ir — proven live 2026-10-29:
    # WP search + post HTML + real 9.6 MB / 220 s MP3 download).
    ("https://musics-fa.com", "wp-json"),
    ("https://minevisam.ir", "wp-json"),
    ("https://musicviral.ir", "wp-json"),
    ("https://musicsweb.ir", "rest_route"),
)


def _search_url(base: str, style: str) -> str:
    if style == "rest_route":
        return f"{base}/?rest_route=/wp/v2/search"
    return f"{base}/wp-json/wp/v2/search"


def _post_url(base: str, style: str, pid: int) -> str:
    if style == "rest_route":
        return f"{base}/?rest_route=/wp/v2/posts/{pid}"
    return f"{base}/wp-json/wp/v2/posts/{pid}"

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


def _looks_like_audio(data: bytes) -> bool:
    """Strict magic-byte gate: a real audio container only (MP3 incl.
    ID3/frame-sync, MP4/M4A ftyp, Ogg, WAV, FLAC). Never trusts the URL
    extension or the content-type header."""
    if not data or len(data) < 16:
        return False
    head = data[:16]
    return (head[:3] == b"ID3"
            or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
            or head[4:8] == b"ftyp"
            or head[:4] == b"OggS"
            or (head[:4] == b"RIFF" and data[8:12] == b"WAVE")
            or head[:4] == b"fLaC")


async def _site_candidates(client: httpx.AsyncClient, base: str, style: str,
                         q: str, artist: str, title: str) -> list[tuple[float, str, str]]:
    """Ranked (rank, file_url, site) candidates from ONE wp site and ONE
    query. Extracted so the registry can expose each MUSIC_SITES entry as
    its own file source; the bundle path below calls it identically."""
    out: list[tuple[float, str, str]] = []
    try:
        resp = await client.get(
            _search_url(base, style),
            params={"search": q, "per_page": 5})
    except Exception:
        return out
    if resp.status_code != 200:
        return out
    try:
        data = resp.json()
    except Exception:
        return out
    if not isinstance(data, list):
        return out
    site = base.replace("https://", "")
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
        # Post page -> real file links. These sites strip the
        # download links from the WP API content, so when
        # the API content yields none, fall back to the
        # post's public HTML page (search result `url`).
        file_urls: list[str] = []
        try:
            pr = await client.get(
                _post_url(base, style, pid),
                params={"_fields": "title,content"}, timeout=15)
            if pr.status_code == 200:
                content = ((pr.json() or {}).get("content") or {}).get("rendered") or ""
                file_urls = [m.group(0) for m in _FILE_RE.finditer(content)]
        except Exception:
            file_urls = []
        if not file_urls:
            page_url = p.get("url") or ""
            if page_url.startswith("http"):
                try:
                    hr = await client.get(page_url, timeout=20)
                    if hr.status_code == 200:
                        file_urls = [m.group(0)
                                     for m in _FILE_RE.finditer(hr.text)]
                except Exception:
                    file_urls = []
        if not file_urls:
            continue
        for url in file_urls:
            fname = url.rsplit("/", 1)[-1]
            if not carries_version_tokens(title, fname):
                continue
            rank = token_overlap_ratio(f"{artist} {title}", fname)
            out.append((rank, url, site))
    return out


def _probe_bitrate_kbps(data: bytes, ext: str) -> int | None:
    """Real bitrate via ffprobe; None when unmeasurable. Never guessed."""
    import json as _json
    import shutil
    import subprocess
    import tempfile
    if not data or shutil.which("ffprobe") is None:
        return None
    path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as fh:
            fh.write(data)
            path = fh.name
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=bit_rate:format=bit_rate",
             "-of", "json", path],
            timeout=30, capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        info = _json.loads(proc.stdout or "{}")
        streams = info.get("streams") or [{}]
        fmt = info.get("format") or {}
        br_raw = streams[0].get("bit_rate") or fmt.get("bit_rate")
        return int(round(float(br_raw) / 1000))
    except Exception:
        return None
    finally:
        if path:
            try:
                import os
                os.unlink(path)
            except Exception:
                pass


async def find_full_song_file(artist: str, title: str,
                              sites: tuple | None = None) -> dict | None:
    """Return {"data", "ext", "duration_sec", "source"} or None.

    Only the same recording: the version gate rejects other-remix files,
    and the duration probe rejects short clips.

    `sites` is an extension hook for the source registry: pass a subset
    of MUSIC_SITES to search only those. Default None searches all
    sites exactly as before.
    """
    sites = MUSIC_SITES if sites is None else tuple(sites)
    key = f"fullsong:{artist}|{title}".lower()
    if len(sites) == 1:
        key += f"|{sites[0][0]}"
    hit = _cache_get(key)
    if hit:
        data, ext, duration, source = hit
        log_event(log, "fullsong", stage="cache_hit", source=source,
                  duration_s=round(duration, 1))
        return {"data": data, "ext": ext, "duration_sec": duration, "source": source}

    queries = [q for q in (f"{artist} {title}".strip(), title.strip()) if len(q) >= 2]
    candidates: list[tuple[float, str, str]] = []  # (rank, file_url, site)
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=15, headers=_UA, follow_redirects=True) as client:
            for q in queries:
                for base, style in sites:
                    c = await _site_candidates(client, base, style, q, artist, title)
                    log_event(log, "fullsong", stage="site_search",
                              site=base.replace("https://", ""),
                              query=q[:40], candidates=len(c))
                    candidates.extend(c)
                    if candidates:
                        break
                if candidates:
                    break
            candidates.sort(key=lambda c: c[0], reverse=True)
            # Honest quality preference: among the valid candidates, the
            # highest real bitrate wins (a 320k source over a 128k one).
            # Nothing is upscaled — the bitrate is probed, never guessed.
            valid: list[tuple[int, bytes, str, float, str]] = []
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
                # Content validation: real audio container (magic bytes),
                # then real audio duration (landmark probe, >= 85 s) so a
                # short clip or a mislabelled file is never delivered.
                if not _looks_like_audio(data):
                    continue
                duration = _fingerprint_duration(data)
                if duration < FULL_SONG_MIN_SECONDS:
                    continue
                br = _probe_bitrate_kbps(data, ext) or 0
                valid.append((br, data, ext, duration, source))
            if not valid:
                log_event(log, "fullsong", stage="wp_not_found",
                          ms=int((time.time() - t0) * 1000),
                          candidates=len(candidates))
            else:
                valid.sort(key=lambda v: v[0], reverse=True)
                _br, data, ext, duration, source = valid[0]
                log_event(log, "fullsong", stage="found",
                          ms=int((time.time() - t0) * 1000),
                          source=source, bitrate_kbps=_br,
                          duration_s=round(duration, 1),
                          bytes=len(data))
                _cache_put(key, data, ext, duration, source)
                return {"data": data, "ext": ext, "duration_sec": duration, "source": source}
    except Exception as exc:
        log_event(log, "fullsong", stage="wp_error",
                  error=f"{type(exc).__name__}: {str(exc)[:80]}")
    # Fallback: YouTube (slower, universal coverage). Proven 2026-10-29.
    try:
        from .youtube_full import find_youtube_audio
        yt = await find_youtube_audio(artist, title)
    except Exception as exc:
        log_event(log, "fullsong", stage="youtube_error",
                  error=f"{type(exc).__name__}: {str(exc)[:60]}")
        yt = None
    if yt:
        _cache_put(key, yt["data"], yt["ext"], yt["duration_sec"], yt["source"])
        return yt
    log_event(log, "fullsong", stage="not_found",
              ms=int((time.time() - t0) * 1000), candidates=len(candidates))
    return None
