"""SQLite (stdlib sqlite3) data layer + tiny versioned migrations.

Why sqlite3, not an ORM/server DB, in stage 1: zero extra dependency, the
bot runs locally with one file, and the schema is explicit SQL in
``migrations/*.sql``. A server DB can be added later behind this same
module boundary; callers never write raw SQL outside this file (except
tests, which use these helpers).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

DEFAULT_FEATURES = [
    # Launch version: core features only, no duplicates.
    ("song_search", "جستجوی آهنگ", 1, 1, 1),
    ("audio_id", "شناسایی آهنگ از صدا/فیلم", 2, 1, 1),
    ("tts", "تبدیل متن به ویس فارسی", 3, 1, 1),
    ("link_fetch", "دریافت از لینک", 4, 1, 1),
    ("voice_to_text", "تبدیل ویس به متن", 5, 1, 1),
    ("help_support", "راهنما و پشتیبانی", 6, 1, 1),
]

DEFAULT_SETTINGS = {
    "bot_name": "ربات جستجوی آهنگ و ویدیو",
    "welcome_message": "سلام! به ربات جستجوی آهنگ و ویدیو خوش آمدید.",
    "support_username": "",
    "daily_search_limit": "50",
    "recent_searches_enabled": "1",
}


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    return {row[0] for row in conn.execute("SELECT version FROM schema_version")}


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply every migrations/*.sql file, in name order, exactly once."""
    done = applied_versions(conn)
    applied: list[str] = []
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = sql_file.stem
        if version in done:
            continue
        conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, int(time.time())),
        )
        conn.commit()
        applied.append(version)
    _seed(conn)
    conn.commit()
    return applied


def _seed(conn: sqlite3.Connection) -> None:
    for key, title, order, enabled, visible in DEFAULT_FEATURES:
        conn.execute(
            "INSERT OR IGNORE INTO features (key, title, display_order, enabled, visible) VALUES (?,?,?,?,?)",
            (key, title, order, enabled, visible),
        )
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (key, value)
        )


def init_db(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    apply_migrations(conn)
    return conn


# --- users ---------------------------------------------------------------
def upsert_user(conn, telegram_id: int, username: str = "", first_name: str = "") -> dict:
    now = int(time.time())
    conn.execute(
        """INSERT INTO users (telegram_id, username, first_name, created_at, last_seen_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,
             first_name=excluded.first_name, last_seen_at=excluded.last_seen_at""",
        (telegram_id, username, first_name, now, now),
    )
    conn.commit()
    return get_user(conn, telegram_id) or {}


def get_user(conn, telegram_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    return dict(row) if row else None


def list_users(conn, limit: int = 50, offset: int = 0) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM users ORDER BY last_seen_at DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return [dict(r) for r in rows]


def count_users(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])






# --- features -------------------------------------------------------------
def list_features(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM features ORDER BY display_order").fetchall()
    return [dict(r) for r in rows]


def get_feature(conn, key: str) -> dict | None:
    row = conn.execute("SELECT * FROM features WHERE key=?", (key,)).fetchone()
    return dict(row) if row else None


def feature_enabled(conn, key: str) -> bool:
    feat = get_feature(conn, key)
    return bool(feat and feat["enabled"] and feat["visible"])


def set_feature(conn, key: str, enabled: bool | None = None, visible: bool | None = None,
                title: str | None = None, by_id: int | None = None) -> None:
    feat = get_feature(conn, key)
    if feat is None:
        raise KeyError(f"unknown feature: {key}")
    conn.execute(
        "UPDATE features SET enabled=?, visible=?, title=? WHERE key=?",
        (
            feat["enabled"] if enabled is None else (1 if enabled else 0),
            feat["visible"] if visible is None else (1 if visible else 0),
            title if title is not None else feat["title"],
            key,
        ),
    )
    audit(conn, by_id, "set_feature", f"{key} enabled={enabled} visible={visible}")
    conn.commit()


# --- settings -------------------------------------------------------------
def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn, key: str, value: str, by_id: int | None = None) -> None:
    old = get_setting(conn, key, "")
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.execute(
        "INSERT INTO settings_history (key, old_value, new_value, changed_by, changed_at) VALUES (?,?,?,?,?)",
        (key, old, value, by_id, int(time.time())),
    )
    audit(conn, by_id, "set_setting", f"{key}={value}")
    conn.commit()


def reset_settings_section(conn, keys: list[str], by_id: int | None = None) -> None:
    for key in keys:
        if key in DEFAULT_SETTINGS:
            set_setting(conn, key, DEFAULT_SETTINGS[key], by_id=by_id)


def list_settings(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM settings")}


# --- searches / file cache -------------------------------------------------
def record_search(conn, telegram_id: int, query: str, source: str = "", ok: bool = True) -> None:
    conn.execute(
        "INSERT INTO search_history (telegram_id, query, source, ok, created_at) VALUES (?,?,?,?,?)",
        (telegram_id, query, source, 1 if ok else 0, int(time.time())),
    )
    conn.commit()


def recent_searches(conn, telegram_id: int, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM search_history WHERE telegram_id=? ORDER BY created_at DESC LIMIT ?",
        (telegram_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def searches_today(conn, telegram_id: int) -> int:
    day_start = int(time.time()) - (int(time.time()) % 86400)
    row = conn.execute(
        "SELECT COUNT(*) FROM search_history WHERE telegram_id=? AND created_at>=?",
        (telegram_id, day_start),
    ).fetchone()
    return int(row[0]) if row else 0




# --- ads (web panel) -------------------------------------------------------
def list_ads(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM ads ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def add_ad(conn, text: str, by_id: int | None = None) -> None:
    import time as _t
    conn.execute("INSERT INTO ads (text, enabled, created_at) VALUES (?,?,?)",
                 (text, 1, int(_t.time())))
    audit(conn, by_id, "add_ad", text[:80])
    conn.commit()


def set_ad_enabled(conn, ad_id: int, enabled: bool, by_id: int | None = None) -> None:
    conn.execute("UPDATE ads SET enabled=? WHERE id=?", (1 if enabled else 0, ad_id))
    audit(conn, by_id, "set_ad", f"{ad_id} enabled={enabled}")
    conn.commit()


def set_user_blocked(conn, telegram_id: int, blocked: bool, by_id: int | None = None) -> None:
    conn.execute("UPDATE users SET blocked=? WHERE telegram_id=?", (1 if blocked else 0, telegram_id))
    audit(conn, by_id, "set_user_blocked", f"{telegram_id} blocked={blocked}")
    conn.commit()


# --- playlists / favorites / cache ---------------------------------------
def create_playlist(conn, telegram_id: int, name: str, description: str = "", color: str = "") -> dict:
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO playlists (telegram_id, name, description, color, created_at) VALUES (?,?,?,?,?)",
        (telegram_id, name.strip(), description, color, now))
    conn.commit()
    return get_playlist(conn, cur.lastrowid) or {}


def get_playlist(conn, playlist_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
    return dict(row) if row else None


def list_playlists(conn, telegram_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM playlists WHERE telegram_id=? ORDER BY created_at", (telegram_id,)).fetchall()
    return [dict(r) for r in rows]


def rename_playlist(conn, playlist_id: int, name: str, description: str | None = None) -> None:
    pl = get_playlist(conn, playlist_id)
    if pl is None:
        raise KeyError("playlist not found")
    conn.execute("UPDATE playlists SET name=?, description=? WHERE id=?",
                 (name.strip(), pl["description"] if description is None else description, playlist_id))
    conn.commit()


def delete_playlist(conn, playlist_id: int) -> None:
    conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
    conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    conn.commit()


def playlist_items(conn, playlist_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY position, id", (playlist_id,)).fetchall()
    return [dict(r) for r in rows]


def add_to_playlist(conn, playlist_id: int, title: str, artist: str = "", url: str = "", preview_url: str = "") -> bool:
    items = playlist_items(conn, playlist_id)
    if any(i["title"] == title and i["artist"] == artist for i in items):
        return False
    conn.execute(
        "INSERT OR IGNORE INTO playlist_items (playlist_id, title, artist, url, preview_url, position) VALUES (?,?,?,?,?,?)",
        (playlist_id, title, artist, url, preview_url, len(items)))
    conn.commit()
    return True


def remove_from_playlist(conn, item_id: int) -> None:
    conn.execute("DELETE FROM playlist_items WHERE id=?", (item_id,))
    conn.commit()


def reorder_playlist(conn, playlist_id: int, item_ids: list[int]) -> None:
    for pos, item_id in enumerate(item_ids):
        conn.execute("UPDATE playlist_items SET position=? WHERE id=? AND playlist_id=?",
                     (pos, item_id, playlist_id))
    conn.commit()


def add_favorite(conn, telegram_id: int, title: str, artist: str = "", url: str = "", preview_url: str = "") -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO favorites (telegram_id, title, artist, url, preview_url, created_at) VALUES (?,?,?,?,?,?)",
        (telegram_id, title, artist, url, preview_url, int(time.time())))
    conn.commit()
    return cur.rowcount > 0


def list_favorites(conn, telegram_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM favorites WHERE telegram_id=? ORDER BY created_at DESC", (telegram_id,)).fetchall()
    return [dict(r) for r in rows]


def remove_favorite(conn, fav_id: int) -> None:
    conn.execute("DELETE FROM favorites WHERE id=?", (fav_id,))
    conn.commit()


def get_cached_search(conn, key: str, max_age: int = 86400) -> str | None:
    row = conn.execute("SELECT result_json, created_at FROM search_cache WHERE key=?", (key,)).fetchone()
    if not row or int(time.time()) - int(row[1]) > max_age:
        return None
    return row[0]


def set_cached_search(conn, key: str, result_json: str) -> None:
    conn.execute(
        "INSERT INTO search_cache (key, result_json, created_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET result_json=excluded.result_json, created_at=excluded.created_at",
        (key, result_json, int(time.time())))
    conn.commit()


def get_bot_id(conn, config) -> str:
    """DB setting wins over env; empty when unset (never a fabricated ID)."""
    value = get_setting(conn, "bot_id", "")
    return value.strip() or (config.bot_id or "").strip()


def get_web_url(conn, config) -> str:
    value = get_setting(conn, "web_url", "")
    return value.strip() or (config.web_url or "").strip()


def get_channels(conn, config) -> list[str]:
    value = get_setting(conn, "channel_usernames", "")
    raw = value.strip() or (config.channel_usernames or "").strip()
    return [c.strip().lstrip("@") for c in raw.split(",") if c.strip()]


# --- audio index (files actually sent/forwarded to the bot) ------------
def _ensure_audio_index(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audio_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL DEFAULT '',
            chunk_hashes TEXT NOT NULL DEFAULT '[]', size INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '', artist TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL DEFAULT '', post_url TEXT NOT NULL DEFAULT '',
            caption TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL DEFAULT 0)""")
    conn.commit()


def index_audio_file(conn, fingerprint: dict, title: str = "", artist: str = "",
                     channel: str = "", post_url: str = "", caption: str = "") -> None:
    """Index a file the bot has actually seen. Idempotent on full hash."""
    _ensure_audio_index(conn)
    existing = conn.execute("SELECT id FROM audio_index WHERE sha256=?",
                            (fingerprint.get("sha256") or "",)).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO audio_index (sha256, chunk_hashes, size, title, artist, channel, post_url, caption, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (fingerprint.get("sha256") or "", json_dumps(fingerprint.get("chunk_hashes") or []),
         fingerprint.get("size") or 0, title, artist, channel, post_url, caption, int(time.time())))
    conn.commit()


def list_audio_index(conn) -> list[dict]:
    _ensure_audio_index(conn)
    rows = conn.execute("SELECT * FROM audio_index ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


# --- audit -----------------------------------------------------------------
def audit(conn, actor_id: int | None, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log (actor_id, action, detail, created_at) VALUES (?,?,?,?)",
        (actor_id, action, detail, int(time.time())),
    )


def recent_audit(conn, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
