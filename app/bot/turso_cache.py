"""Persistent URL->file_id cache using Turso (cloud SQLite).

This is an OPTIONAL separate cache. If Turso is not configured or fails,
the bot falls back to the local SQLite cache (current behavior).
This module never causes the bot to crash - all errors are caught.

Env vars (set on Render):
  TURSO_URL: libsql://avana-bot-db-....turso.io
  TURSO_AUTH_TOKEN: (secret, from Turso dashboard)
"""
from __future__ import annotations

import hashlib
import logging
import os
import time

from .. import timing as _timing

_log = logging.getLogger("avana.turso_cache")

_turso_conn = None
_turso_ok = False


def _normalize_url(url: str) -> str:
    """Normalize URL for cache key (same as file_cache._normalize_url).

    Fix round 3: spellings of the same reel share one key — the
    "www." host prefix is dropped and instagram /reel|p|tv|reels/
    paths collapse to /reel/<code>. Keep in sync with
    file_cache._normalize_url (same rules, same order).
    """
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(url.strip())
        # Lowercase host, strip trailing slash, drop query/fragment
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = p.path.rstrip("/") or "/"
        if netloc == "instagram.com":
            import re as _re
            _m = _re.match(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)$", path)
            if _m:
                path = "/reel/" + _m.group(1)
        return urlunparse((p.scheme.lower(), netloc, path, "", "", ""))
    except Exception:
        return url.strip().lower()


def _url_key(url: str) -> str:
    return "url:" + hashlib.md5(_normalize_url(url).encode()).hexdigest()


_USER_TABLES_OK = False


def ensure_user_tables(conn) -> None:
    """Create the user-owned tables in Turso (P1-14), idempotently.

    Same columns as the local migrations (001/003/016): users (incl.
    blocked), user_language, playlists (+ playlist_items), favorites,
    settings, features, ads. Called once per process from startup
    (run_bot) — never from a user path, so the DDL cost is never in
    anyone's waiting time. Safe to call again (CREATE TABLE IF NOT
    EXISTS). Raises on failure so callers can decide to fail open.
    """
    global _USER_TABLES_OK
    if _USER_TABLES_OK:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            first_name TEXT NOT NULL DEFAULT '',
            blocked INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT NOT NULL DEFAULT '',
            blocked_until INTEGER,
            created_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_language (
            telegram_id INTEGER PRIMARY KEY,
            lang TEXT NOT NULL DEFAULT 'fa'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS playlist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT '',
            artist TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            preview_url TEXT NOT NULL DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0,
            UNIQUE(playlist_id, title, artist)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            artist TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            preview_url TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT 0,
            UNIQUE(telegram_id, title, artist)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS features (
            key TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            display_order INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            visible INTEGER NOT NULL DEFAULT 1,
            message TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            photo_file_id TEXT NOT NULL DEFAULT '',
            link TEXT NOT NULL DEFAULT '',
            button_text TEXT NOT NULL DEFAULT '',
            views INTEGER NOT NULL DEFAULT 0,
            clicks INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    _USER_TABLES_OK = True


# Step 1b identify-result cache (2026-11-09): the FINAL identification
# result keyed by SHORTCODE (ig:<shortcode> | ap:<videohash> |
# tg:<telegram file_id> | sha:<content hash>) — title/artist/source,
# the file KIND (full song vs clip audio) and a SENDABLE file_id, so a
# repeat tap sends the stored file immediately with no bank/Shazam/
# AudD call. Disk paths are NEVER stored here: a path is meaningless
# on another instance/restart — only the file_id survives. A row
# without a file_id is name-only (the tap still saves the lookup).
_IDENTIFY_RESULT_DDL = """
    CREATE TABLE IF NOT EXISTS identify_result_cache (
        cache_key TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        artist TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT '',
        file_id TEXT NOT NULL DEFAULT '',
        duration_sec REAL NOT NULL DEFAULT 0,
        quality TEXT NOT NULL DEFAULT '',
        extra TEXT NOT NULL DEFAULT '',
        cover_file_id TEXT NOT NULL DEFAULT '',
        resolve_ms INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    )
"""
# «❌ آهنگ اشتباه» — the disputed name + its source, the link, and
# what the re-check found. Admin analytics only; written
# fire-and-forget AFTER the answer, never in a waiting path. The
# store opens its OWN connection per call (never the cached global:
# the write runs in a background thread, and a connection created
# on the event-loop thread must not be used from another thread).
_WRONG_SONG_REPORTS_DDL = """
    CREATE TABLE IF NOT EXISTS wrong_song_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        link TEXT NOT NULL DEFAULT '',
        wrong_title TEXT NOT NULL DEFAULT '',
        wrong_artist TEXT NOT NULL DEFAULT '',
        wrong_source TEXT NOT NULL DEFAULT '',
        new_title TEXT NOT NULL DEFAULT '',
        new_artist TEXT NOT NULL DEFAULT '',
        new_source TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL
    )
"""


_turso_down_logged = False


def _log_turso_down(msg: str, *args) -> None:
    """Error-level the FIRST time the mirror is found down (fix round
    3): Turso-down used to be info/warning-only, so a dead durable
    cache was invisible in Render logs. Later occurrences stay debug
    to avoid spamming every request."""
    global _turso_down_logged
    if not _turso_down_logged:
        _turso_down_logged = True
        _log.error(msg, *args)
    else:
        _log.debug(msg, *args)


def _turso_invalidate() -> None:
    """Drop a broken cached Turso connection so the next call
    reconnects from scratch (fix round 3). A connection that fails
    mid-process was previously reused forever, so every later read
    silently returned None and the durable cache looked dead.
    Never raises."""
    global _turso_conn, _turso_ok
    try:
        if _turso_conn is not None:
            _turso_conn.close()
    except Exception:
        pass
    _turso_conn = None
    _turso_ok = False


def _get_conn():
    """Get or create Turso connection. Returns None if not configured/fails."""
    global _turso_conn, _turso_ok
    if _turso_conn is not None:
        return _turso_conn if _turso_ok else None

    url = os.environ.get("TURSO_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        _log_turso_down(
            "turso_cache: not configured (TURSO_URL/TOKEN missing) — durable cache disabled")
        _turso_ok = False
        return None

    try:
        import libsql_experimental as libsql
        conn = libsql.connect(url, auth_token=token)
        # Create table if not exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS url_file_cache (
                cache_key TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                file_type TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS suggest_results (
                result_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                url TEXT NOT NULL,
                preview_url TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS song_file_cache (
                cache_key TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                file_type TEXT,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute(_WRONG_SONG_REPORTS_DDL)
        conn.execute(_IDENTIFY_RESULT_DDL)
        # Full-song mismatch fix (2026-11-09): the file's own verified
        # identity (ID3 title/artist) + probed duration, stored next to
        # the file_id so a cache hit can be re-checked before send.
        # Idempotent: an ALTER on an existing column raises and is ignored.
        for _ddl in (
            "ALTER TABLE song_file_cache ADD COLUMN verified_title TEXT",
            "ALTER TABLE song_file_cache ADD COLUMN verified_artist TEXT",
            "ALTER TABLE song_file_cache ADD COLUMN duration_sec REAL",
            "ALTER TABLE identify_result_cache ADD COLUMN resolve_ms INTEGER",
        ):
            try:
                conn.execute(_ddl)
            except Exception:
                pass
        conn.commit()
        _turso_conn = conn
        _turso_ok = True
        _log.info("turso_cache: connected OK (persistent cache active)")
        return conn
    except ImportError:
        _log_turso_down(
            "turso_cache: libsql_experimental not installed — durable cache disabled")
        _turso_ok = False
        return None
    except Exception as e:
        _log_turso_down("turso_cache: connection failed: %s — durable cache disabled", e)
        _turso_ok = False
        return None


def get_url_file_id(url: str, req_id: str | None = None) -> tuple | None:
    """Get (file_id, file_type) from persistent Turso cache. None if miss/error."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        key = _url_key(url)
        _tq = time.perf_counter()
        cur = conn.execute(
            "SELECT file_id, file_type, expires_at FROM url_file_cache WHERE cache_key = ?",
            (key,))
        _timing.note_turso(req_id, "url_file_cache.get",
                           (time.perf_counter() - _tq) * 1000, logger=_log)
        row = cur.fetchone()
        if not row:
            return None
        file_id, file_type, expires_at = row[0], row[1], row[2]
        if int(expires_at) < int(time.time()):
            # Expired - delete
            try:
                conn.execute("DELETE FROM url_file_cache WHERE cache_key = ?", (key,))
                conn.commit()
            except Exception:
                pass
            return None
        return (file_id, file_type)
    except Exception as e:
        _turso_invalidate()
        _log.debug("turso_cache: get failed: %s", e)
        return None


def store_url_file_id(url: str, file_id: str, file_type: str, ttl_days: int = 30,
                      req_id: str | None = None) -> bool:
    """Store URL->file_id in persistent Turso cache. Returns True if stored."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        key = _url_key(url)
        now = int(time.time())
        expires = now + (ttl_days * 86400)
        _tq = time.perf_counter()
        conn.execute("""
            INSERT OR REPLACE INTO url_file_cache
            (cache_key, file_id, file_type, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
        """, (key, file_id, file_type, now, expires))
        _timing.note_turso(req_id, "url_file_cache.store",
                           (time.perf_counter() - _tq) * 1000, logger=_log)
        conn.commit()
        return True
    except Exception as e:
        _turso_invalidate()
        _log.debug("turso_cache: store failed: %s", e)
        return False


def _song_key(artist: str, title: str) -> str:
    """Cache key for a song (same shape as file_cache._cache_key)."""
    return f"song:{artist.strip().lower()}|{title.strip().lower()}"


def get_song_file(artist: str, title: str, req_id: str | None = None) -> dict | None:
    """Cached song row: {file_id, verified_title, verified_artist,
    duration_sec} or None if miss/expired/error.

    Rows stored before the verified columns existed return None for them
    (legacy = unverified: the caller must NOT send from such a row).
    """
    conn = _get_conn()
    if not conn:
        return None
    try:
        key = _song_key(artist, title)
        _tq = time.perf_counter()
        cur = conn.execute(
            "SELECT file_id, expires_at, verified_title, verified_artist,"
            " duration_sec FROM song_file_cache WHERE cache_key = ?",
            (key,))
        _timing.note_turso(req_id, "song_file_cache.get",
                           (time.perf_counter() - _tq) * 1000, logger=_log)
        row = cur.fetchone()
        if not row:
            return None
        file_id, expires_at = row[0], row[1]
        if int(expires_at) < int(time.time()):
            # Expired - delete
            try:
                conn.execute("DELETE FROM song_file_cache WHERE cache_key = ?", (key,))
                conn.commit()
            except Exception:
                pass
            return None
        return {"file_id": file_id, "verified_title": row[2],
                "verified_artist": row[3], "duration_sec": row[4] or 0}
    except Exception as e:
        _turso_invalidate()
        _log.debug("turso_cache: get_song_file failed: %s", e)
        return None


def delete_song_file_id(artist: str, title: str, req_id: str | None = None) -> bool:
    """Delete a cached song row (poisoned entry). Never raises."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute("DELETE FROM song_file_cache WHERE cache_key = ?",
                     (_song_key(artist, title),))
        conn.commit()
        return True
    except Exception as e:
        _turso_invalidate()
        _log.debug("turso_cache: delete_song_file_id failed: %s", e)
        return False


def get_song_file_id(artist: str, title: str, req_id: str | None = None) -> str | None:
    """Get a cached song file_id from persistent Turso cache. None if miss/error."""
    row = get_song_file(artist, title, req_id=req_id)
    return row["file_id"] if row else None


def store_song_file_id(artist: str, title: str, file_id: str, file_type: str = "audio",
                       ttl_days: int = 7, req_id: str | None = None,
                       verified_title: str | None = None,
                       verified_artist: str | None = None,
                       duration_sec: float | None = None) -> bool:
    """Store a song file_id in persistent Turso cache. Returns True if stored.

    verified_title/verified_artist are the FILE's own identity (ID3 tags,
    probed at send time); duration_sec is the probed length. A re-store
    without them keeps any previously stored verified identity (COALESCE)
    instead of wiping it.
    """
    conn = _get_conn()
    if not conn:
        return False
    try:
        key = _song_key(artist, title)
        now = int(time.time())
        expires = now + (ttl_days * 86400)
        _tq = time.perf_counter()
        conn.execute("""
            INSERT INTO song_file_cache
            (cache_key, file_id, file_type, expires_at, created_at,
             verified_title, verified_artist, duration_sec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              file_id=excluded.file_id,
              file_type=excluded.file_type,
              expires_at=excluded.expires_at,
              created_at=excluded.created_at,
              verified_title=COALESCE(excluded.verified_title, song_file_cache.verified_title),
              verified_artist=COALESCE(excluded.verified_artist, song_file_cache.verified_artist),
              duration_sec=COALESCE(excluded.duration_sec, song_file_cache.duration_sec)
        """, (key, file_id, file_type, expires, now,
              verified_title, verified_artist, duration_sec))
        _timing.note_turso(req_id, "song_file_cache.store",
                           (time.perf_counter() - _tq) * 1000, logger=_log)
        conn.commit()
        return True
    except Exception as e:
        _turso_invalidate()
        _log.debug("turso_cache: store_song_file_id failed: %s", e)
        return False


def get_identify_result(key: str, req_id: str | None = None) -> dict | None:
    """Step 1b: the cached FINAL identify result for a shortcode key.

    Returns {status, title, artist, source, kind, file_id,
    duration_sec, quality, extra, cover_file_id, created_at, ttl}
    or None on miss/expired/error. A row without a file_id is
    name-only (kind "pending" is left to the caller): the tap still
    saves the whole lookup. Expired rows are deleted. Never raises."""
    conn = _get_conn()
    if not conn or not key:
        return None
    try:
        _tq = time.perf_counter()
        cur = conn.execute(
            "SELECT status, title, artist, source, kind, file_id,"
            " duration_sec, quality, extra, cover_file_id, resolve_ms,"
            " created_at, expires_at"
            " FROM identify_result_cache WHERE cache_key = ?",
            (key,))
        _timing.note_turso(req_id, "identify_result_cache.get",
                           (time.perf_counter() - _tq) * 1000, logger=_log)
        row = cur.fetchone()
        if not row:
            return None
        (status, title, artist, source, kind, file_id, duration_sec,
         quality, extra, cover_file_id, resolve_ms, created_at,
         expires_at) = row
        if int(expires_at) < int(time.time()):
            try:
                conn.execute("DELETE FROM identify_result_cache"
                             " WHERE cache_key = ?", (key,))
                conn.commit()
            except Exception:
                pass
            return None
        return {"status": status, "title": title or "",
                "artist": artist or "", "source": source or "",
                "kind": kind or "", "file_id": file_id or "",
                "duration_sec": duration_sec or 0,
                "quality": quality or "", "extra": extra or "",
                "cover_file_id": cover_file_id or "",
                "resolve_ms": resolve_ms or 0,
                "created_at": created_at,
                "ttl": max(0, int(expires_at) - int(created_at))}
    except Exception as e:
        _turso_invalidate()
        _log.debug("turso_cache: get_identify_result failed: %s", e)
        return None


def store_identify_result(key: str, entry: dict,
                          req_id: str | None = None) -> bool:
    """Step 1b: store the FINAL identify result under a shortcode key.

    ``entry`` carries status/title/artist/source/kind/file_id/
    duration_sec/quality/extra/cover_file_id/resolve_ms/created_at/ttl
    (ttl seconds from created_at). Only the file_id is persisted — a
    disk path never survives a restart. resolve_ms is the bg job's
    file-resolution time for this entry (0 when unknown), so the tap
    can log it as the search_file stage. Never raises; returns True
    when stored."""
    conn = _get_conn()
    if not conn or not key or not isinstance(entry, dict):
        return False
    try:
        now = int(entry.get("created_at") or time.time())
        ttl = int(entry.get("ttl") or 0)
        if ttl <= 0:
            return False
        _tq = time.perf_counter()
        conn.execute("""
            INSERT OR REPLACE INTO identify_result_cache
            (cache_key, status, title, artist, source, kind, file_id,
             duration_sec, quality, extra, cover_file_id, resolve_ms,
             created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (key, entry.get("status") or "", entry.get("title") or "",
              entry.get("artist") or "", entry.get("source") or "",
              entry.get("kind") or "", entry.get("file_id") or "",
              entry.get("duration_sec") or 0, entry.get("quality") or "",
              entry.get("extra") or "", entry.get("cover_file_id") or "",
              int(entry.get("resolve_ms") or 0),
              now, now + ttl))
        _timing.note_turso(req_id, "identify_result_cache.store",
                           (time.perf_counter() - _tq) * 1000, logger=_log)
        conn.commit()
        return True
    except Exception as e:
        _turso_invalidate()
        _log.debug("turso_cache: store_identify_result failed: %s", e)
        return False


def delete_identify_result(key: str, req_id: str | None = None) -> bool:
    """Step 1b: delete the cached result for a shortcode key
    (wrong-song dispute: the rejected name must never be re-shown).
    Never raises."""
    conn = _get_conn()
    if not conn or not key:
        return False
    try:
        conn.execute("DELETE FROM identify_result_cache WHERE cache_key = ?",
                     (key,))
        conn.commit()
        return True
    except Exception as e:
        _turso_invalidate()
        _log.debug("turso_cache: delete_identify_result failed: %s", e)
        return False


def delete_identify_result_if(key: str, title: str, artist: str,
                              req_id: str | None = None) -> bool:
    """Step 1b: delete the cached result ONLY when the row still holds
    the given (title, artist) — the wrong-song dispute's Turso half.
    One atomic statement, so a bg delivery that already stored the
    NEW result under the same key is never wiped by the delete.
    Never raises."""
    conn = _get_conn()
    if not conn or not key:
        return False
    try:
        cur = conn.execute(
            "DELETE FROM identify_result_cache"
            " WHERE cache_key = ? AND title = ? AND artist = ?",
            (key, title or "", artist or ""))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        _turso_invalidate()
        _log.debug("turso_cache: delete_identify_result_if failed: %s", e)
        return False


def get_suggest_result(key: str, req_id: str | None = None) -> dict | None:
    """Get a persisted suggestion result. None if miss/expired/error."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        _tq = time.perf_counter()
        cur = conn.execute(
            "SELECT title, artist, url, preview_url, expires_at "
            "FROM suggest_results WHERE result_key = ?",
            (key,))
        _timing.note_turso(req_id, "suggest_results.get",
                           (time.perf_counter() - _tq) * 1000, logger=_log)
        row = cur.fetchone()
        if not row:
            return None
        title, artist, url, preview_url, expires_at = (
            row[0], row[1], row[2], row[3], row[4])
        if int(expires_at) < int(time.time()):
            # Expired - delete
            try:
                conn.execute("DELETE FROM suggest_results WHERE result_key = ?",
                             (key,))
                conn.commit()
            except Exception:
                pass
            return None
        return {"title": title, "artist": artist,
                "url": url, "preview_url": preview_url}
    except Exception as e:
        _log.debug("turso_cache: get_suggest_result failed: %s", e)
        return None


def store_suggest_result(key: str, title: str, artist: str = "", url: str = "",
                         preview_url: str = "", ttl_days: int = 7,
                         req_id: str | None = None) -> bool:
    """Persist a suggestion result so its button survives restarts."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        now = int(time.time())
        expires = now + (ttl_days * 86400)
        _tq = time.perf_counter()
        conn.execute("""
            INSERT OR REPLACE INTO suggest_results
            (result_key, title, artist, url, preview_url, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (key, title, artist, url, preview_url, now, expires))
        _timing.note_turso(req_id, "suggest_results.store",
                           (time.perf_counter() - _tq) * 1000, logger=_log)
        conn.commit()
        return True
    except Exception as e:
        _log.debug("turso_cache: store_suggest_result failed: %s", e)
        return False


def store_wrong_song_report(user_id: int, link: str,
                            wrong_title: str, wrong_artist: str,
                            wrong_source: str,
                            new_title: str = "", new_artist: str = "",
                            new_source: str = "", outcome: str = "",
                            req_id: str | None = None) -> bool:
    """One row per «❌ آهنگ اشتباه» tap (batch F): the disputed name +
    its source, the link, and what the re-check found (outcome:
    "different_confirmed" | "different_maybe" | "not_found" | "error"
    | "limit"). Admin analytics only. Fail-open, never raises — the
    caller writes fire-and-forget AFTER the answer, never in a
    waiting path.

    Opens its OWN connection per call (never the cached global):
    the write runs in a background thread via asyncio.to_thread, and
    a connection created on the event-loop thread must not be used
    from another thread."""
    url = os.environ.get("TURSO_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        return False
    try:
        import libsql_experimental as libsql
        conn = libsql.connect(url, auth_token=token)
    except Exception as e:
        _log.debug("turso_cache: store_wrong_song_report connect failed: %s", e)
        return False
    try:
        now = int(time.time())
        _tq = time.perf_counter()
        conn.execute(_WRONG_SONG_REPORTS_DDL)
        conn.execute("""
            INSERT INTO wrong_song_reports
            (user_id, link, wrong_title, wrong_artist, wrong_source,
             new_title, new_artist, new_source, outcome, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (int(user_id or 0), (link or "")[:500],
              wrong_title or "", wrong_artist or "", wrong_source or "",
              new_title or "", new_artist or "", new_source or "",
              outcome or "", now))
        _timing.note_turso(req_id, "wrong_song_reports.store",
                           (time.perf_counter() - _tq) * 1000, logger=_log)
        conn.commit()
        return True
    except Exception as e:
        _log.debug("turso_cache: store_wrong_song_report failed: %s", e)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
