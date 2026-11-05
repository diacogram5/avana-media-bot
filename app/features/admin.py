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
from ..i18n import t

# --- brief failure lines (one short Persian line per failure) ---------------
BRIEF = {
    "not_owner": t("adm_brief_not_owner"),
    "not_found": t("adm_brief_not_found"),
    "save_failed": t("adm_brief_save_failed"),
    "wipe_failed": t("adm_brief_wipe_failed"),
    "name_too_long": t("adm_brief_name_too_long"),
    "empty": t("adm_brief_empty"),
    "text_too_long": t("adm_brief_text_too_long"),
    "core_not_deletable": t("adm_brief_core_not_deletable"),
    "bad_id": t("adm_brief_bad_id"),
}


def brief(kind: str, lang: str = "fa") -> str:
    """Localized brief failure line."""
    return t({
        "not_owner": "adm_brief_not_owner",
        "not_found": "adm_brief_not_found",
        "save_failed": "adm_brief_save_failed",
        "wipe_failed": "adm_brief_wipe_failed",
        "name_too_long": "adm_brief_name_too_long",
        "empty": "adm_brief_empty",
        "text_too_long": "adm_brief_text_too_long",
        "core_not_deletable": "adm_brief_core_not_deletable",
        "bad_id": "adm_brief_bad_id",
    }[kind], lang)

# --- speech gate messages (exact wording from spec section 8) ---------------
GATE_TTS_VERIFIED = t("adm_gate_tts_ok")
GATE_TTS_INTERNAL = t("adm_gate_tts_robot")
GATE_STT_NO_KEY = t("adm_gate_vtt_no_key")
GATE_STT_UNVERIFIED = t("adm_gate_vtt_unverified")
GATE_STT_DISABLED = t("adm_gate_vtt_disabled")
GATE_GENERIC_DISABLED = t("adm_gate_disabled")


def gate_text(kind: str, lang: str = "fa") -> str:
    """Localized speech-gate message (handlers.py calls this with the user's lang)."""
    return t({
        "tts_verified": "adm_gate_tts_ok",
        "tts_internal": "adm_gate_tts_robot",
        "stt_no_key": "adm_gate_vtt_no_key",
        "stt_unverified": "adm_gate_vtt_unverified",
        "stt_disabled": "adm_gate_vtt_disabled",
        "generic_disabled": "adm_gate_disabled",
    }[kind], lang)

# --- spec texts (exact) ------------------------------------------------------
FEATS_EXPLAINER = t("adm_feat_explain")
FEAT_VISIBLE = t("adm_feat_visible")
FEAT_HIDDEN = t("adm_feat_hidden")

ADS_EXPLAINER = t("adm_ads_explain")
ADS_EMPTY = t("adm_ads_empty")
AD_DEFAULT_BUTTON = t("adm_ad_view_default")

KEY_TITLE = t("adm_key_label")
KEY_EXPLAINER = t("adm_key_explain")
KEY_STATUS_VERIFIED = t("adm_verified")
KEY_STATUS_SAVED = t("adm_set_unverified")
KEY_STATUS_MISSING = t("cmn_unset")
KEY_PROMPT = t("adm_key_placeholder")
KEY_NOTE = t("adm_key_hint")

USERS_EMPTY = t("adm_users_empty")

CLEAR_TITLE = t("adm_clear_view_title")
CLEAR_EXPLAINER = t("adm_clear_explain")
CLEAR_48H = (
    "محدودیت رسمی تلگرام (قانون Bot API): ربات فقط پیام‌هایی را می‌تواند حذف کند که کمتر از "
    "۴۸ ساعت از ارسالشان گذشته باشد؛ پیام‌های قدیمی‌تر برای هیچ رباتی قابل حذف نیستند. "
    "در چت خصوصی، ربات هم پیام‌های خودش و هم پیام‌های شما را — در همین بازه ۴۸ ساعته — حذف می‌کند."
)
CLEAR_SEPARATE = t("adm_clear_separate")
CLEAR_DONE = t("adm_clear_done")

WIPE_TITLE = t("adm_wipe_title")
WIPE_EXPLAINER = t("adm_wipe_explain")
WIPE_WORKING = t("adm_deleting")

CORE_FEATURE_KEYS = (
    "song_search", "audio_id", "tts", "link_fetch", "voice_to_text", "help_support",
)


# --- owner guard (local; never imported from app.bot.handlers) ---------------
def _is_owner(event, config) -> bool:
    uid = getattr(getattr(event, "from_user", None), "id", None)
    return config.owner_id is not None and uid == config.owner_id


async def _deny_owner(event, lang: str = "fa") -> None:
    answer = getattr(event, "answer", None)
    if not callable(answer):
        return
    if isinstance(event, CallbackQuery):
        await answer(brief("not_owner", lang), show_alert=True)
    else:
        await answer(brief("not_owner", lang))


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
            try:
                _ulang = dbmod.user_lang_or_default(
                    conn, getattr(getattr(event, "from_user", None), "id", None))
            except Exception:
                _ulang = "fa"
            await _deny_owner(event, _ulang)
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


def _back_panel_kb(lang: str = "fa") -> InlineKeyboardMarkup:
    return _kb([[_btn(t("adm_back_to_panel", lang), "adm:panel")]])


def _cancel_kb(lang: str = "fa") -> InlineKeyboardMarkup:
    """In-bot «لغو» for every admin FSM prompt: clears the stale state and
    lands back on the admin panel. Reuses the existing admin «لغو» style."""
    return _kb([[_btn(t("adm_cancel", lang), "adm:cancel")]])


async def _edit(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb)


# --- features (buttons) ------------------------------------------------------
def is_custom_feature(key: str) -> bool:
    return key.startswith("custom_")


def add_custom_feature(conn: sqlite3.Connection, name: str, reply_text: str,
                       by_id: int | None = None, lang: str = "fa") -> str:
    name = (name or "").strip()
    reply_text = (reply_text or "").strip()
    if not name:
        raise ValueError(brief("empty", lang))
    if len(name) > 80:
        raise ValueError(brief("name_too_long", lang))
    if len(reply_text) > 2000:
        raise ValueError(brief("text_too_long", lang))
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
                   by_id: int | None = None, lang: str = "fa") -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError(brief("empty", lang))
    if len(name) > 80:
        raise ValueError(brief("name_too_long", lang))
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


def delete_feature(conn: sqlite3.Connection, key: str, by_id: int | None = None, lang: str = "fa") -> None:
    if key in CORE_FEATURE_KEYS:
        raise ValueError(brief("core_not_deletable", lang))
    if not is_custom_feature(key):
        raise ValueError(brief("core_not_deletable", lang))
    conn.execute("DELETE FROM features WHERE key=?", (key,))
    dbmod.audit(conn, by_id, "admin_delete_feature", key)
    conn.commit()


def feats_view(conn: sqlite3.Connection,
               lang: str = "fa") -> tuple[str, InlineKeyboardMarkup]:
    lines = [t("adm_feat_edit_title", lang), "", t("adm_feat_explain", lang), ""]
    rows = []
    for f in dbmod.list_features(conn):
        status = t("adm_feat_visible", lang) if f["enabled"] else t("adm_feat_hidden", lang)
        custom_mark = t("adm_custom_mark", lang) if is_custom_feature(f["key"]) else ""
        lines.append(f"• {f['title']} — {status}{custom_mark}")
        btns = [
            _btn(t("adm_edit", lang), f"adm:feat:rename:{f['key']}"),
            _btn(t("adm_disabled", lang) if f["enabled"] else t("adm_active", lang),
                 f"adm:feat:toggle:{f['key']}"),
            _btn(t("adm_up", lang), f"adm:feat:move:{f['key']}:up"),
            _btn(t("adm_down", lang), f"adm:feat:move:{f['key']}:down"),
        ]
        if is_custom_feature(f["key"]):
            btns.append(_btn(t("adm_delete_button", lang), f"adm:feat:del:{f['key']}"))
        rows.append(btns)
    rows.append([_btn(t("adm_add_button", lang), "adm:feat:add")])
    rows.append([_btn(t("adm_back_to_panel", lang), "adm:panel")])
    return "\n".join(lines), _kb(rows)


# --- ads ----------------------------------------------------------------------
def get_ad(conn: sqlite3.Connection, ad_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
    return dict(row) if row else None


def list_ads_full(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM ads ORDER BY id DESC")]


def create_ad(conn: sqlite3.Connection, title: str, text: str, photo_file_id: str = "",
              link: str = "", button_text: str = "", by_id: int | None = None, lang: str = "fa") -> int:
    title = (title or "").strip()
    text = (text or "").strip()
    if not title or not text:
        raise ValueError(brief("empty", lang))
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


def ad_render(ad: dict, lang: str = "fa") -> str:
    """Automatic professional template: `📢 تبلیغ\\n{title}\\n{text}`."""
    return (t("adm_ad_render_head", lang) +
            f"{ad.get('title', '')}\n{ad.get('text', '')}")


def ads_view(conn: sqlite3.Connection,
             lang: str = "fa") -> tuple[str, InlineKeyboardMarkup]:
    ads = list_ads_full(conn)
    from ..bot.ads import ad_every_n
    every = ad_every_n(conn)
    lines = [t("adm_ads_title", lang), "",
             t("adm_ads_auto_line", lang).format(n=every),
             "", t("adm_ads_explain", lang), ""]
    rows = [[_btn(t("adm_ad_new", lang), "adm:ad:new"),
             _btn(t("adm_ad_every", lang), "adm:ad:every")]]
    if not ads:
        lines.append(t("adm_ads_empty", lang))
    for ad in ads:
        state = t("adm_active", lang) if ad.get("enabled") else t("adm_stopped", lang)
        lines.append(
            f"• {ad.get('title') or '—'}\n"
            + t("adm_ad_stats_line", lang).format(views=ad.get("views", 0),
                                                 clicks=ad.get("clicks", 0))
            + f" · {state}\n"
            f"  {ad_render(ad, lang)[:200]}"
        )
        rows.append([
            _btn(t("adm_edit", lang), f"adm:ad:edit:{ad['id']}"),
            _btn(t("adm_stopped", lang) if ad.get("enabled") else t("adm_active", lang),
                 f"adm:ad:toggle:{ad['id']}"),
            _btn(t("adm_delete", lang), f"adm:ad:del:{ad['id']}"),
        ])
    rows.append([_btn(t("adm_back_to_panel", lang), "adm:panel")])
    return "\n".join(lines), _kb(rows)


AD_EDIT_FIELDS = (
    ("title", "adm_adedit_f_title"),
    ("text", "adm_adedit_f_text"),
    ("link", "adm_adedit_f_link"),
    ("button_text", "adm_adedit_f_button"),
    ("photo", "adm_adedit_f_photo"),
)
AD_EDIT_PROMPTS = {
    "title": "adm_adedit_title",
    "text": "adm_adedit_text",
    "link": "adm_adedit_link",
    "button_text": "adm_adedit_button",
    "photo": "adm_adedit_photo",
}


def ad_edit_prompt(field: str, lang: str = "fa") -> str:
    key = AD_EDIT_PROMPTS.get(field, "adm_adedit_title")
    text = t(key, lang)
    if field == "link":
        text = text.format(delete_word=t("adm_delete_word", lang))
    return text


def ad_edit_field_label(field: str, lang: str = "fa") -> str:
    key = next((lk for f, lk in AD_EDIT_FIELDS if f == field), field)
    return t(key, lang) if key != field else field


# --- OpenAI key box (never display the value) ----------------------------------
def get_openai_key(conn: sqlite3.Connection, config) -> str:
    """DB setting wins over env — the integrator uses this, never config directly."""
    value = dbmod.get_setting(conn, "openai_api_key", "").strip()
    return value or (getattr(config, "openai_api_key", "") or "").strip()


def is_openai_verified(conn: sqlite3.Connection) -> bool:
    return dbmod.get_setting(conn, "openai_key_verified", "0") == "1"


def set_openai_key(conn: sqlite3.Connection, key: str, by_id: int | None = None, lang: str = "fa") -> None:
    key = (key or "").strip()
    if not key:
        raise ValueError(brief("empty", lang))
    dbmod.set_setting(conn, "openai_api_key", key, by_id=by_id)
    dbmod.set_setting(conn, "openai_key_verified", "0", by_id=by_id)


def set_key_verified(conn: sqlite3.Connection, verified: bool,
                     by_id: int | None = None) -> None:
    dbmod.set_setting(conn, "openai_key_verified", "1" if verified else "0", by_id=by_id)


def key_status(conn: sqlite3.Connection, config, lang: str = "fa") -> str:
    if is_openai_verified(conn):
        return t("adm_verified", lang)
    if get_openai_key(conn, config):
        return t("adm_set_unverified", lang)
    return t("cmn_unset", lang)


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


def keybox_view(conn: sqlite3.Connection, config,
                lang: str = "fa") -> tuple[str, InlineKeyboardMarkup]:
    status = key_status(conn, config, lang)
    text = "\n".join([t("adm_key_label", lang), "", t("adm_key_explain", lang), "",
                       t("adm_status_line", lang).format(status=status)])
    kb = _kb([
        [_btn(t("adm_key_save", lang), "adm:key:save"),
         _btn(t("adm_key_verify", lang), "adm:key:verify")],
        [_btn(t("adm_back_to_panel", lang), "adm:panel")],
    ])
    return text, kb


# --- users --------------------------------------------------------------------
def delete_user(conn: sqlite3.Connection, telegram_id: int,
                by_id: int | None = None) -> None:
    conn.execute("DELETE FROM users WHERE telegram_id=?", (telegram_id,))
    dbmod.audit(conn, by_id, "admin_delete_user", str(telegram_id))
    conn.commit()


def users_view(conn: sqlite3.Connection,
               lang: str = "fa") -> tuple[str, InlineKeyboardMarkup]:
    users = dbmod.list_users(conn, limit=50)
    lines = [t("adm_users_title", lang), ""]
    rows = []
    if not users:
        lines.append(t("adm_users_empty", lang))
    for u in users:
        mark = t("adm_user_blocked_mark", lang) if u.get("blocked") else ""
        lines.append(f"• {u['telegram_id']} {u.get('username') or ''}{mark}")
        rows.append([
            _btn(t("adm_unblock", lang) if u.get("blocked") else t("adm_block", lang),
                 f"adm:user:block:{u['telegram_id']}"),
            _btn(t("adm_delete", lang), f"adm:user:del:{u['telegram_id']}"),
        ])
    rows.append([_btn(t("adm_back_to_panel", lang), "adm:panel")])
    return "\n".join(lines), _kb(rows)


# --- settings -----------------------------------------------------------------
SETTINGS_FIELDS = (
    ("welcome_message", "adm_set_welcome"),
    ("help_message", "adm_set_help"),
    ("support_text", "adm_set_support"),
    ("not_found_text", "adm_set_notfound"),
    ("bot_id", "adm_set_botid"),
    ("channel_usernames", "adm_set_channels"),
    ("suno_sites", "adm_set_bank_sites"),
    ("audd_token", "adm_set_audd"),
)
SENSITIVE_SETTING_KEYS = {"audd_token"}


def settings_view(conn: sqlite3.Connection,
                  lang: str = "fa") -> tuple[str, InlineKeyboardMarkup]:
    lines = [t("adm_settings_title", lang), ""]
    rows = []
    for key, label_key in SETTINGS_FIELDS:
        label = t(label_key, lang)
        if key in SENSITIVE_SETTING_KEYS:
            shown = t("cmn_set", lang) if dbmod.get_setting(conn, key, "") else t("cmn_unset", lang)
        else:
            val = dbmod.get_setting(conn, key, "")
            shown = (val[:60] + "…") if len(val) > 60 else (val or "—")
        lines.append(f"• {label}" + t("adm_setting_value_line", lang).format(shown=shown))
        rows.append([_btn(t("adm_edit_prefix", lang).format(label=label[:24]), f"adm:set:{key}")])
    rows.append([_btn(t("adm_back_to_panel", lang), "adm:panel")])
    return "\n".join(lines), _kb(rows)


def setting_label(key: str, lang: str = "fa") -> str:
    label_key = next((lk for k, lk in SETTINGS_FIELDS if k == key), key)
    return t(label_key, lang) if label_key != key else key


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

    Returns (deleted, failed). "Message to delete not found" counts as
    deleted — the message is already gone, which is the goal. Any other
    failure (e.g. Telegram's 48-hour delete limit) is counted as failed,
    never silently claimed as deleted and never a reason to abort the
    remaining deletions.
    """
    from aiogram.exceptions import TelegramBadRequest
    deleted = failed = 0
    for row in rows:
        try:
            await bot.delete_message(row["chat_id"], row["message_id"])
            deleted += 1
        except TelegramBadRequest as exc:
            if "message to delete not found" in str(exc).lower():
                deleted += 1
            else:
                failed += 1
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


def clear_view(lang: str = "fa") -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join([t("adm_clear_view_title", lang), "", t("adm_clear_explain", lang),
                       "", t("adm_clear_48h", lang), "", t("adm_clear_separate", lang)])
    kb = _kb([
        [_btn(t("adm_clear_title", lang), "adm:clear:state")],
        [_btn(t("adm_clear_msgs_title", lang), "adm:clear:msgs")],
        [_btn(t("adm_wipe_btn", lang), "adm:wipe")],
        [_btn(t("adm_cancel", lang), "adm:clear:no")],
    ])
    return text, kb


def clear_msgs_view(count: int, lang: str = "fa") -> tuple[str, InlineKeyboardMarkup]:
    text = (t("adm_clear_msgs_head", lang).format(count=count) +
            t("adm_clear_48h", lang))
    kb = _kb([
        [_btn(t("adm_clear_msgs_yes", lang), "adm:clear:msgs:yes"),
         _btn(t("adm_cancel", lang), "adm:clear:msgs:no")],
    ])
    return text, kb


async def clear_state(callback, conn: sqlite3.Connection, config, state) -> None:
    """(a) «پاک‌سازی وضعیت گفتگو»: the zero-leakage reset the user
    expects. FSM state AND data are cleared, stale in-memory tokens
    (old «🎵 شناسایی موسیقی» buttons) are purged, and stale tracked-
    message references are dropped — without deleting chat messages
    (that is option (b)) and without touching stored data (option (c)).
    The user lands back on the post-/start state and can start a new
    operation with nothing leaking from the previous one.
    """
    user_id = callback.from_user.id
    try:
        from ..bot.handlers import purge_user_registries
        purge_user_registries(user_id)
    except Exception:
        pass
    conn.execute("DELETE FROM outbox WHERE user_id=?", (user_id,))
    conn.commit()
    try:
        await state.clear()
    except Exception:
        pass
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    try:
        from ..bot.handlers import _main_reply_kb
        kb = _main_reply_kb(conn, user_id)
    except Exception:
        kb = None
    await callback.message.answer(
        "✅ وضعیت گفتگو پاک شد و به حالت اولیه برگشتید؛ می‌توانید عملیات جدید را بدون هیچ اثری از عملیات قبلی شروع کنید.",
        reply_markup=kb)


async def clear_msgs_card(callback, conn: sqlite3.Connection, config, lang: str = "fa") -> None:
    """(b) «حذف پیام‌های چت»: confirm card with the honest 48h limit."""
    rows = list_outbox(conn, callback.from_user.id)
    await _edit(callback, *clear_msgs_view(len(rows), lang))


async def clear_msgs_yes(callback, conn: sqlite3.Connection, config) -> None:
    """(b) confirmed: delete the tracked chat messages, counting real
    outcomes. A failure is counted and reported honestly — never a fake
    success — and never aborts the remaining deletions."""
    user_id = callback.from_user.id
    rows = list_outbox(conn, user_id)
    deleted, failed = await delete_tracked_messages(callback.bot, rows)
    conn.execute("DELETE FROM outbox WHERE user_id=?", (user_id,))
    conn.commit()
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    if not rows:
        text = "پیامی برای پاک‌سازی پیدا نشد."
    elif failed:
        text = (f"✅ {deleted} پیام پاک شد؛ {failed} پیام پاک نشد.\n\n{CLEAR_48H}")
    else:
        text = f"✅ هر {deleted} پیام پاک شد."
    # Re-assert the persistent main menu after clearing (collapse-aware).
    try:
        from ..bot.handlers import _main_reply_kb
        kb = _main_reply_kb(conn, user_id)
    except Exception:
        kb = None
    await callback.message.answer(text, reply_markup=kb)


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


def wipe_view(lang: str = "fa") -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join([t("adm_wipe_title", lang), "", t("adm_wipe_explain", lang)])
    kb = _kb([
        [_btn(t("adm_wipe_yes", lang), "adm:wipe:yes"),
         _btn(t("adm_cancel", lang), "adm:wipe:no")],
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
    (AdmStates.ad_title, "adm_adflow_title", "title", False),
    (AdmStates.ad_photo, "adm_adflow_photo", "photo", True),
    (AdmStates.ad_desc, "adm_adflow_desc", "text", False),
    (AdmStates.ad_link, "adm_adflow_link", "link", True),
    (AdmStates.ad_button, "adm_adflow_button", "button_text", True),
]


def ad_flow_prompt(step_key: str, lang: str = "fa") -> str:
    text = t(step_key, lang)
    if "{skip}" in text:
        text = text.format(skip=t("adm_skip", lang))
    return text


def _panel_view(conn: sqlite3.Connection, config,
                lang: str = "fa") -> tuple[str, InlineKeyboardMarkup]:
    status = key_status(conn, config, lang)
    lines = [t("adm_panel_title", lang),
             t("adm_keybox_status_line", lang).format(status=status), ""]
    from . import watermark as _wm
    _wm_state = t("adm_active", lang) if _wm.is_enabled(conn) else t("adm_inactive_soon", lang)
    kb = _kb([
        [_btn(t("adm_feat_edit_title", lang), "adm:feats")],
        [_btn(t("adm_ads_title", lang), "adm:ads")],
        [_btn(t("adm_key_title", lang), "adm:keybox")],
        [_btn(t("adm_users_title", lang), "adm:users")],
        [_btn(t("adm_settings_title", lang), "adm:settings")],
        [_btn(t("adm_wm_state_line", lang).format(state=_wm_state), "adm:watermark")],
    ])
    return "\n".join(lines), kb


def _watermark_view(conn: sqlite3.Connection,
                    lang: str = "fa") -> tuple[str, InlineKeyboardMarkup]:
    from . import watermark as _wm
    en = _wm.is_enabled(conn)
    lines = [
        t("adm_wm_title", lang),
        "",
        t("adm_status_line", lang).format(
            status=t("adm_active", lang) if en else t("adm_inactive_soon", lang)),
        "",
        t("adm_wm_explain", lang),
    ]
    rows = []
    rows.append([_btn(t("adm_deactivate", lang), "adm:watermark:off")] if en
                else [_btn(t("adm_activate", lang), "adm:watermark:on")])
    rows.append([_btn(t("adm_back_to_panel", lang), "adm:panel")])
    return "\n".join(lines), _kb(rows)


def register(router: Router) -> None:
    ar = Router(name="admin")

    # -- panel ------------------------------------------------------------------
    @ar.callback_query(F.data == "adm:panel")
    @_owner_only
    async def panel(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await _edit(callback, *_panel_view(conn, config, lang))

    @ar.callback_query(F.data == "adm:cancel")
    @_owner_only
    async def adm_cancel(callback: CallbackQuery, conn: sqlite3.Connection,
                         config, state: FSMContext):
        """Cancel any admin FSM prompt: clear the stale state and land back
        on the admin panel. Every adm:* awaiting prompt offers this."""
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await state.clear()
        await _edit(callback, *_panel_view(conn, config, lang))

    # -- watermark-removal stub (disabled by default) ---------------------------
    @ar.callback_query(F.data == "adm:watermark")
    @_owner_only
    async def watermark_card(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await _edit(callback, *_watermark_view(conn, lang))

    @ar.callback_query(F.data == "adm:watermark:on")
    @_owner_only
    async def watermark_on(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        from . import watermark as _wm
        _wm.set_enabled(conn, True, by_id=callback.from_user.id)
        await callback.answer(t("adm_enabled_done", lang))
        await _edit(callback, *_watermark_view(conn, lang))

    @ar.callback_query(F.data == "adm:watermark:off")
    @_owner_only
    async def watermark_off(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        from . import watermark as _wm
        _wm.set_enabled(conn, False, by_id=callback.from_user.id)
        await callback.answer(t("adm_disabled_done", lang))
        await _edit(callback, *_watermark_view(conn, lang))

    # -- features (buttons) ------------------------------------------------------
    @ar.callback_query(F.data == "adm:feats")
    @_owner_only
    async def feats(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await _edit(callback, *feats_view(conn, lang))

    @ar.callback_query(F.data.startswith("adm:feat:toggle:"))
    @_owner_only
    async def feat_toggle(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        key = callback.data.split(":", 3)[3]
        feat = dbmod.get_feature(conn, key)
        if feat is None:
            await callback.answer(brief("not_found", lang), show_alert=True)
            return
        dbmod.set_feature(conn, key, enabled=not bool(feat["enabled"]),
                          by_id=callback.from_user.id)
        await _edit(callback, *feats_view(conn, lang))

    @ar.callback_query(F.data.startswith("adm:feat:move:"))
    @_owner_only
    async def feat_move(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        _, _, _, rest = callback.data.split(":", 3)
        key, _, direction = rest.partition(":")
        move_feature(conn, key, direction if direction in ("up", "down") else "up")
        await _edit(callback, *feats_view(conn, lang))

    @ar.callback_query(F.data.startswith("adm:feat:rename:"))
    @_owner_only
    async def feat_rename_start(callback: CallbackQuery, conn: sqlite3.Connection,
                                config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        key = callback.data.split(":", 3)[3]
        feat = dbmod.get_feature(conn, key)
        if feat is None:
            await callback.answer(brief("not_found", lang), show_alert=True)
            return
        await state.set_state(AdmStates.feat_rename)
        await state.update_data(feat_key=key)
        await callback.answer()
        await callback.message.answer(
            f"نام فعلی: {feat['title']}\nنام جدید دکمه را بنویسید (حداکثر ۸۰ کاراکتر):",
            reply_markup=_cancel_kb(lang))

    @ar.message(AdmStates.feat_rename, F.text)
    @menu_escape
    @_owner_only
    async def feat_rename_save(message: Message, conn: sqlite3.Connection,
                               config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(message, 'from_user', None), 'id', None))
        data = await state.get_data()
        try:
            rename_feature(conn, data.get("feat_key", ""), message.text,
                           by_id=message.from_user.id)
        except ValueError as e:
            await message.answer(str(e))
            return
        await state.clear()
        text, kb = feats_view(conn, lang)
        await message.answer(t("adm_feat_renamed", lang), reply_markup=kb)

    @ar.callback_query(F.data == "adm:feat:add")
    @_owner_only
    async def feat_add_start(callback: CallbackQuery, conn: sqlite3.Connection,
                             config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await state.set_state(AdmStates.feat_add_name)
        await callback.answer()
        await callback.message.answer(t("adm_feat_add_name_prompt", lang),
                                        reply_markup=_cancel_kb(lang))

    @ar.message(AdmStates.feat_add_name, F.text)
    @menu_escape
    @_owner_only
    async def feat_add_name(message: Message, conn: sqlite3.Connection,
                            config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(message, 'from_user', None), 'id', None))
        name = (message.text or "").strip()
        if not name:
            await message.answer(brief("empty", lang))
            return
        if len(name) > 80:
            await message.answer(brief("name_too_long", lang))
            return
        await state.update_data(feat_name=name)
        await state.set_state(AdmStates.feat_add_text)
        await message.answer(t("adm_feat_add_text_prompt", lang),
                             reply_markup=_cancel_kb(lang))

    @ar.message(AdmStates.feat_add_text, F.text)
    @menu_escape
    @_owner_only
    async def feat_add_text(message: Message, conn: sqlite3.Connection,
                            config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(message, 'from_user', None), 'id', None))
        data = await state.get_data()
        try:
            key = add_custom_feature(conn, data.get("feat_name", ""), message.text,
                                     by_id=message.from_user.id)
        except ValueError as e:
            await message.answer(str(e))
            return
        await state.clear()
        text, kb = feats_view(conn, lang)
        await message.answer(ft("adm_feat_added", lang), reply_markup=kb)

    @ar.callback_query(F.data.startswith("adm:feat:del:"))
    @_owner_only
    async def feat_del_confirm(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        key = callback.data.split(":", 3)[3]
        feat = dbmod.get_feature(conn, key)
        if feat is None:
            await callback.answer(brief("not_found", lang), show_alert=True)
            return
        kb = _kb([
            [_btn(t("adm_feat_del_title", lang), f"adm:feat:del:yes:{key}"), _btn(t("adm_cancel", lang), "adm:feats")],
        ])
        await _edit(callback, f"«{feat['title']}» برای همیشه از منوی ربات حذف شود؟", kb)

    @ar.callback_query(F.data.startswith("adm:feat:del:yes:"))
    @_owner_only
    async def feat_del_yes(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        key = callback.data.split(":", 4)[4]
        try:
            delete_feature(conn, key, by_id=callback.from_user.id)
        except ValueError as e:
            await callback.answer(str(e), show_alert=True)
            return
        await _edit(callback, *feats_view(conn, lang))

    # -- ads ---------------------------------------------------------------------
    @ar.callback_query(F.data == "adm:ads")
    @_owner_only
    async def ads(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await _edit(callback, *ads_view(conn, lang))

    @ar.callback_query(F.data == "adm:ad:new")
    @_owner_only
    async def ad_new_start(callback: CallbackQuery, conn: sqlite3.Connection,
                           config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await state.set_state(AdmStates.ad_title)
        await state.update_data(ad={})
        await callback.answer()
        await callback.message.answer(ad_flow_prompt(AD_FLOW[0][1], lang),
                                        reply_markup=_cancel_kb(lang))

    @ar.message(AdmStates.ad_photo, F.photo)
    @_owner_only
    async def ad_photo_got(message: Message, conn: sqlite3.Connection,
                           config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(message, 'from_user', None), 'id', None))
        await _ad_store_and_advance(message, conn, state, "photo",
                                    message.photo[-1].file_id)

    @ar.callback_query(F.data == "adm:ad:skip")
    @_owner_only
    async def ad_skip(callback: CallbackQuery, conn: sqlite3.Connection,
                      config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        cur = await state.get_state()
        step = next((s for s in AD_FLOW if s[0].state == cur), None)
        if step is None or not step[3]:
            await callback.answer(brief("not_found", lang), show_alert=True)
            return
        by_id = callback.from_user.id if callback.from_user else None
        await _ad_store_and_advance(callback.message, conn, state, step[2], "",
                                    by_id=by_id)
        await callback.answer()

    async def _ad_store_and_advance(msg, conn, state: FSMContext, field: str,
                                    value: str, by_id=None):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(msg, 'from_user', None), 'id', None))
        data = await state.get_data()
        ad = data.get("ad", {})
        ad[field] = value
        await state.update_data(ad=ad)
        idx = next(i for i, s in enumerate(AD_FLOW) if s[2] == field)
        if idx + 1 < len(AD_FLOW):
            nxt = AD_FLOW[idx + 1]
            await state.set_state(nxt[0])
            _rows = [[_btn(t("adm_skip", lang), "adm:ad:skip")]] if nxt[3] else []
            _rows.append([_btn(t("adm_cancel", lang), "adm:cancel")])
            kb = _kb(_rows)
            await msg.answer(ad_flow_prompt(nxt[1], lang), reply_markup=kb)
        else:
            await _finish_ad(msg, conn, state, by_id)

    async def _finish_ad(msg, conn, state: FSMContext, by_id=None):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(msg, 'from_user', None), 'id', None))
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
        kb = _kb([[_btn(t("adm_back_to_ads", lang), "adm:ads")]])
        await msg.answer(f"✅ تبلیغ ساخته شد.\n\n{ad_render(saved)}", reply_markup=kb)

    @ar.message(AdmStates.ad_title, F.text)
    @menu_escape
    @_owner_only
    async def ad_title_got(message: Message, conn: sqlite3.Connection,
                           config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(message, 'from_user', None), 'id', None))
        title = (message.text or "").strip()
        if not title:
            await message.answer(brief("empty", lang))
            return
        data = await state.get_data()
        ad = data.get("ad", {})
        ad["title"] = title
        await state.update_data(ad=ad)
        await state.set_state(AdmStates.ad_photo)
        await message.answer(ad_flow_prompt(AD_FLOW[1][1], lang),
                             reply_markup=_kb([[_btn(t("adm_skip", lang), "adm:ad:skip")],
                                               [_btn(t("adm_cancel", lang), "adm:cancel")]]))

    @ar.message(AdmStates.ad_desc, F.text)
    @menu_escape
    @_owner_only
    async def ad_desc_got(message: Message, conn: sqlite3.Connection,
                          config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(message, 'from_user', None), 'id', None))
        text = (message.text or "").strip()
        if not text:
            await message.answer(brief("empty", lang))
            return
        data = await state.get_data()
        ad = data.get("ad", {})
        ad["text"] = text
        await state.update_data(ad=ad)
        await state.set_state(AdmStates.ad_link)
        await message.answer(ad_flow_prompt(AD_FLOW[3][1], lang),
                             reply_markup=_kb([[_btn(t("adm_skip", lang), "adm:ad:skip")],
                                               [_btn(t("adm_cancel", lang), "adm:cancel")]]))

    @ar.message(AdmStates.ad_link, F.text)
    @menu_escape
    @_owner_only
    async def ad_link_got(message: Message, conn: sqlite3.Connection,
                          config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(message, 'from_user', None), 'id', None))
        data = await state.get_data()
        ad = data.get("ad", {})
        ad["link"] = (message.text or "").strip()
        await state.update_data(ad=ad)
        await state.set_state(AdmStates.ad_button)
        await message.answer(ad_flow_prompt(AD_FLOW[4][1], lang),
                             reply_markup=_kb([[_btn(t("adm_skip", lang), "adm:ad:skip")],
                                               [_btn(t("adm_cancel", lang), "adm:cancel")]]))

    @ar.message(AdmStates.ad_button, F.text)
    @menu_escape
    @_owner_only
    async def ad_button_got(message: Message, conn: sqlite3.Connection,
                            config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(message, 'from_user', None), 'id', None))
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
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        from ..bot.ads import ad_every_n
        await state.set_state(AdmStates.ad_every)
        await callback.answer()
        await callback.message.answer(
            t("adm_ad_every_prompt", lang).format(n=ad_every_n(conn)),
            reply_markup=_cancel_kb(lang))

    @ar.message(AdmStates.ad_every)
    @menu_escape
    @_owner_only
    async def ad_every_got(message: Message, conn: sqlite3.Connection,
                           config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(message, 'from_user', None), 'id', None))
        raw = (message.text or "").strip()
        try:
            n = int(raw)
        except ValueError:
            n = 0
        if not 1 <= n <= 50:
            await message.answer(t("adm_ad_every_invalid", lang))
            return
        dbmod.set_setting(conn, "ad_every_n", str(n), by_id=message.from_user.id)
        await state.clear()
        text, kb = ads_view(conn, lang)
        await message.answer(ft("adm_ad_every_set", lang), reply_markup=kb)

    @ar.callback_query(F.data.startswith("adm:ad:toggle:"))
    @_owner_only
    async def ad_toggle(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        try:
            ad_id = int(callback.data.split(":", 3)[3])
        except ValueError:
            await callback.answer(brief("bad_id", lang), show_alert=True)
            return
        ad = get_ad(conn, ad_id)
        if ad is None:
            await callback.answer(brief("not_found", lang), show_alert=True)
            return
        dbmod.set_ad_enabled(conn, ad_id, not bool(ad["enabled"]),
                             by_id=callback.from_user.id)
        await _edit(callback, *ads_view(conn, lang))

    @ar.callback_query(F.data.startswith("adm:ad:del:"))
    @_owner_only
    async def ad_del_confirm(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        try:
            ad_id = int(callback.data.split(":", 3)[3])
        except ValueError:
            await callback.answer(brief("bad_id", lang), show_alert=True)
            return
        ad = get_ad(conn, ad_id)
        if ad is None:
            await callback.answer(brief("not_found", lang), show_alert=True)
            return
        kb = _kb([
            [_btn(t("cmn_confirm_delete", lang), f"adm:ad:del:yes:{ad_id}"), _btn(t("adm_cancel", lang), "adm:ads")],
        ])
        await _edit(callback,
                      t("adm_ad_del_confirm", lang).format(
                          title=ad.get('title') or t("adm_ad_word", lang)), kb)

    @ar.callback_query(F.data.startswith("adm:ad:del:yes:"))
    @_owner_only
    async def ad_del_yes(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        try:
            ad_id = int(callback.data.split(":", 4)[4])
        except ValueError:
            await callback.answer(brief("bad_id", lang), show_alert=True)
            return
        delete_ad(conn, ad_id, by_id=callback.from_user.id)
        await _edit(callback, *ads_view(conn, lang))

    @ar.callback_query(F.data.startswith("adm:ad:edit:"))
    @_owner_only
    async def ad_edit_menu(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        try:
            ad_id = int(callback.data.split(":", 3)[3])
        except ValueError:
            await callback.answer(brief("bad_id", lang), show_alert=True)
            return
        ad = get_ad(conn, ad_id)
        if ad is None:
            await callback.answer(brief("not_found", lang), show_alert=True)
            return
        rows = [[_btn(label, f"adm:ad:editfield:{ad_id}:{field}")]
                for field, label in AD_EDIT_FIELDS]
        rows.append([_btn(t("adm_back_to_ads", lang), "adm:ads")])
        await _edit(callback, f"ویرایش تبلیغ «{ad.get('title') or ''}»:", _kb(rows))

    @ar.callback_query(F.data.startswith("adm:ad:editfield:"))
    @_owner_only
    async def ad_edit_start(callback: CallbackQuery, conn: sqlite3.Connection,
                            config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        parts = callback.data.split(":", 4)
        try:
            ad_id = int(parts[3])
        except (ValueError, IndexError):
            await callback.answer(brief("bad_id", lang), show_alert=True)
            return
        field = parts[4] if len(parts) > 4 else ""
        if field not in AD_EDIT_PROMPTS or get_ad(conn, ad_id) is None:
            await callback.answer(brief("not_found", lang), show_alert=True)
            return
        await state.set_state(AdmStates.ad_edit)
        await state.update_data(ad_edit_id=ad_id, ad_edit_field=field)
        await callback.answer()
        await callback.message.answer(ad_edit_prompt(field, lang),
                                        reply_markup=_cancel_kb(lang))

    @ar.message(AdmStates.ad_edit)
    @menu_escape
    @_owner_only
    async def ad_edit_save(message: Message, conn: sqlite3.Connection,
                           config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(message, 'from_user', None), 'id', None))
        data = await state.get_data()
        ad_id = data.get("ad_edit_id")
        field = data.get("ad_edit_field")
        ad = get_ad(conn, ad_id) if ad_id else None
        if ad is None or field not in AD_EDIT_PROMPTS:
            await state.clear()
            await message.answer(brief("not_found", lang))
            return
        if field == "photo":
            if not message.photo:
                await message.answer(t("adm_ad_send_photo", lang))
                return
            value = message.photo[-1].file_id
        else:
            value = (message.text or "").strip()
            if field == "link" and value == t("adm_delete_word", lang):
                value = ""
            if field in ("title", "text") and not value:
                await message.answer(brief("empty", lang))
                return
            if field == "button_text" and not value:
                value = AD_DEFAULT_BUTTON
        db_field = "photo_file_id" if field == "photo" else field
        update_ad(conn, ad_id, by_id=message.from_user.id, **{db_field: value})
        await state.clear()
        await message.answer(t("adm_saved", lang), reply_markup=_kb([[_btn(t("adm_back_to_ads", lang), "adm:ads")]]))

    # -- OpenAI key box ----------------------------------------------------------
    @ar.callback_query(F.data == "adm:keybox")
    @_owner_only
    async def keybox(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await _edit(callback, *keybox_view(conn, config, lang))

    @ar.callback_query(F.data == "adm:key:save")
    @_owner_only
    async def key_save_start(callback: CallbackQuery, conn: sqlite3.Connection,
                             config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await state.set_state(AdmStates.key_input)
        await callback.answer()
        await callback.message.answer(f"{KEY_PROMPT}\n{KEY_NOTE}",
                                        reply_markup=_cancel_kb(lang))

    @ar.message(AdmStates.key_input, F.text)
    @menu_escape
    @_owner_only
    async def key_save(message: Message, conn: sqlite3.Connection,
                       config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(message, 'from_user', None), 'id', None))
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
                             reply_markup=_back_panel_kb(lang))

    @ar.callback_query(F.data == "adm:key:verify")
    @_owner_only
    async def key_verify(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        key = get_openai_key(conn, config)
        if not key:
            await callback.answer(t("adm_key_not_set", lang), show_alert=True)
            return
        await callback.answer(t("adm_key_verifying", lang))
        ok, detail = await openai_models_check(key)
        if ok:
            set_key_verified(conn, True, by_id=callback.from_user.id)
            text, kb = keybox_view(conn, config, lang)
            await callback.message.edit_text(text, reply_markup=kb)
            await callback.answer(t("adm_key_verified", lang))
        else:
            # Honest failure: stays unverified; dependent features stay gated.
            await callback.answer(detail, show_alert=True)

    # -- users -------------------------------------------------------------------
    @ar.callback_query(F.data == "adm:users")
    @_owner_only
    async def users(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await _edit(callback, *users_view(conn, lang))

    @ar.callback_query(F.data.startswith("adm:user:block:"))
    @_owner_only
    async def user_block(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        try:
            uid = int(callback.data.split(":", 3)[3])
        except ValueError:
            await callback.answer(brief("bad_id", lang), show_alert=True)
            return
        user = dbmod.get_user(conn, uid)
        if user is None:
            await callback.answer(brief("not_found", lang), show_alert=True)
            return
        dbmod.set_user_blocked(conn, uid, not bool(user.get("blocked")),
                               by_id=callback.from_user.id)
        await _edit(callback, *users_view(conn, lang))

    @ar.callback_query(F.data.startswith("adm:user:del:"))
    @_owner_only
    async def user_del_confirm(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        try:
            uid = int(callback.data.split(":", 3)[3])
        except ValueError:
            await callback.answer(brief("bad_id", lang), show_alert=True)
            return
        kb = _kb([
            [_btn(t("cmn_confirm_delete", lang), f"adm:user:del:yes:{uid}"), _btn(t("adm_cancel", lang), "adm:users")],
        ])
        await _edit(callback, ft("adm_user_del_confirm", lang), kb)

    @ar.callback_query(F.data.startswith("adm:user:del:yes:"))
    @_owner_only
    async def user_del_yes(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        try:
            uid = int(callback.data.split(":", 4)[4])
        except ValueError:
            await callback.answer(brief("bad_id", lang), show_alert=True)
            return
        delete_user(conn, uid, by_id=callback.from_user.id)
        await _edit(callback, *users_view(conn, lang))

    # -- settings ------------------------------------------------------------------
    @ar.callback_query(F.data == "adm:settings")
    @_owner_only
    async def settings(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await _edit(callback, *settings_view(conn, lang))

    @ar.callback_query(F.data.startswith("adm:set:"))
    @_owner_only
    async def set_start(callback: CallbackQuery, conn: sqlite3.Connection,
                        config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        key = callback.data.split(":", 2)[2]
        if key not in [k for k, _ in SETTINGS_FIELDS]:
            await callback.answer(brief("not_found", lang), show_alert=True)
            return
        await state.set_state(AdmStates.set_value)
        await state.update_data(set_key=key)
        await callback.answer()
        await callback.message.answer(f"{setting_label(key)}\nمقدار جدید را بنویسید:",
                                        reply_markup=_cancel_kb(lang))

    @ar.message(AdmStates.set_value, F.text)
    @menu_escape
    @_owner_only
    async def set_save(message: Message, conn: sqlite3.Connection,
                       config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(message, 'from_user', None), 'id', None))
        data = await state.get_data()
        key = data.get("set_key", "")
        if key not in [k for k, _ in SETTINGS_FIELDS]:
            await state.clear()
            await message.answer(brief("not_found", lang))
            return
        dbmod.set_setting(conn, key, (message.text or "").strip(),
                          by_id=message.from_user.id)
        await state.clear()
        await message.answer(t("adm_saved", lang), reply_markup=_kb([[_btn(t("adm_back_to_settings", lang), "adm:settings")]]))

    # -- clear-chat (every user, not just the owner) -------------------------------
    # Three VISUALLY DISTINCT operations: (a) reset conversation state,
    # (b) delete tracked chat messages, (c) delete saved history.
    @ar.callback_query(F.data == "adm:clear")
    async def clear_card(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await _edit(callback, *clear_view(lang))

    @ar.callback_query(F.data == "adm:clear:no")
    async def clear_cancel(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            await callback.message.edit_text(t("adm_cancelled", lang))

    @ar.callback_query(F.data == "adm:clear:state")
    async def clear_state_cb(callback: CallbackQuery, conn: sqlite3.Connection,
                             config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await clear_state(callback, conn, config, state)

    @ar.callback_query(F.data == "adm:clear:msgs")
    async def clear_msgs_cb(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await clear_msgs_card(callback, conn, config)

    @ar.callback_query(F.data == "adm:clear:msgs:no")
    async def clear_msgs_cancel(callback: CallbackQuery, conn: sqlite3.Connection,
                                config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            await callback.message.edit_text(t("adm_cancelled", lang))

    @ar.callback_query(F.data == "adm:clear:msgs:yes")
    async def clear_msgs_yes_cb(callback: CallbackQuery, conn: sqlite3.Connection,
                                config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await clear_msgs_yes(callback, conn, config)

    # -- delete-all-my-data (every user) --------------------------------------------
    @ar.callback_query(F.data == "adm:wipe")
    async def wipe_card(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await _edit(callback, *wipe_view(lang))

    @ar.callback_query(F.data == "adm:wipe:no")
    async def wipe_cancel(callback: CallbackQuery, conn: sqlite3.Connection, config):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            await callback.message.edit_text(t("adm_cancelled", lang))

    @ar.callback_query(F.data == "adm:wipe:yes")
    async def wipe_yes(callback: CallbackQuery, conn: sqlite3.Connection,
                       config, state: FSMContext):
        lang = dbmod.user_lang_or_default(conn, getattr(getattr(callback, 'from_user', None), 'id', None))
        user_id = callback.from_user.id
        await callback.answer()
        try:
            await callback.message.edit_text(WIPE_WORKING)
        except Exception:
            pass
        try:
            favs, pls, hist = wipe_user_data(conn, user_id)
        except Exception:
            await callback.message.answer(brief("wipe_failed", lang))
            return
        await state.clear()
        await callback.message.answer(
            t("adm_wipe_done", lang).format(favs=favs, pls=pls, hist=hist)
        )

    router.include_router(ar)
