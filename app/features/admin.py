"""Owner admin extension: panels, ads, OpenAI key box, users, settings,
clear-chat and delete-all-my-data. Callback prefix: ``adm:``. FSM states: ``adm:*``.

This module only ADDS panels; it never replaces handlers in app/bot/handlers.py.

Wiring notes for the integrator (app/bot/handlers.py owner):
- After every outgoing bot message, call ``track(conn, user_id, chat_id, message_id)``
  so «پاک‌سازی گفتگو» can delete those messages best-effort later.
- Gate speech features on ``is_openai_verified(conn)``; the exact gate messages
  are the ``GATE_*`` constants below (wording from spec section 8).
- Add a row to the /admin message with a button ``callback_data="adm:panel"``
  linking to this panel (cmd_admin lives in handlers.py, not here).
"""
from __future__ import annotations

import functools
import sqlite3
import time

import httpx
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from .. import db as dbmod
from ..bot.logic import menu_escape

# --- brief failure lines (one short Persian line per failure) ---------------
BRIEF = {
    "not_owner": "⛔ فقط مالک ربات می‌تواند از این بخش استفاده کند.",
    "not_found": "یافت نشد؛ دوباره تلاش کنید.",
    "save_failed": "ذخیره انجام نشد؛ دوباره تلاش کنید.",
    "wipe_failed": "حذف اطلاعات انجام نشد؛ دوباره تلاش کنید.",
    "name_too_long": "نام دکمه حداکثر ۸۰ کاراکتر باشد.",
    "empty": "متن خالی است؛ دوباره بفرستید.",
    "text_too_long": "متن پاسخ حداکثر ۲۰۰۰ کاراکتر باشد.",
    "core_not_deletable": "دکمه‌های اصلی قابل حذف نیستند.",
    "bad_id": "شناسه نامعتبر است.",
}

# --- speech gate messages (exact wording from spec section 8) ---------------
GATE_TTS_VERIFIED = (
    "موتور گفتار: هوش برتر OpenAI وصل و عملکردش تأیید شده است و ویس با صدای طبیعی همان ساخته می‌شود."
)
GATE_TTS_INTERNAL = (
    "فعلاً ویس با صدای موقت داخلی ساخته می‌شود که رباتیک است و صدای نهایی نیست؛ "
    "صدای طبیعی فقط با کلید تأییدشدهٔ هوش برتر فعال می‌شود («پنل مدیریت ← تنظیمات ← کلید هوش برتر»)."
)
GATE_STT_NO_KEY = (
    "تبدیل ویس به متن هنوز فعال نیست؛ هوش برتر وصل نیست. مدیر باید کلید OpenAI را "
    "در «پنل مدیریت ← تنظیمات» وارد کند و عملکرد آن تأیید شود."
)
GATE_STT_UNVERIFIED = (
    "تبدیل ویس به متن هنوز فعال نیست؛ کلید هوش برتر ذخیره شده، اما عملکرد آن در "
    "«پنل مدیریت ← تنظیمات» تأیید نشده است. تا تأیید عملکرد، این قابلیت فعال نمی‌شود."
)
GATE_STT_DISABLED = "قابلیت تبدیل ویس به متن در حال حاضر توسط مدیر غیرفعال شده است."
GATE_GENERIC_DISABLED = "این قابلیت در حال حاضر توسط مدیر غیرفعال شده است."

# --- spec texts (exact) ------------------------------------------------------
FEATS_EXPLAINER = (
    "نام هر دکمه را ویرایش کنید، آن را فعال یا غیرفعال کنید، جایش را در منو بالا و پایین کنید، "
    "دکمه تازه بسازید یا دکمه سفارشی را حذف کنید؛ همه تغییرها بدون ویرایش کد، بلافاصله در منوی ربات "
    "اعمال و برای همیشه ذخیره می‌شوند."
)
FEAT_VISIBLE = "در منوی ربات نمایش داده می‌شود"
FEAT_HIDDEN = "غیرفعال — از منوی ربات پنهان است"

ADS_EXPLAINER = (
    "مسیر یکپارچه تبلیغات: عنوان، عکس و توضیح مختصر را بدهید؛ قالب و متن حرفه‌ای تبلیغ خودکار "
    "ساخته می‌شود و با همان قالب در ربات نمایش داده می‌شود. هر تبلیغ را می‌توانید ویرایش یا متوقف کنید."
)
ADS_EMPTY = (
    "هنوز تبلیغی ساخته نشده است. تبلیغ‌های فعال با برچسب «تبلیغ» بالای نمای ربات نمایش داده می‌شوند."
)
AD_DEFAULT_BUTTON = "مشاهده و اطلاعات بیشتر"

KEY_TITLE = "کلید هوش برتر (OpenAI API Key)"
KEY_EXPLAINER = (
    "صدای طبیعی و انسانیِ متن به ویس فقط با این کلید (پس از «تأیید عملکرد») فعال می‌شود؛ "
    "بدون آن ویس با صدای موقت و رباتیک داخلی ساخته می‌شود. شرط تبدیل ویس به متن هم همین کلید است."
)
KEY_STATUS_VERIFIED = "تأیید شده ✓"
KEY_STATUS_SAVED = "تنظیم شده — تأیید نشده"
KEY_STATUS_MISSING = "تنظیم نشده"
KEY_PROMPT = "sk-… کلید را اینجا بچسبان"
KEY_NOTE = "کلید جدید را از platform.openai.com بساز و اینجا بچسبان؛ کلید را در چت نفرست"

USERS_EMPTY = (
    "هنوز کاربری ثبت نشده است. کاربران ربات را با فرم بالا ثبت کنید تا از اینجا مسدود یا مدیریت شوند."
)

CLEAR_TITLE = "پاک‌سازی وضعیت گفت‌وگو"
CLEAR_EXPLAINER = (
    "وضعیت فعلی گفتگو پاک می‌شود: عملیات جاری، ورودی موقت و مرحله فعلی "
    "ریست می‌شود و به وضعیت اولیه /start برمی‌گردید. "
    "آهنگ‌ها، علاقه‌مندی‌ها، پلی‌لیست‌ها و اطلاعات ذخیره‌شده شما دست نمی‌خورد. "
    "(پیام‌های قبلی ربات فقط در محدوده‌ای که خود تلگرام اجازه می‌دهد حذف می‌شوند.)"
)
CLEAR_SEPARATE = (
    "عملیات جدا: حذف کامل اطلاعات ذخیره‌شده (علاقه‌مندی‌ها، پلی‌لیست‌ها و سابقه جستجو) از دیتابیس — "
    "با پاک‌سازی گفتگو فرق دارد و فقط با تأیید نهایی شما انجام می‌شود."
)
CLEAR_DONE = "گفتگو پاک‌سازی شد؛ پیام‌های ربات (در محدوده مجاز تلگرام) و داده‌های موقت حذف شدند. ✅"

WIPE_TITLE = "حذف کامل اطلاعات من"
WIPE_EXPLAINER = (
    "این عملیات جدا از پاک‌سازی گفتگو است و غیرقابل بازگشت است. فقط اطلاعات ذخیره‌شده خود شما "
    "در دیتابیس حذف می‌شود: همه علاقه‌مندی‌ها، همه پلی‌لیست‌ها و آهنگ‌های داخلشان، و سابقه جستجوی شما. "
    "تنظیمات ربات، بانک هوش برتر و اطلاعات کاربران دیگر دست نمی‌خورد و هیچ فایل بانکی حذف نمی‌شود."
)
WIPE_WORKING = "در حال حذف…"

CORE_FEATURE_KEYS = (
    "song_search", "audio_id", "tts", "link_fetch", "voice_to_text", "help_support",
)


# --- owner guard (local; never imported from app.bot.handlers) ---------------
def _is_owner(event, config) -> bool:
    uid = getattr(getattr(event, "from_user", None), "id", None)
    return config.owner_id is not None and uid == config.owner_id


async def _deny_owner(event) -> None:
    answer = getattr(event, "answer", None)
    if not callable(answer):
        return
    if isinstance(event, CallbackQuery):
        await answer(BRIEF["not_owner"], show_alert=True)
    else:
        await answer(BRIEF["not_owner"])


def _owner_only(handler):
    """Decorator: non-owner callers get one short Persian line, nothing else.

    Root-cause fix (2026-10-29): aiogram may pass kwargs the handler did
    not declare (live crash: ``panel() got an unexpected keyword argument
    'dispatcher'``). The wrapper now filters forwarded kwargs to the
    handler's declared parameters instead of crashing.
    """
    import inspect
    try:
        _params = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        _params = {}
    _accepts_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in _params.values())
    _allowed = set(_params)

    @functools.wraps(handler)
    async def wrapper(event, conn, config, *args, **kwargs):
        if not _is_owner(event, config):
            await _deny_owner(event)
            return None
        if not _accepts_kwargs:
            kwargs = {k: v for k, v in kwargs.items() if k in _allowed}
        return await handler(event, conn, config, *args, **kwargs)
    return wrapper


# --- small UI helpers --------------------------------------------------------
def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_panel_kb() -> InlineKeyboardMarkup:
    return _kb([[_btn("⬅ بازگشت به پنل", "adm:panel")]])


def _cancel_kb() -> InlineKeyboardMarkup:
    """In-bot «لغو» for every admin FSM prompt: clears the stale state and
    lands back on the admin panel. Reuses the existing admin «لغو» style."""
    return _kb([[_btn("لغو", "adm:cancel")]])


async def _edit(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb)


# --- features (buttons) ------------------------------------------------------
def is_custom_feature(key: str) -> bool:
    return key.startswith("custom_")


def add_custom_feature(conn: sqlite3.Connection, name: str, reply_text: str,
                       by_id: int | None = None) -> str:
    name = (name or "").strip()
    reply_text = (reply_text or "").strip()
    if not name:
        raise ValueError(BRIEF["empty"])
    if len(name) > 80:
        raise ValueError(BRIEF["name_too_long"])
    if len(reply_text) > 2000:
        raise ValueError(BRIEF["text_too_long"])
    key = f"custom_{int(time.time() * 1000)}"
    max_order = conn.execute("SELECT COALESCE(MAX(display_order), 0) FROM features").fetchone()[0]
    conn.execute(
        "INSERT INTO features (key, title, display_order, enabled, visible, message) VALUES (?,?,?,?,?,?)",
        (key, name, max_order + 1, 1, 1, reply_text),
    )
    dbmod.audit(conn, by_id, "admin_add_feature", f"{key} {name}")
    conn.commit()
    return key


def rename_feature(conn: sqlite3.Connection, key: str, name: str,
                   by_id: int | None = None) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError(BRIEF["empty"])
    if len(name) > 80:
        raise ValueError(BRIEF["name_too_long"])
    dbmod.set_feature(conn, key, title=name, by_id=by_id)


def move_feature(conn: sqlite3.Connection, key: str, direction: str) -> bool:
    feats = dbmod.list_features(conn)
    idx = next((i for i, f in enumerate(feats) if f["key"] == key), None)
    if idx is None:
        return False
    other = idx - 1 if direction == "up" else idx + 1
    if other < 0 or other >= len(feats):
        return False
    a, b = feats[idx], feats[other]
    conn.execute("UPDATE features SET display_order=? WHERE key=?", (b["display_order"], a["key"]))
    conn.execute("UPDATE features SET display_order=? WHERE key=?", (a["display_order"], b["key"]))
    conn.commit()
    return True


def delete_feature(conn: sqlite3.Connection, key: str, by_id: int | None = None) -> None:
    if key in CORE_FEATURE_KEYS:
        raise ValueError(BRIEF["core_not_deletable"])
    if not is_custom_feature(key):
        raise ValueError(BRIEF["core_not_deletable"])
    conn.execute("DELETE FROM features WHERE key=?", (key,))
    dbmod.audit(conn, by_id, "admin_delete_feature", key)
    conn.commit()


def feats_view(conn: sqlite3.Connection) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🧩 ویرایش دکمه‌ها", "", FEATS_EXPLAINER, ""]
    rows = []
    for f in dbmod.list_features(conn):
        status = FEAT_VISIBLE if f["enabled"] else FEAT_HIDDEN
        custom_mark = " · دکمه سفارشی" if is_custom_feature(f["key"]) else ""
        lines.append(f"• {f['title']} — {status}{custom_mark}")
        btns = [
            _btn("ویرایش", f"adm:feat:rename:{f['key']}"),
            _btn("غیرفعال" if f["enabled"] else "فعال", f"adm:feat:toggle:{f['key']}"),
            _btn("بالا", f"adm:feat:move:{f['key']}:up"),
            _btn("پایین", f"adm:feat:move:{f['key']}:down"),
        ]
        if is_custom_feature(f["key"]):
            btns.append(_btn("حذف دکمه", f"adm:feat:del:{f['key']}"))
        rows.append(btns)
    rows.append([_btn("➕ افزودن دکمه جدید", "adm:feat:add")])
    rows.append([_btn("⬅ بازگشت به پنل", "adm:panel")])
    return "\n".join(lines), _kb(rows)


# --- ads ----------------------------------------------------------------------
def get_ad(conn: sqlite3.Connection, ad_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
    return dict(row) if row else None


def list_ads_full(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM ads ORDER BY id DESC")]


def create_ad(conn: sqlite3.Connection, title: str, text: str, photo_file_id: str = "",
              link: str = "", button_text: str = "", by_id: int | None = None) -> int:
    title = (title or "").strip()
    text = (text or "").strip()
    if not title or not text:
        raise ValueError(BRIEF["empty"])
    cur = conn.execute(
        "INSERT INTO ads (title, text, photo_file_id, link, button_text, enabled, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (title, text, photo_file_id.strip(), link.strip(),
         button_text.strip() or AD_DEFAULT_BUTTON, 1, int(time.time())),
    )
    dbmod.audit(conn, by_id, "admin_add_ad", f"{cur.lastrowid} {title}")
    conn.commit()
    return cur.lastrowid


def update_ad(conn: sqlite3.Connection, ad_id: int, by_id: int | None = None, **fields) -> None:
    allowed = {"title", "text", "photo_file_id", "link", "button_text", "enabled", "views", "clicks"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(ad_id)
    conn.execute(f"UPDATE ads SET {', '.join(sets)} WHERE id=?", vals)
    dbmod.audit(conn, by_id, "admin_update_ad", f"{ad_id} {sorted(fields)}")
    conn.commit()


def delete_ad(conn: sqlite3.Connection, ad_id: int, by_id: int | None = None) -> None:
    conn.execute("DELETE FROM ads WHERE id=?", (ad_id,))
    dbmod.audit(conn, by_id, "admin_delete_ad", str(ad_id))
    conn.commit()


def ad_inc(conn: sqlite3.Connection, ad_id: int, field: str) -> None:
    if field not in ("views", "clicks"):
        return
    conn.execute(f"UPDATE ads SET {field}={field}+1 WHERE id=?", (ad_id,))
    conn.commit()


def ad_render(ad: dict) -> str:
    """Automatic professional template: `📢 تبلیغ\\n{title}\\n{text}`."""
    return f"📢 تبلیغ\n{ad.get('title', '')}\n{ad.get('text', '')}"


def ads_view(conn: sqlite3.Connection) -> tuple[str, InlineKeyboardMarkup]:
    ads = list_ads_full(conn)
    from ..bot.ads import ad_every_n
    every = ad_every_n(conn)
    lines = ["📢 تبلیغات", "",
             f"نمایش خودکار به کاربران: هر {every} پاسخ (به مالک نمایش داده نمی‌شود)",
             "", ADS_EXPLAINER, ""]
    rows = [[_btn("➕ ساخت تبلیغ جدید", "adm:ad:new"),
             _btn("⚙ فاصله نمایش", "adm:ad:every")]]
    if not ads:
        lines.append(ADS_EMPTY)
    for ad in ads:
        state = "فعال" if ad.get("enabled") else "متوقف"
        lines.append(
            f"• {ad.get('title') or '—'}\n"
            f"  نمایش: {ad.get('views', 0)} · کلیک: {ad.get('clicks', 0)} · {state}\n"
            f"  {ad_render(ad)[:200]}"
        )
        rows.append([
            _btn("ویرایش", f"adm:ad:edit:{ad['id']}"),
            _btn("متوقف" if ad.get("enabled") else "فعال", f"adm:ad:toggle:{ad['id']}"),
            _btn("حذف", f"adm:ad:del:{ad['id']}"),
        ])
    rows.append([_btn("⬅ بازگشت به پنل", "adm:panel")])
    return "\n".join(lines), _kb(rows)


AD_EDIT_FIELDS = (
    ("title", "ویرایش عنوان"),
    ("text", "ویرایش متن"),
    ("link", "ویرایش لینک"),
    ("button_text", "ویرایش متن دکمه"),
    ("photo", "ویرایش عکس"),
)
AD_EDIT_PROMPTS = {
    "title": "عنوان تبلیغ جدید را بنویسید:",
    "text": "متن تبلیغ جدید را بنویسید:",
    "link": "لینک جدید (https://…) — برای حذف، «حذف» بنویسید:",
    "button_text": "متن دکمه جدید — خالی یعنی خودکار:",
    "photo": "عکس جدید را بفرستید:",
}


# --- OpenAI key box (never display the value) ----------------------------------
def get_openai_key(conn: sqlite3.Connection, config) -> str:
    """DB setting wins over env — the integrator uses this, never config directly."""
    value = dbmod.get_setting(conn, "openai_api_key", "").strip()
    return value or (getattr(config, "openai_api_key", "") or "").strip()


def is_openai_verified(conn: sqlite3.Connection) -> bool:
    return dbmod.get_setting(conn, "openai_key_verified", "0") == "1"


def set_openai_key(conn: sqlite3.Connection, key: str, by_id: int | None = None) -> None:
    key = (key or "").strip()
    if not key:
        raise ValueError(BRIEF["empty"])
    dbmod.set_setting(conn, "openai_api_key", key, by_id=by_id)
    dbmod.set_setting(conn, "openai_key_verified", "0", by_id=by_id)


def set_key_verified(conn: sqlite3.Connection, verified: bool,
                     by_id: int | None = None) -> None:
    dbmod.set_setting(conn, "openai_key_verified", "1" if verified else "0", by_id=by_id)


def key_status(conn: sqlite3.Connection, config) -> str:
    if is_openai_verified(conn):
        return KEY_STATUS_VERIFIED
    if get_openai_key(conn, config):
        return KEY_STATUS_SAVED
    return KEY_STATUS_MISSING


async def openai_models_check(key: str) -> tuple[bool, str]:
    """Real GET https://api.openai.com/v1/models with the key (20s timeout).

    Returns (ok, detail). detail is a short honest Persian line on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
    except Exception:
        return False, "بررسی کلید ممکن نشد؛ اتصال به OpenAI برقرار نشد."
    if resp.status_code == 200:
        return True, "ok"
    if resp.status_code == 401:
        return False, "کلید معتبر نیست (خطای احراز هویت)."
    return False, f"تأیید کلید ناموفق بود (کد {resp.status_code})."


def keybox_view(conn: sqlite3.Connection, config) -> tuple[str, InlineKeyboardMarkup]:
    status = key_status(conn, config)
    text = "\n".join([KEY_TITLE, "", KEY_EXPLAINER, "", f"وضعیت: {status}"])
    kb = _kb([
        [_btn("💾 ذخیره کلید", "adm:key:save"), _btn("🔍 تأیید عملکرد کلید", "adm:key:verify")],
        [_btn("⬅ بازگشت به پنل", "adm:panel")],
    ])
    return text, kb


# --- users --------------------------------------------------------------------
def delete_user(conn: sqlite3.Connection, telegram_id: int,
                by_id: int | None = None) -> None:
    conn.execute("DELETE FROM users WHERE telegram_id=?", (telegram_id,))
    dbmod.audit(conn, by_id, "admin_delete_user", str(telegram_id))
    conn.commit()


def users_view(conn: sqlite3.Connection) -> tuple[str, InlineKeyboardMarkup]:
    users = dbmod.list_users(conn, limit=50)
    lines = ["👥 کاربران", ""]
    rows = []
    if not users:
        lines.append(USERS_EMPTY)
    for u in users:
        mark = " ⛔ مسدود" if u.get("blocked") else ""
        lines.append(f"• {u['telegram_id']} {u.get('username') or ''}{mark}")
        rows.append([
            _btn("رفع مسدودی" if u.get("blocked") else "مسدود کردن",
                 f"adm:user:block:{u['telegram_id']}"),
            _btn("حذف", f"adm:user:del:{u['telegram_id']}"),
        ])
    rows.append([_btn("⬅ بازگشت به پنل", "adm:panel")])
    return "\n".join(lines), _kb(rows)


# --- settings -----------------------------------------------------------------
SETTINGS_FIELDS = (
    ("welcome_message", "متن خوش‌آمد و شروع (/start)"),
    ("help_message", "متن راهنما (/help)"),
    ("support_text", "متن پشتیبانی"),
    ("not_found_text", "متن «نتیجه‌ای پیدا نشد»"),
    ("bot_id", "آیدی یا لینک ربات (در صفحه ربات نمایش داده نمی‌شود؛ هنگام ارسال یا اشتراک‌گذاری محتوا، خودکار به آن اضافه می‌شود)"),
    ("channel_usernames", "چنل‌های تلگرام موتور دوم (یوزرنیم هر چنل عمومی، هرکدام در یک خط؛ وقتی موتور اصلی آهنگی را پیدا نکرد، پست‌های این چنل‌ها جستجو می‌شود)"),
    ("suno_sites", "سایت‌های آهنگ هوش برتر برای بانک (دامنه هر سایت در یک خط؛ فعلاً فقط suno.com از نظر فنی فعال است — آهنگ‌های عمومی آن در جست‌وجوی دوم بانک با اثر انگشت صوتی مقایسه می‌شوند)"),
    ("audd_token", "کلید AudD (اختیاری، برای شناسایی آهنگ از روی صدای ویدیو)"),
)
SENSITIVE_SETTING_KEYS = {"audd_token"}


def settings_view(conn: sqlite3.Connection) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["⚙️ تنظیمات", ""]
    rows = []
    for key, label in SETTINGS_FIELDS:
        if key in SENSITIVE_SETTING_KEYS:
            shown = "تنظیم شده" if dbmod.get_setting(conn, key, "") else "تنظیم نشده"
        else:
            val = dbmod.get_setting(conn, key, "")
            shown = (val[:60] + "…") if len(val) > 60 else (val or "—")
        lines.append(f"• {label}\n  مقدار: {shown}")
        rows.append([_btn(f"ویرایش: {label[:24]}", f"adm:set:{key}")])
    rows.append([_btn("⬅ بازگشت به پنل", "adm:panel")])
    return "\n".join(lines), _kb(rows)


def setting_label(key: str) -> str:
    return next((label for k, label in SETTINGS_FIELDS if k == key), key)


# --- outbox / clear-chat --------------------------------------------------------
def track(conn: sqlite3.Connection, user_id: int, chat_id: int, message_id: int) -> None:
    """Record a message (outgoing bot message or incoming user message) so
    «پاک‌سازی گفتگو» can delete it later. Favorites, playlists, history,
    settings and the bank are never stored here and never deleted by it."""
    conn.execute(
        "INSERT INTO outbox (user_id, chat_id, message_id, created_at) VALUES (?,?,?,?)",
        (user_id, chat_id, message_id, int(time.time())),
    )
    conn.commit()


def list_outbox(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM outbox WHERE user_id=? ORDER BY id", (user_id,))]


async def delete_tracked_messages(bot, rows: list[dict]) -> tuple[int, int]:
    """Delete tracked chat messages, counting real outcomes.

    Returns (deleted, failed). A failure (e.g. Telegram's 48-hour delete
    limit) is counted, never silently claimed as deleted and never a
    reason to abort the remaining deletions.
    """
    deleted = failed = 0
    for row in rows:
        try:
            await bot.delete_message(row["chat_id"], row["message_id"])
            deleted += 1
        except Exception:
            failed += 1
    return deleted, failed


def clear_chat_data(conn: sqlite3.Connection, user_id: int) -> int:
    """Delete ONLY the user's tracked bot messages + temp cache.

    search_cache has no per-user keys in this codebase, so the temp cache is
    cleared globally (it is regenerable cache, not user data). Favorites,
    playlists, search history, settings and the bank are never touched.
    """
    count = conn.execute("SELECT COUNT(*) FROM outbox WHERE user_id=?",
                         (user_id,)).fetchone()[0]
    conn.execute("DELETE FROM outbox WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM search_cache")
    conn.commit()
    return count


def clear_view() -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join([CLEAR_TITLE, "", CLEAR_EXPLAINER, "", CLEAR_SEPARATE])
    kb = _kb([
        [_btn("بله، پیام‌ها را پاک کن", "adm:clear:yes"), _btn("لغو", "adm:clear:no")],
        [_btn("🗑 حذف کامل اطلاعات من", "adm:wipe")],
    ])
    return text, kb


# --- delete-all-my-data ---------------------------------------------------------
def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _user_column(conn: sqlite3.Connection, table: str) -> str | None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for cand in ("telegram_id", "user_id"):
        if cand in cols:
            return cand
    return None


def wipe_user_data(conn: sqlite3.Connection, user_id: int) -> tuple[int, int, int]:
    """Delete ONLY this user's favorites, playlists(+items), search_history
    and download jobs. Returns (favorites, playlists, history) counts.
    Never touches the bank, settings, or other users.
    """
    favs = conn.execute("SELECT COUNT(*) FROM favorites WHERE telegram_id=?",
                        (user_id,)).fetchone()[0]
    playlists = conn.execute("SELECT COUNT(*) FROM playlists WHERE telegram_id=?",
                             (user_id,)).fetchone()[0]
    history = conn.execute("SELECT COUNT(*) FROM search_history WHERE telegram_id=?",
                           (user_id,)).fetchone()[0]
    conn.execute("DELETE FROM favorites WHERE telegram_id=?", (user_id,))
    pl_ids = [r[0] for r in conn.execute(
        "SELECT id FROM playlists WHERE telegram_id=?", (user_id,))]
    for pid in pl_ids:
        conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
    conn.execute("DELETE FROM playlists WHERE telegram_id=?", (user_id,))
    conn.execute("DELETE FROM search_history WHERE telegram_id=?", (user_id,))
    # download jobs table belongs to another feature module; delete only
    # when present, using whichever user column it actually has.
    for table in ("download_jobs", "downloads"):
        if _table_exists(conn, table):
            col = _user_column(conn, table)
            if col:
                conn.execute(f"DELETE FROM {table} WHERE {col}=?", (user_id,))
    conn.commit()
    return favs, playlists, history


def wipe_view() -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join([WIPE_TITLE, "", WIPE_EXPLAINER])
    kb = _kb([
        [_btn("بله، اطلاعاتم را کامل حذف کن", "adm:wipe:yes"), _btn("لغو", "adm:wipe:no")],
    ])
    return text, kb


# --- FSM states (adm:*) ----------------------------------------------------------
class AdmStates(StatesGroup):
    feat_rename = State(state="adm:feat:rename")
    feat_add_name = State(state="adm:feat:add_name")
    feat_add_text = State(state="adm:feat:add_text")
    ad_title = State(state="adm:ad:title")
    ad_photo = State(state="adm:ad:photo")
    ad_desc = State(state="adm:ad:desc")
    ad_link = State(state="adm:ad:link")
    ad_button = State(state="adm:ad:button")
    ad_edit = State(state="adm:ad:edit")
    ad_every = State(state="adm:ad:every")
    key_input = State(state="adm:key")
    set_value = State(state="adm:set:value")


AD_FLOW = [
    (AdmStates.ad_title, "عنوان تبلیغ را بنویسید:", "title", False),
    (AdmStates.ad_photo, "عکس تبلیغ (اختیاری) — عکس را بفرستید یا «رد شدن» را بزنید:", "photo", True),
    (AdmStates.ad_desc, "توضیح مختصر — چند کلمه کافی است؛ مثلاً: فروشگاه پوشاک با ارسال سریع به سراسر کشور", "text", False),
    (AdmStates.ad_link, "لینک (https://…) — اختیاری:", "link", True),
    (AdmStates.ad_button, "متن دکمه (اختیاری؛ خالی یعنی خودکار):", "button_text", True),
]


def _panel_view(conn: sqlite3.Connection, config) -> tuple[str, InlineKeyboardMarkup]:
    status = key_status(conn, config)
    lines = ["🛡 پنل مدیریت", f"🧠 کلید هوش برتر: {status}", ""]
    from . import watermark as _wm
    _wm_state = "فعال" if _wm.is_enabled(conn) else "غیرفعال — به‌زودی"
    kb = _kb([
        [_btn("🧩 ویرایش دکمه‌ها", "adm:feats")],
        [_btn("📢 تبلیغات", "adm:ads")],
        [_btn("🧠 کلید هوش برتر", "adm:keybox")],
        [_btn("👥 کاربران", "adm:users")],
        [_btn("⚙️ تنظیمات", "adm:settings")],
        [_btn(f"🖼 حذف نوشته روی ویدیو: {_wm_state}", "adm:watermark")],
    ])
    return "\n".join(lines), kb


def _watermark_view(conn: sqlite3.Connection) -> tuple[str, InlineKeyboardMarkup]:
    from . import watermark as _wm
    en = _wm.is_enabled(conn)
    lines = [
        "🖼 حذف نوشته و شناسه روی ویدیو",
        "",
        f"وضعیت: {'فعال' if en else 'غیرفعال — به‌زودی'}",
        "",
        "در نسخه فعلی این قابلیت غیرفعال است و هیچ پردازشی انجام نمی‌شود؛ "
        "دانلود عادی ویدیو کاملاً مستقل از آن کار می‌کند. فعال‌سازی آینده "
        "فقط با همین تنظیم صریح مدیر ممکن است.",
    ]
    rows = []
    rows.append([_btn("⏸ غیرفعال‌سازی", "adm:watermark:off")] if en
                else [_btn("✅ فعال‌سازی", "adm:watermark:on")])
    rows.append([_btn("⬅ بازگشت به پنل", "adm:panel")])
    return "\n".join(lines), _kb(rows)


def register(router: Router) -> None:
    ar = Router(name="admin")

    # -- panel ------------------------------------------------------------------
    @ar.callback_query(F.data == "adm:panel")
    @_owner_only
    async def panel(callback: CallbackQuery, conn: sqlite3.Connection, config):
        await _edit(callback, *_panel_view(conn, config))

    @ar.callback_query(F.data == "adm:cancel")
    @_owner_only
    async def adm_cancel(callback: CallbackQuery, conn: sqlite3.Connection,
                         config, state: FSMContext):
        """Cancel any admin FSM prompt: clear the stale state and land back
        on the admin panel. Every adm:* awaiting prompt offers this."""
        await state.clear()
        await _edit(callback, *_panel_view(conn, config))

    # -- watermark-removal stub (disabled by default) ---------------------------
    @ar.callback_query(F.data == "adm:watermark")
    @_owner_only
    async def watermark_card(callback: CallbackQuery, conn: sqlite3.Connection, config):
        await _edit(callback, *_watermark_view(conn))

    @ar.callback_query(F.data == "adm:watermark:on")
    @_owner_only
    async def watermark_on(callback: CallbackQuery, conn: sqlite3.Connection, config):
        from . import watermark as _wm
        _wm.set_enabled(conn, True, by_id=callback.from_user.id)
        await callback.answer("فعال شد.")
        await _edit(callback, *_watermark_view(conn))

    @ar.callback_query(F.data == "adm:watermark:off")
    @_owner_only
    async def watermark_off(callback: CallbackQuery, conn: sqlite3.Connection, config):
        from . import watermark as _wm
        _wm.set_enabled(conn, False, by_id=callback.from_user.id)
        await callback.answer("غیرفعال شد.")
        await _edit(callback, *_watermark_view(conn))

    # -- features (buttons) ------------------------------------------------------
    @ar.callback_query(F.data == "adm:feats")
    @_owner_only
    async def feats(callback: CallbackQuery, conn: sqlite3.Connection, config):
        await _edit(callback, *feats_view(conn))

    @ar.callback_query(F.data.startswith("adm:feat:toggle:"))
    @_owner_only
    async def feat_toggle(callback: CallbackQuery, conn: sqlite3.Connection, config):
        key = callback.data.split(":", 3)[3]
        feat = dbmod.get_feature(conn, key)
        if feat is None:
            await callback.answer(BRIEF["not_found"], show_alert=True)
            return
        dbmod.set_feature(conn, key, enabled=not bool(feat["enabled"]),
                          by_id=callback.from_user.id)
        await _edit(callback, *feats_view(conn))

    @ar.callback_query(F.data.startswith("adm:feat:move:"))
    @_owner_only
    async def feat_move(callback: CallbackQuery, conn: sqlite3.Connection, config):
        _, _, _, rest = callback.data.split(":", 3)
        key, _, direction = rest.partition(":")
        move_feature(conn, key, direction if direction in ("up", "down") else "up")
        await _edit(callback, *feats_view(conn))

    @ar.callback_query(F.data.startswith("adm:feat:rename:"))
    @_owner_only
    async def feat_rename_start(callback: CallbackQuery, conn: sqlite3.Connection,
                                config, state: FSMContext):
        key = callback.data.split(":", 3)[3]
        feat = dbmod.get_feature(conn, key)
        if feat is None:
            await callback.answer(BRIEF["not_found"], show_alert=True)
            return
        await state.set_state(AdmStates.feat_rename)
        await state.update_data(feat_key=key)
        await callback.answer()
        await callback.message.answer(
            f"نام فعلی: {feat['title']}\nنام جدید دکمه را بنویسید (حداکثر ۸۰ کاراکتر):",
            reply_markup=_cancel_kb())

    @ar.message(AdmStates.feat_rename, F.text)
    @menu_escape
    @_owner_only
    async def feat_rename_save(message: Message, conn: sqlite3.Connection,
                               config, state: FSMContext):
        data = await state.get_data()
        try:
            rename_feature(conn, data.get("feat_key", ""), message.text,
                           by_id=message.from_user.id)
        except ValueError as e:
            await message.answer(str(e))
            return
        await state.clear()
        text, kb = feats_view(conn)
        await message.answer("✅ نام دکمه ذخیره شد.", reply_markup=kb)

    @ar.callback_query(F.data == "adm:feat:add")
    @_owner_only
    async def feat_add_start(callback: CallbackQuery, conn: sqlite3.Connection,
                             config, state: FSMContext):
        await state.set_state(AdmStates.feat_add_name)
        await callback.answer()
        await callback.message.answer("نام دکمه (مثلاً: قوانین استفاده)",
                                        reply_markup=_cancel_kb())

    @ar.message(AdmStates.feat_add_name, F.text)
    @menu_escape
    @_owner_only
    async def feat_add_name(message: Message, conn: sqlite3.Connection,
                            config, state: FSMContext):
        name = (message.text or "").strip()
        if not name:
            await message.answer(BRIEF["empty"])
            return
        if len(name) > 80:
            await message.answer(BRIEF["name_too_long"])
            return
        await state.update_data(feat_name=name)
        await state.set_state(AdmStates.feat_add_text)
        await message.answer("متن پاسخ دکمه (وقتی کاربر آن را می‌زند)",
                             reply_markup=_cancel_kb())

    @ar.message(AdmStates.feat_add_text, F.text)
    @menu_escape
    @_owner_only
    async def feat_add_text(message: Message, conn: sqlite3.Connection,
                            config, state: FSMContext):
        data = await state.get_data()
        try:
            key = add_custom_feature(conn, data.get("feat_name", ""), message.text,
                                     by_id=message.from_user.id)
        except ValueError as e:
            await message.answer(str(e))
            return
        await state.clear()
        text, kb = feats_view(conn)
        await message.answer(f"✅ دکمه جدید به منوی ربات اضافه شد ({key}).", reply_markup=kb)

    @ar.callback_query(F.data.startswith("adm:feat:del:"))
    @_owner_only
    async def feat_del_confirm(callback: CallbackQuery, conn: sqlite3.Connection, config):
        key = callback.data.split(":", 3)[3]
        feat = dbmod.get_feature(conn, key)
        if feat is None:
            await callback.answer(BRIEF["not_found"], show_alert=True)
            return
        kb = _kb([
            [_btn("تأیید حذف دکمه", f"adm:feat:del:yes:{key}"), _btn("لغو", "adm:feats")],
        ])
        await _edit(callback, f"«{feat['title']}» برای همیشه از منوی ربات حذف شود؟", kb)

    @ar.callback_query(F.data.startswith("adm:feat:del:yes:"))
    @_owner_only
    async def feat_del_yes(callback: CallbackQuery, conn: sqlite3.Connection, config):
        key = callback.data.split(":", 4)[4]
        try:
            delete_feature(conn, key, by_id=callback.from_user.id)
        except ValueError as e:
            await callback.answer(str(e), show_alert=True)
            return
        await _edit(callback, *feats_view(conn))

    # -- ads ---------------------------------------------------------------------
    @ar.callback_query(F.data == "adm:ads")
    @_owner_only
    async def ads(callback: CallbackQuery, conn: sqlite3.Connection, config):
        await _edit(callback, *ads_view(conn))

    @ar.callback_query(F.data == "adm:ad:new")
    @_owner_only
    async def ad_new_start(callback: CallbackQuery, conn: sqlite3.Connection,
                           config, state: FSMContext):
        await state.set_state(AdmStates.ad_title)
        await state.update_data(ad={})
        await callback.answer()
        await callback.message.answer(AD_FLOW[0][1],
                                        reply_markup=_cancel_kb())

    @ar.message(AdmStates.ad_photo, F.photo)
    @_owner_only
    async def ad_photo_got(message: Message, conn: sqlite3.Connection,
                           config, state: FSMContext):
        await _ad_store_and_advance(message, conn, state, "photo",
                                    message.photo[-1].file_id)

    @ar.callback_query(F.data == "adm:ad:skip")
    @_owner_only
    async def ad_skip(callback: CallbackQuery, conn: sqlite3.Connection,
                      config, state: FSMContext):
        cur = await state.get_state()
        step = next((s for s in AD_FLOW if s[0].state == cur), None)
        if step is None or not step[3]:
            await callback.answer(BRIEF["not_found"], show_alert=True)
            return
        by_id = callback.from_user.id if callback.from_user else None
        await _ad_store_and_advance(callback.message, conn, state, step[2], "",
                                    by_id=by_id)
        await callback.answer()

    async def _ad_store_and_advance(msg, conn, state: FSMContext, field: str,
                                    value: str, by_id=None):
        data = await state.get_data()
        ad = data.get("ad", {})
        ad[field] = value
        await state.update_data(ad=ad)
        idx = next(i for i, s in enumerate(AD_FLOW) if s[2] == field)
        if idx + 1 < len(AD_FLOW):
            nxt = AD_FLOW[idx + 1]
            await state.set_state(nxt[0])
            _rows = [[_btn("رد شدن", "adm:ad:skip")]] if nxt[3] else []
            _rows.append([_btn("لغو", "adm:cancel")])
            kb = _kb(_rows)
            await msg.answer(nxt[1], reply_markup=kb)
        else:
            await _finish_ad(msg, conn, state, by_id)

    async def _finish_ad(msg, conn, state: FSMContext, by_id=None):
        data = await state.get_data()
        ad = data.get("ad", {})
        try:
            ad_id = create_ad(conn, ad.get("title", ""), ad.get("text", ""),
                              ad.get("photo", ""), ad.get("link", ""),
                              ad.get("button_text", "") or AD_DEFAULT_BUTTON,
                              by_id=by_id)
        except ValueError as e:
            await msg.answer(str(e))
            return
        await state.clear()
        saved = get_ad(conn, ad_id)
        kb = _kb([[_btn("⬅ بازگشت به تبلیغات", "adm:ads")]])
        await msg.answer(f"✅ تبلیغ ساخته شد.\n\n{ad_render(saved)}", reply_markup=kb)

    @ar.message(AdmStates.ad_title, F.text)
    @menu_escape
    @_owner_only
    async def ad_title_got(message: Message, conn: sqlite3.Connection,
                           config, state: FSMContext):
        title = (message.text or "").strip()
        if not title:
            await message.answer(BRIEF["empty"])
            return
        data = await state.get_data()
        ad = data.get("ad", {})
        ad["title"] = title
        await state.update_data(ad=ad)
        await state.set_state(AdmStates.ad_photo)
        await message.answer(AD_FLOW[1][1],
                             reply_markup=_kb([[_btn("رد شدن", "adm:ad:skip")],
                                               [_btn("لغو", "adm:cancel")]]))

    @ar.message(AdmStates.ad_desc, F.text)
    @menu_escape
    @_owner_only
    async def ad_desc_got(message: Message, conn: sqlite3.Connection,
                          config, state: FSMContext):
        text = (message.text or "").strip()
        if not text:
            await message.answer(BRIEF["empty"])
            return
        data = await state.get_data()
        ad = data.get("ad", {})
        ad["text"] = text
        await state.update_data(ad=ad)
        await state.set_state(AdmStates.ad_link)
        await message.answer(AD_FLOW[3][1],
                             reply_markup=_kb([[_btn("رد شدن", "adm:ad:skip")],
                                               [_btn("لغو", "adm:cancel")]]))

    @ar.message(AdmStates.ad_link, F.text)
    @menu_escape
    @_owner_only
    async def ad_link_got(message: Message, conn: sqlite3.Connection,
                          config, state: FSMContext):
        data = await state.get_data()
        ad = data.get("ad", {})
        ad["link"] = (message.text or "").strip()
        await state.update_data(ad=ad)
        await state.set_state(AdmStates.ad_button)
        await message.answer(AD_FLOW[4][1],
                             reply_markup=_kb([[_btn("رد شدن", "adm:ad:skip")],
                                               [_btn("لغو", "adm:cancel")]]))

    @ar.message(AdmStates.ad_button, F.text)
    @menu_escape
    @_owner_only
    async def ad_button_got(message: Message, conn: sqlite3.Connection,
                            config, state: FSMContext):
        data = await state.get_data()
        ad = data.get("ad", {})
        ad["button_text"] = (message.text or "").strip() or AD_DEFAULT_BUTTON
        await state.update_data(ad=ad)
        by_id = message.from_user.id if message.from_user else None
        await _finish_ad(message, conn, state, by_id=by_id)

    @ar.callback_query(F.data == "adm:ad:every")
    @_owner_only
    async def ad_every_start(callback: CallbackQuery, conn: sqlite3.Connection,
                             config, state: FSMContext):
        from ..bot.ads import ad_every_n
        await state.set_state(AdmStates.ad_every)
        await callback.answer()
        await callback.message.answer(
            f"تبلیغ هر چند پاسخ به کاربران نمایش داده شود؟ (فعلی: هر {ad_every_n(conn)} پاسخ)\n"
            "یک عدد بین 1 تا 50 بنویسید:",
            reply_markup=_cancel_kb())

    @ar.message(AdmStates.ad_every)
    @menu_escape
    @_owner_only
    async def ad_every_got(message: Message, conn: sqlite3.Connection,
                           config, state: FSMContext):
        raw = (message.text or "").strip()
        try:
            n = int(raw)
        except ValueError:
            n = 0
        if not 1 <= n <= 50:
            await message.answer("عدد معتبر نیست؛ یک عدد بین 1 تا 50 بنویسید:")
            return
        dbmod.set_setting(conn, "ad_every_n", str(n), by_id=message.from_user.id)
        await state.clear()
        text, kb = ads_view(conn)
        await message.answer(f"✅ فاصله نمایش تبلیغ شد: هر {n} پاسخ.", reply_markup=kb)

    @ar.callback_query(F.data.startswith("adm:ad:toggle:"))
    @_owner_only
    async def ad_toggle(callback: CallbackQuery, conn: sqlite3.Connection, config):
        try:
            ad_id = int(callback.data.split(":", 3)[3])
        except ValueError:
            await callback.answer(BRIEF["bad_id"], show_alert=True)
            return
        ad = get_ad(conn, ad_id)
        if ad is None:
            await callback.answer(BRIEF["not_found"], show_alert=True)
            return
        dbmod.set_ad_enabled(conn, ad_id, not bool(ad["enabled"]),
                             by_id=callback.from_user.id)
        await _edit(callback, *ads_view(conn))

    @ar.callback_query(F.data.startswith("adm:ad:del:"))
    @_owner_only
    async def ad_del_confirm(callback: CallbackQuery, conn: sqlite3.Connection, config):
        try:
            ad_id = int(callback.data.split(":", 3)[3])
        except ValueError:
            await callback.answer(BRIEF["bad_id"], show_alert=True)
            return
        ad = get_ad(conn, ad_id)
        if ad is None:
            await callback.answer(BRIEF["not_found"], show_alert=True)
            return
        kb = _kb([
            [_btn("تأیید حذف", f"adm:ad:del:yes:{ad_id}"), _btn("لغو", "adm:ads")],
        ])
        await _edit(callback, f"«{ad.get('title') or 'تبلیغ'}» حذف شود؟", kb)

    @ar.callback_query(F.data.startswith("adm:ad:del:yes:"))
    @_owner_only
    async def ad_del_yes(callback: CallbackQuery, conn: sqlite3.Connection, config):
        try:
            ad_id = int(callback.data.split(":", 4)[4])
        except ValueError:
            await callback.answer(BRIEF["bad_id"], show_alert=True)
            return
        delete_ad(conn, ad_id, by_id=callback.from_user.id)
        await _edit(callback, *ads_view(conn))

    @ar.callback_query(F.data.startswith("adm:ad:edit:"))
    @_owner_only
    async def ad_edit_menu(callback: CallbackQuery, conn: sqlite3.Connection, config):
        try:
            ad_id = int(callback.data.split(":", 3)[3])
        except ValueError:
            await callback.answer(BRIEF["bad_id"], show_alert=True)
            return
        ad = get_ad(conn, ad_id)
        if ad is None:
            await callback.answer(BRIEF["not_found"], show_alert=True)
            return
        rows = [[_btn(label, f"adm:ad:editfield:{ad_id}:{field}")]
                for field, label in AD_EDIT_FIELDS]
        rows.append([_btn("⬅ بازگشت به تبلیغات", "adm:ads")])
        await _edit(callback, f"ویرایش تبلیغ «{ad.get('title') or ''}»:", _kb(rows))

    @ar.callback_query(F.data.startswith("adm:ad:editfield:"))
    @_owner_only
    async def ad_edit_start(callback: CallbackQuery, conn: sqlite3.Connection,
                            config, state: FSMContext):
        parts = callback.data.split(":", 4)
        try:
            ad_id = int(parts[3])
        except (ValueError, IndexError):
            await callback.answer(BRIEF["bad_id"], show_alert=True)
            return
        field = parts[4] if len(parts) > 4 else ""
        if field not in AD_EDIT_PROMPTS or get_ad(conn, ad_id) is None:
            await callback.answer(BRIEF["not_found"], show_alert=True)
            return
        await state.set_state(AdmStates.ad_edit)
        await state.update_data(ad_edit_id=ad_id, ad_edit_field=field)
        await callback.answer()
        await callback.message.answer(AD_EDIT_PROMPTS[field],
                                        reply_markup=_cancel_kb())

    @ar.message(AdmStates.ad_edit)
    @menu_escape
    @_owner_only
    async def ad_edit_save(message: Message, conn: sqlite3.Connection,
                           config, state: FSMContext):
        data = await state.get_data()
        ad_id = data.get("ad_edit_id")
        field = data.get("ad_edit_field")
        ad = get_ad(conn, ad_id) if ad_id else None
        if ad is None or field not in AD_EDIT_PROMPTS:
            await state.clear()
            await message.answer(BRIEF["not_found"])
            return
        if field == "photo":
            if not message.photo:
                await message.answer("عکس را بفرستید.")
                return
            value = message.photo[-1].file_id
        else:
            value = (message.text or "").strip()
            if field == "link" and value == "حذف":
                value = ""
            if field in ("title", "text") and not value:
                await message.answer(BRIEF["empty"])
                return
            if field == "button_text" and not value:
                value = AD_DEFAULT_BUTTON
        db_field = "photo_file_id" if field == "photo" else field
        update_ad(conn, ad_id, by_id=message.from_user.id, **{db_field: value})
        await state.clear()
        await message.answer("✅ ذخیره شد.", reply_markup=_kb([[_btn("⬅ بازگشت به تبلیغات", "adm:ads")]]))

    # -- OpenAI key box ----------------------------------------------------------
    @ar.callback_query(F.data == "adm:keybox")
    @_owner_only
    async def keybox(callback: CallbackQuery, conn: sqlite3.Connection, config):
        await _edit(callback, *keybox_view(conn, config))

    @ar.callback_query(F.data == "adm:key:save")
    @_owner_only
    async def key_save_start(callback: CallbackQuery, conn: sqlite3.Connection,
                             config, state: FSMContext):
        await state.set_state(AdmStates.key_input)
        await callback.answer()
        await callback.message.answer(f"{KEY_PROMPT}\n{KEY_NOTE}",
                                        reply_markup=_cancel_kb())

    @ar.message(AdmStates.key_input, F.text)
    @menu_escape
    @_owner_only
    async def key_save(message: Message, conn: sqlite3.Connection,
                       config, state: FSMContext):
        key = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        try:
            set_openai_key(conn, key, by_id=message.from_user.id)
        except ValueError as e:
            await state.clear()
            await message.answer(str(e))
            return
        await state.clear()
        # The value is never shown — only the state line.
        await message.answer(f"کلید ذخیره شد ✓\nوضعیت: {KEY_STATUS_SAVED}",
                             reply_markup=_back_panel_kb())

    @ar.callback_query(F.data == "adm:key:verify")
    @_owner_only
    async def key_verify(callback: CallbackQuery, conn: sqlite3.Connection, config):
        key = get_openai_key(conn, config)
        if not key:
            await callback.answer("کلیدی تنظیم نشده است.", show_alert=True)
            return
        await callback.answer("در حال بررسی عملکرد کلید…")
        ok, detail = await openai_models_check(key)
        if ok:
            set_key_verified(conn, True, by_id=callback.from_user.id)
            text, kb = keybox_view(conn, config)
            await callback.message.edit_text(text, reply_markup=kb)
            await callback.answer("کلید تأیید شد ✓")
        else:
            # Honest failure: stays unverified; dependent features stay gated.
            await callback.answer(detail, show_alert=True)

    # -- users -------------------------------------------------------------------
    @ar.callback_query(F.data == "adm:users")
    @_owner_only
    async def users(callback: CallbackQuery, conn: sqlite3.Connection, config):
        await _edit(callback, *users_view(conn))

    @ar.callback_query(F.data.startswith("adm:user:block:"))
    @_owner_only
    async def user_block(callback: CallbackQuery, conn: sqlite3.Connection, config):
        try:
            uid = int(callback.data.split(":", 3)[3])
        except ValueError:
            await callback.answer(BRIEF["bad_id"], show_alert=True)
            return
        user = dbmod.get_user(conn, uid)
        if user is None:
            await callback.answer(BRIEF["not_found"], show_alert=True)
            return
        dbmod.set_user_blocked(conn, uid, not bool(user.get("blocked")),
                               by_id=callback.from_user.id)
        await _edit(callback, *users_view(conn))

    @ar.callback_query(F.data.startswith("adm:user:del:"))
    @_owner_only
    async def user_del_confirm(callback: CallbackQuery, conn: sqlite3.Connection, config):
        try:
            uid = int(callback.data.split(":", 3)[3])
        except ValueError:
            await callback.answer(BRIEF["bad_id"], show_alert=True)
            return
        kb = _kb([
            [_btn("تأیید حذف", f"adm:user:del:yes:{uid}"), _btn("لغو", "adm:users")],
        ])
        await _edit(callback, f"کاربر {uid} حذف شود؟", kb)

    @ar.callback_query(F.data.startswith("adm:user:del:yes:"))
    @_owner_only
    async def user_del_yes(callback: CallbackQuery, conn: sqlite3.Connection, config):
        try:
            uid = int(callback.data.split(":", 4)[4])
        except ValueError:
            await callback.answer(BRIEF["bad_id"], show_alert=True)
            return
        delete_user(conn, uid, by_id=callback.from_user.id)
        await _edit(callback, *users_view(conn))

    # -- settings ------------------------------------------------------------------
    @ar.callback_query(F.data == "adm:settings")
    @_owner_only
    async def settings(callback: CallbackQuery, conn: sqlite3.Connection, config):
        await _edit(callback, *settings_view(conn))

    @ar.callback_query(F.data.startswith("adm:set:"))
    @_owner_only
    async def set_start(callback: CallbackQuery, conn: sqlite3.Connection,
                        config, state: FSMContext):
        key = callback.data.split(":", 2)[2]
        if key not in [k for k, _ in SETTINGS_FIELDS]:
            await callback.answer(BRIEF["not_found"], show_alert=True)
            return
        await state.set_state(AdmStates.set_value)
        await state.update_data(set_key=key)
        await callback.answer()
        await callback.message.answer(f"{setting_label(key)}\nمقدار جدید را بنویسید:",
                                        reply_markup=_cancel_kb())

    @ar.message(AdmStates.set_value, F.text)
    @menu_escape
    @_owner_only
    async def set_save(message: Message, conn: sqlite3.Connection,
                       config, state: FSMContext):
        data = await state.get_data()
        key = data.get("set_key", "")
        if key not in [k for k, _ in SETTINGS_FIELDS]:
            await state.clear()
            await message.answer(BRIEF["not_found"])
            return
        dbmod.set_setting(conn, key, (message.text or "").strip(),
                          by_id=message.from_user.id)
        await state.clear()
        await message.answer("✅ ذخیره شد.", reply_markup=_kb([[_btn("⬅ بازگشت به تنظیمات", "adm:settings")]]))

    # -- clear-chat (every user, not just the owner) -------------------------------
    @ar.callback_query(F.data == "adm:clear")
    async def clear_card(callback: CallbackQuery, conn: sqlite3.Connection, config):
        await _edit(callback, *clear_view())

    @ar.callback_query(F.data == "adm:clear:no")
    async def clear_cancel(callback: CallbackQuery, conn: sqlite3.Connection, config):
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            await callback.message.edit_text("لغو شد.")

    @ar.callback_query(F.data == "adm:clear:yes")
    async def clear_yes(callback: CallbackQuery, conn: sqlite3.Connection,
                        config, state: FSMContext):
        user_id = callback.from_user.id
        rows = list_outbox(conn, user_id)
        deleted, failed = await delete_tracked_messages(callback.bot, rows)
        clear_chat_data(conn, user_id)
        await state.clear()
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass
        # Honest result: never a fake success message.
        if not rows:
            text = "پیامی برای پاک‌سازی پیدا نشد."
        elif failed:
            text = (f"✅ {deleted} پیام پاک شد؛ {failed} پیام به دلیل محدودیت "
                    "تلگرام پاک نشد (پیام‌های قدیمی‌تر از ۴۸ ساعت قابل حذف نیستند).")
        else:
            text = CLEAR_DONE
        # Re-assert the persistent main menu after clearing (lazy import:
        # handlers imports this module at load time).
        try:
            from ..bot.handlers import _main_reply_kb
            kb = _main_reply_kb(conn)
        except Exception:
            kb = None
        await callback.message.answer(text, reply_markup=kb)

    # -- delete-all-my-data (every user) --------------------------------------------
    @ar.callback_query(F.data == "adm:wipe")
    async def wipe_card(callback: CallbackQuery, conn: sqlite3.Connection, config):
        await _edit(callback, *wipe_view())

    @ar.callback_query(F.data == "adm:wipe:no")
    async def wipe_cancel(callback: CallbackQuery, conn: sqlite3.Connection, config):
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            await callback.message.edit_text("لغو شد.")

    @ar.callback_query(F.data == "adm:wipe:yes")
    async def wipe_yes(callback: CallbackQuery, conn: sqlite3.Connection,
                       config, state: FSMContext):
        user_id = callback.from_user.id
        await callback.answer()
        try:
            await callback.message.edit_text(WIPE_WORKING)
        except Exception:
            pass
        try:
            favs, pls, hist = wipe_user_data(conn, user_id)
        except Exception:
            await callback.message.answer(BRIEF["wipe_failed"])
            return
        await state.clear()
        await callback.message.answer(
            f"اطلاعات ذخیره‌شده شما حذف شد: {favs} علاقه‌مندی، {pls} پلی‌لیست و {hist} سابقه جستجو. "
            "تنظیمات و بانک هوش برتر دست نخورد."
        )

    router.include_router(ar)
