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
    # Web-parity capabilities menu: unique display_order, exact Persian
    # titles. visible=1 -> shown in the «قابلیت‌ها» menu and the persistent
    # bottom menu; link_fetch + audio_id keep their automatic
    # trigger-on-send behaviour too, tts + voice_to_text live behind the
    # OpenAI key gate. Gating (can_use_feature) checks `enabled` only.
    ("song_search", "جستجوی آهنگ و ویدیو", 1, 1, 1),
    ("suggest", "درخواست آهنگ", 2, 1, 1),
    ("downloader", "دانلودر هوشمند", 3, 1, 1),
    ("professional", "🎵 حرفه‌ای", 4, 1, 1),
    ("lyrics", "متن آهنگ", 5, 1, 1),
    ("clear_chat", "پاک‌سازی وضعیت گفت‌وگو", 6, 1, 1),
    ("history", "تاریخچه جستجو", 7, 1, 1),
    ("help_support", "راهنما و پشتیبانی", 8, 1, 1),
    ("audio_id", "شناسایی آهنگ از فایل", 9, 1, 1),
    ("tts", "تبدیل متن به ویس", 10, 1, 0),
    ("voice_to_text", "تبدیل ویس به متن", 11, 1, 0),
    ("link_fetch", "دریافت از لینک", 12, 1, 1),
]

DEFAULT_SETTINGS = {
    "bot_name": "ربات جستجوی آهنگ و ویدیو",
    "welcome_message": (
        "🎵 به آوانو خوش آمدی!\n\n"
        "دستیار هوشمند جستجو و شناسایی آهنگ و ویدیو.\n\n"
        "🔎 جستجوی آهنگ و خواننده\n"
        "🎧 شناسایی موسیقی از روی کلیپ\n"
        "📹 دریافت ویدیو از منابع پشتیبانی‌شده\n"
        "🎼 جستجوی آهنگ‌های مشابه و دریافت فایل موسیقی\n\n"
        "👇 برای شروع، یکی از گزینه‌های منو را انتخاب کن."
    ),
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
    # Verified public music channels (checked 2026-10-29 via t.me): these are
    # real bank sources. Files enter the bank only via user forwards or when
    # the bot is a channel admin (Bot API limitation).
    for ident, label, order in [
        ("melodio", "melobit | ملوبیت [Iranian]", 0),
        ("AvangRecords", "Avang Music [Iranian]", 1),
        ("radiojavan", "Radio Javan [Iranian]", 2),
        ("imba_official", "IMBA Music [Foreign]", 3),
    ]:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO bank_sources"
                " (kind, identifier, label, enabled, sort_order, status, last_check)"
                " VALUES ('channel', ?, ?, 1, ?, 'verified_public', ?)",
                (ident, label, order, int(time.time())),
            )
        except Exception:
            pass


def init_db(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    apply_migrations(conn)
    return conn


# --- secret masking (P1-17) -------------------------------------------------
# Display/history masking only: the real value is always stored in the
# settings table itself. A key is sensitive when its name ends in _key /
# _token / _secret (case-insensitive) — that covers openai_api_key,
# audd_token, acrcloud_access_key/secret, spotify_client_secret, etc.
MASKED_VALUE = "••••••"
_SENSITIVE_SUFFIXES = ("_key", "_token", "_secret")


def is_sensitive_setting(key: str) -> bool:
    k = (key or "").strip().lower()
    return k in ("key", "token", "secret") or k.endswith(_SENSITIVE_SUFFIXES)


def mask_setting_value(key: str, value: str) -> str:
    """Masked display/history value for a setting; '' stays ''."""
    if not value:
        return value
    return MASKED_VALUE if is_sensitive_setting(key) else value


# --- Turso backing for user-owned tables (P1-14) ----------------------------
# Render's free tier wipes local data/bot.db on every restart/redeploy, so
# user-owned rows are mirrored to Turso (already the durable store for
# suggest_results, url_file_cache, exclusive_songs). Design — HOT-PATH
# SAFE (user order): NO Turso query ever runs in a user's waiting path.
#   * Local-first reads: every read below hits local SQLite only, so
#     list/count/render latency is unchanged. A restart is repaired by
#     restore_from_turso() at startup (before polling starts), which
#     copies Turso rows back into the fresh local DB — that is what
#     makes count_users (the admin user-count root cause) correct
#     after a restart, and what keeps blocked flags, playlists,
#     favourites and language choices alive across restarts.
#   * Deferred writes: every local write below only marks its row
#     dirty (a pure in-memory set add — no I/O). A background flush
#     (60 s heartbeat + a fire-and-forget task after the link answer
#     is sent) mirrors dirty rows to Turso. The user save therefore
#     always happens AFTER the answer is sent, never before it.
#   * Single writer: this bot is the only writer, so on restore the
#     local row wins on conflict (INSERT OR REPLACE local -> Turso).
#   * Best effort throughout: a Turso failure never raises and never
#     blocks the local write, so the bot keeps working exactly as
#     today when Turso is down.
# Tables NOT migrated (deliberate): search_history / search_cache /
# identify_cache / menu_collapse / download_jobs / outbox /
# speed_metrics / audio_index / audit_log / settings_history —
# regenerable caches, per-device state, or logs.
import threading as _threading

_DIRTY: dict[str, set] = {}
_DIRTY_DELETED: dict[str, set] = {}
_DIRTY_LOCK = _threading.Lock()
_RESTORE_DONE = False
_FLUSH_LOCK = _threading.Lock()

_TURSO_USER_COLS = ("telegram_id", "username", "first_name", "blocked",
                    "block_reason", "blocked_until", "created_at", "last_seen_at")
_TURSO_PLAYLIST_COLS = ("id", "telegram_id", "name", "description", "color",
                        "created_at")
_TURSO_ITEM_COLS = ("id", "playlist_id", "title", "artist", "url",
                    "preview_url", "position")
_TURSO_FAV_COLS = ("id", "telegram_id", "title", "artist", "url",
                   "preview_url", "created_at")
_TURSO_AD_COLS = ("id", "text", "enabled", "created_at", "title",
                  "photo_file_id", "link", "button_text", "views", "clicks")
_TURSO_FEATURE_COLS = ("key", "title", "display_order", "enabled",
                       "visible", "message")
# table -> (key column in the local table, turso columns, turso key col)
_TURSO_TABLES = {
    "users": ("telegram_id", _TURSO_USER_COLS, "telegram_id"),
    "user_language": ("telegram_id", ("telegram_id", "lang"), "telegram_id"),
    "playlists": ("id", _TURSO_PLAYLIST_COLS, "id"),
    "playlist_items": ("id", _TURSO_ITEM_COLS, "id"),
    "favorites": ("telegram_id", _TURSO_FAV_COLS, "telegram_id"),
    "settings": ("key", ("key", "value"), "key"),
    "features": ("key", _TURSO_FEATURE_COLS, "key"),
    "ads": ("id", _TURSO_AD_COLS, "id"),
}
# Tables whose local id is AUTOINCREMENT — their sqlite_sequence must be
# re-seeded after a restore (see restore_from_turso).
_TURSO_AUTOINC_TABLES = ("playlists", "playlist_items", "favorites", "ads")


def _turso_conn():
    """Shared Turso connection via turso_cache, or None. Never raises.

    Lazy import: turso_cache lives under app.bot; importing it at module
    load would couple this low-level module to the bot package.
    """
    try:
        from .bot import turso_cache as _tc
        return _tc._get_conn()
    except Exception:
        return None


def _turso_read(sql: str, params=()):
    """Turso SELECT rows (tuples) or None when Turso is unavailable/failed."""
    tconn = _turso_conn()
    if tconn is None:
        return None
    try:
        return tconn.execute(sql, params).fetchall()
    except Exception:
        return None


def _turso_write(sql: str, params=()) -> bool:
    """Best-effort Turso write. True if applied; never raises."""
    tconn = _turso_conn()
    if tconn is None:
        return False
    try:
        tconn.execute(sql, params)
        tconn.commit()
        return True
    except Exception:
        return False


def mark_dirty(table: str, key) -> None:
    """Mark one local row for the next background Turso mirror.

    Pure memory (no I/O) — safe to call from any write path, including
    the hot path: the mirror itself happens later, off the user's wait.
    """
    with _DIRTY_LOCK:
        _DIRTY.setdefault(table, set()).add(key)
        _DIRTY_DELETED.get(table, set()).discard(key)


def mark_deleted(table: str, key) -> None:
    """Mark one locally-deleted row so the mirror deletes it in Turso."""
    with _DIRTY_LOCK:
        _DIRTY_DELETED.setdefault(table, set()).add(key)
        _DIRTY.get(table, set()).discard(key)


def dirty_keys() -> dict[str, frozenset]:
    """Snapshot of pending dirty keys (tests / diagnostics)."""
    with _DIRTY_LOCK:
        return {t: frozenset(ks) for t, ks in _DIRTY.items() if ks}


def is_user_blocked(conn, telegram_id: int | None) -> bool:
    """Cycle-free blocked check for callback handlers (P1-16).

    Same source as handlers._blocked (users.blocked via get_user) but
    importable from app.features without importing handlers.py. Reads
    LOCAL SQLite only — the blocked gate itself uses the in-memory
    cache (app.bot.blocked_cache) and never calls this in a user path.
    """
    if telegram_id is None:
        return False
    user = get_user(conn, int(telegram_id))
    return bool(user and user.get("blocked"))


def _mirror_row_to_turso(table: str, row: dict | None, key) -> None:
    """Mirror one local row to Turso (REPLACE), or DELETE it when the
    local row is gone. Best effort; never raises."""
    _key_col, cols, _tkey = _TURSO_TABLES[table]
    if row is None:
        _turso_write(f"DELETE FROM {table} WHERE {_key_col}=?", (key,))
        if table == "playlists":
            _turso_write("DELETE FROM playlist_items WHERE playlist_id=?", (key,))
        return
    ph = ", ".join("?" for _ in cols)
    _turso_write(
        f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({ph})",
        tuple(row.get(c) if not isinstance(row.get(c), bool) else (1 if row.get(c) else 0)
              for c in cols))


def flush_dirty_to_turso(db_path: str) -> dict:
    """Mirror all dirty rows to Turso. Returns {"mirrored": n, ...}.

    Runs OFF the event loop (thread): it opens its OWN SQLite
    connection — the loop's connection must never be used from a
    thread. Concurrent flushes are skipped (one runs at a time).
    Never raises; a Turso failure leaves the rows dirty for the next
    flush. Called from the 60 s heartbeat and (fire-and-forget) after
    the link answer is sent — never before an answer.
    """
    if not _FLUSH_LOCK.acquire(blocking=False):
        return {"mirrored": 0, "skipped": True}
    mirrored = 0
    try:
        with _DIRTY_LOCK:
            work = {t: set(ks) for t, ks in _DIRTY.items() if ks}
            gone = {t: set(ks) for t, ks in _DIRTY_DELETED.items() if ks}
            _DIRTY.clear()
            _DIRTY_DELETED.clear()
        if not work and not gone:
            return {"mirrored": 0}
        if _turso_conn() is None:
            # Turso unavailable: re-queue everything for the next flush.
            with _DIRTY_LOCK:
                for t, ks in work.items():
                    _DIRTY.setdefault(t, set()).update(ks)
                for t, ks in gone.items():
                    _DIRTY_DELETED.setdefault(t, set()).update(ks)
            return {"mirrored": 0, "turso": False}
        try:
            from .bot import turso_cache as _tc
            _tc.ensure_user_tables(_turso_conn())
        except Exception:
            pass
        try:
            local = connect(db_path)
        except Exception:
            with _DIRTY_LOCK:
                for t, ks in work.items():
                    _DIRTY.setdefault(t, set()).update(ks)
                for t, ks in gone.items():
                    _DIRTY_DELETED.setdefault(t, set()).update(ks)
            return {"mirrored": 0, "local": False}
        try:
            for table, keys in gone.items():
                for key in keys:
                    _mirror_row_to_turso(table, None, key)
                    mirrored += 1
            for table, keys in work.items():
                key_col, cols, _tk = _TURSO_TABLES[table]
                for key in keys:
                    if table == "playlist_items":
                        # Keyed by playlist_id: replace the whole item
                        # set of that playlist (positions included).
                        _turso_write(
                            "DELETE FROM playlist_items WHERE playlist_id=?",
                            (key,))
                        try:
                            items = local.execute(
                                "SELECT * FROM playlist_items WHERE playlist_id=?",
                                (key,)).fetchall()
                        except Exception:
                            items = []
                        for it in items:
                            d = dict(it)
                            _mirror_row_to_turso("playlist_items", d, d["id"])
                            mirrored += 1
                        continue
                    if table == "favorites":
                        # Keyed by telegram_id: replace the user's set.
                        _turso_write(
                            "DELETE FROM favorites WHERE telegram_id=?", (key,))
                        try:
                            favs = local.execute(
                                "SELECT * FROM favorites WHERE telegram_id=?",
                                (key,)).fetchall()
                        except Exception:
                            favs = []
                        for fv in favs:
                            d = dict(fv)
                            _mirror_row_to_turso("favorites", d, d["id"])
                            mirrored += 1
                        continue
                    try:
                        r = local.execute(
                            f"SELECT * FROM {table} WHERE {key_col}=?",
                            (key,)).fetchone()
                    except Exception:
                        r = None
                    _mirror_row_to_turso(table, dict(r) if r else None, key)
                    mirrored += 1
        finally:
            try:
                local.close()
            except Exception:
                pass
        return {"mirrored": mirrored}
    finally:
        _FLUSH_LOCK.release()


def restore_from_turso(conn) -> dict:
    """One-time Turso -> local restore at startup (P1-14). Returns counts.

    Runs BEFORE polling starts, so no user waits on it. For each
    user-owned table: Turso rows missing locally are inserted
    (INSERT OR IGNORE — local wins on conflict), then local rows are
    pushed up to Turso (INSERT OR REPLACE — this bot is the only
    writer, so local is fresher than Turso whenever they differ, e.g.
    a write that never got flushed before the last restart).
    AUTOINCREMENT counters (sqlite_sequence) are re-seeded to the
    restored max id so a fresh local create can never reuse an id
    Turso already holds for a different row. Never raises.
    """
    global _RESTORE_DONE
    if _RESTORE_DONE:
        return {"restored": 0, "again": True}
    _RESTORE_DONE = True
    counts = {"restored": 0}
    try:
        tconn = _turso_conn()
        if tconn is None:
            return {"restored": 0, "turso": False}
        try:
            from .bot import turso_cache as _tc
            _tc.ensure_user_tables(tconn)
        except Exception:
            pass
        for table, (_key_col, cols, _tk) in _TURSO_TABLES.items():
            try:
                rows = tconn.execute(
                    f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            except Exception:
                continue
            ph = ", ".join("?" for _ in cols)
            try:
                for row in rows:
                    try:
                        cur = conn.execute(
                            f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) "
                            f"VALUES ({ph})", tuple(row))
                        counts["restored"] += cur.rowcount
                    except Exception:
                        pass
                # Push local state up (local wins: only writer).
                local_rows = conn.execute(
                    f"SELECT {', '.join(cols)} FROM {table}").fetchall()
                for row in local_rows:
                    try:
                        tconn.execute(
                            f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
                            f"VALUES ({ph})", tuple(row))
                    except Exception:
                        pass
                # Re-seed AUTOINCREMENT counters for id-keyed tables.
                if table in _TURSO_AUTOINC_TABLES:
                    try:
                        conn.execute(
                            "INSERT INTO sqlite_sequence (name, seq) "
                            "VALUES (?, (SELECT COALESCE(MAX(id), 0) FROM "
                            f"{table})) ON CONFLICT(name) DO UPDATE SET "
                            "seq=MAX(seq, excluded.seq)", (table,))
                    except Exception:
                        pass
            except Exception:
                continue
        try:
            tconn.commit()
        except Exception:
            pass
        try:
            conn.commit()
        except Exception:
            pass
    except Exception:
        pass
    return counts


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
    # P1-14: local write only — the Turso mirror is deferred (dirty
    # mark, no I/O); the flush runs after the answer is sent.
    mark_dirty("users", telegram_id)
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
    # P1-14: deferred Turso mirror (dirty mark only, no I/O).
    mark_dirty("features", key)


# --- persistent menu collapse (per user) ----------------------------------
def menu_collapsed(conn, telegram_id: int) -> bool:
    """True when this user collapsed the bottom menu (only «☰ منو» shown).

    Stored in the DB, so it survives bot code restarts. Honest limit: on
    Render's free tier bot.db itself is ephemeral, so a restart/redeploy
    resets this to open (False) — see migrations/013_menu_collapse.sql.
    """
    row = conn.execute(
        "SELECT collapsed FROM menu_collapse WHERE telegram_id=?",
        (telegram_id,)).fetchone()
    return bool(row and row[0])


def set_menu_collapsed(conn, telegram_id: int, collapsed: bool) -> None:
    conn.execute(
        "INSERT INTO menu_collapse (telegram_id, collapsed) VALUES (?,?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET collapsed=excluded.collapsed",
        (telegram_id, 1 if collapsed else 0))
    conn.commit()


# --- per-user UI language (fa/en/ar) --------------------------------------
SUPPORTED_LANGS = ("fa", "en", "ar")


def user_language(conn, telegram_id: int) -> str | None:
    """The user's chosen UI language, or None when never chosen.

    Stored in the DB (migration 016), same pattern as menu_collapse.
    Returns None (not "fa") when the user never picked: callers show the
    language picker on first /start and fall back to "fa" elsewhere.
    Honest limit: on Render's free tier bot.db is ephemeral, so a
    restart/redeploy resets this to None — same as every other per-user
    setting. The language never affects download/identify logic.
    """
    row = conn.execute(
        "SELECT lang FROM user_language WHERE telegram_id=?",
        (telegram_id,)).fetchone()
    lang = row[0] if row else None
    return lang if lang in SUPPORTED_LANGS else None


def set_user_language(conn, telegram_id: int, lang: str) -> None:
    if lang not in SUPPORTED_LANGS:
        raise ValueError(f"unsupported language: {lang!r}")
    conn.execute(
        "INSERT INTO user_language (telegram_id, lang) VALUES (?,?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET lang=excluded.lang",
        (telegram_id, lang))
    conn.commit()
    # P1-14: deferred Turso mirror (dirty mark only, no I/O).
    mark_dirty("user_language", telegram_id)


def user_lang_or_default(conn, telegram_id: int | None) -> str:
    """UI language for rendering: stored choice, else Persian ("fa")."""
    if telegram_id is None:
        return "fa"
    return user_language(conn, telegram_id) or "fa"


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
        # P1-17: settings_history and the audit line must never hold the real
        # secret — store the masked value. The settings table above keeps the
        # real value (only display/history are masked).
        (key, mask_setting_value(key, old), mask_setting_value(key, value),
         by_id, int(time.time())),
    )
    audit(conn, by_id, "set_setting", f"{key}={mask_setting_value(key, value)}")
    conn.commit()
    # P1-14: deferred Turso mirror (dirty mark only, no I/O).
    mark_dirty("settings", key)


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
    cur = conn.execute("INSERT INTO ads (text, enabled, created_at) VALUES (?,?,?)",
                       (text, 1, int(_t.time())))
    audit(conn, by_id, "add_ad", text[:80])
    conn.commit()
    # P1-14: deferred Turso mirror (dirty mark only, no I/O).
    mark_dirty("ads", cur.lastrowid)


def set_ad_enabled(conn, ad_id: int, enabled: bool, by_id: int | None = None) -> None:
    conn.execute("UPDATE ads SET enabled=? WHERE id=?", (1 if enabled else 0, ad_id))
    audit(conn, by_id, "set_ad", f"{ad_id} enabled={enabled}")
    conn.commit()
    # P1-14: deferred Turso mirror (dirty mark only, no I/O).
    mark_dirty("ads", ad_id)


def set_user_blocked(conn, telegram_id: int, blocked: bool, by_id: int | None = None) -> None:
    conn.execute("UPDATE users SET blocked=? WHERE telegram_id=?", (1 if blocked else 0, telegram_id))
    audit(conn, by_id, "set_user_blocked", f"{telegram_id} blocked={blocked}")
    conn.commit()
    # P1-14: deferred Turso mirror (dirty mark only, no I/O). The
    # in-memory blocked cache is updated by the caller (admin/web
    # panel), not here — this module stays cache-agnostic.
    mark_dirty("users", telegram_id)


# --- playlists / favorites / cache ---------------------------------------
def create_playlist(conn, telegram_id: int, name: str, description: str = "", color: str = "") -> dict:
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO playlists (telegram_id, name, description, color, created_at) VALUES (?,?,?,?,?)",
        (telegram_id, name.strip(), description, color, now))
    conn.commit()
    # P1-14: deferred Turso mirror (dirty mark only, no I/O).
    mark_dirty("playlists", cur.lastrowid)
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
    # P1-14: deferred Turso mirror (dirty mark only, no I/O).
    mark_dirty("playlists", playlist_id)


def delete_playlist(conn, playlist_id: int) -> None:
    conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
    conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    conn.commit()
    # P1-14: deferred Turso mirror — tombstones, so the flush deletes
    # the rows in Turso too.
    mark_deleted("playlists", playlist_id)
    mark_deleted("playlist_items", playlist_id)


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
    # P1-14: deferred Turso mirror of the whole item set (dirty mark
    # only, no I/O).
    mark_dirty("playlist_items", playlist_id)
    return True


def remove_from_playlist(conn, item_id: int) -> None:
    row = conn.execute(
        "SELECT playlist_id FROM playlist_items WHERE id=?", (item_id,)).fetchone()
    pid = row[0] if row else None
    conn.execute("DELETE FROM playlist_items WHERE id=?", (item_id,))
    conn.commit()
    # P1-14: deferred Turso mirror of the whole item set (dirty mark
    # only, no I/O).
    if pid is not None:
        mark_dirty("playlist_items", pid)


def reorder_playlist(conn, playlist_id: int, item_ids: list[int]) -> None:
    for pos, item_id in enumerate(item_ids):
        conn.execute("UPDATE playlist_items SET position=? WHERE id=? AND playlist_id=?",
                     (pos, item_id, playlist_id))
    conn.commit()
    # P1-14: deferred Turso mirror of the whole item set (dirty mark
    # only, no I/O).
    mark_dirty("playlist_items", playlist_id)


def add_favorite(conn, telegram_id: int, title: str, artist: str = "", url: str = "", preview_url: str = "") -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO favorites (telegram_id, title, artist, url, preview_url, created_at) VALUES (?,?,?,?,?,?)",
        (telegram_id, title, artist, url, preview_url, int(time.time())))
    conn.commit()
    # P1-14: deferred Turso mirror of the user's set (dirty mark only).
    if cur.rowcount > 0:
        mark_dirty("favorites", telegram_id)
    return cur.rowcount > 0


def list_favorites(conn, telegram_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM favorites WHERE telegram_id=? ORDER BY created_at DESC", (telegram_id,)).fetchall()
    return [dict(r) for r in rows]


def remove_favorite(conn, fav_id: int) -> None:
    row = conn.execute(
        "SELECT telegram_id FROM favorites WHERE id=?", (fav_id,)).fetchone()
    uid = row[0] if row else None
    conn.execute("DELETE FROM favorites WHERE id=?", (fav_id,))
    conn.commit()
    # P1-14: deferred Turso mirror of the user's set (dirty mark only).
    if uid is not None:
        mark_dirty("favorites", uid)


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


# Audio-content-hash identify cache.
#
# Keys: "hash:<sha256 of the input audio bytes>" (content-keyed, stable
# across re-uploads) or "url:<source url>" (lets a later flow skip the
# download entirely when the same audio URL is seen again, e.g. the
# one-click identify button after a link identify).
# Only terminal results ("identified" / "not_found") are stored —
# lookup/transport errors are never cached, so a transient failure can
# never turn into a permanent answer.
IDENTIFY_CACHE_TTL = 7 * 86400       # hash keys: audio content is immutable
IDENTIFY_CACHE_URL_TTL = 86400       # url keys: signed CDN URLs rotate
# Fix round 3: a stored "not_found" is a SHORT negative (10 min,
# matching the 1b negative TTL) — a transient miss must never block
# re-identification for 7 days. Applied at read in get_identify_cache.
IDENTIFY_CACHE_NOTFOUND_TTL = 600
IDENTIFY_CACHE_MAX_ROWS = 1000       # cap; oldest rows evicted first


def get_identify_cache(conn, key: str, max_age: int = IDENTIFY_CACHE_TTL) -> dict | None:
    row = conn.execute(
        "SELECT status, title, artist, created_at FROM identify_cache WHERE key=?",
        (key,)).fetchone()
    if not row:
        return None
    _age = int(time.time()) - int(row[3])
    # Fix round 3: a "not_found" row expires after the SHORT negative
    # TTL (10 min) even when the caller allows a longer max_age — the
    # 7d hash TTL applies to identified results only. A transient
    # miss is re-identified on the next tap after 10 min instead of
    # being bank-only-blocked for a week.
    _limit = (min(max_age, IDENTIFY_CACHE_NOTFOUND_TTL)
              if row[0] == "not_found" else max_age)
    if _age > _limit:
        return None
    return {"status": row[0], "title": row[1] or "", "artist": row[2] or ""}


def set_identify_cache(conn, key: str, status: str,
                       title: str = "", artist: str = "") -> None:
    """Store a terminal identify result. Silently ignores anything that
    is not identified/not_found (honesty: errors are never cached)."""
    if status not in ("identified", "not_found"):
        return
    conn.execute(
        "INSERT INTO identify_cache (key, status, title, artist, created_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET status=excluded.status, title=excluded.title, "
        "artist=excluded.artist, created_at=excluded.created_at",
        (key, status, title or "", artist or "", int(time.time())))
    conn.commit()
    _prune_identify_cache(conn)


def _prune_identify_cache(conn) -> None:
    """Keep the table small: drop the oldest rows beyond the cap."""
    try:
        conn.execute(
            "DELETE FROM identify_cache WHERE key NOT IN "
            "(SELECT key FROM identify_cache "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
            (IDENTIFY_CACHE_MAX_ROWS,))
        conn.commit()
    except Exception:
        pass


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
