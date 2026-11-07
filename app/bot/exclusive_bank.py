"""Exclusive Avano song bank — private, admin-curated full songs.

User order 2026-11-07: a hidden bank of songs Avano owns (or is licensed
to distribute in full). NO new main-menu item or button: the bank plugs
into the EXISTING identification path as a priority source.

Design
------
* Storage: Turso table ``exclusive_songs`` (same DB as the story archive;
  metadata only — the MP3 bytes live on Telegram, referenced by file_id).
  The table survives Render restarts; local SQLite is NOT used.
* Fingerprint: Chromaprint via the vendored static ``fpcalc`` binary
  (``app/bin/fpcalc`` — statically linked, zero runtime deps; runs on
  Render's Linux x86_64). A song's fingerprint is the raw fpcalc integer
  array, stored comma-separated. Matching is a sliding-window bit
  similarity (fraction of equal bits) between the sample's array and the
  stored array — verified in the sandbox 2026-11-07 on real music:
  same song 0.86–0.98, different song 0.62–0.63, 128 kbps re-encode of the
  same song 0.93. Gate: similarity >= 0.75 AND >= 15 matched items.
  An uncertain result is NEVER declared a match.
* Quality: the REAL bitrate is probed with ffprobe from the downloaded
  bytes (full file, or the first 2 MB head). A file is labelled 320 kbps
  ONLY when ffprobe reports 320000. Upconverting a low-bitrate file does
  not create real quality, so the measured value is stored and shown.
* Add flow: admin sends the MP3 in the admin panel (``xbank:panel``).
  Full-file download (Bot API, <= 20 MB) is tried first so the stored
  fingerprint covers the WHOLE song; for larger files the first-2 MB
  Range head is fingerprinted instead and the row is flagged
  ``fp_partial=1`` (only the song's opening is matchable then).
* Identification: ``_identify_audio`` in handlers.py fingerprints the
  validated sample AFTER stage 1 and BEFORE Shazam. A bank match returns
  the bank song's title/artist; every "identified" delivery point then
  routes through ``find_by_title_artist`` and sends the exclusive file
  with the exclusive card instead of the catalog card. No match -> the
  existing Shazam -> AudD -> ACRCloud chain runs untouched.
* Failure policy: ANY bank error (missing fpcalc, no Turso, probe
  failure) fails OPEN — the old identification path continues, and the
  admin add flow refuses honestly instead of storing an unmatchable row.
"""

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

_log = logging.getLogger("avana.exclusive_bank")

# --- fingerprint gates (measured 2026-11-07, see module docstring) ---
MATCH_MIN_SIM = 0.75
MATCH_MIN_ITEMS = 15
DUP_MIN_SIM = 0.95
FP_MAX_SECONDS = 900          # fingerprint the whole song (admin add)
FP_SAMPLE_MAX_SECONDS = 120   # identify-time samples are short anyway
HEAD_MAX_BYTES = 2 * 1024 * 1024
FPCALC_TIMEOUT_SEC = 20
FFPROBE_TIMEOUT_SEC = 15

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS exclusive_songs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    file_id TEXT NOT NULL,
    file_unique_id TEXT NOT NULL,
    duration_sec REAL NOT NULL DEFAULT 0,
    bitrate_kbps INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL DEFAULT '',
    fp_duration REAL NOT NULL DEFAULT 0,
    fp_partial INTEGER NOT NULL DEFAULT 0,
    cover_file_id TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    identify_count INTEGER NOT NULL DEFAULT 0,
    download_count INTEGER NOT NULL DEFAULT 0,
    added_by INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_xb_title_artist
    ON exclusive_songs(title COLLATE NOCASE, artist COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_xb_active ON exclusive_songs(active);
"""

_xb_conn = None
_xb_ok = None
_tables_ready = False


def _get_conn():
    """Turso connection for the exclusive bank. None when unavailable."""
    global _xb_conn, _xb_ok
    if _xb_conn is not None:
        return _xb_conn if _xb_ok else None
    url = os.environ.get("TURSO_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        _log.info("exclusive_bank: Turso not configured")
        _xb_ok = False
        return None
    try:
        import libsql_experimental as libsql
        conn = libsql.connect(url, auth_token=token)
        _init_tables(conn)
        _xb_conn = conn
        _xb_ok = True
        _log.info("exclusive_bank: Turso tables ready")
        return conn
    except ImportError:
        _log.warning("exclusive_bank: libsql_experimental not installed")
        _xb_ok = False
        return None
    except Exception as e:
        _log.warning("exclusive_bank: Turso connect failed: %s", e)
        _xb_ok = False
        return None


def _init_tables(conn) -> None:
    global _tables_ready
    if _tables_ready:
        return
    for stmt in (s.strip() for s in _TABLE_DDL.split(";") if s.strip()):
        conn.execute(stmt)
    conn.commit()
    _tables_ready = True


def bank_available() -> bool:
    return _get_conn() is not None


# ---------------------------------------------------------------------------
# Fingerprint engine (pure; fpcalc subprocess)
# ---------------------------------------------------------------------------

def _fpcalc_path() -> str:
    return str(Path(__file__).resolve().parent.parent / "bin" / "fpcalc")


def _ensure_executable(path: str) -> bool:
    try:
        if os.path.isfile(path) and not os.access(path, os.X_OK):
            os.chmod(path, 0o755)
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except Exception:
        return False


def fingerprint_file(path: str, max_seconds: int = FP_MAX_SECONDS):
    """Run fpcalc on an audio file. Returns (duration, [ints]) or (None, None).

    Never raises. ``-raw`` gives the integer array directly (no compressed
    fingerprint decoding needed).
    """
    fpcalc = _fpcalc_path()
    if not _ensure_executable(fpcalc):
        _log.warning("exclusive_bank: fpcalc not executable at %s", fpcalc)
        return None, None
    try:
        r = subprocess.run(
            [fpcalc, "-raw", "-length", str(max_seconds), path],
            capture_output=True, text=True, timeout=FPCALC_TIMEOUT_SEC)
    except Exception as e:
        _log.warning("exclusive_bank: fpcalc failed: %s", e)
        return None, None
    dur, items = None, None
    for line in (r.stdout or "").splitlines():
        if line.startswith("DURATION="):
            try:
                dur = float(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("FINGERPRINT="):
            try:
                items = [int(x) for x in line.split("=", 1)[1].split(",") if x]
            except ValueError:
                items = None
    if not items:
        return None, None
    return dur, items


def fingerprint_bytes(data: bytes, suffix: str = ".bin"):
    """Fingerprint raw audio bytes via a temp file. (duration, [ints]) or (None, None)."""
    if not data:
        return None, None
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, data)
        os.close(fd)
        fd = None
        return fingerprint_file(path, max_seconds=FP_SAMPLE_MAX_SECONDS)
    except Exception as e:
        _log.warning("exclusive_bank: fingerprint_bytes failed: %s", e)
        return None, None
    finally:
        try:
            if fd is not None:
                os.close(fd)
        except Exception:
            pass
        try:
            os.unlink(path)
        except Exception:
            pass


def _bit_similarity(a, b) -> float:
    """Fraction of equal bits between two equal-length int arrays."""
    if not a:
        return 0.0
    tot = 0
    for x, y in zip(a, b):
        tot += 32 - bin((x ^ y) & 0xFFFFFFFF).count("1")
    return tot / (32 * len(a))


def best_match(query, stored):
    """Best sliding-window match of query items inside stored items.

    Returns (similarity, offset, matched_items). A short query inside a
    long song aligns at the excerpt's position; the offset is the stored
    index where the best alignment starts.
    """
    n = len(query)
    if n == 0 or len(stored) < n:
        return 0.0, -1, 0
    best, boff = -1.0, -1
    for off in range(0, len(stored) - n + 1):
        sim = _bit_similarity(query, stored[off:off + n])
        if sim > best:
            best, boff = sim, off
    return best, boff, n


def match_against(query_items, songs):
    """Match a query fingerprint against bank rows.

    ``songs``: iterable of dicts with keys id/title/artist/fingerprint
    (comma-separated ints). Returns (song, similarity) for the best
    CONFIDENT match, else (None, best_similarity_seen). The gate is the
    whole point: below it, there is no match at all.
    """
    if not query_items or len(query_items) < MATCH_MIN_ITEMS:
        return None, 0.0
    best_song, best_sim = None, 0.0
    for song in songs:
        try:
            stored = [int(x) for x in (song.get("fingerprint") or "").split(",") if x]
        except ValueError:
            continue
        if len(stored) < MATCH_MIN_ITEMS:
            continue
        sim, _off, _n = best_match(query_items, stored)
        if sim > best_sim:
            best_sim, best_song = sim, song
    if best_song is not None and best_sim >= MATCH_MIN_SIM:
        return best_song, best_sim
    return None, best_sim


# ---------------------------------------------------------------------------
# Bitrate probe (ffprobe) + head download
# ---------------------------------------------------------------------------

def probe_bitrate(path: str) -> int:
    """Measured audio bitrate in kbps (0 when unknown). Never raises.

    ffprobe reads the stream bit_rate from the MP3 frame headers (or the
    Xing header) — a truncated head reports the same true value as the
    full file (verified 2026-11-07: 320k head -> 320000, 128k head -> 128000).
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=bit_rate",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SEC)
        out = (r.stdout or "").strip().split(",")
        for field in out:
            field = field.strip()
            if field.isdigit():
                return int(field) // 1000
    except Exception as e:
        _log.warning("exclusive_bank: ffprobe failed: %s", e)
    return 0


def probe_duration(path: str) -> float:
    """Media duration in seconds (0 when unknown). Never raises."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SEC)
        return float((r.stdout or "").strip().split(",")[0])
    except Exception:
        return 0.0


def download_head(bot_token: str, file_path: str, max_bytes: int = HEAD_MAX_BYTES):
    """Download the first ``max_bytes`` of a Telegram file via Range.

    Returns bytes or None. The Bot API file URL is a plain HTTPS endpoint,
    so a Range request returns 206 with exactly the requested prefix —
    verified in the sandbox 2026-11-07 (206, 200000 bytes, ffprobe reads
    the true bitrate from the head). Never raises.
    """
    if not bot_token or not file_path:
        return None
    url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    try:
        req = urllib.request.Request(
            url, headers={"Range": f"bytes=0-{max_bytes - 1}",
                          "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status not in (200, 206):
                return None
            return r.read(max_bytes)
    except Exception as e:
        _log.warning("exclusive_bank: head download failed: %s", e)
        return None


def analyse_audio_bytes(data: bytes):
    """Bitrate + fingerprint from audio bytes (full file or head).

    Returns dict(bitrate_kbps, duration_sec, fp_duration, fp_items) with
    fp_items a comma-separated string ("" when fingerprinting failed).
    Never raises.
    """
    fd, path = tempfile.mkstemp(suffix=".mp3")
    try:
        os.write(fd, data)
        os.close(fd)
        fd = None
        bitrate = probe_bitrate(path)
        duration = probe_duration(path)
        fp_dur, fp_items = fingerprint_file(path, max_seconds=FP_MAX_SECONDS)
        return {"bitrate_kbps": bitrate,
                "duration_sec": duration,
                "fp_duration": fp_dur or 0.0,
                "fp_items": ",".join(str(x) for x in fp_items) if fp_items else ""}
    except Exception as e:
        _log.warning("exclusive_bank: analyse_audio_bytes failed: %s", e)
        return {"bitrate_kbps": 0, "duration_sec": 0.0,
                "fp_duration": 0.0, "fp_items": ""}
    finally:
        try:
            if fd is not None:
                os.close(fd)
        except Exception:
            pass
        try:
            os.unlink(path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# DB layer (conn passed in; Turso libsql and sqlite3 share the API used here)
# ---------------------------------------------------------------------------

def _row_to_dict(row, cols):
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return dict(zip(cols, row))


_COLS = ("id", "title", "artist", "file_id", "file_unique_id",
         "duration_sec", "bitrate_kbps", "file_size", "fingerprint",
         "fp_duration", "fp_partial", "cover_file_id", "description",
         "active", "identify_count", "download_count", "added_by", "added_at")


def _norm(s: str) -> str:
    return (s or "").strip().casefold()


def list_songs(conn, active_only: bool = False):
    _init_tables(conn)
    q = f"SELECT {', '.join(_COLS)} FROM exclusive_songs"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY added_at DESC, id DESC"
    try:
        rows = conn.execute(q).fetchall()
    except Exception:
        rows = conn.execute(q).fetchall()
    return [_row_to_dict(r, _COLS) for r in rows]


def get_song(conn, song_id: int):
    _init_tables(conn)
    row = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM exclusive_songs WHERE id = ?",
        (song_id,)).fetchone()
    return _row_to_dict(row, _COLS)


def find_by_title_artist(conn, title: str, artist: str):
    """Exact (case-insensitive) title+artist lookup, active songs only."""
    _init_tables(conn)
    for song in list_songs(conn, active_only=True):
        if _norm(song["title"]) == _norm(title) and _norm(song["artist"]) == _norm(artist):
            return song
    return None


def find_duplicate(conn, title: str, artist: str, fp_items):
    """Duplicate = same title+artist, or same audio (fingerprint >= 0.95).

    Returns (song, reason) with reason 'title' or 'audio', else (None, '').
    """
    _init_tables(conn)
    for song in list_songs(conn):
        if _norm(song["title"]) == _norm(title) and _norm(song["artist"]) == _norm(artist):
            return song, "title"
    if fp_items:
        try:
            stored_q = [int(x) for x in fp_items] if isinstance(fp_items, list) else \
                [int(x) for x in str(fp_items).split(",") if x]
        except ValueError:
            stored_q = []
        if len(stored_q) >= MATCH_MIN_ITEMS:
            for song in list_songs(conn):
                try:
                    stored = [int(x) for x in (song.get("fingerprint") or "").split(",") if x]
                except ValueError:
                    continue
                if len(stored) < MATCH_MIN_ITEMS:
                    continue
                sim, _o, _n = best_match(stored_q, stored)
                if sim >= DUP_MIN_SIM:
                    return song, "audio"
                sim2, _o2, _n2 = best_match(stored, stored_q)
                if sim2 >= DUP_MIN_SIM:
                    return song, "audio"
    return None, ""


def add_song(conn, *, title, artist, file_id, file_unique_id, duration_sec,
             bitrate_kbps, file_size, fingerprint, fp_duration, fp_partial,
             cover_file_id="", description="", added_by=0):
    _init_tables(conn)
    now = int(time.time())
    cur = conn.execute(
        """INSERT INTO exclusive_songs
           (title, artist, file_id, file_unique_id, duration_sec,
            bitrate_kbps, file_size, fingerprint, fp_duration, fp_partial,
            cover_file_id, description, active, identify_count,
            download_count, added_by, added_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, ?, ?)""",
        (title.strip(), artist.strip(), file_id, file_unique_id, duration_sec,
         bitrate_kbps, file_size, fingerprint, fp_duration, 1 if fp_partial else 0,
         cover_file_id or "", description or "", added_by, now))
    conn.commit()
    try:
        return cur.lastrowid
    except Exception:
        row = conn.execute(
            "SELECT id FROM exclusive_songs WHERE file_unique_id = ?",
            (file_unique_id,)).fetchone()
        return row[0] if row else None


def update_song(conn, song_id: int, **fields):
    _init_tables(conn)
    allowed = ("title", "artist", "cover_file_id", "description", "active")
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    vals.append(song_id)
    conn.execute(
        f"UPDATE exclusive_songs SET {', '.join(sets)} WHERE id = ?", tuple(vals))
    conn.commit()


def set_active(conn, song_id: int, active: bool) -> None:
    update_song(conn, song_id, active=1 if active else 0)


def delete_song(conn, song_id: int) -> None:
    _init_tables(conn)
    conn.execute("DELETE FROM exclusive_songs WHERE id = ?", (song_id,))
    conn.commit()


def inc_identify(conn, song_id: int) -> None:
    try:
        conn.execute("UPDATE exclusive_songs SET identify_count = identify_count + 1 WHERE id = ?",
                     (song_id,))
        conn.commit()
    except Exception:
        pass


def inc_download(conn, song_id: int) -> None:
    try:
        conn.execute("UPDATE exclusive_songs SET download_count = download_count + 1 WHERE id = ?",
                     (song_id,))
        conn.commit()
    except Exception:
        pass


def match_sample(conn, query_items):
    """Fingerprint-match a sample against ACTIVE bank songs.

    Returns (song, similarity) or (None, best_similarity). Never raises;
    an empty bank or any error means 'no match' (caller falls through).
    """
    try:
        songs = list_songs(conn, active_only=True)
    except Exception as e:
        _log.warning("exclusive_bank: match_sample list failed: %s", e)
        return None, 0.0
    try:
        return match_against(query_items, songs)
    except Exception as e:
        _log.warning("exclusive_bank: match_against failed: %s", e)
        return None, 0.0


def exclusive_card_text(song) -> str:
    """The delivery card, exactly as the user specified (plus the bot id,
    which the standing rule requires on everything the bot sends). The
    bitrate shown is the MEASURED one — never a claimed 320."""
    kbps = int(song.get("bitrate_kbps") or 0)
    quality = f"MP3 {kbps}kbps" if kbps else "MP3"
    return (f"🎵 {song['title']}\n"
            f"🎤 {song['artist']}\n"
            f"💎 نسخه اختصاصی آوانو\n"
            f"🎧 کیفیت: {quality}\n"
            f"\n@AvanoMediaBot")

# ---------------------------------------------------------------------------
# Admin handlers (lazy aiogram; owner-only; inside the existing admin panel)
# ---------------------------------------------------------------------------

def _fmt_duration(sec: float) -> str:
    try:
        s = int(sec)
        return f"{s // 60}:{s % 60:02d}"
    except Exception:
        return "—"


def _panel_text(songs) -> str:
    n_active = sum(1 for s in songs if s.get("active"))
    lines = ["💎 بانک اختصاصی آوانو",
             f"{len(songs)} آهنگ — {n_active} فعال",
             "",
             "آهنگ‌های این بانک فقط از راه شناسایی آهنگ پیدا می‌شوند؛",
             "هیچ دکمه‌ای در منوی اصلی اضافه نشده است."]
    return "\n".join(lines)


def _song_detail_text(song) -> str:
    kbps = int(song.get("bitrate_kbps") or 0)
    fp = "کامل" if not song.get("fp_partial") else "جزئی (فقط ابتدای آهنگ)"
    lines = [f"🎵 {song['title']}",
             f"🎤 {song['artist']}",
             f"🎧 کیفیت واقعی: MP3 {kbps}kbps" if kbps else "🎧 کیفیت واقعی: نامشخص",
             f"⏱ مدت: {_fmt_duration(song.get('duration_sec') or 0)}",
             f"🔍 اثر انگشت: {fp}",
             f"📊 شناسایی: {song.get('identify_count') or 0} | دانلود: {song.get('download_count') or 0}",
             f"وضعیت: {'✅ فعال' if song.get('active') else '❌ غیرفعال'}"]
    if song.get("description"):
        lines.append(f"📝 {song['description']}")
    return "\n".join(lines)


def register_exclusive_bank_handlers(router) -> None:
    """Attach the admin-only exclusive-bank handlers to the router.

    All aiogram imports are lazy (inside this function) so the pure
    DB/fingerprint layer above stays importable without aiogram (sandbox
    tests). Registered right after the router is created in handlers.py —
    before the main audio handler — so the add-flow FSM states win over
    the normal audio flow, exactly like the story-archive manual add.
    """
    from aiogram import F
    from aiogram.fsm.context import FSMContext
    from aiogram.filters import StateFilter
    from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                               InlineKeyboardMarkup, Message)

    def _btn(text, cb=None, callback_data=None):
        return InlineKeyboardButton(text=text,
                                    callback_data=cb if cb is not None else callback_data)

    def _is_owner_cb(callback, config) -> bool:
        try:
            return (config.owner_id is not None
                    and callback.from_user.id == config.owner_id)
        except Exception:
            return False

    def _is_owner_msg(message, config) -> bool:
        try:
            return (config.owner_id is not None
                    and message.from_user.id == config.owner_id)
        except Exception:
            return False

    def _panel_kb(songs):
        rows = []
        for s in songs[:20]:
            mark = "✅" if s.get("active") else "❌"
            rows.append([_btn(f"{mark} {s['title']} — {s['artist']}",
                              f"xbank:view:{s['id']}")])
        rows.append([_btn("➕ افزودن آهنگ", "xbank:add")])
        rows.append([_btn("🔙 بازگشت", "adm:panel")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _view_kb(song):
        rows = [[_btn("❌ غیرفعال کردن" if song.get("active") else "✅ فعال کردن",
                      f"xbank:toggle:{song['id']}")],
                [_btn("✏️ ویرایش نام آهنگ", f"xbank:edit_title:{song['id']}"),
                 _btn("✏️ ویرایش خواننده", f"xbank:edit_artist:{song['id']}")],
                [_btn("✏️ ویرایش توضیحات", f"xbank:edit_desc:{song['id']}")],
                [_btn("🗑 حذف آهنگ", f"xbank:del:{song['id']}")],
                [_btn("🔙 بازگشت به لیست", "xbank:panel")]]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def _show_panel(callback, config):
        conn = _get_conn()
        if conn is None:
            await callback.answer("بانک در دسترس نیست.", show_alert=True)
            return
        songs = list_songs(conn)
        await callback.message.edit_text(_panel_text(songs),
                                         reply_markup=_panel_kb(songs))

    @router.callback_query(F.data == "xbank:panel")
    async def xbank_panel(callback: CallbackQuery, config):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        await callback.answer()
        await _show_panel(callback, config)

    @router.callback_query(F.data.startswith("xbank:view:"))
    async def xbank_view(callback: CallbackQuery, config):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        await callback.answer()
        conn = _get_conn()
        if conn is None:
            await callback.answer("بانک در دسترس نیست.", show_alert=True)
            return
        try:
            sid = int(callback.data.split(":")[2])
        except Exception:
            return
        song = get_song(conn, sid)
        if song is None:
            await callback.message.edit_text("این آهنگ دیگر در بانک نیست.")
            return
        await callback.message.edit_text(_song_detail_text(song),
                                         reply_markup=_view_kb(song))

    @router.callback_query(F.data.startswith("xbank:toggle:"))
    async def xbank_toggle(callback: CallbackQuery, config):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        conn = _get_conn()
        if conn is None:
            await callback.answer("بانک در دسترس نیست.", show_alert=True)
            return
        try:
            sid = int(callback.data.split(":")[2])
        except Exception:
            return
        song = get_song(conn, sid)
        if song is None:
            return
        set_active(conn, sid, not bool(song.get("active")))
        await callback.answer("تغییر کرد.")
        song = get_song(conn, sid)
        await callback.message.edit_text(_song_detail_text(song),
                                         reply_markup=_view_kb(song))

    @router.callback_query(F.data.startswith("xbank:del:"))
    async def xbank_del_ask(callback: CallbackQuery, config):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        try:
            sid = int(callback.data.split(":")[2])
        except Exception:
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_btn("🗑 بله، حذف شود", f"xbank:delok:{sid}")],
            [_btn("🔙 انصراف", f"xbank:view:{sid}")]])
        await callback.answer()
        await callback.message.edit_text(
            "این آهنگ از بانک اختصاصی حذف شود؟\nفایل اصلی در تلگرام می‌ماند؛ فقط رکورد بانک پاک می‌شود.",
            reply_markup=kb)

    @router.callback_query(F.data.startswith("xbank:delok:"))
    async def xbank_del_ok(callback: CallbackQuery, config):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        conn = _get_conn()
        if conn is None:
            await callback.answer("بانک در دسترس نیست.", show_alert=True)
            return
        try:
            sid = int(callback.data.split(":")[2])
        except Exception:
            return
        delete_song(conn, sid)
        await callback.answer("حذف شد.")
        await _show_panel(callback, config)

    # ---- edit (one-message FSM steps) ----
    @router.callback_query(F.data.startswith("xbank:edit_title:"))
    async def xbank_edit_title(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        sid = callback.data.split(":")[2]
        await state.set_state("xbank:edit:title")
        await state.update_data(song_id=int(sid))
        await callback.answer()
        await callback.message.answer("نام تازه آهنگ را بفرستید.")

    @router.callback_query(F.data.startswith("xbank:edit_artist:"))
    async def xbank_edit_artist(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        sid = callback.data.split(":")[2]
        await state.set_state("xbank:edit:artist")
        await state.update_data(song_id=int(sid))
        await callback.answer()
        await callback.message.answer("نام تازه خواننده را بفرستید.")

    @router.callback_query(F.data.startswith("xbank:edit_desc:"))
    async def xbank_edit_desc(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        sid = callback.data.split(":")[2]
        await state.set_state("xbank:edit:desc")
        await state.update_data(song_id=int(sid))
        await callback.answer()
        await callback.message.answer("توضیح تازه را بفرستید (یا «—» برای خالی کردن).")

    async def _apply_edit(message, config, state, field):
        if not _is_owner_msg(message, config):
            return
        data = await state.get_data()
        sid = data.get("song_id")
        text = (message.text or "").strip()
        if text in ("/cancel", "لغو"):
            await state.clear()
            await message.answer("لغو شد.")
            return
        conn = _get_conn()
        if conn is None or not sid:
            await state.clear()
            await message.answer("بانک در دسترس نیست.")
            return
        value = "" if (field == "description" and text == "—") else text
        update_song(conn, sid, **{field: value})
        await state.clear()
        song = get_song(conn, sid)
        await message.answer("ذخیره شد." + ("\n\n" + _song_detail_text(song) if song else ""))

    @router.message(F.text, StateFilter("xbank:edit:title"))
    async def xbank_edit_title_msg(message: Message, config, state: FSMContext):
        await _apply_edit(message, config, state, "title")

    @router.message(F.text, StateFilter("xbank:edit:artist"))
    async def xbank_edit_artist_msg(message: Message, config, state: FSMContext):
        await _apply_edit(message, config, state, "artist")

    @router.message(F.text, StateFilter("xbank:edit:desc"))
    async def xbank_edit_desc_msg(message: Message, config, state: FSMContext):
        await _apply_edit(message, config, state, "description")

    # ---- add flow ----
    @router.callback_query(F.data == "xbank:add")
    async def xbank_add_start(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        if _get_conn() is None:
            await callback.answer("بانک در دسترس نیست.", show_alert=True)
            return
        await state.set_state("xbank:add:file")
        await callback.answer()
        await callback.message.answer(
            "فایل MP3 کامل آهنگ را بفرستید.\n"
            "بیت‌ریت واقعی بررسی می‌شود؛ فایلی که واقعاً 320kbps نباشد، "
            "به‌عنوان 320 ثبت نمی‌شود.\n"
            "برای لغو، «لغو» را بفرستید.")

    @router.message(F.audio | F.document, StateFilter("xbank:add:file"))
    async def xbank_add_file(message: Message, config, state: FSMContext):
        if not _is_owner_msg(message, config):
            return
        media = message.audio or message.document
        if media is None:
            return
        mime = (getattr(media, "mime_type", "") or "").lower()
        fname = (getattr(media, "file_name", "") or "").lower()
        if mime and not (mime.startswith("audio/") or mime == "application/octet-stream"):
            await message.answer("این فایل صوتی نیست. یک فایل MP3 بفرستید.")
            return
        if fname and not fname.endswith((".mp3", ".m4a", ".flac", ".wav", ".ogg")):
            await message.answer("پسوند فایل صوتی نیست. یک فایل MP3 بفرستید.")
            return
        await message.answer("⏳ در حال بررسی فایل…")
        import asyncio as _asyncio
        import io as _io

        bot_token = getattr(config, "bot_token", "") or ""
        size = getattr(media, "file_size", 0) or 0
        data = None
        partial = False
        try:
            tg_file = await message.bot.get_file(media.file_id)
            file_path = tg_file.file_path
        except Exception:
            await message.answer("نتوانستم فایل را از تلگرام بگیرم. دوباره بفرستید.")
            return
        if size and size <= 20 * 1024 * 1024:
            try:
                buf = _io.BytesIO()
                await message.bot.download_file(file_path, buf)
                data = buf.getvalue()
            except Exception:
                data = None
        if not data:
            # Larger than the 20 MB Bot API download cap: fingerprint the
            # first 2 MB head instead (partial coverage, flagged below).
            data = await _asyncio.get_running_loop().run_in_executor(
                None, download_head, bot_token, file_path)
            partial = True
        if not data:
            await message.answer("نتوانستم فایل را دانلود کنم. دوباره بفرستید.")
            return
        info = await _asyncio.get_running_loop().run_in_executor(
            None, analyse_audio_bytes, data)
        if not info.get("fp_items"):
            await message.answer(
                "اثر انگشت این فایل ساخته نشد؛ آهنگ بدون اثر انگشت قابل "
                "شناسایی نیست و ثبت نشد.")
            return
        res = {"info": info, "partial": partial,
               "file_id": media.file_id,
               "file_unique_id": media.file_unique_id,
               "file_size": size}
        info = res["info"]
        conn = _get_conn()
        if conn is not None:
            dup, _reason = find_duplicate(conn, "", "", info["fp_items"])
            if dup is not None:
                await message.answer(
                    f"این آهنگ تکراری است — «{dup['title']}» از «{dup['artist']}» "
                    "پیش‌تر در بانک ثبت شده است.")
                await state.clear()
                return
        await state.update_data(
            file_id=res["file_id"], file_unique_id=res["file_unique_id"],
            file_size=res["file_size"], duration_sec=info["duration_sec"],
            bitrate_kbps=info["bitrate_kbps"], fp_items=info["fp_items"],
            fp_duration=info["fp_duration"], fp_partial=res["partial"])
        kbps = info["bitrate_kbps"]
        bitrate_line = (f"بیت‌ریت واقعی: {kbps}kbps ✅"
                        if kbps == 320 else
                        f"بیت‌ریت واقعی: {kbps}kbps — توجه: این فایل 320 نیست و به‌عنوان 320 ثبت نمی‌شود."
                        if kbps else "بیت‌ریت واقعی: نامشخص")
        fp_line = ("اثر انگشت: کامل ✅"
                   if not res["partial"] else
                   "اثر انگشت: جزئی — فایل بزرگ‌تر از ۲۰ مگابایت است و فقط ابتدای آهنگ قابل شناسایی است.")
        sugg_title = (getattr(message.audio, "title", "") or "") if message.audio else ""
        sugg_artist = (getattr(message.audio, "performer", "") or "") if message.audio else ""
        await state.update_data(sugg_title=sugg_title, sugg_artist=sugg_artist)
        await state.set_state("xbank:add:title")
        prompt = f"{bitrate_line}\n{fp_line}\n\nنام آهنگ را بفرستید."
        if sugg_title:
            prompt += f"\nپیشنهاد: {sugg_title}"
        await message.answer(prompt)

    @router.message(F.text, StateFilter("xbank:add:title"))
    async def xbank_add_title(message: Message, config, state: FSMContext):
        if not _is_owner_msg(message, config):
            return
        text = (message.text or "").strip()
        if text in ("/cancel", "لغو"):
            await state.clear()
            await message.answer("لغو شد.")
            return
        if not text:
            await message.answer("نام آهنگ را بفرستید.")
            return
        data = await state.get_data()
        await state.update_data(title=text)
        await state.set_state("xbank:add:artist")
        prompt = "نام خواننده را بفرستید."
        if data.get("sugg_artist"):
            prompt += f"\nپیشنهاد: {data['sugg_artist']}"
        await message.answer(prompt)

    @router.message(F.text, StateFilter("xbank:add:artist"))
    async def xbank_add_artist(message: Message, config, state: FSMContext):
        if not _is_owner_msg(message, config):
            return
        text = (message.text or "").strip()
        if text in ("/cancel", "لغو"):
            await state.clear()
            await message.answer("لغو شد.")
            return
        if not text:
            await message.answer("نام خواننده را بفرستید.")
            return
        await state.update_data(artist=text)
        await state.set_state("xbank:add:cover")
        await message.answer(
            "تصویر کاور را بفرستید، یا «رد» را بنویسید (اختیاری).")

    @router.message(F.photo | F.text, StateFilter("xbank:add:cover"))
    async def xbank_add_cover(message: Message, config, state: FSMContext):
        if not _is_owner_msg(message, config):
            return
        if message.photo:
            await state.update_data(cover_file_id=message.photo[-1].file_id)
        else:
            text = (message.text or "").strip()
            if text in ("/cancel", "لغو"):
                await state.clear()
                await message.answer("لغو شد.")
                return
            await state.update_data(cover_file_id="")
        await state.set_state("xbank:add:desc")
        await message.answer("توضیحات را بفرستید، یا «رد» را بنویسید (اختیاری).")

    @router.message(F.text, StateFilter("xbank:add:desc"))
    async def xbank_add_desc(message: Message, config, state: FSMContext):
        if not _is_owner_msg(message, config):
            return
        text = (message.text or "").strip()
        if text in ("/cancel", "لغو"):
            await state.clear()
            await message.answer("لغو شد.")
            return
        await state.update_data(description="" if text == "رد" else text)
        data = await state.get_data()
        kbps = data.get("bitrate_kbps") or 0
        fp_line = ("اثر انگشت: کامل ✅"
                   if not data.get("fp_partial") else
                   "اثر انگشت: جزئی (فقط ابتدای آهنگ)")
        confirm = "\n".join([
            "ثبت این آهنگ در بانک اختصاصی؟",
            "",
            f"🎵 {data.get('title')}",
            f"🎤 {data.get('artist')}",
            f"🎧 کیفیت واقعی: MP3 {kbps}kbps" if kbps else "🎧 کیفیت واقعی: نامشخص",
            f"⏱ مدت: {_fmt_duration(data.get('duration_sec') or 0)}",
            fp_line,
            f"📝 {data.get('description')}" if data.get("description") else "",
        ])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_btn("✅ ثبت در بانک", "xbank:add:confirm")],
            [_btn("❌ لغو", "xbank:add:cancel")]])
        await state.set_state("xbank:add:confirm")
        await message.answer(confirm, reply_markup=kb)

    @router.callback_query(F.data == "xbank:add:confirm")
    async def xbank_add_confirm(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        data = await state.get_data()
        conn = _get_conn()
        if conn is None:
            await callback.answer("بانک در دسترس نیست.", show_alert=True)
            await state.clear()
            return
        dup, reason = find_duplicate(conn, data.get("title", ""),
                                     data.get("artist", ""),
                                     data.get("fp_items", ""))
        if dup is not None:
            await state.clear()
            await callback.answer("تکراری است.", show_alert=True)
            await callback.message.answer(
                f"این آهنگ تکراری است — «{dup['title']}» از «{dup['artist']}» "
                "پیش‌تر در بانک ثبت شده است.")
            return
        sid = add_song(
            conn, title=data.get("title", ""), artist=data.get("artist", ""),
            file_id=data.get("file_id", ""), file_unique_id=data.get("file_unique_id", ""),
            duration_sec=data.get("duration_sec") or 0,
            bitrate_kbps=data.get("bitrate_kbps") or 0,
            file_size=data.get("file_size") or 0,
            fingerprint=data.get("fp_items", ""),
            fp_duration=data.get("fp_duration") or 0,
            fp_partial=bool(data.get("fp_partial")),
            cover_file_id=data.get("cover_file_id", ""),
            description=data.get("description", ""),
            added_by=callback.from_user.id)
        await state.clear()
        await callback.answer("ثبت شد.")
        song = get_song(conn, sid) if sid else None
        text = "✅ آهنگ در بانک اختصاصی ثبت شد."
        if song is not None:
            text += "\n\n" + _song_detail_text(song)
        await callback.message.edit_text(text,
                                         reply_markup=_panel_kb(list_songs(conn)))

    @router.callback_query(F.data == "xbank:add:cancel")
    async def xbank_add_cancel(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        await state.clear()
        await callback.answer("لغو شد.")
        await _show_panel(callback, config)
