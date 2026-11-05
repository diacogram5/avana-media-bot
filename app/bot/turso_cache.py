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

_log = logging.getLogger("avana.turso_cache")

_turso_conn = None
_turso_ok = False


def _normalize_url(url: str) -> str:
    """Normalize URL for cache key (same as file_cache._normalize_url)."""
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(url.strip())
        # Lowercase host, strip trailing slash, drop query/fragment
        netloc = p.netloc.lower()
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme.lower(), netloc, path, "", "", ""))
    except Exception:
        return url.strip().lower()


def _url_key(url: str) -> str:
    return "url:" + hashlib.md5(_normalize_url(url).encode()).hexdigest()


def _get_conn():
    """Get or create Turso connection. Returns None if not configured/fails."""
    global _turso_conn, _turso_ok
    if _turso_conn is not None:
        return _turso_conn if _turso_ok else None

    url = os.environ.get("TURSO_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        _log.info("turso_cache: not configured (TURSO_URL/TOKEN missing)")
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
        conn.commit()
        _turso_conn = conn
        _turso_ok = True
        _log.info("turso_cache: connected OK (persistent cache active)")
        return conn
    except ImportError:
        _log.warning("turso_cache: libsql_experimental not installed")
        _turso_ok = False
        return None
    except Exception as e:
        _log.warning("turso_cache: connection failed: %s", e)
        _turso_ok = False
        return None


def get_url_file_id(url: str) -> tuple | None:
    """Get (file_id, file_type) from persistent Turso cache. None if miss/error."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        key = _url_key(url)
        cur = conn.execute(
            "SELECT file_id, file_type, expires_at FROM url_file_cache WHERE cache_key = ?",
            (key,))
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
        _log.debug("turso_cache: get failed: %s", e)
        return None


def store_url_file_id(url: str, file_id: str, file_type: str, ttl_days: int = 30) -> bool:
    """Store URL->file_id in persistent Turso cache. Returns True if stored."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        key = _url_key(url)
        now = int(time.time())
        expires = now + (ttl_days * 86400)
        conn.execute("""
            INSERT OR REPLACE INTO url_file_cache
            (cache_key, file_id, file_type, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
        """, (key, file_id, file_type, now, expires))
        conn.commit()
        return True
    except Exception as e:
        _log.debug("turso_cache: store failed: %s", e)
        return False
