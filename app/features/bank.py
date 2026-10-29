"""Superior-AI bank: two-stage song identification.

The normal search runs first (elsewhere); when it cannot identify a song
with confidence, a ``bank:search:{token}`` button is shown for the same
request. Clicking it searches the *same audio sample* against this
bank's files by real audio content (spectral-landmark fingerprints from
``app.search.landmark``), never by filename or caption.

Bank sources are restricted to exactly two kinds: public Telegram
channels registered by the manager, and registered superior-AI song
sites (only suno.com is technically active). Manually added direct
files were removed and never re-enter the bank.

Audience rules:
- Channel/source names NEVER appear in public messages.
- Every failure sends ONE short Persian line; an extraction/search
  error is never reported as "not found".
- Finished outcomes are stored on the sample row and served from the
  store on repeat clicks; the bank is never searched twice for the
  same sample.

``app.search.landmark`` is imported lazily inside functions so a
missing/broken landmark module degrades to an honest decode error,
never a fabricated fingerprint.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
import urllib.parse

import httpx
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import Config

# --------------------------------------------------------------------------
# Constants / strings (exact Persian wording from the web-ui spec)
# --------------------------------------------------------------------------
BANK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
SAMPLE_DATA_CAP = 3 * 1024 * 1024       # user upload bytes fingerprinted directly
DOWNLOAD_CAP = 15 * 1024 * 1024        # any single download
DOWNLOAD_TIMEOUT = 30
INDEX_TIMEOUT = 20
CHANNEL_CAP = 5                        # max registered channels
SOURCE_KIND_CAP = 10                   # max sources per kind (generic add)
CHANNEL_FILES_PER_RUN = 10             # max new files per channel per index run
SEARCHING_STALE_SEC = 3 * 60
MIN_LANDMARKS = 20

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,}$")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$")

# Public bank strings (exact copies from web-ui-spec.md section 6)
MSG_FOUND = "آهنگ با تطبیق محتوای صوتی در بانک هوش برتر پیدا شد."
MSG_SEARCHING = "در حال جست‌وجو در بانک هوش برتر…"
MSG_NO_SAMPLE = (
    "نمونه صوتی این درخواست پیدا نشد؛ لینک یا فایل را دوباره ارسال کنید "
    "تا نمونه تازه ساخته شود."
)
MSG_IN_PROGRESS = "جست‌وجو در بانک در حال انجام است؛ چند لحظه دیگر نتیجه را ببینید."
MSG_EMPTY = "بانک در حال حاضر فایلی برای جست‌وجو ندارد"
MSG_EXTRACT_ERR = (
    "استخراج صدای نمونه این درخواست ناموفق بود؛ این «پیدا نشد» نیست. "
    "فایل صوتی یا ویدیو را مستقیم ارسال کنید تا از روی صدای خودش جست‌وجو شود."
)
MSG_CLIENT_ERR = "جست‌وجو در بانک انجام نشد؛ دوباره تلاش کنید."
LABEL_BANK = "بانک هوش برتر"
LABEL_SUNO = "فایل ایندکس‌شده از Suno — سایت هوش برتر"
BTN_DOWNLOAD = "دانلود آهنگ"
BTN_SOURCE_POST = "پست منبع"

# Admin strings (exact copies from web-ui-spec.md section 16 / web-logic-spec.md section 5)
SUGGESTED_BANK_CHANNELS = [
    ("AiMusical", "آهنگ‌های هوش مصنوعی"),
    ("ghadimy_ai", "ریمیکس و آهنگ هوش مصنوعی"),
    ("radiojavan", "آهنگ و میکس تلگرامی"),
    ("esssi_ai", "آهنگ‌های ساخته هوش مصنوعی"),
    ("PersianPopAi", "ریمیکس‌های هوش مصنوعی"),
    ("icyremix", "ریمیکس‌های تلگرامی"),
    ("djsoroushmiix", "ریمیکس‌های تلگرامی"),
]
STATUS_INDEXABLE = "قابل ایندکس"
STATUS_BLOCKED = "از وب قابل ایندکس نیست"
STATUS_UNCHECKED = "هنوز بررسی نشده"

MSG_BAD_USERNAME = "یوزرنیم چنل معتبر نیست."
MSG_BAD_DOMAIN = "دامنه سایت معتبر نیست."
MSG_DUP_CHANNEL = "این چنل قبلاً در چنل‌های بانک ثبت شده است."
MSG_DUP_SOURCE = "این منبع قبلاً در بانک ثبت شده است."
MSG_DUP_IDENTIFIER = "منبع دیگری با همین شناسه ثبت شده است."
MSG_CHANNEL_CAP = (
    "سقف ۵ چنل بانک پر است؛ اول یک چنل را از "
    "«تنظیمات ← چنل‌های تلگرام» حذف کنید."
)
MSG_KIND_CAP = "سقف منابع این نوع پر است؛ اول یک منبع را حذف کنید."
MSG_CHANNEL_ADDED = (
    "چنل به چنل‌های بانک اضافه شد؛ با «ایندکس از چنل‌ها» "
    "از مسیر وب تلگرام ایندکس می‌شود."
)
MSG_SOURCE_ADDED = "منبع به بانک اضافه شد."
MSG_SOURCE_DELETED = "منبع از بانک حذف شد."
MSG_SOURCE_EDITED = "منبع ویرایش شد."
MSG_CONFIRM_DELETE = "تأیید حذف"
MSG_NO_CHANNEL_SOURCE = (
    "منبع چنل فعالی برای ایندکس وجود ندارد؛ یک چنل را در "
    "«مدیریت منابع بانک» اضافه یا فعال کنید."
)
MSG_INDEX_ADDED = "{n} فایل صوتی تازه از چنل‌ها ایندکس شد."
MSG_INDEX_NONE_NEW = "فایل صوتی تازه‌ای در چنل‌ها پیدا نشد (فایل‌های قبلی قبلاً ایندکس شده‌اند)."
MSG_INDEX_NOTHING = (
    "در چنل‌های ثبت‌شده پستی با فایل صوتی قابل ایندکس دیده نشد؛ "
    "بانک هنوز خالی است."
)
MSG_INDEX_BLOCKED = (
    "هیچ فایلی ایندکس نشد: چنل {chs} برای وب ناشناس تلگرام قابل مشاهده نیست؛ "
    "تلگرام پیش‌نمایش و فایل‌های این چنل را از دسترس عمومی بسته است "
    "(محتوای محافظت‌شده)، پس فایل‌هایش از این مسیر به بانک نمی‌رسند."
)
MSG_DL_MISSING = "فایل این نتیجه در بانک پیدا نشد."

PANEL_TITLE = "بانک هوش برتر"
PANEL_DESC = (
    "جست‌وجوی دوم ربات («جست‌وجو در بانک هوش برتر») نمونه صوتی کاربر را با "
    "اثر انگشت صوتی فایل‌های این بانک مقایسه می‌کند — بر اساس محتوای واقعی "
    "صدا، نه نام فایل یا کپشن. منبع بانک فقط دو چیز است: فایل‌های صوتی "
    "چنل‌های تلگرام عمومیِ ثبت‌شده در تنظیمات و آهنگ‌های عمومی سایت‌های "
    "هوش برتر ثبت‌شده (مثل Suno)؛ فایل دستی با آدرس مستقیم به بانک اضافه "
    "نمی‌شود."
)
PANEL_EMPTY = (
    "بانک خالی است؛ هیچ چنل تلگرام یا سایت هوش برتری ثبت نشده و جست‌وجوی "
    "دوم تا ثبت چنل یا سایت نتیجه‌ای ندارد."
)
PANEL_GUIDE = (
    "چنل‌های عمومی و سایت‌ها از «پنل مدیریت ← تنظیمات» ثبت می‌شوند: "
    "یوزرنیم چنل‌ها در بخش «چنل‌های تلگرام» و دامنه سایت (مثل suno.com) "
    "در بخش «سایت‌های آهنگ هوش برتر»."
)


# --------------------------------------------------------------------------
# Lazy landmark access
# --------------------------------------------------------------------------
def _landmark():
    """Return the landmark module, or None when it cannot be imported."""
    try:
        from ..search import landmark
        return landmark
    except ImportError:
        return None


def _fingerprint(data: bytes) -> dict | None:
    lm = _landmark()
    if lm is None:
        return None
    try:
        return lm.fingerprint_bytes(data or b"")
    except Exception:
        return None


def _pack(landmarks: list[int]) -> str:
    lm = _landmark()
    if lm is None:
        raise RuntimeError("landmark module unavailable")
    return lm.pack_landmarks(landmarks or [])


def _unpack(chroma: str) -> list[int]:
    lm = _landmark()
    if lm is None:
        return []
    try:
        return lm.unpack_landmarks(chroma or "")
    except Exception:
        return []


def _similarity(sample_flat: list[int], bank_chroma: str) -> dict | None:
    lm = _landmark()
    if lm is None:
        return None
    bank_flat = _unpack(bank_chroma)
    if len(bank_flat) < 40:
        return None
    try:
        return lm.landmark_similarity(
            {"landmarks": sample_flat, "landmark_count": len(sample_flat) // 2},
            {"landmarks": bank_flat, "landmark_count": len(bank_flat) // 2},
        )
    except Exception:
        return None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
class _TooLarge(Exception):
    pass


def _now() -> int:
    return int(time.time())


def _download_bytes(url: str, max_bytes: int = DOWNLOAD_CAP,
                    timeout: int = DOWNLOAD_TIMEOUT) -> bytes:
    with httpx.stream("GET", url, headers={"User-Agent": BANK_UA},
                      timeout=timeout, follow_redirects=True) as r:
        r.raise_for_status()
        buf = bytearray()
        for chunk in r.iter_bytes(65536):
            buf += chunk
            if len(buf) > max_bytes:
                raise _TooLarge()
    return bytes(buf)


def _norm_channel(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = re.sub(r"^(www\.)?t\.me/", "", s, flags=re.I)
    s = s.strip().lstrip("@").rstrip("/")
    return s


def _norm_domain(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = s.split("/")[0].strip()
    return s


def _is_owner(uid: int | None, config: Config) -> bool:
    return config.owner_id is not None and uid == config.owner_id


def _fresh_conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _run_in_conn(db_path: str, func, *args):
    """Run a blocking sync bank function with its own DB connection.

    A separate connection is used (never the shared event-loop one),
    so this is safe inside asyncio.to_thread.
    """
    c = _fresh_conn(db_path)
    try:
        return func(*args, c)
    finally:
        c.close()


# --------------------------------------------------------------------------
# DB layer (functions taking conn)
# --------------------------------------------------------------------------
def list_bank_sources(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM bank_sources ORDER BY sort_order, id").fetchall()
    return [dict(r) for r in rows]


def _source_count(conn, kind: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM bank_sources WHERE kind=?", (kind,)).fetchone()
    return int(row[0]) if row else 0


def add_bank_source(conn, kind: str, identifier: str,
                    label: str = "") -> dict:
    """Unified add; returns {"ok": bool, "message": str, "id": int|None}."""
    kind = "site" if kind == "site" else "channel"
    if kind == "channel":
        ident = _norm_channel(identifier)
        if not USERNAME_RE.match(ident):
            return {"ok": False, "message": MSG_BAD_USERNAME, "id": None}
        dup_msg = MSG_DUP_CHANNEL
        cap_msg = MSG_CHANNEL_CAP if _source_count(conn, "channel") >= CHANNEL_CAP else None
        ok_msg = MSG_CHANNEL_ADDED
    else:
        ident = _norm_domain(identifier)
        if not DOMAIN_RE.match(ident) or ".." in ident:
            return {"ok": False, "message": MSG_BAD_DOMAIN, "id": None}
        dup_msg = MSG_DUP_SOURCE
        cap_msg = None
        ok_msg = MSG_SOURCE_ADDED
    exists = conn.execute(
        "SELECT id FROM bank_sources WHERE kind=? AND lower(identifier)=lower(?)",
        (kind, ident)).fetchone()
    if exists:
        return {"ok": False, "message": dup_msg, "id": None}
    if cap_msg:
        return {"ok": False, "message": cap_msg, "id": None}
    if _source_count(conn, kind) >= SOURCE_KIND_CAP:
        return {"ok": False, "message": MSG_KIND_CAP, "id": None}
    row = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM bank_sources WHERE kind=?",
                       (kind,)).fetchone()
    sort_order = (int(row[0]) if row else -1) + 1
    cur = conn.execute(
        "INSERT INTO bank_sources (kind, identifier, label, enabled, sort_order)"
        " VALUES (?,?,?,?,?)",
        (kind, ident, label or "", 1, sort_order))
    conn.commit()
    return {"ok": True, "message": ok_msg, "id": cur.lastrowid}


def add_bank_channel(conn, channel: str) -> dict:
    """Legacy-shaped entry point: adds a Telegram channel source."""
    return add_bank_source(conn, "channel", channel)


def update_bank_source(conn, row_id: int, identifier: str,
                       label: str | None = None) -> dict:
    row = conn.execute("SELECT * FROM bank_sources WHERE id=?",
                       (row_id,)).fetchone()
    if not row:
        return {"ok": False, "message": MSG_CLIENT_ERR}
    kind = row["kind"]
    if kind == "channel":
        ident = _norm_channel(identifier)
        if not USERNAME_RE.match(ident):
            return {"ok": False, "message": MSG_BAD_USERNAME}
    else:
        ident = _norm_domain(identifier)
        if not DOMAIN_RE.match(ident) or ".." in ident:
            return {"ok": False, "message": MSG_BAD_DOMAIN}
    dup = conn.execute(
        "SELECT id FROM bank_sources WHERE kind=? AND lower(identifier)=lower(?) AND id!=?",
        (kind, ident, row_id)).fetchone()
    if dup:
        return {"ok": False, "message": MSG_DUP_IDENTIFIER}
    if label is None:
        label = row["label"]
    conn.execute("UPDATE bank_sources SET identifier=?, label=? WHERE id=?",
                 (ident, label, row_id))
    conn.commit()
    return {"ok": True, "message": MSG_SOURCE_EDITED}


def delete_bank_source(conn, row_id: int) -> dict:
    conn.execute("DELETE FROM bank_sources WHERE id=?", (row_id,))
    conn.commit()
    # Indexed files stay listed (individually deletable) but are no
    # longer searched once their source is gone/disabled.
    return {"ok": True, "message": MSG_SOURCE_DELETED}


def toggle_bank_source(conn, row_id: int) -> int | None:
    row = conn.execute("SELECT enabled FROM bank_sources WHERE id=?",
                       (row_id,)).fetchone()
    if not row:
        return None
    new = 0 if row["enabled"] else 1
    conn.execute("UPDATE bank_sources SET enabled=? WHERE id=?", (new, row_id))
    conn.commit()
    return new


def move_bank_source(conn, row_id: int, direction: str) -> bool:
    """Swap sort_order with the neighbour of the same kind. Returns True on move."""
    row = conn.execute("SELECT * FROM bank_sources WHERE id=?",
                       (row_id,)).fetchone()
    if not row:
        return False
    if direction == "up":
        cmp_op, order = "<", "DESC"
    else:
        cmp_op, order = ">", "ASC"
    other = conn.execute(
        f"SELECT * FROM bank_sources WHERE kind=? AND sort_order {cmp_op} ?"
        f" ORDER BY sort_order {order}, id {order} LIMIT 1",
        (row["kind"], row["sort_order"])).fetchone()
    if not other:
        # Fall back to neighbour by id when sort_orders collide.
        other = conn.execute(
            f"SELECT * FROM bank_sources WHERE kind=? AND id {cmp_op} ?"
            f" ORDER BY id {order} LIMIT 1",
            (row["kind"], row["id"])).fetchone()
        if not other:
            return False
    conn.execute("UPDATE bank_sources SET sort_order=? WHERE id=?",
                 (other["sort_order"], row["id"]))
    conn.execute("UPDATE bank_sources SET sort_order=? WHERE id=?",
                 (row["sort_order"], other["id"]))
    conn.commit()
    return True


def list_bank_items(conn, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM audio_bank ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def delete_bank_item(conn, item_id: int) -> bool:
    cur = conn.execute("DELETE FROM audio_bank WHERE id=?", (item_id,))
    conn.commit()
    return cur.rowcount > 0


def bank_item_count(conn) -> int:
    row = conn.execute("SELECT COUNT(*) FROM audio_bank").fetchone()
    return int(row[0]) if row else 0


def get_bank_sample(conn, token: str) -> dict | None:
    row = conn.execute("SELECT * FROM bank_samples WHERE token=?",
                       (token,)).fetchone()
    return dict(row) if row else None


def set_sample_state(conn, token: str, state: str,
                     outcome: dict | None = None) -> None:
    conn.execute(
        "UPDATE bank_samples SET state=?, result_json=?, updated_at=? WHERE token=?",
        (state, json.dumps(outcome or {}, ensure_ascii=False), _now(), token))
    conn.commit()


def get_channel_status(conn, channel: str) -> dict | None:
    row = conn.execute("SELECT * FROM bank_channel_status WHERE channel=?",
                       (channel,)).fetchone()
    return dict(row) if row else None


def set_channel_status(conn, channel: str, status: str,
                       seen_posts: int = 0, audio_files: int = 0,
                       indexed_count: int = 0,
                       has_fingerprint: int = 0) -> None:
    conn.execute(
        """INSERT INTO bank_channel_status
           (channel, status, seen_posts, audio_files, indexed_count, has_fingerprint)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(channel) DO UPDATE SET
             status=excluded.status, seen_posts=excluded.seen_posts,
             audio_files=excluded.audio_files,
             indexed_count=excluded.indexed_count,
             has_fingerprint=excluded.has_fingerprint""",
        (channel, status, seen_posts, audio_files, indexed_count, has_fingerprint))
    conn.commit()


# --------------------------------------------------------------------------
# Sample arming (called by the search flows when normal search missed)
# --------------------------------------------------------------------------
def arm_sample(kind: str, seed: str, data: bytes | None, url: str,
               clue: str, conn) -> str | None:
    """Fingerprint an audio sample and store/re-arm its bank_samples row.

    Returns the deterministic token (sha256(seed)[:40]) on success, or
    None when no usable fingerprint exists — the caller then shows the
    honest extraction-error line and never a fake bank button.
    """
    payload: bytes | None = None
    if data and len(data) <= SAMPLE_DATA_CAP:
        payload = data
    elif url:
        try:
            payload = _download_bytes(url)
        except Exception:
            return None
    if not payload:
        return None
    fp = _fingerprint(payload)
    if (fp is None or fp.get("status") != "ok"
            or (fp.get("landmark_count") or 0) < MIN_LANDMARKS):
        return None
    try:
        chroma = _pack(fp.get("landmarks") or [])
    except Exception:
        return None
    token = hashlib.sha256((seed or "").encode("utf-8")).hexdigest()[:40]
    now = _now()
    conn.execute(
        """INSERT INTO bank_samples
           (token, kind, chroma, clue, state, result_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(token) DO UPDATE SET
             kind=excluded.kind, chroma=excluded.chroma, clue=excluded.clue,
             state='ready', result_json='', updated_at=excluded.updated_at""",
        (token, kind or "", chroma, (clue or "")[:300], "ready", "",
         now, now))
    conn.commit()
    return token


# --------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------
_POST_SPLIT_RE = re.compile(r'data-post="([^"]+)"')
_AUDIO_URL_RE = re.compile(
    r'href="(https?://[^"\s]+\.(?:mp3|m4a|ogg|wav|flac)[^"\s]*)"', re.I)
_TEXT_RE = re.compile(r'tgme_widget_message_text[^>]*>(.*?)</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _iter_post_bodies(page_html: str):
    """Yield (post_id, body) pairs, reusing channel.py's data-post split pattern."""
    blocks = _POST_SPLIT_RE.split(page_html or "")
    for i in range(1, len(blocks) - 1, 2):
        yield blocks[i], blocks[i + 1]


def _post_audio_urls(body: str) -> list[str]:
    urls = []
    for u in _AUDIO_URL_RE.findall(body or ""):
        u = u.replace("&amp;", "&")
        if u not in urls:
            urls.append(u)
    return urls


def _post_text(body: str) -> str:
    m = _TEXT_RE.search(body or "")
    raw = m.group(1) if m else ""
    text = _TAG_RE.sub(" ", raw)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_preview(channel: str, timeout: int = INDEX_TIMEOUT) -> str:
    resp = httpx.get(f"https://t.me/s/{channel}",
                     headers={"User-Agent": BANK_UA},
                     timeout=timeout, follow_redirects=True)
    if resp.status_code != 200:
        return ""
    return resp.text or ""


def _channel_indexed_count(conn, channel: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM audio_bank WHERE source='channel' AND lower(channel)=lower(?)",
        (channel,)).fetchone()
    return int(row[0]) if row else 0


def index_channels(conn) -> dict:
    """Grow the bank from enabled channel sources.

    Returns {"added": int, "total": int, "blocked": [channels]}.
    Blocked channels are recorded in bank_channel_status for the admin
    panel only — never leaked into public messages.
    """
    sources = [s for s in list_bank_sources(conn)
               if s["kind"] == "channel" and s["enabled"]]
    added = 0
    blocked: list[str] = []
    for src in sources:
        channel = src["identifier"]
        try:
            page = _fetch_preview(channel)
        except Exception:
            page = ""
        if not page or 'data-post="' not in page:
            blocked.append(channel)
            set_channel_status(conn, channel, "blocked")
            continue
        seen_posts = 0
        audio_files = 0
        new_this_channel = 0
        for post_id, body in _iter_post_bodies(page):
            seen_posts += 1
            post_url = f"https://t.me/{channel}/{post_id}"
            exists = conn.execute(
                "SELECT id FROM audio_bank WHERE post_url=?",
                (post_url,)).fetchone()
            if exists:
                continue
            urls = _post_audio_urls(body)
            audio_files += len(urls)
            if not urls or new_this_channel >= CHANNEL_FILES_PER_RUN:
                continue
            audio_url = urls[0]
            try:
                data = _download_bytes(audio_url)
            except Exception:
                continue
            fp = _fingerprint(data)
            if (fp is None or fp.get("status") != "ok"
                    or (fp.get("landmark_count") or 0) < MIN_LANDMARKS):
                continue
            try:
                chroma = _pack(fp.get("landmarks") or [])
            except Exception:
                continue
            title = _post_text(body).split("\n")[0][:120]
            conn.execute(
                """INSERT INTO audio_bank
                   (source, channel, title, artist, chroma, file_url, post_url, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ("channel", channel, title, "", chroma, audio_url,
                 post_url, _now()))
            conn.commit()
            added += 1
            new_this_channel += 1
        indexed = _channel_indexed_count(conn, channel)
        set_channel_status(conn, channel, "indexable", seen_posts,
                           audio_files, indexed, 1 if indexed else 0)
    total = bank_item_count(conn)
    return {"added": added, "total": total, "blocked": blocked}


def index_suno(conn, clue: str) -> dict:
    """Best-effort Suno indexing for this sample's clue. Silent skips."""
    row = conn.execute(
        "SELECT * FROM bank_sources WHERE kind='site' AND lower(identifier)='suno.com'"
        " AND enabled=1").fetchone()
    if not row:
        return {"added": 0}
    clue = (clue or "").strip()
    if len(clue) < 2:
        return {"added": 0}
    added = 0
    try:
        q = urllib.parse.quote(f"site:suno.com/song {clue}")
        resp = httpx.get(f"https://html.duckduckgo.com/html/?q={q}",
                         headers={"User-Agent": BANK_UA},
                         timeout=INDEX_TIMEOUT, follow_redirects=True)
        uuids = []
        for u in re.findall(r"suno\.com/song/([0-9a-fA-F-]{36})",
                            resp.text or ""):
            if u not in uuids:
                uuids.append(u)
        for uuid in uuids[:5]:
            try:
                clip = httpx.get(
                    f"https://studio-api.prod.suno.com/api/clip/{uuid}",
                    headers={"User-Agent": BANK_UA},
                    timeout=INDEX_TIMEOUT, follow_redirects=True).json()
                audio_url = (clip or {}).get("audio_url") or ""
                title = str((clip or {}).get("title") or "")[:160]
                if not audio_url:
                    continue
                exists = conn.execute(
                    "SELECT id FROM audio_bank WHERE file_url=? OR post_url=?",
                    (audio_url,
                     f"https://suno.com/song/{uuid}")).fetchone()
                if exists:
                    continue
                data = _download_bytes(audio_url)
                fp = _fingerprint(data)
                if (fp is None or fp.get("status") != "ok"
                        or (fp.get("landmark_count") or 0) < MIN_LANDMARKS):
                    continue
                chroma = _pack(fp.get("landmarks") or [])
                conn.execute(
                    """INSERT INTO audio_bank
                       (source, channel, title, artist, chroma, file_url, post_url, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    ("suno", "", title, "", chroma, audio_url,
                     f"https://suno.com/song/{uuid}", _now()))
                conn.commit()
                added += 1
            except Exception:
                continue
    except Exception:
        pass
    return {"added": added}


# --------------------------------------------------------------------------
# Bank search
# --------------------------------------------------------------------------
def _usable_rows(conn) -> list[dict]:
    sources = list_bank_sources(conn)
    enabled_channels = {s["identifier"].lower() for s in sources
                        if s["kind"] == "channel" and s["enabled"]}
    suno_on = any(s["kind"] == "site" and s["identifier"].lower() == "suno.com"
                  and s["enabled"] for s in sources)
    rows = conn.execute("SELECT * FROM audio_bank").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("source") == "channel" and (d.get("channel") or "").lower() in enabled_channels:
            out.append(d)
        elif d.get("source") == "suno" and suno_on:
            out.append(d)
        # Legacy/other sources are listed nowhere and never searched.
    return out


def _sanitize_stored(outcome: dict) -> dict:
    """Any stored error mentioning a channel is replaced by the generic line."""
    if (outcome.get("status") == "error"
            and ("@" in str(outcome.get("message", ""))
                 or "قابل مشاهده نیست" in str(outcome.get("message", "")))):
        outcome = dict(outcome)
        outcome["message"] = MSG_EMPTY
    return outcome


def search_bank(token: str, conn) -> dict:
    """Run (or serve) the second-stage bank search.

    Returns {"status": "found"|"not_found"|"error", "message": str,
             "hits": [...], "bank_count": int}.
    Public messages never carry channel names.
    """
    token = (token or "")[:64]
    sample = get_bank_sample(conn, token)
    if not sample:
        return {"status": "error", "message": MSG_NO_SAMPLE,
                "hits": [], "bank_count": 0}
    state = sample.get("state") or ""
    if state in ("found", "not_found", "error") and sample.get("result_json"):
        try:
            return _sanitize_stored(json.loads(sample["result_json"]))
        except (ValueError, TypeError):
            pass  # corrupted store -> run fresh below
    if state == "searching":
        updated = sample.get("updated_at") or 0
        if _now() - updated < SEARCHING_STALE_SEC:
            return {"status": "error", "message": MSG_IN_PROGRESS,
                    "hits": [], "bank_count": 0}
        # stale -> run fresh
    sample_flat = _unpack(sample.get("chroma") or "")
    if len(sample_flat) < 40:
        outcome = {"status": "error", "message": MSG_EXTRACT_ERR,
                   "hits": [], "bank_count": 0}
        set_sample_state(conn, token, "error", outcome)
        return outcome
    try:
        set_sample_state(conn, token, "searching")
        # Grow the index first, from the ONLY two allowed sources.
        try:
            index_channels(conn)
        except Exception:
            pass
        try:
            index_suno(conn, sample.get("clue") or "")
        except Exception:
            pass
        usable = _usable_rows(conn)
        bank_count = len(usable)
        if bank_count == 0:
            outcome = {"status": "error", "message": MSG_EMPTY,
                       "hits": [], "bank_count": 0}
            set_sample_state(conn, token, "error", outcome)
            return outcome
        best = None
        best_votes = -1
        for r in usable:
            sim = _similarity(sample_flat, r.get("chroma") or "")
            if not sim:
                continue
            if sim.get("votes", 0) > best_votes:
                best_votes = sim["votes"]
                best = (r, sim)
        if best is not None and best[1].get("match"):
            r, sim = best
            score = float(sim.get("ratio", 0.0))
            pct = int(round(score * 100))
            hit = {
                "id": r["id"],
                "title": r.get("title") or "",
                "artist": r.get("artist") or "",
                "source": r.get("source") or "",
                "channel": "",
                "post_url": r.get("post_url") or "",
                "file_url": r.get("file_url") or "",
                "score": min(1.0, max(0.0, score)),
                "pct": pct,
            }
            outcome = {"status": "found", "message": MSG_FOUND,
                       "hits": [hit], "bank_count": bank_count}
            set_sample_state(conn, token, "found", outcome)
            return outcome
        outcome = {
            "status": "not_found",
            "message": f"در بانک هوش برتر ({bank_count} فایل ایندکس‌شده) با تطبیق محتوای صوتی پیدا نشد.",
            "hits": [], "bank_count": bank_count,
        }
        set_sample_state(conn, token, "not_found", outcome)
        return outcome
    except Exception:
        outcome = {"status": "error", "message": MSG_CLIENT_ERR,
                   "hits": [], "bank_count": 0}
        try:
            set_sample_state(conn, token, "error", outcome)
        except Exception:
            pass
        return outcome


def bank_search_button(token: str) -> dict:
    """Data the main handlers need to render the second-search button."""
    return {"text": "🔎 جست‌وجو در بانک هوش برتر",
            "callback_data": f"bank:search:{token}"}


# --------------------------------------------------------------------------
# Admin panel rendering
# --------------------------------------------------------------------------
def _source_label(s: dict) -> str:
    kind_fa = "چنل" if s["kind"] == "channel" else "سایت"
    label = f'{kind_fa} {s["identifier"]}'
    if s.get("label"):
        label += f' ({s["label"]})'
    return label


def _source_indexed(conn, s: dict) -> int:
    if s["kind"] == "channel":
        return _channel_indexed_count(conn, s["identifier"])
    if s["identifier"].lower() == "suno.com":
        row = conn.execute(
            "SELECT COUNT(*) FROM audio_bank WHERE source='suno'").fetchone()
        return int(row[0]) if row else 0
    return 0


def render_bank_panel(conn, suggest_status: dict | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Admin bank panel: (text, keyboard). Channel names appear here only."""
    sources = list_bank_sources(conn)
    channels = [s for s in sources if s["kind"] == "channel"]
    sites = [s for s in sources if s["kind"] == "site"]
    total = bank_item_count(conn)

    lines = [f"🏛 {PANEL_TITLE}", "", PANEL_DESC, ""]
    if not sources:
        lines += [PANEL_EMPTY, "", PANEL_GUIDE]

    kb: list[list[InlineKeyboardButton]] = []

    if channels:
        lines += ["", "📡 چنل‌های تلگرام ثبت‌شده"]
        for s in channels:
            n = _source_indexed(conn, s)
            st = get_channel_status(conn, s["identifier"]) or {}
            status_txt = {"indexable": STATUS_INDEXABLE,
                          "blocked": STATUS_BLOCKED}.get(
                st.get("status") or "", STATUS_UNCHECKED)
            en = "فعال — در جست‌وجو شرکت می‌کند" if s["enabled"] else "غیرفعال — در جست‌وجو شرکت نمی‌کند"
            lines.append(
                f'• {_source_label(s)} — {status_txt}\n'
                f'  {n} فایل ایندکس‌شده · {en}')
            if st:
                fp = "اثر انگشت صوتی ثبت شده" if st.get("has_fingerprint") else "اثر انگشت ثبت نشده"
                lines.append(
                    f'  {st.get("seen_posts", 0)} پیام دیده‌شده · '
                    f'{st.get("audio_files", 0)} فایل صوتی دیده‌شده · '
                    f'{st.get("indexed_count", 0)} فایل ایندکس‌شده · {fp}')
            kb.append([
                InlineKeyboardButton(
                    text="غیرفعال" if s["enabled"] else "فعال",
                    callback_data=f"bank:toggle:{s['id']}"),
                InlineKeyboardButton(text="بالا", callback_data=f"bank:up:{s['id']}"),
                InlineKeyboardButton(text="پایین", callback_data=f"bank:down:{s['id']}"),
                InlineKeyboardButton(text="ویرایش", callback_data=f"bank:edit:{s['id']}"),
                InlineKeyboardButton(text="حذف", callback_data=f"bank:del:{s['id']}"),
            ])

    if sites:
        lines += ["", "🌐 سایت‌های هوش برتر"]
        for s in sites:
            n = _source_indexed(conn, s)
            supported = "پشتیبانی‌شده" if s["identifier"].lower() == "suno.com" else "پشتیبانی‌نشده"
            en = "فعال — در جست‌وجو شرکت می‌کند" if s["enabled"] else "غیرفعال — در جست‌وجو شرکت نمی‌کند"
            lines.append(
                f'• {_source_label(s)} — {supported}\n  {n} فایل ایندکس‌شده · {en}')
            kb.append([
                InlineKeyboardButton(
                    text="غیرفعال" if s["enabled"] else "فعال",
                    callback_data=f"bank:toggle:{s['id']}"),
                InlineKeyboardButton(text="بالا", callback_data=f"bank:up:{s['id']}"),
                InlineKeyboardButton(text="پایین", callback_data=f"bank:down:{s['id']}"),
                InlineKeyboardButton(text="ویرایش", callback_data=f"bank:edit:{s['id']}"),
                InlineKeyboardButton(text="حذف", callback_data=f"bank:del:{s['id']}"),
            ])

    kb.append([InlineKeyboardButton(text="➕ افزودن منبع", callback_data="bank:add")])
    kb.append([InlineKeyboardButton(text="🔄 ایندکس از چنل‌ها", callback_data="bank:index")])

    # Suggested channels (admin only)
    suggest_status = suggest_status or {}
    lines += ["", "💡 چنل‌های پیشنهادی برای ایندکس"]
    registered = {s["identifier"].lower() for s in channels}
    for ch, label in SUGGESTED_BANK_CHANNELS:
        if ch.lower() in registered:
            lines.append(f"• {label} (@{ch}) — ثبت شده")
            continue
        st = suggest_status.get(ch, STATUS_UNCHECKED)
        lines.append(f"• {label} (@{ch}) — {st}")
        kb.append([InlineKeyboardButton(
            text=f"افزودن به چنل‌های بانک: @{ch}",
            callback_data=f"bank:sugadd:{ch}")])

    # Indexed files
    lines += ["", f"🗂 فایل‌های ایندکس‌شده ({total})"]
    for item in list_bank_items(conn, 20):
        if item["source"] == "suno":
            src = "Suno — سایت هوش برتر"
        else:
            src = f'چنل @{item["channel"]}' if item.get("channel") else LABEL_BANK
        title = (item.get("title") or "(بدون عنوان)")[:60]
        lines.append(f'• {title} — {src}')
        kb.append([InlineKeyboardButton(
            text=f"🗑 حذف: {title[:30]}",
            callback_data=f"bank:delitem:{item['id']}")])

    kb.append([InlineKeyboardButton(text="🔙 بستن", callback_data="bank:close")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


def probe_suggested() -> dict:
    """Live t.me/s probe for the 7 suggested channels (admin panel only)."""
    out = {}
    for ch, _label in SUGGESTED_BANK_CHANNELS:
        try:
            page = _fetch_preview(ch, timeout=8)
            out[ch] = STATUS_INDEXABLE if (page and 'data-post="' in page) else STATUS_BLOCKED
        except Exception:
            out[ch] = STATUS_BLOCKED
    return out


def admin_index_message(result: dict) -> str:
    """Admin-worded index report; blocked channel names appear here only."""
    if not result.get("has_channels"):
        return MSG_NO_CHANNEL_SOURCE
    blocked = result.get("blocked") or []
    total = result.get("total", 0)
    added = result.get("added", 0)
    if blocked and total == 0:
        chs = "، ".join("@" + c for c in blocked)
        return MSG_INDEX_BLOCKED.format(chs=chs)
    parts = []
    if added > 0:
        parts.append(MSG_INDEX_ADDED.format(n=added))
    elif total > 0:
        parts.append(MSG_INDEX_NONE_NEW)
    else:
        parts.append(MSG_INDEX_NOTHING)
    if blocked:
        parts.append("چنل‌های قابل مشاهده نبود: " + "، ".join("@" + c for c in blocked))
    return "\n".join(parts)


# --------------------------------------------------------------------------
# FSM
# --------------------------------------------------------------------------
class BankStates(StatesGroup):
    add_identifier = State(state="bank:add_identifier")
    edit_identifier = State(state="bank:edit_identifier")


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
def _found_card_text(outcome: dict) -> str:
    hit = outcome["hits"][0]
    title = (hit.get("title") or "(بدون عنوان)").strip()
    artist = (hit.get("artist") or "").strip()
    line1 = f"{title} – {artist}" if artist else title
    src_label = LABEL_SUNO if hit.get("source") == "suno" else LABEL_BANK
    return (f"{MSG_FOUND}\n\n{line1}\n{src_label}\n"
            f"شباهت صوتی {hit.get('pct', 0)}٪")


def _found_card_kb(outcome: dict) -> InlineKeyboardMarkup:
    hit = outcome["hits"][0]
    post_url = hit.get("post_url") or hit.get("file_url") or ""
    row = [InlineKeyboardButton(text=BTN_DOWNLOAD,
                               callback_data=f"bank:dl:{hit['id']}")]
    if post_url:
        row.append(InlineKeyboardButton(text=BTN_SOURCE_POST, url=post_url))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def register(router: Router) -> None:
    fr = Router()

    # -- public: second search -------------------------------------------
    @fr.callback_query(F.data.startswith("bank:search:"))
    async def _bank_search(callback: CallbackQuery, conn: sqlite3.Connection,
                           config: Config):
        token = callback.data.split(":", 2)[2]
        await callback.answer(MSG_SEARCHING)
        try:
            outcome = await asyncio.to_thread(
                _run_in_conn, config.db_path, search_bank, token)
        except Exception:
            await callback.message.answer(MSG_CLIENT_ERR)
            return
        status = outcome.get("status")
        if status == "found":
            await callback.message.answer(_found_card_text(outcome),
                                          reply_markup=_found_card_kb(outcome))
        else:
            await callback.message.answer(outcome.get("message") or MSG_CLIENT_ERR)

    @fr.callback_query(F.data.startswith("bank:dl:"))
    async def _bank_dl(callback: CallbackQuery, conn: sqlite3.Connection,
                       config: Config):
        await callback.answer()
        try:
            item_id = int(callback.data.split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.message.answer(MSG_CLIENT_ERR)
            return
        row = conn.execute("SELECT * FROM audio_bank WHERE id=?",
                           (item_id,)).fetchone()
        if not row or not row["file_url"]:
            await callback.message.answer(MSG_DL_MISSING)
            return
        d = dict(row)
        caption = (d.get("title") or "").strip()
        if d.get("artist"):
            caption = f"{caption} – {d['artist']}".strip(" –")
        try:
            await callback.message.answer_audio(audio=d["file_url"],
                                                caption=caption or None)
        except Exception:
            await callback.message.answer(MSG_CLIENT_ERR)

    # -- admin panel ------------------------------------------------------
    async def _send_panel(message: Message, config: Config, edit: bool = False):
        statuses = await asyncio.to_thread(probe_suggested)

        def _build(db_path):
            c = _fresh_conn(db_path)
            try:
                return render_bank_panel(c, statuses)
            finally:
                c.close()

        text, kb = await asyncio.to_thread(_build, config.db_path)
        if edit:
            try:
                await message.edit_text(text, reply_markup=kb)
            except Exception:
                await message.answer(text, reply_markup=kb)
        else:
            await message.answer(text, reply_markup=kb)

    async def _owner_only(callback: CallbackQuery, config: Config) -> bool:
        if not _is_owner(getattr(callback.from_user, "id", None), config):
            await callback.answer()
            return False
        return True

    @fr.callback_query(F.data == "bank:panel")
    async def _bank_panel(callback: CallbackQuery, conn: sqlite3.Connection,
                          config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        await _send_panel(callback.message, config, edit=True)

    @fr.callback_query(F.data == "bank:close")
    async def _bank_close(callback: CallbackQuery, conn: sqlite3.Connection,
                          config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass

    @fr.callback_query(F.data == "bank:index")
    async def _bank_index(callback: CallbackQuery, conn: sqlite3.Connection,
                          config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer("در حال ایندکس…")

        def _run(db_path):
            c = _fresh_conn(db_path)
            try:
                sources = [s for s in list_bank_sources(c)
                           if s["kind"] == "channel" and s["enabled"]]
                result = index_channels(c)
                result["has_channels"] = bool(sources)
                return result
            finally:
                c.close()

        try:
            result = await asyncio.to_thread(_run, config.db_path)
            msg = admin_index_message(result)
        except Exception:
            msg = MSG_CLIENT_ERR
        try:
            await callback.message.answer(msg)
        except Exception:
            pass

    @fr.callback_query(F.data == "bank:add")
    async def _bank_add(callback: CallbackQuery, conn: sqlite3.Connection,
                        config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="چنل تلگرام",
                                 callback_data="bank:addkind:channel"),
            InlineKeyboardButton(text="سایت هوش برتر",
                                 callback_data="bank:addkind:site"),
        ]])
        await callback.message.answer("نوع منبع جدید را انتخاب کنید:", reply_markup=kb)

    @fr.callback_query(F.data.startswith("bank:addkind:"))
    async def _bank_addkind(callback: CallbackQuery, conn: sqlite3.Connection,
                            config: Config, state: FSMContext):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        kind = callback.data.split(":", 2)[2]
        if kind not in ("channel", "site"):
            return
        await state.set_state(BankStates.add_identifier)
        await state.update_data(kind=kind)
        if kind == "channel":
            await callback.message.answer(
                "یوزرنیم چنل را بفرستید (مثلاً: AiMusical)")
        else:
            await callback.message.answer(
                "دامنه سایت را بفرستید (مثلاً: suno.com)")

    @fr.message(BankStates.add_identifier, F.text)
    async def _bank_add_identifier(message: Message, conn: sqlite3.Connection,
                                   config: Config, state: FSMContext):
        if not _is_owner(getattr(message.from_user, "id", None), config):
            return
        data = await state.get_data()
        await state.clear()
        res = add_bank_source(conn, data.get("kind") or "channel",
                              message.text or "")
        await message.answer(res["message"])

    @fr.callback_query(F.data.startswith("bank:sugadd:"))
    async def _bank_sugadd(callback: CallbackQuery, conn: sqlite3.Connection,
                           config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        channel = callback.data.split(":", 2)[2]
        res = add_bank_channel(conn, channel)
        await callback.message.answer(res["message"])

    @fr.callback_query(F.data.startswith("bank:toggle:"))
    async def _bank_toggle(callback: CallbackQuery, conn: sqlite3.Connection,
                           config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            toggle_bank_source(conn, int(callback.data.split(":", 2)[2]))
        except (ValueError, IndexError):
            pass
        await _send_panel(callback.message, config, edit=True)

    @fr.callback_query(F.data.startswith("bank:up:"))
    async def _bank_up(callback: CallbackQuery, conn: sqlite3.Connection,
                       config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            move_bank_source(conn, int(callback.data.split(":", 2)[2]), "up")
        except (ValueError, IndexError):
            pass
        await _send_panel(callback.message, config, edit=True)

    @fr.callback_query(F.data.startswith("bank:down:"))
    async def _bank_down(callback: CallbackQuery, conn: sqlite3.Connection,
                         config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            move_bank_source(conn, int(callback.data.split(":", 2)[2]), "down")
        except (ValueError, IndexError):
            pass
        await _send_panel(callback.message, config, edit=True)

    @fr.callback_query(F.data.startswith("bank:del:"))
    async def _bank_del(callback: CallbackQuery, conn: sqlite3.Connection,
                        config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            row_id = int(callback.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=MSG_CONFIRM_DELETE,
                                 callback_data=f"bank:delconfirm:{row_id}"),
            InlineKeyboardButton(text="لغو", callback_data="bank:panel"),
        ]])
        await callback.message.answer("این منبع حذف شود؟", reply_markup=kb)

    @fr.callback_query(F.data.startswith("bank:delconfirm:"))
    async def _bank_delconfirm(callback: CallbackQuery, conn: sqlite3.Connection,
                               config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            res = delete_bank_source(conn, int(callback.data.split(":", 2)[2]))
            await callback.message.answer(res["message"])
        except (ValueError, IndexError):
            pass
        await _send_panel(callback.message, config, edit=True)

    @fr.callback_query(F.data.startswith("bank:edit:"))
    async def _bank_edit(callback: CallbackQuery, conn: sqlite3.Connection,
                         config: Config, state: FSMContext):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            row_id = int(callback.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return
        row = conn.execute("SELECT * FROM bank_sources WHERE id=?",
                           (row_id,)).fetchone()
        if not row:
            return
        await state.set_state(BankStates.edit_identifier)
        await state.update_data(row_id=row_id)
        kind_fa = "یوزرنیم چنل" if row["kind"] == "channel" else "دامنه سایت"
        await callback.message.answer(f"{kind_fa} جدید را بفرستید:")

    @fr.message(BankStates.edit_identifier, F.text)
    async def _bank_edit_identifier(message: Message, conn: sqlite3.Connection,
                                    config: Config, state: FSMContext):
        if not _is_owner(getattr(message.from_user, "id", None), config):
            return
        data = await state.get_data()
        await state.clear()
        res = update_bank_source(conn, int(data.get("row_id") or 0),
                                 message.text or "")
        await message.answer(res["message"])

    @fr.callback_query(F.data.startswith("bank:delitem:"))
    async def _bank_delitem(callback: CallbackQuery, conn: sqlite3.Connection,
                            config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            item_id = int(callback.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=MSG_CONFIRM_DELETE,
                                 callback_data=f"bank:delitemconfirm:{item_id}"),
            InlineKeyboardButton(text="لغو", callback_data="bank:panel"),
        ]])
        await callback.message.answer("این فایل از بانک حذف شود؟", reply_markup=kb)

    @fr.callback_query(F.data.startswith("bank:delitemconfirm:"))
    async def _bank_delitemconfirm(callback: CallbackQuery,
                                   conn: sqlite3.Connection, config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            ok = delete_bank_item(conn, int(callback.data.split(":", 2)[2]))
            await callback.message.answer(
                "فایل از بانک حذف شد." if ok else MSG_CLIENT_ERR)
        except (ValueError, IndexError):
            pass
        await _send_panel(callback.message, config, edit=True)

    router.include_router(fr)
