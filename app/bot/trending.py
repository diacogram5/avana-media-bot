"""Trending songs from Instagram Reels (Snaplytics daily chart).

Data source: https://instagram-trending.snaplytics.io/ (free tool, no API).
The 50-item chart is embedded in the page HTML; we fetch it once per day
with a plain HTTP GET (respectful: 1 request/day, browser User-Agent).

Storage: Turso (persistent cloud SQLite) when configured; otherwise an
in-memory cache of the last valid list (survives within the process).
We NEVER modify the local bot.db schema without explicit approval.

All network I/O runs in an executor thread -- never blocks the event loop.
A module-level lock prevents concurrent fetches.

Validation: a fetched list replaces the stored one ONLY if it passes
validation (>=10 items, each with rank/title). Incomplete data never
replaces the last valid list.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

log = logging.getLogger("avana.trending")

TRENDING_URL = "https://instagram-trending.snaplytics.io/"
FETCH_TIMEOUT = 30
STALE_AFTER_SEC = 24 * 3600  # refresh once per day
MIN_VALID_ITEMS = 10
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")

# In-memory cache of the last valid list (fallback when Turso is down)
_mem_cache: dict = {"items": [], "fetched_at": 0}
_fetch_lock = asyncio.Lock()
_turso_checked = False
_turso_available = False


# ---------------------------------------------------------------------------
# HTML parsing (regex-based; no new dependencies)
# ---------------------------------------------------------------------------

# Matches: <a href="https://www.instagram.com/reels/audio/<id>/" ...>...</a>
# Non-greedy; each card has one such link.
_AUDIO_LINK_RE = re.compile(
    r'<a href="https://www\.instagram\.com/reels/audio/(\d+)/"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_SPAN_RE = re.compile(
    r'<span class="[^"]*text-ink[^"]*">(.*?)</span>', re.DOTALL)
_ARTIST_DIV_RE = re.compile(
    r'<(?:div|span) class="[^"]*text-\[0\.6[67]rem\][^"]*text-muted[^"]*">'
    r'(.*?)</(?:div|span)>',
    re.DOTALL)
_RANK_BADGE_RE = re.compile(r'rounded-full[^>]*>(\d+)</span>')


def _clean_text(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_trending_html(html: str) -> list[dict]:
    """Parse the Snaplytics trending page HTML into a ranked list.

    Returns [{"rank": int, "title": str, "artist": str, "audio_id": str,
              "movement": str}, ...] sorted by rank. movement is one of
    "up", "down", "new", "same" (best-effort from the HTML indicators).
    """
    items: list[dict] = []
    for m in _AUDIO_LINK_RE.finditer(html):
        audio_id = m.group(1)
        inner = m.group(2)

        tm = _TITLE_SPAN_RE.search(inner)
        title = _clean_text(tm.group(1)) if tm else ""
        am = _ARTIST_DIV_RE.search(inner)
        artist = _clean_text(am.group(1)) if am else ""
        # Artist may include trailing stats like "· 11d" -- strip them
        artist = artist.split("·")[0].strip()
        rm = _RANK_BADGE_RE.search(inner)
        rank = 0
        if rm:
            try:
                rank = int(rm.group(1))
            except ValueError:
                pass

        # Movement indicator
        movement = "same"
        tmove = re.search(r'title="(Up|Down|New)[^"]*"', inner)
        if tmove:
            w = tmove.group(1).lower()
            movement = {"up": "up", "down": "down", "new": "new"}.get(w, "same")
        else:
            amove = re.search(r'aria-label="(Up|Down)\s+\d+"', inner)
            if amove:
                movement = amove.group(1).lower()

        if not title or len(title) < 2:
            continue
        items.append({
            "rank": rank or len(items) + 1,
            "title": title[:120],
            "artist": artist[:120],
            "audio_id": audio_id,
            "movement": movement,
        })
    # Sort by rank and re-number sequentially
    items.sort(key=lambda x: x["rank"])
    for i, it in enumerate(items, 1):
        it["rank"] = i
    return items


def validate_items(items: list[dict]) -> bool:
    """A list is valid only if it has enough well-formed entries."""
    if not isinstance(items, list) or len(items) < MIN_VALID_ITEMS:
        return False
    for it in items:
        if not it.get("title") or not it.get("rank"):
            return False
    return True


# ---------------------------------------------------------------------------
# Fetch (runs in executor thread)
# ---------------------------------------------------------------------------

def _fetch_sync() -> list[dict]:
    """Blocking fetch+parse. Called inside run_in_executor only."""
    import urllib.request
    req = urllib.request.Request(TRENDING_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        html = resp.read().decode("utf-8", errors="ignore")
    items = parse_trending_html(html)
    if not validate_items(items):
        raise RuntimeError(f"parsed list failed validation ({len(items)} items)")
    return items


async def fetch_trending() -> list[dict]:
    """Fetch the chart in a thread; never blocks the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_sync)


# ---------------------------------------------------------------------------
# Turso persistent storage (graceful fallback)
# ---------------------------------------------------------------------------

def _turso_conn():
    """Return Turso connection or None. Never raises."""
    global _turso_checked, _turso_available
    try:
        from .turso_cache import _get_conn  # reuse existing connector
        conn = _get_conn()
        if conn is None:
            return None
        # Ensure our table exists (only when Turso is actually reachable)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trending_songs (
                rank INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                audio_id TEXT NOT NULL,
                movement TEXT NOT NULL DEFAULT 'same',
                fetched_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trending_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()
        _turso_available = True
        return conn
    except Exception as e:
        log.warning("trending: turso unavailable (%s)", e)
        return None


def save_trending(items: list[dict]) -> bool:
    """Persist a validated list to Turso + memory. Returns success."""
    global _mem_cache
    now = int(time.time())
    _mem_cache = {"items": items, "fetched_at": now}
    try:
        conn = _turso_conn()
        if conn is None:
            log.info("trending: saved to memory only (turso unavailable)")
            return True
        conn.execute("DELETE FROM trending_songs")
        for it in items:
            conn.execute(
                "INSERT INTO trending_songs "
                "(rank, title, artist, audio_id, movement, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (it["rank"], it["title"], it["artist"], it["audio_id"],
                 it.get("movement", "same"), now),
            )
        conn.execute(
            "INSERT OR REPLACE INTO trending_meta (key, value) VALUES (?, ?)",
            ("last_fetch", str(now)),
        )
        conn.commit()
        log.info("trending: saved %d items to turso", len(items))
        return True
    except Exception as e:
        log.warning("trending: turso save failed (%s); memory kept", e)
        return True  # memory copy is still valid


def load_trending() -> tuple[list[dict], int]:
    """Load (items, fetched_at). Tries Turso, falls back to memory."""
    global _mem_cache
    try:
        conn = _turso_conn()
        if conn is not None:
            rows = conn.execute(
                "SELECT rank, title, artist, audio_id, movement, fetched_at "
                "FROM trending_songs ORDER BY rank"
            ).fetchall()
            if rows:
                items = [
                    {"rank": r[0], "title": r[1], "artist": r[2],
                     "audio_id": r[3], "movement": r[4]}
                    for r in rows
                ]
                fetched_at = rows[0][5]
                _mem_cache = {"items": items, "fetched_at": fetched_at}
                return items, fetched_at
    except Exception as e:
        log.warning("trending: turso load failed (%s)", e)
    return _mem_cache["items"], _mem_cache["fetched_at"]


def is_stale(fetched_at: int) -> bool:
    return (time.time() - fetched_at) > STALE_AFTER_SEC


async def get_trending(force_refresh: bool = False,
                       ) -> tuple[list[dict], int, bool]:
    """Return (items, fetched_at, is_stale_flag).

    Uses the stored list; triggers a background refresh only when stale
    (or forced). A failed/invalid refresh never replaces the last valid
    list. Concurrent callers share one fetch via the module lock.
    """
    items, fetched_at = load_trending()
    if items and not force_refresh and not is_stale(fetched_at):
        return items, fetched_at, False

    # Need refresh -- single-flight via lock
    async with _fetch_lock:
        # Re-check after acquiring (another caller may have refreshed)
        items2, fetched_at2 = load_trending()
        if items2 and not force_refresh and not is_stale(fetched_at2):
            return items2, fetched_at2, False
        try:
            fresh = await fetch_trending()
        except Exception as e:
            log.warning("trending: refresh failed (%s); keeping old list", e)
            return items, fetched_at, True
        # validate BEFORE replacing (fetch already validates, double-check)
        if not validate_items(fresh):
            log.warning("trending: fresh list invalid; keeping old list")
            return items, fetched_at, True
        save_trending(fresh)
        return fresh, int(time.time()), False


async def refresh_if_needed() -> None:
    """Called at bot startup: refresh in background if stale. Non-blocking."""
    async def _bg():
        try:
            items, fetched_at = load_trending()
            if not items or is_stale(fetched_at):
                log.info("trending: startup refresh (stale or empty)")
                await get_trending(force_refresh=False)
            else:
                log.info("trending: cache fresh, no startup fetch")
        except Exception as e:
            log.warning("trending: startup refresh failed (%s)", e)
    asyncio.get_running_loop().create_task(_bg())
