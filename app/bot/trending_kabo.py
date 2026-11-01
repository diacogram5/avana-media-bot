"""Hottest trending Reels audio from Kabo (growth-ranked).

Data source: https://kabo.sh/tools/trending-instagram-audio (free board).
Only the 5 publicly visible items are used -- the 15 login-walled items
are NEVER fetched or bypassed.

Data is extracted from the page's JSON-LD structured data (title, artist,
Instagram audio URL) plus the visible growth metrics (new reels in 24h,
day-over-day %). If the growth metrics are absent from static HTML, the
items are still shown with title/artist/link but without fabricated numbers.

Storage: Turso (persistent) when configured; in-memory fallback otherwise.
We NEVER modify the local bot.db schema.

All network I/O runs in an executor thread -- never blocks the event loop.
A module-level lock prevents concurrent fetches.

Validation: a fetched list replaces the stored one ONLY if it passes
validation (>=3 items with title). Incomplete data never replaces the
last valid list.
"""
from __future__ import annotations

import asyncio
import html as _htmlmod
import json
import logging
import re
import time

log = logging.getLogger("avana.trending_kabo")

KABO_URL = "https://kabo.sh/tools/trending-instagram-audio"
FETCH_TIMEOUT = 30
STALE_AFTER_SEC = 24 * 3600
MIN_VALID_ITEMS = 3
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")

_mem_cache: dict = {"items": [], "fetched_at": 0}
_fetch_lock = asyncio.Lock()

_JSONLD_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
# Growth metrics appear in the rendered summary text, e.g.:
# "+44,345 reels since yesterday, +1.1% day over day"
_GROWTH_RE = re.compile(
    r'\+([\d,]+)\s+reels\s+since\s+yesterday,\s*\+([\d.]+)%', re.IGNORECASE)


def _parse_growth_metrics(html: str) -> dict[str, dict]:
    """Extract per-song growth from the summary paragraph.

    Returns {song_title_lower: {"new_24h": str, "growth_pct": str}}.
    Best-effort: only the #1 song is named in the summary with numbers.
    """
    out: dict[str, dict] = {}
    # The summary names the #1 explicitly with numbers; HTML entities decoded
    text = _htmlmod.unescape(html)
    m = re.search(
        r'"([^"]+)"\s+by\s+.+?\s+is\s+the\s+fastest-climbing[^(]*'
        r'\(\+([\d,]+)\s+reels\s+since\s+yesterday,\s*\+([\d.]+)%',
        text, re.IGNORECASE)
    if m:
        out[m.group(1).strip().lower()] = {
            "new_24h": m.group(2).strip(),
            "growth_pct": m.group(3).strip() + "%",
        }
    return out


def parse_kabo_html(html: str) -> list[dict]:
    """Parse Kabo's trending page. Returns max 5 public items.

    Each item: {"rank", "title", "artist", "audio_url", "new_24h",
                "growth_pct"}. Growth fields are "" when unavailable --
    never fabricated.
    """
    items: list[dict] = []
    m = _JSONLD_RE.search(html)
    if not m:
        return items
    try:
        data = json.loads(_htmlmod.unescape(m.group(1)))
    except (json.JSONDecodeError, ValueError):
        return items

    growth = _parse_growth_metrics(html)

    for node in data.get("@graph", []):
        if node.get("@type") != "ItemList":
            continue
        for el in node.get("itemListElement", []):
            pos = el.get("position", 0)
            # Only the 5 public items (positions 1-5)
            if not isinstance(pos, int) or pos < 1 or pos > 5:
                continue
            it = el.get("item", {})
            name = (it.get("name") or "").strip()
            if not name or "hidden" in name.lower():
                continue
            artist = it.get("byArtist", "")
            if isinstance(artist, dict):
                artist = artist.get("name", "")
            artist = str(artist).strip()
            url = (it.get("url") or "").strip()
            # Only keep valid Instagram audio URLs
            audio_url = ""
            if re.match(r"https://www\.instagram\.com/reels/audio/\d+/?$",
                        url):
                audio_url = url
            g = growth.get(name.lower(), {})
            items.append({
                "rank": pos,
                "title": name[:120],
                "artist": artist[:120],
                "audio_url": audio_url,
                "new_24h": g.get("new_24h", ""),
                "growth_pct": g.get("growth_pct", ""),
            })
    items.sort(key=lambda x: x["rank"])
    return items


def validate_items(items: list[dict]) -> bool:
    if not isinstance(items, list) or len(items) < MIN_VALID_ITEMS:
        return False
    return all(it.get("title") for it in items)


def _fetch_sync() -> list[dict]:
    """Blocking fetch+parse. Called inside run_in_executor only."""
    import urllib.request
    req = urllib.request.Request(KABO_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        html = resp.read().decode("utf-8", errors="ignore")
    items = parse_kabo_html(html)
    if not validate_items(items):
        raise RuntimeError(f"parsed list failed validation ({len(items)} items)")
    return items


async def fetch_hot_trends() -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_sync)


def _turso_conn():
    try:
        from .turso_cache import _get_conn
        conn = _get_conn()
        if conn is None:
            return None
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kabo_hot_trends (
                rank INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                audio_url TEXT NOT NULL DEFAULT '',
                new_24h TEXT NOT NULL DEFAULT '',
                growth_pct TEXT NOT NULL DEFAULT '',
                fetched_at INTEGER NOT NULL
            )
        """)
        conn.commit()
        return conn
    except Exception as e:
        log.warning("kabo: turso unavailable (%s)", e)
        return None


def save_hot_trends(items: list[dict]) -> bool:
    global _mem_cache
    now = int(time.time())
    _mem_cache = {"items": items, "fetched_at": now}
    try:
        conn = _turso_conn()
        if conn is None:
            log.info("kabo: saved to memory only (turso unavailable)")
            return True
        conn.execute("DELETE FROM kabo_hot_trends")
        for it in items:
            conn.execute(
                "INSERT INTO kabo_hot_trends "
                "(rank, title, artist, audio_url, new_24h, growth_pct,"
                " fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (it["rank"], it["title"], it["artist"], it["audio_url"],
                 it["new_24h"], it["growth_pct"], now),
            )
        conn.commit()
        log.info("kabo: saved %d items to turso", len(items))
        return True
    except Exception as e:
        log.warning("kabo: turso save failed (%s); memory kept", e)
        return True


def load_hot_trends() -> tuple[list[dict], int]:
    global _mem_cache
    try:
        conn = _turso_conn()
        if conn is not None:
            rows = conn.execute(
                "SELECT rank, title, artist, audio_url, new_24h, growth_pct,"
                " fetched_at FROM kabo_hot_trends ORDER BY rank"
            ).fetchall()
            if rows:
                items = [
                    {"rank": r[0], "title": r[1], "artist": r[2],
                     "audio_url": r[3], "new_24h": r[4], "growth_pct": r[5]}
                    for r in rows
                ]
                fetched_at = rows[0][6]
                _mem_cache = {"items": items, "fetched_at": fetched_at}
                return items, fetched_at
    except Exception as e:
        log.warning("kabo: turso load failed (%s)", e)
    return _mem_cache["items"], _mem_cache["fetched_at"]


def is_stale(fetched_at: int) -> bool:
    return (time.time() - fetched_at) > STALE_AFTER_SEC


async def get_hot_trends(force_refresh: bool = False,
                         ) -> tuple[list[dict], int, bool]:
    items, fetched_at = load_hot_trends()
    if items and not force_refresh and not is_stale(fetched_at):
        return items, fetched_at, False
    async with _fetch_lock:
        items2, fetched_at2 = load_hot_trends()
        if items2 and not force_refresh and not is_stale(fetched_at2):
            return items2, fetched_at2, False
        try:
            fresh = await fetch_hot_trends()
        except Exception as e:
            log.warning("kabo: refresh failed (%s); keeping old list", e)
            return items, fetched_at, True
        if not validate_items(fresh):
            log.warning("kabo: fresh list invalid; keeping old list")
            return items, fetched_at, True
        save_hot_trends(fresh)
        return fresh, int(time.time()), False
