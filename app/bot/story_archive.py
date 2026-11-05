"""Iranian story-clip archive: curated Telegram channels + admin submissions.

Design approved 2026-11-05 (user). Implements:
  Phase 1: channel/archive management (Turso tables + admin panel)
  Phase 2: new-post intake (channel_post) + per-clip relevance + manual add
  Phase 3: user menus + 5/page delivery + dedup + seen history
  Phase 4: YouTube complementary (link discovery only)

Hard constraints honored:
- $0, Render Free: intake does NO downloads; forwards are server-side.
- Pixabay feature (story_clips.py), Instagram downloader, Snaplytics: untouched.
- Turso: EXISTING database, new tables only (free plan 5GB/500M reads —
  metadata-only tables, <10MB even at 10k clips). No new database.
- Dedup is EXACT-match only (file_unique_id + source post id).
  Similar clips are NEVER removed (user req #5).
- Every DB op is try/except isolated: this module can never crash the bot.
- Protected-content posts are never archived (respect author restriction).
"""
from __future__ import annotations

import logging
import re
import time

_log = logging.getLogger("avana.story_archive")

# ---------------------------------------------------------------------------
# Turso connection (same DB as turso_cache.py / trending.py; own tables)
# ---------------------------------------------------------------------------
_ta_conn = None
_ta_ok = False


_TABLE_DDLS = [
    """CREATE TABLE IF NOT EXISTS story_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        chat_id INTEGER,
        category TEXT NOT NULL,
        subcategory TEXT NOT NULL,
        auto_approve INTEGER DEFAULT 0,
        watermark_ok INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        added_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS story_archive (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_unique_id TEXT UNIQUE NOT NULL,
        file_id TEXT NOT NULL,
        source_chat_id INTEGER,
        source_message_id INTEGER,
        source_username TEXT DEFAULT '',
        mode TEXT DEFAULT 'forward',
        category TEXT NOT NULL,
        subcategory TEXT NOT NULL,
        orig_caption TEXT DEFAULT '',
        duration INTEGER DEFAULT 0,
        width INTEGER DEFAULT 0,
        height INTEGER DEFAULT 0,
        watermark_ok INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        added_at INTEGER NOT NULL,
        added_by INTEGER DEFAULT 0
    )""",
    """CREATE INDEX IF NOT EXISTS idx_arch_cat
       ON story_archive(category, subcategory, status)""",
    """CREATE TABLE IF NOT EXISTS story_seen (
        user_id INTEGER NOT NULL,
        file_unique_id TEXT NOT NULL,
        seen_at INTEGER NOT NULL,
        PRIMARY KEY (user_id, file_unique_id)
    )""",
    """CREATE TABLE IF NOT EXISTS story_yt_cache (
        cat_sub TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        fetched_at INTEGER NOT NULL
    )""",
]

def _get_conn():
    """Turso connection for story-archive tables. None when unavailable."""
    global _ta_conn, _ta_ok
    if _ta_conn is not None:
        return _ta_conn if _ta_ok else None
    import os
    url = os.environ.get("TURSO_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        _log.info("story_archive: Turso not configured")
        _ta_ok = False
        return None
    try:
        import libsql_experimental as libsql
        conn = libsql.connect(url, auth_token=token)
        for _ddl in _TABLE_DDLS:
            conn.execute(_ddl)
        # defensive migration for DBs created before watermark_ok existed
        for _mig in (
            "ALTER TABLE story_channels ADD COLUMN watermark_ok INTEGER DEFAULT 0",
            "ALTER TABLE story_archive ADD COLUMN watermark_ok INTEGER DEFAULT 0",
        ):
            try:
                conn.execute(_mig)
            except Exception:
                pass  # column already exists
        conn.commit()
        _ta_conn = conn
        _ta_ok = True
        _log.info("story_archive: Turso tables ready")
        return conn
    except ImportError:
        _log.warning("story_archive: libsql_experimental not installed")
        _ta_ok = False
        return None
    except Exception as e:
        _log.warning("story_archive: Turso connect failed: %s", e)
        _ta_ok = False
        return None


def turso_available() -> bool:
    return _get_conn() is not None


# ---------------------------------------------------------------------------
# Persian keyword lists for per-clip relevance (user req #4).
# Channel categorization alone is NOT enough: every clip's caption is scored.
# ---------------------------------------------------------------------------
SUBCAT_KEYWORDS: dict[tuple[str, str], list[str]] = {
    ("love", "express"): ["عشق", "عاشق", "دوستت دارم", "عاشقانه", "معشوق", "دلبر", "جانان", "عشقم"],
    ("love", "longing"): ["دلتنگی", "دلم تنگ", "دوری", "انتظار", "فراق", "دلم برات تنگ"],
    ("love", "onesided"): ["یک‌طرفه", "یک طرفه", "بی‌جواب"],
    ("love", "couple"): ["دونفره", "دو نفره", "کنار هم", "زوج"],
    ("sad", "breakup"): ["جدایی", "ترک", "رفتی", "تمام شد", "خداحافظ", "جدا شدیم"],
    ("sad", "lonely"): ["تنهایی", "تنها", "کسی نیست"],
    ("sad", "tears"): ["بغض", "گریه", "اشک"],
    ("sad", "betrayal"): ["بی‌وفا", "بی وفا", "خیانت", "نامرد"],
    ("occasion", "birthday"): ["تولد", "تولدت مبارک"],
    ("occasion", "anniversary"): ["سالگرد"],
    ("occasion", "valentine"): ["روز عشق", "ولنتاین"],
    ("occasion", "parents"): ["مادر", "پدر", "روز مادر", "روز پدر"],
    ("occasion", "calendar"): ["عید", "نوروز", "یلدا", "مبارک"],
    ("deep", "pride"): ["غرور", "مغرور"],
    ("deep", "silence"): ["سکوت", "ساکت"],
    ("deep", "indifferent"): ["بی‌تفاوت", "بی تفاوت", "برام مهم نیست"],
    ("deep", "sarcasm"): ["کنایه", "طعنه"],
    ("happy", "hope"): ["امید", "امیدوار", "فردا"],
    ("happy", "success"): ["موفقیت", "پیروزی", "موفق"],
    ("happy", "friendship"): ["رفاقت", "رفیق"],
    ("happy", "freshstart"): ["شروع تازه", "شروع", "از نو", "آغاز"],
}

_FA_NORM = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "‌": " ", " ": " "})


def _norm_fa(s: str) -> str:
    s = (s or "").translate(_FA_NORM)
    s = re.sub(r"[\u064b-\u0652]", "", s)  # diacritics
    return re.sub(r"\s+", " ", s).strip()


def relevance_verdict(caption: str, category: str, subcategory: str) -> tuple[str, int]:
    """Per-clip topical check (user req #4).

    Returns (verdict, score); verdict is 'ok' or 'review'.
    'review' clips go to the admin moderation queue — never auto-deleted.
    """
    kws = SUBCAT_KEYWORDS.get((category, subcategory), [])
    if not kws:
        return ("ok", 0)  # no keyword list: trust the channel categorization
    cap = _norm_fa(caption)
    if not cap:
        return ("review", 0)  # cannot verify an empty caption -> human check
    score = sum(1 for kw in kws if _norm_fa(kw) in cap)
    return ("ok", score) if score > 0 else ("review", 0)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------
def register_channel(username: str, chat_id: int, category: str,
                     subcategory: str) -> tuple[bool, str]:
    conn = _get_conn()
    if not conn:
        return (False, "db")
    try:
        username = username.strip().lstrip("@").lower()
        conn.execute("""
            INSERT OR REPLACE INTO story_channels
            (username, chat_id, category, subcategory, auto_approve, status, added_at)
            VALUES (?, ?, ?, ?, COALESCE((SELECT auto_approve FROM story_channels
                                           WHERE username=?), 0),
                    'active', ?)
        """, (username, chat_id, category, subcategory, username, int(time.time())))
        conn.commit()
        return (True, "ok")
    except Exception as e:
        _log.warning("register_channel failed: %s", e)
        return (False, "db")


def list_channels() -> list[dict]:
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.execute(
            "SELECT username, chat_id, category, subcategory, auto_approve, watermark_ok, status,"
            " (SELECT COUNT(*) FROM story_archive a WHERE a.source_username=c.username"
            "  AND a.status='active') AS nclips"
            " FROM story_channels c ORDER BY added_at")
        return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    except Exception as e:
        _log.warning("list_channels failed: %s", e)
        return []


def get_channel_by_chat_id(chat_id: int) -> dict | None:
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.execute(
            "SELECT username, chat_id, category, subcategory, auto_approve, watermark_ok, status"
            " FROM story_channels WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in cur.description], row))
    except Exception as e:
        _log.warning("get_channel_by_chat_id failed: %s", e)
        return None


def set_channel_status(username: str, status: str) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute("UPDATE story_channels SET status=? WHERE username=?",
                     (status, username.strip().lstrip("@").lower()))
        conn.commit()
        return True
    except Exception as e:
        _log.warning("set_channel_status failed: %s", e)
        return False


def set_channel_auto_approve(username: str, on: bool) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute("UPDATE story_channels SET auto_approve=? WHERE username=?",
                     (1 if on else 0, username.strip().lstrip("@").lower()))
        conn.commit()
        return True
    except Exception as e:
        _log.warning("set_channel_auto_approve failed: %s", e)
        return False


def set_channel_watermark(username: str, on: bool) -> bool:
    """Allow Waveframe watermarking for re-sent clips of this channel.

    Forwarded clips are NEVER watermarked (user req: direct forward keeps
    the original untouched). Only download→process→re-send clips.
    """
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute("UPDATE story_channels SET watermark_ok=? WHERE username=?",
                     (1 if on else 0, username.strip().lstrip("@").lower()))
        conn.commit()
        return True
    except Exception as e:
        _log.warning("set_channel_watermark failed: %s", e)
        return False


def delete_channel(username: str) -> bool:
    """Remove the channel registration. Archived clips stay (source kept)."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute("DELETE FROM story_channels WHERE username=?",
                     (username.strip().lstrip("@").lower(),))
        conn.commit()
        return True
    except Exception as e:
        _log.warning("delete_channel failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Archive + dedup (EXACT-match only — similar clips are never removed)
# ---------------------------------------------------------------------------
def is_duplicate(file_unique_id: str, source_chat_id=None,
                 source_message_id=None) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.execute(
            "SELECT 1 FROM story_archive WHERE file_unique_id=?",
            (file_unique_id,))
        if cur.fetchone():
            return True
        if source_chat_id and source_message_id:
            cur = conn.execute(
                "SELECT 1 FROM story_archive WHERE source_chat_id=?"
                " AND source_message_id=?",
                (source_chat_id, source_message_id))
            if cur.fetchone():
                return True
        return False
    except Exception as e:
        _log.warning("is_duplicate failed: %s", e)
        return False


def archive_clip(*, file_unique_id: str, file_id: str, category: str,
                 subcategory: str, mode: str = "forward",
                 source_chat_id=None, source_message_id=None,
                 source_username: str = "", orig_caption: str = "",
                 duration: int = 0, width: int = 0, height: int = 0,
                 watermark_ok: int = 0,
                 status: str = "active", added_by: int = 0) -> tuple[bool, str]:
    """Insert a clip. Returns (ok, 'ok'|'duplicate'|'db')."""
    if is_duplicate(file_unique_id, source_chat_id, source_message_id):
        return (False, "duplicate")
    conn = _get_conn()
    if not conn:
        return (False, "db")
    try:
        conn.execute("""
            INSERT INTO story_archive
            (file_unique_id, file_id, source_chat_id, source_message_id,
             source_username, mode, category, subcategory, orig_caption,
             duration, width, height, watermark_ok, status, added_at, added_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (file_unique_id, file_id, source_chat_id, source_message_id,
              source_username, mode, category, subcategory, orig_caption[:500],
              duration, width, height, int(watermark_ok or 0), status,
              int(time.time()), added_by))
        conn.commit()
        return (True, "ok")
    except Exception as e:
        _log.warning("archive_clip failed: %s", e)
        return (False, "db")


def intake_channel_post(*, file_unique_id: str, file_id: str, chat_id: int,
                        message_id: int, username: str, category: str,
                        subcategory: str, caption: str, duration: int,
                        width: int, height: int,
                        auto_approve: bool,
                        watermark_ok: bool = False) -> tuple[bool, str]:
    """Phase-2 intake for a new channel post. Returns (ok, outcome).

    outcome: 'ok' | 'duplicate' | 'pending' | 'db'
    """
    if is_duplicate(file_unique_id, chat_id, message_id):
        return (False, "duplicate")
    verdict, _score = relevance_verdict(caption, category, subcategory)
    status = "active" if (verdict == "ok" or auto_approve) else "pending"
    ok, res = archive_clip(
        file_unique_id=file_unique_id, file_id=file_id,
        category=category, subcategory=subcategory, mode="forward",
        source_chat_id=chat_id, source_message_id=message_id,
        source_username=username, orig_caption=caption,
        duration=duration, width=width, height=height,
        watermark_ok=1 if watermark_ok else 0, status=status)
    if not ok:
        return (False, res)
    return (True, "pending" if status == "pending" else "ok")


def get_clip(clip_id: int) -> dict | None:
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.execute("SELECT * FROM story_archive WHERE id=?", (clip_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in cur.description], row))
    except Exception as e:
        _log.warning("get_clip failed: %s", e)
        return None


def list_clips(category: str, subcategory: str, user_id: int,
               page: int, per_page: int = 5) -> tuple[list[dict], int]:
    """Unseen-first listing for a user. Returns (clips, total_pages)."""
    conn = _get_conn()
    if not conn:
        return ([], 0)
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM story_archive"
            " WHERE category=? AND subcategory=? AND status='active'",
            (category, subcategory))
        total = cur.fetchone()[0]
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, pages - 1))
        cur = conn.execute(
            """SELECT a.*,
                      CASE WHEN s.seen_at IS NULL THEN 0 ELSE 1 END AS seen
               FROM story_archive a
               LEFT JOIN story_seen s
                 ON s.user_id=? AND s.file_unique_id=a.file_unique_id
               WHERE a.category=? AND a.subcategory=? AND a.status='active'
               ORDER BY seen ASC, a.added_at DESC
               LIMIT ? OFFSET ?""",
            (user_id, category, subcategory, per_page, page * per_page))
        cols = [d[0] for d in cur.description]
        return ([dict(zip(cols, r)) for r in cur.fetchall()], pages)
    except Exception as e:
        _log.warning("list_clips failed: %s", e)
        return ([], 0)


def count_active_clips() -> int:
    conn = _get_conn()
    if not conn:
        return 0
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM story_archive WHERE status='active'")
        return cur.fetchone()[0]
    except Exception:
        return 0


def pending_clips(limit: int = 20) -> list[dict]:
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.execute(
            "SELECT * FROM story_archive WHERE status='pending'"
            " ORDER BY added_at DESC LIMIT ?", (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        _log.warning("pending_clips failed: %s", e)
        return []


def set_clip_status(clip_id: int, status: str) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute("UPDATE story_archive SET status=? WHERE id=?",
                     (status, clip_id))
        conn.commit()
        return True
    except Exception as e:
        _log.warning("set_clip_status failed: %s", e)
        return False


def move_clip(clip_id: int, category: str, subcategory: str) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute(
            "UPDATE story_archive SET category=?, subcategory=?, status='active'"
            " WHERE id=?", (category, subcategory, clip_id))
        conn.commit()
        return True
    except Exception as e:
        _log.warning("move_clip failed: %s", e)
        return False


def delete_clip(clip_id: int) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute("DELETE FROM story_archive WHERE id=?", (clip_id,))
        conn.commit()
        return True
    except Exception as e:
        _log.warning("delete_clip failed: %s", e)
        return False


def mark_seen(user_id: int, file_unique_id: str) -> None:
    conn = _get_conn()
    if not conn:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO story_seen (user_id, file_unique_id, seen_at)"
            " VALUES (?,?,?)", (user_id, file_unique_id, int(time.time())))
        conn.commit()
        # bounded history: keep 300 latest per user, drop entries older than 30d
        conn.execute(
            """DELETE FROM story_seen WHERE user_id=? AND file_unique_id NOT IN
               (SELECT file_unique_id FROM story_seen WHERE user_id=?
                ORDER BY seen_at DESC LIMIT 300)""", (user_id, user_id))
        conn.execute(
            "DELETE FROM story_seen WHERE seen_at < ?",
            (int(time.time()) - 30 * 86400,))
        conn.commit()
    except Exception as e:
        _log.warning("mark_seen failed: %s", e)


def archive_stats() -> dict:
    conn = _get_conn()
    if not conn:
        return {"channels": 0, "clips": 0, "pending": 0}
    try:
        out = {}
        for key, q in (
            ("channels", "SELECT COUNT(*) FROM story_channels WHERE status='active'"),
            ("clips", "SELECT COUNT(*) FROM story_archive WHERE status='active'"),
            ("pending", "SELECT COUNT(*) FROM story_archive WHERE status='pending'"),
        ):
            cur = conn.execute(q)
            out[key] = cur.fetchone()[0]
        return out
    except Exception as e:
        _log.warning("archive_stats failed: %s", e)
        return {"channels": 0, "clips": 0, "pending": 0}


# ---------------------------------------------------------------------------
# Corporate caption for re-sent clips (mode='resend').
# Forwarded clips keep the native Telegram forward header (user req #8).
# ---------------------------------------------------------------------------
def corporate_caption(clip: dict) -> str:
    m, s = divmod(max(0, int(clip.get("duration") or 0)), 60)
    dur = f"{m:02d}:{s:02d}"
    src = clip.get("source_username") or ""
    src_line = f"\n📎 منبع: @{src}" if src else ""
    return (f"🎬 AVANO MEDIA\n"
            f"✨ منتخب استوری آوانو\n\n"
            f"⏱ مدت: {dur}{src_line}\n\n"
            f"@AvanoMediaBot")

# ---------------------------------------------------------------------------

def iranian_clip_count() -> int:
    """Active Iranian clips — used by the story:cat chooser."""
    try:
        return count_active_clips()
    except Exception:
        return 0



def iranian_chooser_kb():
    """Iranian/stock chooser (lazy aiogram import: works on Render)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇮🇷 کلیپ‌های ایرانی", callback_data="sarch:home")],
        [InlineKeyboardButton(text="🌍 استوک خارجی", callback_data="story:cat")],
    ])


def _clip_row_label(clip: dict, n: int) -> str:
    cap = (clip.get("orig_caption") or "").strip().split("\n")[0][:32]
    dur = clip.get("duration") or 0
    dstr = f" ⏱{dur//60}:{dur%60:02d}" if dur else ""
    base = cap if cap else f"کلیپ {n}"
    return f"🎬 {base}{dstr}"

def register_story_archive_handlers(router) -> None:
    """Attach all story-archive handlers to the repo's aiogram router.

    All aiogram imports are lazy (inside this function) so the pure
    DB/logic layer above stays importable without aiogram (sandbox tests).
    Safe to call once at startup; never touches existing handlers.
    """
    from aiogram import F  # lazy: aiogram is present on Render
    from aiogram.fsm.context import FSMContext
    from aiogram.filters import StateFilter
    from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                               InlineKeyboardMarkup, Message)

    def _btn(text, cb):
        return InlineKeyboardButton(text=text, callback_data=cb)


    # Callback prefix: sarch:  (story: is the Pixabay module — untouched)
    # ---------------------------------------------------------------------------




    def _is_owner_cb(callback: CallbackQuery, config) -> bool:
        try:
            return config.owner_id is not None and callback.from_user.id == config.owner_id
        except Exception:
            return False


    def _is_owner_msg(message: Message, config) -> bool:
        try:
            return config.owner_id is not None and message.from_user.id == config.owner_id
        except Exception:
            return False


    def _cat_title(cat_id: str) -> str:
        try:
            from .story_clips import CATEGORIES
            for c in CATEGORIES:
                if c["id"] == cat_id:
                    return f'{c["emoji"]} {c["title"]}'
        except Exception:
            pass
        return cat_id


    def _sub_title(cat_id: str, sub_id: str) -> str:
        try:
            from .story_clips import CATEGORIES
            for c in CATEGORIES:
                if c["id"] == cat_id:
                    for s in c["subtopics"]:
                        if s["id"] == sub_id:
                            return s["title"]
        except Exception:
            pass
        return sub_id


    # ---------------- admin panel ----------------

    @router.callback_query(F.data == "sarch:panel")
    async def sarch_panel(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        await callback.answer()
        st = archive_stats()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_btn("➕ ثبت کانال جدید", callback_data="sarch:addchan")],
            [_btn("📋 کانال‌ها", callback_data="sarch:chans")],
            [_btn(f"⏳ صف تأیید ({st['pending']})", callback_data="sarch:queue")],
            [_btn("🗂 مرور آرشیو", callback_data="sarch:browse")],
            [_btn("📥 افزودن دستی کلیپ", callback_data="sarch:manual")],
            [_btn("🔙 پنل مدیریت", callback_data="adm:panel")],
        ])
        txt = (f"🎬 <b>آرشیو استوری ایرانی</b>\n\n"
               f"📡 کانال‌های فعال: {st['channels']}\n"
               f"🎞 کلیپ‌های فعال: {st['clips']}\n"
               f"⏳ در انتظار تأیید: {st['pending']}\n\n"
               f"💡 برای افزودن کلیپ قدیمی، آن را به همین‌جا فوروارد کنید"
               f" (بعد از «📥 افزودن دستی کلیپ»).")
        try:
            await callback.message.edit_text(txt, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await callback.message.answer(txt, parse_mode="HTML", reply_markup=kb)


    @router.callback_query(F.data == "sarch:addchan")
    async def sarch_addchan(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            await callback.answer("⛔", show_alert=True)
            return
        await callback.answer()
        await state.set_state("sarch:addchan:username")
        await callback.message.answer(
            "📡 یوزرنیم کانال عمومی را بفرستید (مثلاً <code>@clip_love</code>).\n"
            "⚠️ ربات باید ادمین آن کانال باشد.",
            parse_mode="HTML")


    @router.message(F.text.startswith("@"), StateFilter("sarch:addchan:username"))
    async def sarch_addchan_username(message: Message, config, state: FSMContext):
        cur = await state.get_state()
        if cur != "sarch:addchan:username":
            return
        if not _is_owner_msg(message, config):
            await state.clear()
            return
        username = message.text.strip().lstrip("@").lower()
        # verify the bot is admin in the channel (user req #3: only with access)
        try:
            chat = await message.bot.get_chat(f"@{username}")
            member = await message.bot.get_chat_member(chat.id, (await message.bot.me()).id)
            if getattr(member, "status", "") not in ("administrator", "creator"):
                await message.answer(
                    "⛔ ربات در این کانال ادمین نیست. اول ربات را ادمین کنید، بعد دوباره تلاش کنید.")
                await state.clear()
                return
            chat_id = chat.id
        except Exception:
            await message.answer("❌ کانال پیدا نشد یا دسترسی ندارم. یوزرنیم را بررسی کنید.")
            await state.clear()
            return
        await state.update_data(sarch_chan_username=username, sarch_chan_id=chat_id)
        await state.set_state("sarch:addchan:cat")
        try:
            from .story_clips import CATEGORIES
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [_btn(f'{c["emoji"]} {c["title"]}', callback_data=f'sarch:regcat:{c["id"]}')]
                for c in CATEGORIES
            ] + [[_btn("❌ لغو", callback_data="sarch:panel")]])
        except Exception:
            kb = None
        await message.answer("📂 دسته‌ی این کانال را انتخاب کنید:", reply_markup=kb)


    @router.callback_query(F.data.startswith("sarch:regcat:"))
    async def sarch_regcat(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        await callback.answer()
        data = await state.get_data()
        if not data.get("sarch_chan_username"):
            await state.clear()
            return
        cat = callback.data.split(":")[2]
        await state.update_data(sarch_chan_cat=cat)
        await state.set_state("sarch:addchan:sub")
        try:
            from .story_clips import CATEGORIES
            subs = next(c["subtopics"] for c in CATEGORIES if c["id"] == cat)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [_btn(s["title"], callback_data=f'sarch:regsub:{s["id"]}')]
                for s in subs
            ] + [[_btn("❌ لغو", callback_data="sarch:panel")]])
        except Exception:
            kb = None
        await callback.message.answer("📁 زیردسته را انتخاب کنید:", reply_markup=kb)


    @router.callback_query(F.data.startswith("sarch:regsub:"))
    async def sarch_regsub(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        await callback.answer()
        data = await state.get_data()
        username = data.get("sarch_chan_username")
        chat_id = data.get("sarch_chan_id")
        cat = data.get("sarch_chan_cat")
        sub = callback.data.split(":")[2]
        await state.clear()
        if not (username and chat_id and cat):
            return
        ok, _ = register_channel(username, chat_id, cat, sub)
        if ok:
            await callback.message.answer(
                f"✅ کانال @{username} ثبت شد.\n{_cat_title(cat)} ← {_sub_title(cat, sub)}\n"
                f"از این به بعد پست‌های جدیدش خودکار آرشیو می‌شود.")
        else:
            await callback.message.answer("❌ ثبت ناموفق بود (مشکل دیتابیس).")


    @router.callback_query(F.data == "sarch:chans")
    async def sarch_chans(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        await callback.answer()
        chans = list_channels()
        if not chans:
            txt = "📡 هنوز کانالی ثبت نشده."
        else:
            lines = ["📡 <b>کانال‌های ثبت‌شده:</b>"]
            for c in chans:
                st = {"active": "✅", "paused": "⏸", "error": "⚠️"}.get(c["status"], "❓")
                lines.append(
                    f'{st} @{c["username"]} — {_cat_title(c["category"])} ← '
                    f'{_sub_title(c["category"], c["subcategory"])} '
                    f'({c["nclips"]} کلیپ)')
            txt = "\n".join(lines)
        kb_rows = []
        for c in chans:
            u = c["username"]
            kb_rows.append([
                _btn(f'{"⏸" if c["status"]=="active" else "▶"} @{u}',
                     callback_data=f'sarch:chantoggle:{u}'),
                _btn(f'{"💧✅" if c.get("watermark_ok") else "💧"}',
                     callback_data=f'sarch:chanwm:{u}'),
                _btn("🗑", callback_data=f'sarch:chandel:{u}'),
            ])
        kb_rows.append([_btn("🔙", callback_data="sarch:panel")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        try:
            await callback.message.edit_text(txt, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await callback.message.answer(txt, parse_mode="HTML", reply_markup=kb)


    @router.callback_query(F.data.startswith("sarch:chantoggle:"))
    async def sarch_chantoggle(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        u = callback.data.split(":")[2]
        chans = {c["username"]: c for c in list_channels()}
        cur = (chans.get(u) or {}).get("status", "active")
        set_channel_status(u, "paused" if cur == "active" else "active")
        await callback.answer("انجام شد")
        await sarch_chans(callback, config, state)


    @router.callback_query(F.data.startswith("sarch:chanwm:"))
    async def sarch_chanwm(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        u = callback.data.split(":")[2]
        chans = {c["username"]: c for c in list_channels()}
        cur = bool((chans.get(u) or {}).get("watermark_ok"))
        set_channel_watermark(u, not cur)
        await callback.answer("💧 واترمارک " + ("فعال شد" if not cur else "غیرفعال شد"))
        await sarch_chans(callback, config, state)

    @router.callback_query(F.data.startswith("sarch:chandel:"))
    async def sarch_chandel(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        u = callback.data.split(":")[2]
        delete_channel(u)
        await callback.answer("حذف شد (کلیپ‌های آرشیو می‌مانند)")
        await sarch_chans(callback, config, state)


    # ---------------- moderation queue ----------------

    @router.callback_query(F.data == "sarch:queue")
    async def sarch_queue(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        await callback.answer()
        items = pending_clips(5)
        if not items:
            await callback.message.answer("⏳ صف تأیید خالی است.",
                                          reply_markup=InlineKeyboardMarkup(
                                              inline_keyboard=[[ _btn("🔙", callback_data="sarch:panel")]]))
            return
        clip = items[0]
        cap = (clip.get("orig_caption") or "")[:200]
        txt = (f"⏳ <b>بررسی ارتباط موضوعی</b>\n"
               f"دسته: {_cat_title(clip['category'])} ← {_sub_title(clip['category'], clip['subcategory'])}\n"
               f"منبع: @{clip.get('source_username') or '—'}\n"
               f"کپشن: {cap or '—'}\n\n"
               f"این کلیپ را ببینید و تأیید کنید:")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_btn("✅ تأیید", callback_data=f'sarch:qok:{clip["id"]}')],
            [_btn("📁 تأیید با دسته‌ی دیگر", callback_data=f'sarch:qmove:{clip["id"]}')],
            [_btn("🗑 رد", callback_data=f'sarch:qdel:{clip["id"]}')],
            [_btn("⏭ بعدی", callback_data="sarch:queue")],
            [_btn("🔙", callback_data="sarch:panel")],
        ])
        # show the clip itself for visual relevance check
        try:
            if clip.get("mode") == "forward" and clip.get("source_chat_id") and clip.get("source_message_id"):
                await callback.message.bot.forward_message(
                    callback.message.chat.id, clip["source_chat_id"], clip["source_message_id"])
            else:
                await callback.message.bot.send_video(
                    callback.message.chat.id, clip["file_id"])
        except Exception:
            pass
        await callback.message.answer(txt, parse_mode="HTML", reply_markup=kb)


    @router.callback_query(F.data.startswith("sarch:qok:"))
    async def sarch_qok(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        set_clip_status(int(callback.data.split(":")[2]), "active")
        await callback.answer("✅ تأیید شد")
        await sarch_queue(callback, config, state)


    @router.callback_query(F.data.startswith("sarch:qdel:"))
    async def sarch_qdel(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        delete_clip(int(callback.data.split(":")[2]))
        await callback.answer("🗑 رد شد")
        await sarch_queue(callback, config, state)


    @router.callback_query(F.data.startswith("sarch:qmove:"))
    async def sarch_qmove(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        await callback.answer()
        clip_id = callback.data.split(":")[2]
        await state.update_data(sarch_move_id=int(clip_id))
        await state.set_state("sarch:move:cat")
        try:
            from .story_clips import CATEGORIES
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [_btn(f'{c["emoji"]} {c["title"]}', callback_data=f'sarch:movecat:{c["id"]}')]
                for c in CATEGORIES
            ] + [[_btn("❌ لغو", callback_data="sarch:panel")]])
        except Exception:
            kb = None
        await callback.message.answer("📂 دسته‌ی درست را انتخاب کنید:", reply_markup=kb)


    @router.callback_query(F.data.startswith("sarch:movecat:"))
    async def sarch_movecat(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        await callback.answer()
        cat = callback.data.split(":")[2]
        await state.update_data(sarch_move_cat=cat)
        await state.set_state("sarch:move:sub")
        try:
            from .story_clips import CATEGORIES
            subs = next(c["subtopics"] for c in CATEGORIES if c["id"] == cat)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [_btn(s["title"], callback_data=f'sarch:movesub:{s["id"]}')]
                for s in subs
            ] + [[_btn("❌ لغو", callback_data="sarch:panel")]])
        except Exception:
            kb = None
        await callback.message.answer("📁 زیردسته‌ی درست:", reply_markup=kb)


    @router.callback_query(F.data.startswith("sarch:movesub:"))
    async def sarch_movesub(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        data = await state.get_data()
        clip_id = data.get("sarch_move_id")
        cat = data.get("sarch_move_cat")
        sub = callback.data.split(":")[2]
        await state.clear()
        if clip_id and cat:
            move_clip(clip_id, cat, sub)
            await callback.answer("✅ منتقل شد")
        await sarch_queue(callback, config, state)


    # ---------------- manual add (owner forwards a clip) ----------------

    @router.callback_query(F.data == "sarch:manual")
    async def sarch_manual(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        await callback.answer()
        await state.set_state("sarch:manual:await_video")
        await callback.message.answer(
            "📥 کلیپ را به همین‌جا <b>فوروارد</b> کنید (از هر کانال عمومی).\n"
            "بعد دسته و زیردسته را انتخاب می‌کنید.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_btn("❌ لغو", callback_data="sarch:panel")]]))


    @router.message(F.video | F.animation, StateFilter("sarch:manual:await_video"))
    async def sarch_manual_video(message: Message, config, state: FSMContext):
        cur = await state.get_state()
        if cur != "sarch:manual:await_video":
            return
        if not _is_owner_msg(message, config):
            return
        media = message.video or message.animation
        src_username, src_chat, src_msg = "", None, None
        fo = getattr(message, "forward_origin", None)
        if fo:
            # chat-type forward origin carries the source channel
            chat = getattr(fo, "chat", None)
            if chat is not None:
                src_username = getattr(chat, "username", "") or ""
                src_chat = getattr(chat, "id", None)
                src_msg = getattr(fo, "message_id", None)
        await state.update_data(
            sarch_man_file_id=media.file_id,
            sarch_man_unique=media.file_unique_id,
            sarch_man_dur=getattr(media, "duration", 0) or 0,
            sarch_man_w=getattr(media, "width", 0) or 0,
            sarch_man_h=getattr(media, "height", 0) or 0,
            sarch_man_cap=message.caption or "",
            sarch_man_srcu=src_username, sarch_man_srcc=src_chat, sarch_man_srcm=src_msg)
        await state.set_state("sarch:manual:cat")
        try:
            from .story_clips import CATEGORIES
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [_btn(f'{c["emoji"]} {c["title"]}', callback_data=f'sarch:mancat:{c["id"]}')]
                for c in CATEGORIES
            ] + [[_btn("❌ لغو", callback_data="sarch:panel")]])
        except Exception:
            kb = None
        await message.answer("📂 دسته‌ی این کلیپ:", reply_markup=kb)


    @router.callback_query(F.data.startswith("sarch:mancat:"))
    async def sarch_mancat(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        await callback.answer()
        cat = callback.data.split(":")[2]
        await state.update_data(sarch_man_cat=cat)
        await state.set_state("sarch:manual:sub")
        try:
            from .story_clips import CATEGORIES
            subs = next(c["subtopics"] for c in CATEGORIES if c["id"] == cat)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [_btn(s["title"], callback_data=f'sarch:mansub:{s["id"]}')]
                for s in subs
            ] + [[_btn("❌ لغو", callback_data="sarch:panel")]])
        except Exception:
            kb = None
        await callback.message.answer("📁 زیردسته:", reply_markup=kb)


    @router.callback_query(F.data.startswith("sarch:mansub:"))
    async def sarch_mansub(callback: CallbackQuery, config, state: FSMContext):
        if not _is_owner_cb(callback, config):
            return
        data = await state.get_data()
        sub = callback.data.split(":")[2]
        cat = data.get("sarch_man_cat")
        await state.clear()
        if not (cat and data.get("sarch_man_unique")):
            return
        # admin chose the category: the human IS the relevance check (req #4)
        ok, res = archive_clip(
            file_unique_id=data["sarch_man_unique"], file_id=data["sarch_man_file_id"],
            category=cat, subcategory=sub, mode="resend",
            source_chat_id=data.get("sarch_man_srcc"),
            source_message_id=data.get("sarch_man_srcm"),
            source_username=data.get("sarch_man_srcu") or "",
            orig_caption=data.get("sarch_man_cap") or "",
            duration=data.get("sarch_man_dur") or 0,
            width=data.get("sarch_man_w") or 0, height=data.get("sarch_man_h") or 0,
            status="active", added_by=callback.from_user.id)
        if ok:
            await callback.message.answer(
                f"✅ در آرشیو ذخیره شد.\n{_cat_title(cat)} ← {_sub_title(cat, sub)}")
        elif res == "duplicate":
            await callback.message.answer("⚠️ این کلیپ قبلاً در آرشیو هست.")
        else:
            await callback.message.answer("❌ ذخیره ناموفق بود (مشکل دیتابیس).")
        await callback.answer()


    # ---------------- channel intake (Phase 2) ----------------

    @router.channel_post()
    async def sarch_channel_intake(post: Message):
        """Index new posts from registered channels only (user req #3)."""
        try:
            chat = post.chat
            ch = get_channel_by_chat_id(chat.id)
            if not ch or ch.get("status") != "active":
                return
            media = post.video or post.animation
            if not media:
                return
            if getattr(post, "has_protected_content", False):
                return  # respect the author's restriction
            username = (getattr(chat, "username", "") or ch.get("username") or "")
            ok, outcome = intake_channel_post(
                file_unique_id=media.file_unique_id, file_id=media.file_id,
                chat_id=chat.id, message_id=post.message_id, username=username,
                category=ch["category"], subcategory=ch["subcategory"],
                caption=post.caption or post.text or "",
                duration=getattr(media, "duration", 0) or 0,
                width=getattr(media, "width", 0) or 0,
                height=getattr(media, "height", 0) or 0,
                auto_approve=bool(ch.get("auto_approve")),
                watermark_ok=bool(ch.get("watermark_ok")))
            _log.info("sarch intake @%s msg=%s -> %s", username, post.message_id, outcome)
        except Exception as e:
            _log.warning("sarch_channel_intake failed: %s", e)


    # ---------------- user delivery (Phase 3) ----------------

    _WM_SEM = None

    def _wm_sem():
        nonlocal _WM_SEM
        if _WM_SEM is None:
            import asyncio as _aio
            _WM_SEM = _aio.Semaphore(1)
        return _WM_SEM

    async def deliver_waveframed(bot, chat_id: int, clip: dict) -> str:
        """Download -> Waveframe badge -> send -> cleanup.

        Only for clips where watermark use is permitted (watermark_ok=1).
        Forwarded clips NEVER go through here (user req: direct forward
        keeps the original untouched). Runs in executor; temp dir cleaned.
        On watermark failure, falls back to plain resend (logged).
        """
        import asyncio
        import os
        import tempfile
        tmpd = tempfile.mkdtemp(prefix="sarch_wm_")
        try:
            async with _wm_sem():
                src = os.path.join(tmpd, "src.mp4")
                out = os.path.join(tmpd, "wm.mp4")
                f = await bot.get_file(clip["file_id"])
                await bot.download_file(f.file_path, destination=src)
                loop = asyncio.get_running_loop()

                def _run():
                    from .motion_watermark import apply_waveframe_badge
                    return apply_waveframe_badge(src, out)

                info = await loop.run_in_executor(None, _run)
                _log.info("waveframe applied: %s", info)
                from aiogram.types import FSInputFile
                await bot.send_video(chat_id, FSInputFile(out),
                                     caption=corporate_caption(clip))
                return "ok"
        except Exception as e:
            _log.warning("deliver_waveframed failed, plain resend fallback: %s", e)
            try:
                await bot.send_video(chat_id, clip["file_id"],
                                     caption=corporate_caption(clip))
                return "ok"
            except Exception as e2:
                _log.warning("plain resend also failed: %s", e2)
                return "error"
        finally:
            try:
                import shutil
                shutil.rmtree(tmpd, ignore_errors=True)
            except Exception:
                pass

    async def deliver_clip(bot, chat_id: int, clip: dict) -> str:
        """Deliver one clip. Returns 'ok'|'gone'|'blocked'|'error'.

        User order 2026-11-05: badge on ALL forwards. A true Telegram
        forward cannot be modified, so forward-mode clips are downloaded,
        badged and re-sent (source credit stays in the corporate caption).
        Plain forward remains only as a last-resort fallback.
        """
        try:
            if clip.get("mode") == "forward":
                res = await deliver_waveframed(bot, chat_id, clip)
                if res == "ok":
                    return "ok"
                # last resort: plain forward (original behavior)
                if clip.get("source_chat_id") and clip.get("source_message_id"):
                    try:
                        await bot.forward_message(
                            chat_id, clip["source_chat_id"], clip["source_message_id"])
                        return "ok"
                    except Exception as e:
                        msg = str(e).lower()
                        if "message to forward not found" in msg or "message_id_invalid" in msg:
                            set_clip_status(clip["id"], "unavailable")
                            return "gone"
                        if "protected" in msg:
                            set_clip_status(clip["id"], "unavailable")
                            return "blocked"
                        _log.warning("deliver forward fallback failed: %s", e)
                        return "error"
                return res
            # resend mode: Waveframe badge first when permitted, else plain
            # resend with corporate caption.
            if int(clip.get("watermark_ok") or 0) == 1:
                return await deliver_waveframed(bot, chat_id, clip)
            await bot.send_video(chat_id, clip["file_id"],
                                 caption=corporate_caption(clip))
            return "ok"
        except Exception as e:
            _log.warning("deliver_clip failed: %s", e)
            return "error"


    @router.callback_query(F.data == "sarch:home")
    async def sarch_home(callback: CallbackQuery, config, state: FSMContext):
        await callback.answer()
        try:
            from .story_clips import CATEGORIES
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [_btn(f'{c["emoji"]} {c["title"]}', callback_data=f'sarch:cat:{c["id"]}')]
                for c in CATEGORIES
            ] + [[_btn("🔙", callback_data="story:cat")]])
        except Exception:
            kb = None
        try:
            await callback.message.edit_text(
                "🇮🇷 <b>کلیپ‌های ایرانی</b>\nدسته را انتخاب کنید:",
                parse_mode="HTML", reply_markup=kb)
        except Exception:
            await callback.message.answer(
                "🇮🇷 <b>کلیپ‌های ایرانی</b>\nدسته را انتخاب کنید:",
                parse_mode="HTML", reply_markup=kb)


    @router.callback_query(F.data.startswith("sarch:cat:"))
    async def sarch_cat(callback: CallbackQuery, config, state: FSMContext):
        await callback.answer()
        cat = callback.data.split(":")[2]
        try:
            from .story_clips import CATEGORIES
            subs = next(c["subtopics"] for c in CATEGORIES if c["id"] == cat)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [_btn(s["title"], callback_data=f'sarch:sub:{cat}:{s["id"]}:0')]
                for s in subs
            ] + [[_btn("🔙 دسته‌ها", callback_data="sarch:home")]])
        except Exception:
            kb = None
        try:
            await callback.message.edit_text(
                f"{_cat_title(cat)}\nزیردسته را انتخاب کنید:",
                reply_markup=kb)
        except Exception:
            await callback.message.answer(
                f"{_cat_title(cat)}\nزیردسته را انتخاب کنید:", reply_markup=kb)




    @router.callback_query(F.data.startswith("sarch:sub:"))
    async def sarch_sub(callback: CallbackQuery, config, state: FSMContext):
        await callback.answer()
        _, _, cat, sub, page = callback.data.split(":")
        page = int(page)
        uid = callback.from_user.id
        clips, pages = list_clips(cat, sub, uid, page, per_page=5)
        rows = []
        base_n = page * 5
        for i, clip in enumerate(clips, start=1):
            rows.append([_btn(_clip_row_label(clip, base_n + i),
                              callback_data=f'sarch:play:{clip["id"]}:{cat}:{sub}:{page}')])
        nav = []
        if page > 0:
            nav.append(_btn("◀ قبلی", callback_data=f"sarch:sub:{cat}:{sub}:{page-1}"))
        nav.append(_btn(f"{page+1}/{pages}", callback_data="sarch:noop"))
        if page < pages - 1:
            nav.append(_btn("بعدی ▶", callback_data=f"sarch:sub:{cat}:{sub}:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([_btn("🎥 تماشا در یوتیوب", callback_data=f"sarch:yt:{cat}:{sub}")])
        rows.append([_btn("🔙", callback_data=f"sarch:cat:{cat}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        txt = (f"{_cat_title(cat)} ← {_sub_title(cat, sub)}\n"
               f"🎞 {sum(1 for _ in clips)} کلیپ در این صفحه"
               if clips else
               f"{_cat_title(cat)} ← {_sub_title(cat, sub)}\n"
               f"⏳ هنوز کلیپی در این بخش نیست؛ به‌زودی اضافه می‌شود.")
        try:
            await callback.message.edit_text(txt, reply_markup=kb)
        except Exception:
            await callback.message.answer(txt, reply_markup=kb)


    @router.callback_query(F.data == "sarch:noop")
    async def sarch_noop(callback: CallbackQuery):
        await callback.answer()


    @router.callback_query(F.data.startswith("sarch:play:"))
    async def sarch_play(callback: CallbackQuery, config, state: FSMContext):
        await callback.answer()
        _, _, clip_id, _cat, _sub, _page = callback.data.split(":")
        clip = get_clip(int(clip_id))
        if not clip or clip.get("status") != "active":
            await callback.message.answer("⏳ این کلیپ دیگر در دسترس نیست.")
            return
        res = await deliver_clip(callback.bot, callback.message.chat.id, clip)
        uid = callback.from_user.id
        if res == "ok":
            mark_seen(uid, clip["file_unique_id"])
        elif res == "gone":
            await callback.message.answer(
                "🗑 این کلیپ از کانال منبع حذف شده. یکی دیگر را امتحان کنید 👇")
        elif res == "blocked":
            await callback.message.answer("🔒 فوروارد این کلیپ محدود شده است.")
        else:
            await callback.message.answer("❌ ارسال ناموفق بود؛ لطفاً دوباره تلاش کنید.")


    # ---------------- YouTube complementary (Phase 4: links only) ----------------

    def _yt_key():
        import os
        return os.environ.get("YOUTUBE_API_KEY", "").strip()


    _YT_QUERIES: dict[tuple[str, str], str] = {
        ("love", "express"): "کلیپ عاشقانه ابراز عشق",
        ("love", "longing"): "کلیپ دلتنگی عاشقانه",
        ("love", "onesided"): "کلیپ عشق یک طرفه",
        ("love", "couple"): "کلیپ دونفره عاشقانه",
        ("sad", "breakup"): "کلیپ جدایی غمگین",
        ("sad", "lonely"): "کلیپ تنهایی",
        ("sad", "tears"): "کلیپ بغض و گریه",
        ("sad", "betrayal"): "کلیپ بی وفایی",
        ("occasion", "birthday"): "کلیپ تولد",
        ("occasion", "anniversary"): "کلیپ سالگرد ازدواج",
        ("occasion", "valentine"): "کلیپ روز عشق ولنتاین",
        ("occasion", "parents"): "کلیپ روز مادر پدر",
        ("occasion", "calendar"): "کلیپ تبریک عید",
        ("deep", "pride"): "کلیپ غرور",
        ("deep", "silence"): "کلیپ سکوت",
        ("deep", "indifferent"): "کلیپ بی تفاوتی",
        ("deep", "sarcasm"): "کلیپ کنایه",
        ("happy", "hope"): "کلیپ امید انگیزشی",
        ("happy", "success"): "کلیپ موفقیت",
        ("happy", "friendship"): "کلیپ رفاقت",
        ("happy", "freshstart"): "کلیپ شروع تازه انگیزشی",
    }


    def yt_search(category: str, subcategory: str) -> list[dict]:
        """YouTube link discovery. Never downloads. Returns [] without a key."""
        import json
        import urllib.parse
        import urllib.request
        key = _yt_key()
        if not key:
            return []
        conn = _get_conn()
        cat_sub = f"{category}:{subcategory}"
        now = int(time.time())
        if conn:
            try:
                cur = conn.execute(
                    "SELECT payload, fetched_at FROM story_yt_cache WHERE cat_sub=?",
                    (cat_sub,))
                row = cur.fetchone()
                if row and now - int(row[1]) < 24 * 3600:
                    return json.loads(row[0])
            except Exception:
                pass
        query = _YT_QUERIES.get((category, subcategory), "کلیپ استوری")
        try:
            params = urllib.parse.urlencode({
                "part": "snippet", "type": "video", "videoDuration": "short",
                "maxResults": 5, "q": query, "relevanceLanguage": "fa",
                "regionCode": "IR", "key": key,
            })
            req = urllib.request.Request(
                "https://www.googleapis.com/youtube/v3/search?" + params,
                headers={"User-Agent": "AvanoMediaBot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            out = []
            for it in data.get("items", []):
                vid = ((it.get("id") or {}).get("videoId")) or ""
                sn = it.get("snippet", {}) or {}
                if not vid:
                    continue
                out.append({
                    "video_id": vid,
                    "title": sn.get("title", "")[:80],
                    "thumb": (((sn.get("thumbnails") or {}).get("medium") or {}).get("url")) or "",
                    "url": f"https://www.youtube.com/shorts/{vid}",
                })
            if conn and out:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO story_yt_cache (cat_sub, payload, fetched_at)"
                        " VALUES (?,?,?)", (cat_sub, json.dumps(out), now))
                    conn.commit()
                except Exception:
                    pass
            _log.info("yt_search %s -> %d links", cat_sub, len(out))
            return out
        except Exception as e:
            _log.warning("yt_search failed: %s", e)
            return []


    @router.callback_query(F.data.startswith("sarch:yt:"))
    async def sarch_yt(callback: CallbackQuery, config, state: FSMContext):
        await callback.answer()
        _, _, cat, sub = callback.data.split(":")
        import asyncio
        loop = asyncio.get_running_loop()
        links = await loop.run_in_executor(None, yt_search, cat, sub)
        if not links:
            await callback.message.answer(
                "🎥 لینک یوتیوب فعلاً در دسترس نیست."
                + ("" if _yt_key() else " (کلید API هنوز تنظیم نشده)"))
            return
        rows = []
        for l in links:
            rows.append([_btn(f'▶ {l["title"][:40]}', url=l["url"])])
        rows.append([_btn("🔙", callback_data=f"sarch:sub:{cat}:{sub}:0")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await callback.message.answer(
            f"🎥 <b>تماشا در یوتیوب</b> — {_sub_title(cat, sub)}\n"
            f"⚠️ این‌ها لینک هستند؛ دانلود از یوتیوب انجام نمی‌شود.",
            parse_mode="HTML", reply_markup=kb)


