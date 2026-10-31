"""Telegram file_id cache for Requirement #3.

Stores Telegram file_ids for previously sent files to avoid re-upload.
Implements cache management (Requirement #4):
- TTL expiration (7 days default for songs, 30 days for URLs)
- Validation (checks file still exists before reuse)
- Corrupt file prevention
- Reported content blocking
"""
from __future__ import annotations

import time
import sqlite3
import hashlib
from urllib.parse import urlparse, urlunparse
from typing import Optional

# Cache TTL: 7 days (same as identify_cache)
FILE_CACHE_TTL = 7 * 86400
# URL cache TTL: 30 days (links are stable)
URL_CACHE_TTL = 30 * 86400

def _cache_key(artist: str, title: str) -> str:
    """Generate cache key for a song."""
    return f"song:{artist.strip().lower()}|{title.strip().lower()}"

def _normalize_url(url: str) -> str:
    """Normalize URL for cache key: lowercase host, strip trailing slash, drop query/fragment."""
    try:
        p = urlparse(url.strip())
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/'), '', '', ''))
    except Exception:
        return url.strip().lower()

def _url_cache_key(url: str) -> str:
    """Generate cache key for a URL (MD5 of normalized URL)."""
    norm = _normalize_url(url)
    return "url:" + hashlib.md5(norm.encode()).hexdigest()

def get_cached_file_id(conn: sqlite3.Connection, artist: str, title: str) -> Optional[str]:
    """Get cached Telegram file_id for a song, if valid and not expired.
    
    Returns file_id or None. Checks:
    - Not expired (expires_at > now)
    - Valid (is_valid = 1)
    - Not reported/blocked
    """
    key = _cache_key(artist, title)
    now = int(time.time())
    
    # Check if reported/blocked
    blocked = conn.execute(
        "SELECT 1 FROM reported_content WHERE cache_key=? AND status='blocked'",
        (key,)
    ).fetchone()
    if blocked:
        return None
    
    row = conn.execute(
        """SELECT file_id, expires_at, is_valid FROM file_cache 
           WHERE cache_key=? AND expires_at > ? AND is_valid = 1""",
        (key, now)
    ).fetchone()
    
    if not row:
        return None
    
    # Update use count
    conn.execute(
        "UPDATE file_cache SET use_count = use_count + 1, last_used_at = ? WHERE cache_key=?",
        (now, key)
    )
    conn.commit()
    
    return row["file_id"]

def store_file_id(conn: sqlite3.Connection, artist: str, title: str, 
                  file_id: str, file_type: str = "audio",
                  file_size: int = 0, duration_sec: float = 0,
                  source: str = "") -> bool:
    """Store a Telegram file_id in cache.
    
    Returns True on success, False if content is blocked.
    """
    key = _cache_key(artist, title)
    
    # Don't store blocked content
    blocked = conn.execute(
        "SELECT 1 FROM reported_content WHERE cache_key=? AND status='blocked'",
        (key,)
    ).fetchone()
    if blocked:
        return False
    
    now = int(time.time())
    expires = now + FILE_CACHE_TTL
    
    conn.execute(
        """INSERT INTO file_cache 
           (cache_key, file_id, file_type, title, artist, file_size, 
            duration_sec, source, created_at, expires_at, use_count, last_used_at, is_valid)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1)
           ON CONFLICT(cache_key) DO UPDATE SET
             file_id=excluded.file_id,
             file_size=excluded.file_size,
             duration_sec=excluded.duration_sec,
             expires_at=excluded.expires_at,
             use_count=file_cache.use_count + 1,
             last_used_at=excluded.last_used_at,
             is_valid=1""",
        (key, file_id, file_type, title, artist, file_size, 
         duration_sec, source, now, expires, now)
    )
    conn.commit()
    return True

def invalidate_file(conn: sqlite3.Connection, artist: str, title: str):
    """Mark a cached file as invalid (e.g., if Telegram reports it as bad)."""
    key = _cache_key(artist, title)
    conn.execute(
        "UPDATE file_cache SET is_valid = 0 WHERE cache_key=?",
        (key,)
    )
    conn.commit()

def report_content(conn: sqlite3.Connection, artist: str, title: str, 
                   reason: str, reported_by: int):
    """Report content for review (Requirement #4)."""
    key = _cache_key(artist, title)
    now = int(time.time())
    conn.execute(
        """INSERT INTO reported_content (cache_key, reason, reported_at, reported_by, status)
           VALUES (?, ?, ?, ?, 'pending')
           ON CONFLICT(cache_key) DO UPDATE SET
             reason=excluded.reason,
             reported_at=excluded.reported_at,
             reported_by=excluded.reported_by,
             status='pending'""",
        (key, reason, now, reported_by)
    )
    conn.commit()

def block_content(conn: sqlite3.Connection, artist: str, title: str):
    """Block content (admin action). Also invalidates cache."""
    key = _cache_key(artist, title)
    conn.execute(
        "UPDATE reported_content SET status='blocked' WHERE cache_key=?",
        (key,)
    )
    invalidate_file(conn, artist, title)
    conn.commit()

def prune_expired(conn: sqlite3.Connection) -> int:
    """Remove expired cache entries. Returns count removed."""
    now = int(time.time())
    cur = conn.execute("DELETE FROM file_cache WHERE expires_at < ?", (now,))
    conn.commit()
    return cur.rowcount

def get_cache_stats(conn: sqlite3.Connection) -> dict:
    """Get cache statistics for monitoring."""
    now = int(time.time())
    total = conn.execute("SELECT COUNT(*) FROM file_cache").fetchone()[0]
    valid = conn.execute(
        "SELECT COUNT(*) FROM file_cache WHERE expires_at > ? AND is_valid = 1",
        (now,)
    ).fetchone()[0]
    total_uses = conn.execute("SELECT SUM(use_count) FROM file_cache").fetchone()[0] or 0
    
    return {
        "total_entries": total,
        "valid_entries": valid,
        "total_reuses": total_uses,
    }

# ---------------------------------------------------------------------------
# URL-based cache (for link flow: instant resend of previously sent links)
# ---------------------------------------------------------------------------

def get_cached_url_file_id(conn: sqlite3.Connection, url: str) -> Optional[tuple]:
    """Get cached (file_id, file_type) for a URL, if valid and not expired.
    
    Returns (file_id, file_type) or None.
    """
    key = _url_cache_key(url)
    now = int(time.time())
    
    # Check if reported/blocked
    blocked = conn.execute(
        "SELECT 1 FROM reported_content WHERE cache_key=? AND status='blocked'",
        (key,)
    ).fetchone()
    if blocked:
        return None
    
    row = conn.execute(
        """SELECT file_id, file_type, expires_at, is_valid FROM file_cache 
           WHERE cache_key=? AND expires_at > ? AND is_valid = 1""",
        (key, now)
    ).fetchone()
    
    if not row:
        return None
    
    # Update use count
    conn.execute(
        "UPDATE file_cache SET use_count = use_count + 1, last_used_at = ? WHERE cache_key=?",
        (now, key)
    )
    conn.commit()
    
    return (row["file_id"], row["file_type"])

def store_url_file_id(conn: sqlite3.Connection, url: str,
                      file_id: str, file_type: str = "video") -> bool:
    """Store a Telegram file_id for a URL. Returns True on success."""
    key = _url_cache_key(url)
    
    # Don't store blocked content
    blocked = conn.execute(
        "SELECT 1 FROM reported_content WHERE cache_key=? AND status='blocked'",
        (key,)
    ).fetchone()
    if blocked:
        return False
    
    now = int(time.time())
    expires = now + URL_CACHE_TTL
    
    conn.execute(
        """INSERT INTO file_cache 
           (cache_key, file_id, file_type, title, artist, file_size, 
            duration_sec, source, created_at, expires_at, use_count, last_used_at, is_valid)
           VALUES (?, ?, ?, ?, ?, 0, 0, 'url', ?, ?, 1, ?, 1)
           ON CONFLICT(cache_key) DO UPDATE SET
             file_id=excluded.file_id,
             file_type=excluded.file_type,
             expires_at=excluded.expires_at,
             use_count=file_cache.use_count + 1,
             last_used_at=excluded.last_used_at,
             is_valid=1""",
        (key, file_id, file_type, url[:200], "", now, expires, now)
    )
    conn.commit()
    return True
