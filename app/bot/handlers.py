"""Launch handlers: professional minimal output, auto intent, two core features.

After /start there is NO reply keyboard and no instruction grid.
After a search there is NO button grid either: the result is one clean
card — the video itself, a clean "Title – Artist" line, and the legal
preview audio with its official download link. No Google/YouTube Music/
Spotify buttons, no artist-search or wrong-song buttons, no stats button,
no empty "not found" boxes, no video-search section for a link.

- Plain text -> song search (clean list; first song as a professional card).
- A link -> NO catalog video search; the link's own video is sent, and
  its audio is identified (Shazam signature first, no token; AudD with a
  token as fallback), then shown as one professional card.
- A sent video, audio, voice or document -> audio identification first.
- /voice زن|مرد <text> -> Persian TTS (cloud service only).
- /admin -> owner-only toggles + user blocking (inline, contextual only).
- Search pagination is the only other button, shown only when needed.

Honesty rules enforced here:
- A catalog search result is never called "identified", and a merely
  similar catalog result is never shown as the identified song.
- Identification without a match says so in one short line; it never
  returns an unrelated nearest song.
- Without a TTS key, TTS says so, it never plays a wrong-language voice.
- No full copyrighted file is downloaded from an unauthorised source
  and resent; the audio sent is the legal catalog preview, and the full
  version is only via the official download/purchase link.
"""
from __future__ import annotations

import asyncio
import io
import re
import secrets
import sqlite3
import time as _time
import uuid

import httpx
from aiogram import Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile, BotCommand, CopyTextButton, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, ReplyKeyboardRemove,
)

from .. import db as dbmod
from .. import timing as _timing
from . import blocked_cache
from ..bot.audio_send import send_song_audio
# NEW (2026-11-03): story clips — independent module. Guarded import so a
# module bug can never break existing handlers or the main menu.
try:
    from .story_clips import register_story_handlers as _register_story_handlers
    from .story_clips import story_button_row as _story_button_row
    _STORY_CLIPS_OK = True
except Exception:
    _STORY_CLIPS_OK = False
# NEW (2026-11-05): Iranian story-clip archive — independent module.
# Guarded import so a module bug can never break existing handlers.
try:
    from .story_archive import register_story_archive_handlers as _register_sarch_handlers
    _STORY_ARCHIVE_OK = True
except Exception:
    _STORY_ARCHIVE_OK = False
# NEW (2026-11-07): exclusive Avano song bank — independent module.
# Guarded import so a module bug can never break existing handlers.
try:
    from .exclusive_bank import register_exclusive_bank_handlers as _register_xbank_handlers
    _XBANK_OK = True
except Exception:
    _XBANK_OK = False
from ..bot.media_send import (
    answer_no_preview, has_audio_stream, probe_audio, send_music, send_video,
    transcode_mp3,
)
from ..config import Config
from ..features import admin as adminfeat
from ..features import bank as bankfeat
from ..features import downloader as dlfeat
from ..features import pro as profeat
from ..i18n import t
from ..logging_config import get_logger, log_event
from ..media import process_link
from ..search import SearchEngine, SearchResult
from ..search.fullsong import find_full_song_file
from ..search.itunes import ITunesProvider
from ..search.deezer import DeezerProvider
from ..search.smart import smart_search
from ..search.lyric_phrase import lyric_phrase_search
from ..search.lyrics import (
    LANG_NAMES, fetch_lyrics_with_source, language_picker_rows,
    lyrics_not_found_text, same_language_notice, translate_text,
)
from ..search.channel import search_channels
from ..search.suggest import is_mood_request, suggest_songs
from ..security import (
    RateLimiter,
    resolve_short_link,
    sanitize_text,
    validate_media_url_for_fetch,
    validate_url_for_fetch,
)
from ..services.audio_id import AudioIdResult, identify_acrcloud, identify_bytes, identify_url
from ..services.brain import brain_clean_query
from ..search.audio_match import fingerprint as audio_fingerprint, find_match as find_audio_match
from ..services.link_media import extract_media_url, extract_page_text
from ..services.openai_speech import synthesize_openai, transcribe_whisper
from ..services.shazam_id import identify_bytes_shazam
from ..services.stt import transcribe_bytes
from ..services.tts import synthesize
from .logic import (
    MENU_TOGGLE_CLOSE, MENU_TOGGLE_OPEN,
    attach_bot_id, brief_error, can_use_feature, capabilities_menu,
    feature_title_localized, format_results, identified_card_acceptable,
    is_menu_toggle, is_menu_toggle_close,
    link_video_delivery,
    match_identified_song, menu_title_key, professional_audio_caption,
    toggle_close_text, toggle_open_text,
    welcome_text,
)

log = get_logger(__name__)
router = Router()

# NEW (2026-11-03): story-clips callbacks live in the independent
# app/bot/story_clips.py module. Guarded: never breaks existing handlers.
if _STORY_CLIPS_OK:
    try:
        _register_story_handlers(router)
    except Exception as _e:
        log.warning("story_clips handlers not registered: %s", _e)

# NEW (2026-11-05): Iranian story-clip archive (story_archive.py).
# Guarded: never breaks existing handlers.
if _STORY_ARCHIVE_OK:
    try:
        _register_sarch_handlers(router)
    except Exception as _e:
        log.warning("story_archive handlers not registered: %s", _e)

# NEW (2026-11-07): exclusive Avano song bank (exclusive_bank.py).
# Guarded: never breaks existing handlers. Registered here — before the
# main audio handler — so the add-flow FSM states win over the normal
# audio flow, exactly like the story-archive manual add.
if _XBANK_OK:
    try:
        _register_xbank_handlers(router)
    except Exception as _e:
        log.warning("exclusive_bank handlers not registered: %s", _e)


def _new_request_id() -> str:
    """Generate a unique request ID for tracing a user request end-to-end.
    Format: 8 hex chars (e.g. 'a1b2c3d4'). Used in logs to correlate
    all stages (receive → identify → search → download → send) of one
    request. No secrets or tokens are ever included."""
    return uuid.uuid4().hex[:8]
rate_limiter = RateLimiter()
# Lyrics fetched for cards the user opened: key -> (title, artist, lyrics).
LYRICS_CACHE: dict[str, tuple[str, str, str]] = {}
# Translations shown: (key, lang) -> translated text.
TRANSLATIONS: dict[tuple[str, str], str] = {}

# HOTFIX 2026-11-08: LYRICS_CACHE had no cap/TTL (slow unbounded leak).
# Cap at 200 entries, FIFO eviction on insert.
LYRICS_CACHE_MAX = 200


def _lyrics_cache_put(key: str, value: tuple[str, str, str]) -> None:
    """Store a lyrics entry, evicting the oldest first when at the cap."""
    if key not in LYRICS_CACHE and len(LYRICS_CACHE) >= LYRICS_CACHE_MAX:
        LYRICS_CACHE.pop(next(iter(LYRICS_CACHE)), None)
    LYRICS_CACHE[key] = value


def _user_lang(conn, user_id: int | None) -> str:
    """UI language for a user: stored preference, Persian default.

    NOTE (honest limit): on Render's free tier bot.db is ephemeral — the
    choice persists for the bot's uptime, but a restart/redeploy wipes it
    (same as all other per-user settings). Never claim cross-restart
    persistence on the free tier.
    """
    try:
        return dbmod.user_lang_or_default(conn, user_id or 0)
    except Exception:
        return "fa"


def _lang_picker_kb() -> InlineKeyboardMarkup:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("lang_fa", "fa"), callback_data="lang:set:fa"),
        InlineKeyboardButton(text=t("lang_en", "fa"), callback_data="lang:set:en"),
        InlineKeyboardButton(text=t("lang_ar", "fa"), callback_data="lang:set:ar"),
    ]])


def _track(conn, message: Message, sent) -> None:
    """Record an outgoing bot message for «پاک‌سازی گفتگو» (best effort)."""
    try:
        if sent is not None and getattr(sent, "message_id", None):
            adminfeat.track(conn, message.from_user.id, message.chat.id, sent.message_id)
    except Exception:
        pass


def _track_incoming(conn, message: Message) -> None:
    """Record an incoming user message so «پاک‌سازی گفتگو» can delete the
    user's own messages too (bots may delete incoming messages in private
    chats). Without this, clear-chat visibly "does nothing" because the
    user's messages all remain."""
    try:
        if message.from_user and getattr(message, "message_id", None):
            adminfeat.track(conn, message.from_user.id, message.chat.id,
                            message.message_id)
    except Exception:
        pass


async def _answer(message: Message, conn, text: str, **kwargs):
    """message.answer + outbox tracking."""
    sent = await message.answer(text, **kwargs)
    _track(conn, message, sent)
    return sent


def _web_button(conn, config, lang: str = "fa"):
    """Professional web-fallback button, only when WEB_URL is configured."""
    url = dbmod.get_web_url(conn, config)
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("menu_human_voice_web", lang), url=url)]])


def is_owner(message, config: Config) -> bool:
    uid = getattr(getattr(message, "from_user", None), "id", None)
    return config.owner_id is not None and uid == config.owner_id


def _register(message: Message, conn) -> None:
    if message.from_user:
        dbmod.upsert_user(conn, message.from_user.id,
                          message.from_user.username or "", message.from_user.first_name or "")


def _blocked(conn, user_id: int) -> bool:
    user = dbmod.get_user(conn, user_id)
    return bool(user and user.get("blocked"))


def _with_signature(conn, config: Config, text: str) -> str:
    """Append the bot-ID signature line: the ONE fixed, deliberate place
    the bot ID appears on outbound content (welcome/help). Never under
    media files (no-links policy)."""
    bot_id = dbmod.get_bot_id(conn, config).strip()
    if not bot_id:
        return text
    if not bot_id.startswith("@") and not bot_id.startswith("http"):
        bot_id = "@" + bot_id
    if bot_id in text:
        return text
    return f"{text}\n\n🤖 {bot_id}"


@router.message(Command("start"))
async def cmd_start(message: Message, conn: sqlite3.Connection, config: Config,
                    state: FSMContext | None = None):
    _register(message, conn)
    # Reset the conversation to its initial state without touching any
    # saved user data (favorites, playlists, history, settings).
    if state is not None:
        try:
            await state.clear()
        except Exception:
            pass
    uid = getattr(getattr(message, "from_user", None), "id", None)
    # FIRST /start: no stored language -> language picker BEFORE the
    # normal welcome/menu. The choice is stored per user (migration 016).
    if dbmod.user_language(conn, uid or 0) is None:
        await _answer(message, conn, t("lang_pick_title", "fa"),
                      reply_markup=_lang_picker_kb())
        return
    ulang = _user_lang(conn, uid)
    # Clear any OLD persistent ReplyKeyboard (pre-2026-10-30). The old
    # menu was sent with is_persistent=True and stays visible until
    # explicitly removed — sending InlineKeyboard alone does NOT clear it.
    # Send removal then delete the message immediately (keyboard removal
    # persists after deletion, keeping the chat clean).
    try:
        rm_msg = await message.answer(t("m_menu_updated", ulang),
                                      reply_markup=ReplyKeyboardRemove())
        try:
            await rm_msg.delete()
        except Exception:
            pass
    except Exception:
        pass
    await _answer(message, conn, _with_signature(conn, config, welcome_text(conn, ulang)),
                  reply_markup=_main_reply_kb(conn, uid,
                      is_admin=(config.owner_id is not None and uid == config.owner_id)))


@router.callback_query(F.data.startswith("lang:set:"))
async def lang_set(callback, conn: sqlite3.Connection, config: Config):
    """Store the picked UI language, then show the normal welcome/menu."""
    code = (callback.data.split("lang:set:", 1)[1] or "").strip()
    if code not in ("fa", "en", "ar"):
        await callback.answer(t("m_lang_unsupported", "fa"), show_alert=True)
        return
    uid = getattr(getattr(callback, "from_user", None), "id", None)
    dbmod.set_user_language(conn, uid or 0, code)
    await callback.answer(t("lang_picked", code))
    try:
        await callback.message.edit_text(t("lang_changed_hint", code))
    except Exception:
        pass
    await _answer(callback.message, conn,
                  _with_signature(conn, config, welcome_text(conn, code)),
                  reply_markup=_main_reply_kb(conn, uid,
                      is_admin=(config.owner_id is not None and uid == config.owner_id)))


@router.callback_query(F.data == "lang:change")
async def lang_change(callback, conn: sqlite3.Connection, config: Config):
    """Re-open the language picker (from settings/help)."""
    await callback.message.answer(t("lang_pick_title", _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))),
                                  reply_markup=_lang_picker_kb())
    await callback.answer()


def _main_reply_kb(conn, user_id: int | None = None,
                   is_admin: bool = False) -> InlineKeyboardMarkup:
    """Main menu as INLINE keyboard - matches the reference design exactly.

    Layout (2 columns):
    Row 1: [دانلود اینستاگرام] [جستجوی آهنگ]
    Row 2: [متن آهنگ] [شناسایی موسیقی]  (admin beta: [🎛️ ساخت ریمیکس هوشمند] replaces it)
    Row 3: [تاریخچه جستجو] [درخواست آهنگ]
    Row 4: [راهنما و پشتیبانی] (full width)

    Per user spec: the remix button REPLACES the «شناسایی موسیقی» button
    in the main menu (admin only, beta). The identify feature itself is
    NOT deleted — only its main-menu slot is reused, restorable later.
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    ulang = _user_lang(conn, user_id)

    # Admin beta: remix replaces the identify slot. Import guarded once.
    _remix_active = False
    _remix_dbg = f"is_admin={is_admin}"
    if is_admin:
        try:
            from .remix import is_enabled as _remix_enabled
            _en = bool(_remix_enabled())
            _remix_dbg += f" import_ok=True enabled={_en}"
            _remix_active = _en
        except Exception as e:
            _remix_dbg += f" import_ok=False err={type(e).__name__}:{e}"
            _remix_active = False
    else:
        _remix_dbg += " skipped(not admin)"
    try:
        log_event(log, "remix_menu_check", dbg=_remix_dbg, uid=str(user_id),
                  active=_remix_active)
    except Exception:
        pass

    # Exact button layout from the reference design
    # Using the existing callback mappings
    # NOTE (2026-11-01): trending button occupies the identify slot per user
    # request. The identify feature itself is NOT deleted -- it remains
    # available on video/audio cards via the identify button there.
    rows = [
        [
            InlineKeyboardButton(
                text="🎧 مینی‌اپ آوانو",
                web_app=WebAppInfo(
                    url="https://avano-miniapp-api.avano.workers.dev/")),
        ],
        [
            InlineKeyboardButton(
                text="📷 " + t("m_downloader_instagram", ulang),
                callback_data="dl:open"),
            InlineKeyboardButton(
                text="🔍 " + t("m_song_search", ulang),
                callback_data="menu:search"),
        ],
        [
            InlineKeyboardButton(
                text="📄 " + t("m_lyrics", ulang),
                callback_data="menu:lyrics"),
            InlineKeyboardButton(
                text="🔥 " + t("m_trending", ulang),
                callback_data="trending:open"),
        ],
        [
            InlineKeyboardButton(
                text="🕐 " + t("m_history", ulang),
                callback_data="menu:history"),
            InlineKeyboardButton(
                text="🎵 " + t("m_song_request", ulang),
                callback_data="suggest:start"),
        ],
        [
            InlineKeyboardButton(
                text="🔥 " + t("m_trending_hot", ulang),
                callback_data="kabo:open"),
        ],
        [
            InlineKeyboardButton(
                text="❓ " + t("m_help_support", ulang),
                callback_data="menu:help"),
        ],
    ]
    # NEW (2026-11-03): story clips button — one row from the independent
    # module, inserted before Help. Guarded; menu works without it.
    if _STORY_CLIPS_OK:
        try:
            rows.insert(-1, _story_button_row())
        except Exception:
            pass
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _run_menu_toggle(message: Message, conn: sqlite3.Connection,
                           config: Config | None, text: str) -> None:
    """Flip the persistent-menu collapse state for this user and re-send
    the keyboard. A pure display change: it never clears FSM state and
    never touches any feature — the 10 buttons keep working in both
    states."""
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    collapsed = is_menu_toggle_close(text)
    dbmod.set_menu_collapsed(conn, message.from_user.id, collapsed)
    if collapsed:
        status = t("h_menu_closed", ulang)
    else:
        status = t("m_menu_opened", ulang)
    await _answer(message, conn, status,
                  reply_markup=_main_reply_kb(conn, message.from_user.id,
                      is_admin=bool(config and config.owner_id is not None
                                    and message.from_user.id == config.owner_id)))


def _menu_callback(key: str) -> str | None:
    """Map a feature key to the callback that opens it."""
    mapping = {
        "song_search": "menu:search",
        "suggest": "suggest:start",
        "downloader": "dl:open",
        "professional": "pro:open",
        "lyrics": "menu:lyrics",
        "clear_chat": "adm:clear",
        "history": "menu:history",
        "help_support": "menu:help",
        "link_fetch": "menu:link",
        "audio_id": "menu:file",
    }
    if key in mapping:
        return mapping[key]
    if key.startswith("custom_"):
        return f"menu:custom:{key}"
    return None


@router.callback_query(F.data == "menu:open")
async def menu_open(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await callback.answer()
    rows = []
    for f in capabilities_menu(conn):
        cb = _menu_callback(f["key"])
        if cb:
            rows.append([InlineKeyboardButton(text=f["title"], callback_data=cb)])
    if not rows:
        await callback.message.answer(t("m_no_features", ulang))
        return
    sent = await callback.message.answer(
        "✨ قابلیت‌ها:\nیکی را انتخاب کنید — هر دکمه مستقیم همان قابلیت را اجرا می‌کند.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    try:
        adminfeat.track(conn, callback.from_user.id, callback.message.chat.id, sent.message_id)
    except Exception:
        pass


@router.callback_query(F.data == "menu:link")
async def menu_link_hint(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await callback.answer()
    await callback.message.answer(
        t("m_link_hint", ulang))


@router.callback_query(F.data == "menu:file")
async def menu_file_hint(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await callback.answer()
    await callback.message.answer(
        t("m_file_hint", ulang))


# ---------------------------------------------------------------------------
# Smart Remix beta (admin only). All logic lives in app/bot/remix.py;
# these handlers only wire the Telegram flow.
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "remix:start")
async def remix_start(callback, conn: sqlite3.Connection, config: Config,
                      state: FSMContext):
    """Admin-only entry point for the Smart Remix beta."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from .remix import is_enabled, is_locked
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await callback.answer()
    uid = callback.from_user.id
    if not (config.owner_id is not None and uid == config.owner_id):
        return
    if not is_enabled():
        await callback.message.answer("🧪 نسخه آزمایشی ریمیکس فعلاً غیرفعال است.")
        return
    if is_locked():
        await callback.message.answer(
            "⏳ یک ریمیکس در حال ساخت است. لطفاً بعد از اتمام دوباره تلاش کنید.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 ارسال فایل صوتی",
                              callback_data="remix:method:file")],
        [InlineKeyboardButton(text="🔍 جستجو با نام آهنگ/خواننده",
                              callback_data="remix:method:search")],
        [InlineKeyboardButton(text="❌ لغو", callback_data="remix:cancel")],
    ])
    await callback.message.answer(
        "🎛️ <b>ساخت ریمیکس هوشمند</b> (نسخه آزمایشی)\n\n"
        "روش سفارش را انتخاب کنید:",
        parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "remix:method:file")
async def remix_method_file(callback, conn: sqlite3.Connection, config: Config,
                            state: FSMContext):
    """Method 1: user uploads 2 audio files."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    await callback.answer()
    uid = callback.from_user.id
    if not (config.owner_id is not None and uid == config.owner_id):
        return
    await state.set_state("remix:await_file1")
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ لغو", callback_data="remix:cancel"),
    ]])
    await callback.message.answer(
        "📤 <b>روش ارسال فایل</b>\n\n"
        "🎵 لطفاً <b>فایل صوتی اول</b> را ارسال کنید.\n"
        "• حداکثر ۶۰ ثانیه\n"
        "• فرمت: MP3 / WAV / OGG",
        parse_mode="HTML", reply_markup=cancel_kb)


@router.callback_query(F.data == "remix:method:search")
async def remix_method_search(callback, conn: sqlite3.Connection, config: Config,
                              state: FSMContext):
    """Method 2: user types song/artist names, bot searches."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    await callback.answer()
    uid = callback.from_user.id
    if not (config.owner_id is not None and uid == config.owner_id):
        return
    await state.set_state("remix:await_names")
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ لغو", callback_data="remix:cancel"),
    ]])
    await callback.message.answer(
        "🔍 <b>روش جستجو با نام</b>\n\n"
        "نام <b>دو آهنگ</b> یا <b>دو خواننده</b> را بنویسید.\n"
        "هر کدام را در یک خط جدا بنویسید:\n\n"
        "<i>مثال:</i>\n"
        "Ebi - Ghorbat\n"
        "Googoosh - Ageh Bemooni\n\n"
        "⚠️ فقط آهنگ‌هایی که فایل کامل قابل دانلود داشته باشند "
        "وارد ریمیکس می‌شوند.",
        parse_mode="HTML", reply_markup=cancel_kb)


@router.callback_query(F.data == "remix:cancel")
async def remix_cancel(callback, conn: sqlite3.Connection, config: Config,
                       state: FSMContext):
    """Cancel the remix flow and clean up temp files."""
    import shutil
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await callback.answer()
    try:
        data = await state.get_data()
        tmpdir = data.get("remix_tmpdir")
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        await state.clear()
    except Exception:
        pass
    await callback.message.answer("❌ ساخت ریمیکس لغو شد.")


async def _run_menu_title(message: Message, conn: sqlite3.Connection,
                          config: Config, state: FSMContext | None,
                          key: str) -> None:
    """Run the real feature behind a persistent-menu button title.

    Reply-keyboard buttons arrive as plain text; this maps the (possibly
    admin-renamed) title back to its feature key and performs the same
    operation the inline «قابلیت‌ها» menu would trigger.
    """
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    uid = message.from_user.id
    # A menu tap always escapes any stale conversation state first: the
    # user can never be trapped in a dead-end step.
    if state is not None:
        try:
            await state.clear()
        except Exception:
            pass
    if key == "song_search":
        await message.answer(
            t("m_search_hint", ulang))
    elif key == "suggest":
        allowed, reason = can_use_feature(conn, uid, "suggest")
        if not allowed:
            await message.answer(t(reason, ulang)); return
        if state is not None:
            await state.set_state("suggest")
        await message.answer(t("suggest_prompt", ulang),
                             reply_markup=_cancel_kb(ulang))
    elif key == "downloader":
        allowed, reason = can_use_feature(conn, uid, "downloader")
        if not allowed:
            await message.answer(t(reason, ulang)); return
        if state is not None:
            await state.set_state(dlfeat.DlStates.await_url)
        await message.answer(f"{dlfeat.TITLE}\n\n{dlfeat.EXPLAINER}\n\n{dlfeat.PROMPT_SEND_LINK}",
                             reply_markup=_cancel_kb(ulang))
    elif key == "professional":
        allowed, reason = can_use_feature(conn, uid, "professional")
        if not allowed:
            await message.answer(t(reason, ulang)); return
        text, kb = profeat._render_pro_view(conn, uid)
        await message.answer(text, reply_markup=kb)
    elif key == "lyrics":
        allowed, reason = can_use_feature(conn, uid, "lyrics")
        if not allowed:
            await message.answer(t(reason, ulang)); return
        if state is not None:
            await state.set_state("lyr:await_query")
        await message.answer(t("m_lyrics_prompt", ulang),
                             reply_markup=_cancel_kb(ulang))
    elif key == "clear_chat":
        text, kb = adminfeat.clear_view(ulang)
        await message.answer(text, reply_markup=kb)
    elif key == "history":
        items = dbmod.recent_searches(conn, uid, limit=15)
        if not items:
            await message.answer(t("m_history_empty", ulang))
            return
        import datetime as _dt
        lines = [t("m_history_title", ulang)]
        for it in items:
            ts = _dt.datetime.fromtimestamp(it["created_at"]).strftime("%m-%d %H:%M")
            lines.append(f"• {it['query'][:60]} · {ts}")
        await message.answer("\n".join(lines))
    elif key == "help_support":
        # Honest title: the feature promises «راهنما و پشتیبانی», so the
        # support contact is shown when the owner configured one.
        help_text = t("help", ulang)
        support = dbmod.get_setting(conn, "support_username", "").strip().lstrip("@")
        if support:
            help_text += t("h_support_line", ulang).format(support=support)
        await message.answer(help_text)
    elif key == "link_fetch":
        await message.answer(
            t("m_link_hint", ulang))
    elif key == "audio_id":
        allowed, reason = can_use_feature(conn, uid, "audio_id")
        if not allowed:
            await message.answer(t(reason, ulang)); return
        # Explicit pending-file state: the button now arms a real wait for
        # a file instead of sending a dead hint. The state is honoured by
        # media_handler (file -> identification) and text_handler (a text
        # mid-wait gets a clear "still waiting" notice, never a silent die).
        if state is not None:
            await state.set_state("audio_id:await_file")
        await message.answer(
            t("m_file_hint", ulang),
            reply_markup=_cancel_kb(ulang))
    else:
        feat = dbmod.get_feature(conn, key)
        if feat and feat.get("enabled"):
            await message.answer(feat.get("message") or feat.get("title") or "")


def _menu_title_key(conn, text: str) -> str | None:
    """Backwards-compatible alias (moved to logic.menu_title_key)."""
    return menu_title_key(conn, text)


_LINK_RE = re.compile(
    r"https?://[^\s<>\"\u200c]+"
    r"|(?:www\.)?(?:instagram\.com|tiktok\.com|aparat\.com"
    r"|youtube\.com|youtu\.be)/[^\s<>\"\u200c]*",
    re.IGNORECASE)


def _extract_link(text: str) -> str | None:
    """Find the first link anywhere in the message text.

    A link must reach the link processor even when it is pasted with
    surrounding words, without a scheme, or behind a shortener — it must
    never fall through to the audio/video file prompts or text search.
    """
    m = _LINK_RE.search(text or "")
    if not m:
        return None
    url = m.group(0).rstrip(".,;:!?)]}\"'")
    if not url.lower().startswith("http"):
        url = "https://" + url
    return resolve_short_link(url)


@router.callback_query(F.data == "menu:main")
async def menu_main(callback, conn: sqlite3.Connection, config: Config,
                    state: FSMContext | None = None):
    """«🏠 منوی اصلی»: escape any flow, clear stale state, land back on
    the persistent bottom menu. Every submenu links here."""
    await callback.answer()
    if state is not None:
        try:
            await state.clear()
        except Exception:
            pass
    await callback.message.answer(
        _with_signature(conn, config, welcome_text(conn)),
        reply_markup=_main_reply_kb(conn, callback.from_user.id,
            is_admin=(config.owner_id is not None
                      and callback.from_user.id == config.owner_id)))


def _cancel_kb(lang: str = "fa") -> InlineKeyboardMarkup:
    """In-bot «لغو»: every multi-step flow offers it, so the user never
    needs the phone's hardware back button (which sends nothing to the
    bot) and can never be trapped in a dead-end step."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("m_cancel_btn", lang), callback_data="flow:cancel")]])


@router.callback_query(F.data == "flow:cancel")
async def flow_cancel(callback, conn: sqlite3.Connection, config: Config,
                      state: FSMContext | None = None):
    """Cancel any in-progress flow: clear the conversation state and land
    back on the one predictable standard state — the persistent main-menu
    keyboard plus a short status line."""
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await callback.answer()
    if state is not None:
        try:
            await state.clear()
        except Exception:
            pass
    await callback.message.answer(
        t("m_cancel_done", ulang),
        reply_markup=_main_reply_kb(conn, callback.from_user.id,
            is_admin=(config.owner_id is not None
                      and callback.from_user.id == config.owner_id)))


@router.callback_query(F.data == "menu:search")
async def menu_search_hint(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await callback.answer()
    await callback.message.answer(
        t("m_search_hint", ulang))


@router.callback_query(F.data == "menu:lyrics")
async def menu_lyrics(callback, conn: sqlite3.Connection, config: Config, state: FSMContext):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    allowed, reason = can_use_feature(conn, callback.from_user.id, "lyrics")
    if not allowed:
        await callback.answer(t(reason, ulang), show_alert=True); return
    await state.set_state("lyr:await_query")
    await callback.answer()
    # In-bot cancel: without it this entry point (inline «قابلیت‌ها» menu)
    # leaves the user in lyr:await_query with no visible way back.
    await callback.message.answer(t("m_lyrics_prompt", ulang),
                                  reply_markup=_cancel_kb(ulang))


# ---------------------------------------------------------------------------
# Trending songs (Snaplytics daily chart)
# ---------------------------------------------------------------------------

_TRENDING_PER_PAGE = 10


def _trending_page_kb(items: list[dict], page: int, total_pages: int,
                      ulang: str):
    """Build message text + inline keyboard for a trending page."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from datetime import datetime

    start = page * _TRENDING_PER_PAGE
    page_items = items[start:start + _TRENDING_PER_PAGE]

    mv_icon = {"up": "↑", "down": "↓", "new": "🆕", "same": "▪️"}
    lines = []
    for it in page_items:
        icon = mv_icon.get(it.get("movement", "same"), "▪️")
        title = it["title"]
        artist = it.get("artist", "")
        if artist:
            lines.append(f"{it['rank']}. {icon} <b>{title}</b> — {artist}")
        else:
            lines.append(f"{it['rank']}. {icon} <b>{title}</b> 🎙️")

    # Last update date
    _, fetched_at = __import__("app.bot.trending", fromlist=["load_trending"])\
        .load_trending()
    if fetched_at:
        dt = datetime.fromtimestamp(fetched_at).strftime("%Y-%m-%d")
    else:
        dt = "—"

    text = (
        f"🔥 <b>{t('m_trending', ulang)}</b>\n"
        f"📅 به‌روزرسانی: {dt}\n\n"
        + "\n".join(lines)
    )

    # Buttons: page navigation + per-song search
    kb_rows: list[list] = []
    # Search buttons (2 per row)
    btn_row: list = []
    for it in page_items:
        btn_row.append(InlineKeyboardButton(
            text=f"🔍 {it['rank']}",
            callback_data=f"trending:search:{it['rank']}"))
        if len(btn_row) == 5:
            kb_rows.append(btn_row)
            btn_row = []
    if btn_row:
        kb_rows.append(btn_row)
    # Pagination
    nav: list = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی",
                                       callback_data=f"trending:page:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="بعدی ▶️",
                                       callback_data=f"trending:page:{page+1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton(text="🏠 منوی اصلی",
                                        callback_data="menu:main")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data == "trending:open")
async def trending_open(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await callback.answer(
        "🎧 آوانو، تجربه‌ای نو برای تو!\n"
        "✨ در حال پیدا کردن داغ‌ترین ترندهای موسیقی برای تو...",
        show_alert=False)
    try:
        from .trending import get_trending
        items, fetched_at, _ = await get_trending()
    except Exception:
        items, fetched_at = [], 0
    if not items:
        await callback.message.answer(
            "❌ در حال حاضر فهرست ترند در دسترس نیست. لطفاً بعداً تلاش کنید.")
        return
    total_pages = (len(items) + _TRENDING_PER_PAGE - 1) // _TRENDING_PER_PAGE
    text, kb = _trending_page_kb(items, 0, total_pages, ulang)
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("trending:page:"))
async def trending_page(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    try:
        page = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0
    await callback.answer()
    try:
        from .trending import get_trending
        items, fetched_at, _ = await get_trending()
    except Exception:
        items = []
    if not items:
        await callback.message.answer("❌ فهرست در دسترس نیست.")
        return
    total_pages = (len(items) + _TRENDING_PER_PAGE - 1) // _TRENDING_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    text, kb = _trending_page_kb(items, page, total_pages, ulang)
    try:
        await callback.message.edit_text(text, reply_markup=kb,
                                         parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb,
                                      parse_mode="HTML")


@router.callback_query(F.data.startswith("trending:search:"))
async def trending_search(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    try:
        rank = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("❌", show_alert=True)
        return
    await callback.answer("🔍 در حال جست‌وجو...")
    try:
        from .trending import load_trending
        items, _ = load_trending()
        item = next((x for x in items if x["rank"] == rank), None)
    except Exception:
        item = None
    if not item:
        await callback.message.answer("❌ آهنگ پیدا نشد.")
        return
    query = item["title"]
    if item.get("artist"):
        query += " " + item["artist"]
    # Use the existing song-search engine directly; no FSM state is set,
    # so other conversations are unaffected.
    try:
        await _do_search(callback.message, conn, query, config=config)
    except Exception as e:
        await callback.message.answer(
            "❌ خطا در جست‌وجو. لطفاً بعداً تلاش کنید.")


# ---------------------------------------------------------------------------
# Kabo hot trends (growth-ranked, 5 public items)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "kabo:open")
async def kabo_open(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await callback.answer(
        "🎧 آوانو، تجربه‌ای نو برای تو!\n"
        "✨ در حال پیدا کردن داغ‌ترین ترندهای موسیقی برای تو...",
        show_alert=False)
    try:
        from .trending_kabo import get_hot_trends
        items, fetched_at, _ = await get_hot_trends()
    except Exception:
        items, fetched_at = [], 0
    if not items:
        await callback.message.answer(
            "❌ در حال حاضر ترندهای داغ در دسترس نیست. لطفاً بعداً تلاش کنید.")
        return

    from datetime import datetime
    if fetched_at:
        dt = datetime.fromtimestamp(fetched_at).strftime("%Y-%m-%d")
    else:
        dt = "—"

    lines = []
    for it in items:
        title = it["title"]
        artist = it.get("artist", "")
        header = f"{it['rank']}. <b>{title}</b>"
        if artist:
            header += f" — {artist}"
        details = []
        if it.get("new_24h"):
            details.append(f"📈 +{it['new_24h']} ریلز در ۲۴ ساعت")
        if it.get("growth_pct"):
            details.append(f"🚀 رشد {it['growth_pct']}")
        if it.get("audio_url"):
            details.append(f'<a href="{it["audio_url"]}">🎵 باز کردن در اینستاگرام</a>')
        lines.append(header + ("\n" + " | ".join(details) if details else ""))

    text = (
        f"🔥 <b>{t('m_trending_hot', ulang)}</b>\n"
        f"📅 به‌روزرسانی: {dt}\n"
        f"📊 مرتب‌سازی بر اساس رشد واقعی\n\n"
        + "\n\n".join(lines)
    )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb_rows: list[list] = []
    btn_row: list = []
    for it in items:
        btn_row.append(InlineKeyboardButton(
            text=f"🔍 {it['rank']}",
            callback_data=f"kabo:search:{it['rank']}"))
    kb_rows.append(btn_row)
    kb_rows.append([InlineKeyboardButton(text="🏠 منوی اصلی",
                                        callback_data="menu:main")])
    await callback.message.answer(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("kabo:search:"))
async def kabo_search(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    try:
        rank = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("❌", show_alert=True)
        return
    await callback.answer("🔍 در حال جست‌وجو...")
    try:
        from .trending_kabo import load_hot_trends
        items, _ = load_hot_trends()
        item = next((x for x in items if x["rank"] == rank), None)
    except Exception:
        item = None
    if not item:
        await callback.message.answer("❌ آهنگ پیدا نشد.")
        return
    query = item["title"]
    if item.get("artist"):
        query += " " + item["artist"]
    try:
        await _do_search(callback.message, conn, query, config=config)
    except Exception:
        await callback.message.answer(
            "❌ خطا در جست‌وجو. لطفاً بعداً تلاش کنید.")


@router.callback_query(F.data == "menu:history")
async def menu_history(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await callback.answer()
    items = dbmod.recent_searches(conn, callback.from_user.id, limit=15)
    if not items:
        await callback.message.answer("🕘 تاریخچه جستجوهای اخیر\n\nهنوز جستجویی ثبت نشده است.")
        return
    import datetime as _dt
    lines = [t("m_history_title", ulang)]
    for it in items:
        ts = _dt.datetime.fromtimestamp(it["created_at"]).strftime("%m-%d %H:%M")
        lines.append(f"• {it['query'][:60]} · {ts}")
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data == "menu:help")
async def menu_help(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("lang_change_btn", ulang),
                             callback_data="lang:change")]])
    await callback.message.answer(t("help", ulang), reply_markup=kb)


@router.callback_query(F.data.startswith("menu:custom:"))
async def menu_custom(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    key = callback.data.split(":", 2)[2]
    feat = dbmod.get_feature(conn, key)
    if not feat or not feat.get("enabled"):
        await callback.answer(t("feature_disabled", ulang), show_alert=True); return
    await callback.answer()
    await callback.message.answer(feat.get("message") or feat.get("title") or "")


@router.message(Command("help"))
async def cmd_help(message: Message, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    _register(message, conn)
    # One useful button, not a stats button: voice-to-text.
    rows = [[InlineKeyboardButton(text=t("menu_voice_to_text", ulang),
                             callback_data="voice_to_text:start")],
            [InlineKeyboardButton(text=t("menu_request_song", ulang),
                             callback_data="suggest:start")],
            [InlineKeyboardButton(text=t("menu_playlists", ulang),
                             callback_data="playlists:list")],
            [InlineKeyboardButton(text=t("lang_change_btn", ulang),
                             callback_data="lang:change")]]
    web = _web_button(conn, config, ulang)
    if web is not None:
        rows.extend(web.inline_keyboard)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    # Honest brain status: the superior-intelligence layer is OpenAI and
    # needs OPENAI_API_KEY; without it the bot says so, never fakes an
    # AI answer. Basic rule-based search still works without the key.
    brain_line = (t("m_brain_on", ulang) if config.openai_api_key
                  else t("m_brain_off", ulang))
    await _answer(message, conn, _with_signature(
        conn, config, t("help", ulang) + "\n\n" + brain_line + "\n\n" + t("voice_usage", ulang)),
                    reply_markup=kb)


@router.message(Command("voicetotext", "stt"))
async def cmd_voice_to_text(message: Message, conn: sqlite3.Connection,
                            config: Config, state: FSMContext):
    """Button/command entry for voice-to-text: the next voice is transcribed."""
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    _register(message, conn)
    if _blocked(conn, message.from_user.id):
        await _answer(message, conn, t("blocked", ulang)); return
    allowed, reason = can_use_feature(conn, message.from_user.id, "voice_to_text")
    if not allowed:
        await _answer(message, conn, t(reason, ulang)); return
    await state.set_state("voice_to_text")
    await _answer(message, conn, t("voice_to_text_prompt", ulang),
                  reply_markup=_cancel_kb(ulang))


@router.callback_query(F.data == "voice_to_text:start")
async def voice_to_text_button(callback, conn: sqlite3.Connection,
                               config: Config, state: FSMContext):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    allowed, reason = can_use_feature(conn, callback.from_user.id, "voice_to_text")
    if not allowed:
        await callback.answer(t(reason, ulang), show_alert=True); return
    await state.set_state("voice_to_text")
    await callback.answer()
    await callback.message.answer(t("voice_to_text_prompt", ulang),
                                  reply_markup=_cancel_kb(ulang))



@router.message(Command("suggest", "request"))
async def cmd_suggest(message: Message, conn: sqlite3.Connection, config: Config, state: FSMContext):
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    _register(message, conn)
    await state.set_state("suggest")
    await _answer(message, conn, t("suggest_prompt", ulang),
                  reply_markup=_cancel_kb(ulang))


@router.callback_query(F.data == "suggest:start")
async def suggest_button(callback, conn: sqlite3.Connection, config: Config, state: FSMContext):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await state.set_state("suggest")
    await callback.answer()
    await _answer(callback.message, conn, t("suggest_prompt", ulang),
                  reply_markup=_cancel_kb(ulang))


@router.message(Command("playlists"))
async def cmd_playlists(message: Message, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    _register(message, conn)
    pls = dbmod.list_playlists(conn, message.from_user.id)
    if not pls:
        await _answer(message, conn, t("playlists_empty", ulang)); return
    lines = [t("m_playlists_title", ulang)]
    for pl in pls:
        items = dbmod.playlist_items(conn, pl["id"])
        lines.append(f"• {pl['name']} ({len(items)} آهنگ)" + (f" — {pl['description']}" if pl.get("description") else ""))
    await _answer(message, conn, "\n".join(lines))


@router.callback_query(F.data == "playlists:list")
async def playlists_button(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await callback.answer()
    pls = dbmod.list_playlists(conn, callback.from_user.id)
    if not pls:
        await callback.message.answer(t("playlists_empty", ulang)); return
    lines = [t("m_playlists_title", ulang)]
    for pl in pls:
        lines.append(f"• {pl['name']} " + t("h_pl_songs", ulang).format(n=len(dbmod.playlist_items(conn, pl["id"]))))
    await callback.message.answer("\n".join(lines))


@router.message(Command("newplaylist"))
async def cmd_newplaylist(message: Message, command: CommandObject, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    _register(message, conn)
    name = (command.args or "").strip()
    if not name:
        await _answer(message, conn, t("m_newplaylist_prompt", ulang)); return
    dbmod.create_playlist(conn, message.from_user.id, name)
    await _answer(message, conn, t("playlist_created", ulang))


@router.message(Command("favorites"))
async def cmd_favorites(message: Message, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    _register(message, conn)
    favs = dbmod.list_favorites(conn, message.from_user.id)
    if not favs:
        await _answer(message, conn, t("m_fav_empty", ulang)); return
    lines = [t("m_fav_title", ulang)]
    for f in favs:
        lines.append(f"• {f['title']} – {f['artist']}" if f["artist"] else f"• {f['title']}")
    await _answer(message, conn, "\n".join(lines))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, conn: sqlite3.Connection, state: FSMContext):
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    await state.clear()
    await _answer(message, conn, t("cancelled", ulang),
                  reply_markup=_main_reply_kb(conn, message.from_user.id))


@router.message(Command("admin"))
async def cmd_admin(message: Message, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    _register(message, conn)
    if not is_owner(message, config):
        await message.answer(t("not_admin", ulang))
        return
    feats = dbmod.list_features(conn)
    buttons = [[InlineKeyboardButton(
        text=f"{'✅' if f['enabled'] else '❌'} {f['title']}",
        callback_data=f"feat:toggle:{f['key']}")] for f in feats]
    users = dbmod.list_users(conn, limit=10)
    lines = [t("m_admin_panel_owner", ulang),
             t("h_admin_users", ulang).format(n=dbmod.count_users(conn)),
             t("h_admin_botid", ulang).format(state=t("cmn_set", ulang) if dbmod.get_bot_id(conn, config) else t("cmn_unset", ulang)),
             t("h_admin_web", ulang).format(state=t("cmn_set", ulang) if dbmod.get_web_url(conn, config) else t("cmn_unset", ulang)),
             t("h_admin_channels", ulang).format(n=len(dbmod.get_channels(conn, config)))]
    for u in users:
        lines.append(f"• {u['telegram_id']} {u.get('username') or ''} "
                     f"{t('cmn_blocked', ulang) if u.get('blocked') else ''}")
    buttons.append([InlineKeyboardButton(text=t("m_admin_full", ulang), callback_data="adm:panel")])
    buttons.append([InlineKeyboardButton(text=t("m_bank_btn", ulang), callback_data="bank:panel")])
    # NEW (2026-11-05): Iranian story-clip archive management (guarded).
    if _STORY_ARCHIVE_OK:
        buttons.append([InlineKeyboardButton(text="🎬 آرشیو استوری ایرانی",
                                             callback_data="sarch:panel")])
    # NEW (2026-11-07): exclusive Avano song bank (admin panel only —
    # no new main-menu item, per the user's order).
    if _XBANK_OK:
        buttons.append([InlineKeyboardButton(text="💎 بانک اختصاصی آوانو",
                                             callback_data="xbank:panel")])
    await _answer(message, conn, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("feat:toggle:"))
async def feat_toggle(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    if not is_owner(callback, config):
        await callback.answer(t("not_admin", ulang), show_alert=True)
        return
    key = callback.data.split(":", 2)[2]
    feat = dbmod.get_feature(conn, key)
    if feat:
        dbmod.set_feature(conn, key, enabled=not bool(feat["enabled"]), by_id=callback.from_user.id)
    await callback.answer(t("m_changed", ulang))
    await callback.message.edit_text(t("m_feat_changed", ulang))


@router.message(Command("voice"))
async def cmd_voice(message: Message, command: CommandObject,
                    conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    _register(message, conn)
    if _blocked(conn, message.from_user.id):
        await _answer(message, conn, t("blocked", ulang)); return
    allowed, reason = can_use_feature(conn, message.from_user.id, "tts")
    if not allowed:
        await _answer(message, conn, t(reason, ulang)); return
    args = (command.args or "").strip()
    if not args:
        await _answer(message, conn, t("voice_usage", ulang)); return
    first, _, rest = args.partition(" ")
    gender = "female" if first in ("زن", "خانم", "female") else "male" if first in ("مرد", "آقا", "male") else "female"
    text = rest.strip() if rest.strip() else args
    # Text-to-voice: verified OpenAI TTS first, Azure as fallback.
    # With neither key, it honestly says the speech service is not connected.
    status, audio = await _synthesize_voice(text, gender, config, conn)
    if status == "needs_verification":
        await _answer(message, conn,
                      adminfeat.gate_text("stt_unverified", ulang))
        return
    if status == "needs_key":
        await _answer(message, conn, t("tts_needs_key", ulang), reply_markup=_web_button(conn, config, ulang)); return
    if status != "ok" or not audio:
        await _answer(message, conn, t("tts_error", ulang)); return
    sent = await message.answer_voice(BufferedInputFile(audio, filename="voice.mp3"),
                                      caption=f"{t('tts_ready', ulang)}\n{text[:200]}")
    _track(conn, message, sent)


async def _send_link_video(message: Message, conn, outcome, config=None,
                           bot_id: str = "", resolved: tuple | None = None,
                           req_id: str | None = None, t_req=None):
    """Send the video of the sent link itself for download — no video search.

    Direct public video file: send that same file. Instagram: the real
    video file via the anonymous resolver when fetchable, else the
    official-link card. Other platforms: the official video via its
    official open link. Never fetch private or protected platform
    content. Returns the sent Message (or None) so the caller can fold
    a later fallback into the same card instead of stacking messages.

    `resolved`: the link handler resolves Instagram media once per link
    and shares the result with the identify stage — passing it here
    avoids a second full resolver run for the same URL.
    """
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    from aiogram.types import BufferedInputFile

    delivery, text = link_video_delivery(outcome)
    if delivery == "direct_video_file":
        _t_send = _time.perf_counter()
        sent = await send_video(message, outcome.official_url,
                                caption=t("m_link_video_dl", ulang))
        _timing.log_stage(log, "link_stage", req_id, t_req, "send_video",
                          _t_send)
        if sent is not None:
            _track(conn, message, sent)
            await _attach_identify_button(message, conn, sent,
                                          link_url=outcome.official_url)
            return sent
        await _answer(message, conn, text)
        return None
    elif delivery == "platform_official" and outcome.kind == "instagram":
        sent = await _send_instagram_video_file(message, conn, outcome, config,
                                                resolved=resolved,
                                                req_id=req_id, t_req=t_req)
        if sent == _INSTAGRAM_GATED:
            # Dedicated age-gate line; the caller must not overwrite it
            # with the generic card or run identification stages on it.
            await _answer(message, conn, dlfeat.AGE_GATED)
            return _INSTAGRAM_GATED
        if sent is not None:
            return sent
        # (1b) 2026-11-08: a post DETERMINED to have no video (photo,
        # or carousel without a video item) gets the clear line instead
        # of the ambiguous video-link card. Unknown type keeps the
        # generic card — never claim "no video" without evidence.
        _mt = resolved[4] if resolved is not None and len(resolved) > 4 \
            else "unknown"
        if _mt in ("photo", "carousel"):
            sent = await _answer(message, conn,
                                 t("m_link_no_video", ulang))
            return sent
        sent = await _answer(message, conn, text)
        return sent
    elif delivery == "platform_official":
        sent = await _answer(message, conn, text)
        return sent
    else:  # official_link only (e.g. a direct audio file): show the link, clean.
        await answer_no_preview(message, outcome.official_url)
        return None


def _looks_like_video(data: bytes) -> bool:
    if not data or len(data) < 16:
        return False
    head = data[:16]
    return head[4:8] == b"ftyp" or head[:4] == b"\x00\x00\x00\x18"


def _looks_like_video_file(path: str) -> bool:
    """Magic-byte check on the first 16 bytes of a file on disk (STEP 2:
    the video lives on disk now, never in RAM)."""
    try:
        with open(path, "rb") as f:
            return _looks_like_video(f.read(16))
    except Exception:
        return False


def _looks_like_audio(data: bytes) -> bool:
    """Magic-byte check for audio bytes (mp4/m4a container, MP3, WAV,
    AAC ADTS) — never trusts extensions or content-type alone."""
    if not data or len(data) < 16:
        return False
    head = data[:16]
    return (head[4:8] == b"ftyp" or head[:3] == b"ID3"
            or head[:4] == b"RIFF"
            or (head[0] == 0xFF and (head[1] & 0xF0) == 0xF0))


async def _send_instagram_video_file(message: Message, conn, outcome,
                                     config=None,
                                     resolved: tuple | None = None,
                                     req_id: str | None = None,
                                     t_req=None):
    """Fetch the reel's real video file (anonymous resolver) and send it.

    Returns the sent Message, or None when no fetchable video exists —
    the caller then falls back to the official-link card. Size-capped;
    the bytes must look like a real video container.

    `resolved`: when the caller already ran resolve_instagram_media for
    this link (the identify stage needs the same tuple), it is reused
    instead of running the four-strategy resolver a second time.
    """
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    from aiogram.types import FSInputFile

    import os as _os
    import time as _time
    try:
        if resolved is not None:
            audio_url, video_url, _page_text, _strategy = resolved[:4]
        else:
            from ..services.instagram import resolve_instagram_media
            _r = await resolve_instagram_media(
                outcome.official_url,
                chat_id=getattr(getattr(message, "chat", None), "id", None)
                or getattr(getattr(message, "from_user", None), "id", None))
            audio_url, video_url, _page_text, _strategy = _r[:4]
    except Exception:
        return None
    if not video_url:
        # Classify the miss: an age-gated reel is impossible to fetch
        # anonymously, so it gets the dedicated line instead of the
        # generic "video not handed over" card.
        try:
            from ..services.instagram import check_instagram_gating
            gating = await check_instagram_gating(outcome.official_url)
        except Exception:
            gating = None
        if gating == "age_gated":
            return _INSTAGRAM_GATED
        return None
    _t_dl = _time.perf_counter()
    max_mb = getattr(config, "max_file_mb", 50) if config is not None else 50
    _video_path: str | None = None
    _tmpdir = None
    try:
        from ..services.instagram import fetch_merged_video
        # Instagram DASH serves video and audio as SEPARATE streams:
        # downloading only the video URL yields a SILENT file. Fetch
        # both and mux to one MP4 (falls back to video-only when the
        # audio stream is missing — the healthy path is preserved).
        # STEP 2: the file lives on DISK (streamed, never in RAM);
        # RapidAPI/crawler progressive files skip the mux entirely.
        # P1-8: the resolve output (RapidAPI / DASH media URLs) is
        # validated as media URLs (DNS / private-IP check, cached)
        # before any byte is downloaded; a failure is a download
        # failure exactly like an unreachable stream. The check lives
        # HERE, not inside fetch_merged_video, so the downloader stays
        # directly testable with local URLs (verify_step2).
        if not await validate_media_url_for_fetch(video_url):
            return None
        if audio_url and audio_url != video_url:
            if not await validate_media_url_for_fetch(audio_url):
                audio_url = None
        _video_path, _has_audio, _tmpdir = await fetch_merged_video(
            video_url, audio_url, max_mb=max_mb, req_id=req_id)
        # Step 1b: the merged file is on disk — wake the bg identify
        # (spawned at resolve) so it can read this file instead of
        # downloading the media a second time. It reads the file as
        # soon as it wakes; the audio-stream fallback covers the race
        # with the cleanup below.
        try:
            _signal_identify_bg_video_ready(
                _identify_result_key(link=outcome.official_url), _video_path)
        except Exception:
            pass
    except Exception:
        # Step 1b: download failed — wake the bg so it aborts instead
        # of waiting out its timeout.
        try:
            _signal_identify_bg_video_ready(
                _identify_result_key(link=outcome.official_url), None)
        except Exception:
            pass
        return None
    _size = 0
    try:
        _size = _os.path.getsize(_video_path) if _video_path else 0
    except Exception:
        _size = 0
    _timing.log_stage(log, "link_stage", req_id, t_req, "download", _t_dl,
                      bytes=_size, has_audio=_has_audio)
    if not _video_path or _size > max_mb * 1024 * 1024 \
            or not _looks_like_video_file(_video_path):
        try:
            if _tmpdir is not None:
                _tmpdir.cleanup()
        except Exception:
            pass
        # Step 1b: no usable file — wake the bg so it aborts.
        try:
            _signal_identify_bg_video_ready(
                _identify_result_key(link=outcome.official_url), None)
        except Exception:
            pass
        return None
    _t_send = _time.perf_counter()
    try:
        sent = await send_video(
            message, FSInputFile(_video_path, filename="video.mp4"),
            caption=t("m_link_video_dl", ulang))
    finally:
        try:
            if _tmpdir is not None:
                _tmpdir.cleanup()
        except Exception:
            pass
    _timing.log_stage(log, "link_stage", req_id, t_req, "send_video", _t_send)
    if sent is not None:
        _track(conn, message, sent)
        # Pass the reel's separate audio-stream URL so the one-click
        # «🎵 شناسایی موسیقی» button identifies the reel's REAL audio
        # directly (cheaper than re-extracting from the merged video).
        await _attach_identify_button(message, conn, sent,
                                      audio_url=audio_url,
                                      link_url=outcome.official_url)
    return sent


def _forward_channel(message) -> str:
    """Channel username/title a message was forwarded from, or "".

    Only used to label an indexed file; never used to access a channel.
    """
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None)
        if chat is not None:
            return getattr(chat, "username", "") or getattr(chat, "title", "") or ""
        return getattr(origin, "sender_user_name", "") or ""
    chat = getattr(message, "forward_from_chat", None)
    if chat is not None:
        return getattr(chat, "username", "") or getattr(chat, "title", "") or ""
    return ""


async def _arm_bank_sample(conn, config, kind: str, seed: str,
                           data: bytes | None, url: str, clue: str) -> str | None:
    """Fingerprint a sample for the second search; returns the bank token
    or None. Runs in a thread with a fresh DB connection (sqlite
    connections are not thread-safe). A None return means: no honest
    🔎 button — the caller shows the extraction-error line instead."""
    import asyncio as _asyncio
    import hashlib as _hashlib

    if not seed:
        return None
    if isinstance(seed, bytes):
        seed = _hashlib.sha256(seed).hexdigest()

    def _do():
        c2 = dbmod.connect(config.db_path)
        try:
            return bankfeat.arm_sample(kind, seed, data, url, (clue or "")[:300], c2)
        except Exception:
            return None
        finally:
            try:
                c2.close()
            except Exception:
                pass

    try:
        return await _asyncio.to_thread(_do)
    except Exception:
        return None


def _bank_button(token: str | None, lang: str = "fa"):
    if not token:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("m_bank_search_btn", lang),
                             callback_data=f"bank:search:{token}")]])


# --- one-click «🎵 شناسایی موسیقی» under received videos -------------------------
# Server-side registry: token -> {"user_id", "file_id", "kind", "audio_url"}.
# The button never carries a path — only an opaque token — and the callback
# re-checks ownership, so concurrent users/videos can never be mixed up.
# Tokens live in memory only: after a restart a tap gets the distinct
# "expired" message. "audio_url" is set only for link-fetched videos whose
# sent file is a video-only stream; the reel's real audio then comes from
# that URL instead of the (audio-less) Telegram file.
_VIDEO_IDENTIFY: dict[str, dict] = {}
_VIDEO_IDENTIFY_RUNNING: set[str] = set()


def purge_user_registries(user_id: int) -> int:
    """Drop a user's stale in-memory identify-button tokens.

    Called by «پاک‌سازی وضعیت گفتگو» so a cleared conversation leaves
    zero leakage: old «🎵 شناسایی موسیقی» buttons expire honestly
    instead of operating on a previous operation's files.
    Returns the number of tokens dropped.
    """
    doomed = [tok for tok, entry in _VIDEO_IDENTIFY.items()
              if entry.get("user_id") == user_id]
    for tok in doomed:
        _VIDEO_IDENTIFY.pop(tok, None)
        _VIDEO_IDENTIFY_RUNNING.discard(tok)
    return len(doomed)

# Sentinel returned by _send_instagram_video_file when the reel is
# age/region-gated (oEmbed gating signals): the dedicated message is
# already handled by the caller, and no identification stages may run
# on a reel no anonymous fetch can ever reach.
_INSTAGRAM_GATED = "instagram_age_gated"


def _video_identify_kb(user_id: int, file_id: str, kind: str,
                       label: str | None = None,
                       audio_url: str | None = None,
                       lang: str = "fa",
                       link_url: str | None = None) -> InlineKeyboardMarkup:
    """Build the one-click music-identification button for a video — or,
    with kind="audio" and a «🔄 تلاش مجدد» label, the retry button for an
    audio/voice file whose identification failed.

    ``kind`` is "link" (video fetched from a sent link), "file" (video
    the user sent directly), or "audio" (audio/voice the user sent). The
    registry entry is keyed by an opaque token; the Telegram file_id is
    resolved server-side on click. ``audio_url`` (link kind only) points
    at the reel's separate audio stream — the callback identifies THOSE
    bytes, because the sent video file itself is video-only.
    ``link_url`` is the original sent link (link kind only) so a
    wrong-song report can name the video it disputes."""
    if label is None:
        label = t("m_identify_music_btn", lang)
    token = secrets.token_hex(8)
    _VIDEO_IDENTIFY[token] = {"user_id": user_id, "file_id": file_id,
                              "kind": kind, "audio_url": audio_url,
                              "link": link_url or ""}
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label,
                             callback_data=f"vid:id:{token}")]])


async def _attach_identify_button(message: Message, conn, sent,
                                  audio_url: str | None = None,
                                  link_url: str | None = None) -> None:
    """Attach the one-click identification button under a bot-sent video
    message (best effort; the video must have a downloadable file_id)."""
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    try:
        video = getattr(sent, "video", None)
        file_id = getattr(video, "file_id", None) if video else None
        if not file_id or not message.from_user:
            return
        kb = _video_identify_kb(message.from_user.id, file_id, "link",
                                audio_url=audio_url, lang=ulang,
                                link_url=link_url)
        await sent.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


async def _download_link_audio(url: str, config, req_id: str | None = None,
                               t_req=None) -> bytes:
    """Download a reel's separate audio stream for identification.

    Same validation as the video path: size cap, real container bytes —
    never a renamed or empty payload. P1-8: the audio URL is an
    extracted CDN media URL — validated as a media URL (DNS /
    private-IP check) before fetching; a failure raises ValueError
    like any other failed audio validation.
    """
    import time as _time
    try:
        if not await validate_media_url_for_fetch(url):
            raise ValueError("url validation failed")
    except ValueError:
        raise
    except Exception:
        raise ValueError("url validation failed")
    headers = {"User-Agent": "Mozilla/5.0",
               "Referer": "https://www.instagram.com/"}
    t = _time.perf_counter()
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.content
    _timing.log_stage(log, "identify_stage", req_id, t_req, "download", t,
                      bytes=len(data or b""))
    max_mb = getattr(config, "max_file_mb", 50)
    if not data or len(data) > max_mb * 1024 * 1024 \
            or not _looks_like_audio(data):
        raise ValueError("audio bytes failed validation")
    return data


EXTRACTED_AUDIO_TITLE = t("m_extracted_audio_title")


async def _extract_and_send_clip_audio(message, conn, data: bytes,
                                       caption: str | None = None,
                                       reply_markup=None,
                                       caption_key: str = ""):
    """Extract a video's own audio to a REAL, validated MP3 and send it
    as «صدای استخراج‌شده از ویدیو».

    The fallback guarantee: whenever identification ran on a reachable
    video WITH an audio track but Shazam/AudD found nothing (or the
    lookup errored), the user still gets the video's own audio as a
    playable file — never skipped just because no full-version MP3
    exists in the sources.

    Validation: the video must have an audio stream (ffprobe); the
    bytes are genuinely re-encoded to MP3 (never a renamed non-audio
    file); the output must have real MP3 magic bytes and a probed
    bitrate/duration via the unified pipeline. Returns the sent message,
    or None when extraction is impossible (silent/undecodable bytes) —
    the caller then states that honestly instead of claiming delivery.
    The title is never a guessed song name. The extracted file is
    Avano's own extraction: performer is "Avano" (never "Unknown
    artist") and the duration is the probed real one (never guessed).
    A successful send writes one `sent_extracted_audio` log line
    (bytes + caption key + attached button labels) so the fallback is
    verifiable in the log.
    """
    try:
        if not data:
            return None
        # ffprobe/ffmpeg run in a thread: the sync subprocess calls
        # (ffprobe timeout 30 s, ffmpeg timeout 180 s) must never block
        # the event loop (same pattern as instagram.py fetch_merged_video).
        if await asyncio.to_thread(has_audio_stream, data) is False:
            return None
        src_br, _dur = await asyncio.to_thread(probe_audio, data, "mp4")
        target = 320 if (src_br or 0) >= 300 else max(96, src_br or 128)
        conv = await asyncio.to_thread(transcode_mp3, data, "mp4", target)
        mp3 = conv[0] if conv[0] else None
        if not mp3:
            return None
        # §6 (2026-11-09): transcode_mp3 already probed the REAL duration
        # of exactly these bytes — pass it through instead of discarding
        # it, so the sent audio always carries a real duration even if
        # the pipeline's re-probe ever fails (never 0:00 / "-:--").
        _mp3_dur = conv[2] if len(conv) > 2 else None
        ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
        # show_quality=False: the extracted-audio caption is the user's
        # exact text (byte-exact per the 2026-11-09 order) — the unified
        # pipeline must not append a "🔊 MP3 · 96kbps" line to it.
        sent = await send_music(message, mp3, t("m_extracted_audio_title", ulang),
                                "Avano", caption=caption,
                                reply_markup=reply_markup,
                                duration=_mp3_dur,
                                show_quality=False)
        if sent is not None:
            _track(conn, message, sent)
            try:
                log_event(log, "extract", stage="sent_extracted_audio",
                          bytes=len(mp3), caption_key=caption_key or "",
                          buttons=_keyboard_button_texts(reply_markup))
            except Exception:
                pass
        return sent
    except Exception:
        return None


def _keyboard_button_texts(kb) -> list:
    """Button labels of an inline keyboard, for the log line only."""
    texts = []
    try:
        for _row in getattr(kb, "inline_keyboard", []) or []:
            for _b in _row:
                texts.append(getattr(_b, "text", "") or "")
    except Exception:
        pass
    return texts


def _find_identify_callback(file_id: str, user_id: int, kind: str = "file"):
    """callback_data for re-identifying a video: the EXISTING vid:id
    token for this file when one is registered, else a fresh token
    minted by the usual _video_identify_kb — the retry always runs the
    EXISTING identify flow (same handler, same registry, no cache
    bypass). Returns None only when no token can be minted."""
    if not file_id or not user_id:
        return None
    for tok, entry in _VIDEO_IDENTIFY.items():
        if entry.get("file_id") == file_id and entry.get("user_id") == user_id:
            return f"vid:id:{tok}"
    try:
        kb = _video_identify_kb(user_id, file_id, kind)
        return kb.inline_keyboard[0][0].callback_data
    except Exception:
        return None


def _extracted_audio_kb(title: str, artist: str, ulang,
                        token: str | None = None,
                        file_id: str | None = None,
                        user_id: int | None = None,
                        kind: str = "file"):
    """The buttons under an extracted-audio message (extracted-audio UI,
    2026-11-09): EXACTLY ONE button — re-identify via the video's
    EXISTING identify button (vid:id:<token>). The Google button was
    removed from extracted audio by the user's order; the retry runs
    the previously defined supplementary re-identification, unchanged.
    The single-button row renders full-width by Telegram's layout.
    Never used on an identified/bank send; never playlist/lyrics/
    wrong-song under an extracted file. Returns None when no retry
    callback can be built (a keyboard without the retry would be a
    Google-only keyboard, which the order forbids)."""
    cb = None
    if token:
        cb = f"vid:id:{token}"
    elif file_id and user_id:
        cb = _find_identify_callback(file_id, user_id, kind)
    if cb:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=t("m_extracted_retry_btn", ulang), callback_data=cb)]])
    return None


async def _send_unconfirmed_audio(message, conn, data: bytes,
                                  title: str = "", artist: str = "",
                                  ulang: str = "fa",
                                  token: str | None = None,
                                  file_id: str | None = None,
                                  user_id: int | None = None,
                                  kind: str = "file"):
    """The unconfirmed identification response: ONLY the clip's own
    audio with the new caption + the single retry button. No «شناسایی شد»
    card, no separate text message. Returns the sent message, or None
    when extraction is impossible (the caller then sends the honest
    text)."""
    kb = _extracted_audio_kb(title, artist, ulang, token=token,
                             file_id=file_id, user_id=user_id, kind=kind)
    return await _extract_and_send_clip_audio(
        message, conn, data,
        caption=t("m_extracted_audio_new", ulang),
        reply_markup=kb, caption_key="m_extracted_audio_new")


async def _send_unconfirmed_media(message, conn, data: bytes,
                                  title: str = "", artist: str = "",
                                  ulang: str = "fa",
                                  media_kind: str = "video",
                                  file_id: str | None = None,
                                  user_id: int | None = None):
    """Unconfirmed response for a directly-sent file. Video: the clip's
    own extracted audio (new caption + the single retry button). Audio: the file itself
    back, with the button and NO caption — nothing was extracted from
    it. Voice/document: no audio send (same coverage as before).
    Returns the sent message or None."""
    if media_kind == "video":
        return await _send_unconfirmed_audio(
            message, conn, data, title, artist, ulang,
            file_id=file_id, user_id=user_id, kind="file")
    if media_kind == "audio":
        kb = _extracted_audio_kb(title, artist, ulang, file_id=file_id,
                                 user_id=user_id, kind="audio")
        sent = await send_song_audio(message, file_id or "", "", "",
                                     reply_markup=kb)
        if sent is not None:
            _track(conn, message, sent)
        return sent
    return None


def _shorten_name(name: str, max_len: int) -> str:
    """Shorten one name with an ellipsis so it fits max_len chars."""
    name = (name or "").strip()
    if max_len <= 1:
        return "…"
    if len(name) <= max_len:
        return name
    return name[:max_len - 1].rstrip() + "…"


def _fit_caption(fmt, title: str, artist: str,
                 max_len: int = 1024) -> str:
    """Format a caption from (title, artist), never exceeding max_len
    (Telegram's 1024-char caption limit). The names are shortened with
    an ellipsis — the title first, then the artist — so the fixed
    wording is never cut. A last-resort hard cut covers pathological
    fixed text."""
    title = (title or "").strip()
    artist = (artist or "").strip()
    text = fmt(title, artist)
    if len(text) <= max_len:
        return text
    overhead = len(fmt("", ""))
    budget = max_len - overhead
    if budget <= 0:
        return text[:max_len - 1] + "…"
    t_budget = min(len(title), max(1, (budget * 2) // 3))
    a_budget = budget - t_budget
    if a_budget < 1:
        a_budget = 1
        t_budget = max(1, budget - 1)
    elif len(artist) < a_budget:
        # A short artist frees its share to the title.
        a_budget = len(artist)
        t_budget = min(len(title), budget - a_budget)
    title = _shorten_name(title, t_budget)
    artist = _shorten_name(artist, a_budget)
    text = fmt(title, artist)
    if len(text) > max_len:
        text = text[:max_len - 1] + "…"
    return text


def _identified_audio_row(key: str, ulang) -> list:
    """The ONE row under an identified full song ONLY — never under a
    clip/extracted audio file (the keyboard follows the file's
    origin, 2026-11-09): add-to-playlist + lyrics (& translation),
    side by side. Callbacks/handlers unchanged (pro:pick: / ly:). No
    heart, no wrong-song, no Google/YouTube/retry, no library/save
    button — the wrong-song report lives ONLY under the video (final
    identify UI, 2026-11-09)."""
    return [InlineKeyboardButton(
                text=t("m_id_pl_btn", ulang),
                callback_data=f"pro:pick:{key}"),
            InlineKeyboardButton(
                text=t("m_id_lyrics_btn", ulang),
                callback_data=f"ly:{key}")]


def _identified_song_kb(message, conn, title: str, artist: str,
                        own_media, source: str, ulang):
    """The identified-song keyboard (final identify UI, 2026-11-09).

    Exactly one row — add-to-playlist + lyrics (& translation) — under
    the ONE final audio message. The favorites feature itself is
    untouched (its handler and callbacks stay); only this keyboard
    carries no heart button, and the wrong-song report is NOT here
    anymore (it lives under the video message only). ``own_media`` is
    kept on the stored result so the lyrics/playlist callbacks keep
    working. Returns (keyboard, result_key)."""
    key = profeat.remember_result(title, artist, "", "", own_media=own_media)
    return InlineKeyboardMarkup(
        inline_keyboard=[_identified_audio_row(key, ulang)]), key


def _final_audio_caption(title: str, artist: str, ulang,
                         duration_sec: float = 0, quality: str = "",
                         extra: str = "") -> str:
    """Caption of the ONE final message for a confirmed song: the
    «شناسایی شد» card text, plus the file's real duration and quality
    info (and the bank marker for exclusive files). The card text is
    the caption — a text message cannot be edited into an audio
    message, so the card is deleted and this audio is sent instead."""
    def _fmt(_t, _a):
        text = t("h_song_identified", ulang).format(title=_t, artist=_a)
        label = _fullsong_duration_label(duration_sec, ulang)
        if label:
            text += label
        if extra:
            text += "\n" + extra
        if quality:
            text += "\n🎧 " + quality
        return text
    return _fit_caption(_fmt, title, artist)


async def _delete_card(card_msg) -> None:
    """Best-effort delete of the temporary status message after the
    one final message was sent. Never raises; a failed delete leaves
    the status in place (the final message is already sent)."""
    if card_msg is None:
        return
    try:
        await card_msg.delete()
    except Exception:
        pass


async def _answer_identify_clip_fallback(message, conn, config, data: bytes,
                                         file_id: str,
                                         headline_key: str | None = None,
                                         extra_rows: list | None = None) -> None:
    """Not-identified / lookup-error flow for the one-click video button:
    the clip's own audio is extracted to a real MP3 and sent, then the
    honest message with the bank button. Never a guessed song, and the
    message never claims the audio was delivered when extraction failed.
    """
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    sent = await _extract_and_send_clip_audio(message, conn, data)
    head = t(headline_key, ulang) if headline_key else t("m_identify_head_notfound", ulang)
    if sent is not None:
        text = t("h_clip_audio_ready", ulang).format(head=head)
    else:
        text = t("h_clip_audio_failed", ulang).format(head=head)
    _tok = await _arm_bank_sample(conn, config, "video_button", file_id,
                                  data, "", "")
    rows: list = []
    _bb = _bank_button(_tok, ulang)
    if _bb:
        rows.extend(_bb.inline_keyboard)
    if extra_rows:
        rows.extend(extra_rows)
    await _answer(message, conn, text,
                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
                  if rows else None)


async def _send_identify_cache_hit(message, conn, hit: dict, token: str,
                                   entry: dict, ulang: str,
                                   cache_key: str,
                                   req_id: str | None = None):
    """Send a shortcode-cache hit with the SAME send as the fresh path:
    full song -> identified caption + [playlist, lyrics] one row;
    clip audio -> corporate caption + single retry button. A staged
    disk file is sent via FSInputFile; the returned file_id then
    replaces the disk path in the cache (memory + Turso) and the disk
    file is deleted, so the NEXT tap is a file_id hit. A bank file_id
    is sent without title/performer (exactly like
    _send_exclusive_song); any other stored file_id is sent with
    title/performer (exactly like the cached send in
    _deliver_fullsong_bg). Returns the sent message, or None when
    nothing sendable exists (the caller then falls through to the
    normal flow). Never raises."""
    try:
        from aiogram.types import FSInputFile
        import os as _os
        title = hit.get("title", "")
        artist = hit.get("artist", "")
        source = hit.get("source") or "cache"
        kind = hit.get("kind") or "clip"
        fid = hit.get("file_id") or ""
        disk = hit.get("disk_path") or ""
        dur = hit.get("duration_sec") or 0
        sent = None
        if kind == "full":
            kb, _key = _identified_song_kb(
                message, conn, title, artist,
                ("video", entry.get("file_id")), source, ulang)
            caption = _final_audio_caption(
                title, artist, ulang, duration_sec=dur,
                quality=hit.get("quality") or "",
                extra=hit.get("extra") or "")
            if fid:
                if source == "bank":
                    # Exactly like _send_exclusive_song: cover first,
                    # plain send on failure.
                    _cover = hit.get("cover_file_id") or ""
                    try:
                        if _cover:
                            sent = await message.answer_audio(
                                fid, caption=caption, reply_markup=kb,
                                thumbnail=_cover)
                    except Exception:
                        sent = None
                    if sent is None:
                        sent = await message.answer_audio(
                            fid, caption=caption, reply_markup=kb)
                else:
                    sent = await message.answer_audio(
                        fid, caption=caption, title=title,
                        performer=artist, reply_markup=kb)
            elif disk and _os.path.exists(disk):
                sent = await message.answer_audio(
                    FSInputFile(disk, filename=f"{title or 'song'}.mp3"),
                    title=title, performer=artist,
                    duration=int(dur) if dur else None,
                    caption=caption, reply_markup=kb)
        else:
            kb = _extracted_audio_kb(title, artist, ulang, token=token)
            caption = t("m_extracted_audio_new", ulang)
            if fid:
                sent = await message.answer_audio(
                    fid, caption=caption, reply_markup=kb,
                    title=t("m_extracted_audio_title", ulang),
                    performer="Avano",
                    duration=int(dur) if dur else None)
            elif disk and _os.path.exists(disk):
                sent = await message.answer_audio(
                    FSInputFile(disk, filename="clip.mp3"),
                    title=t("m_extracted_audio_title", ulang),
                    performer="Avano",
                    duration=int(dur) if dur else None,
                    caption=caption, reply_markup=kb)
        if sent is None:
            return None
        try:
            _track(conn, message, sent)
        except Exception:
            pass
        # Disk -> file_id upgrade: the next tap is a file_id hit.
        try:
            new_fid = ""
            audio = getattr(sent, "audio", None)
            if audio is not None:
                new_fid = getattr(audio, "file_id", "") or ""
            if new_fid and new_fid != fid:
                if disk:
                    try:
                        _os.unlink(disk)
                    except Exception:
                        pass
                _identify_result_store_send(
                    cache_key, title, artist, source, kind, new_fid,
                    duration_sec=dur,
                    status=hit.get("status") or "identified",
                    quality=hit.get("quality") or "",
                    extra=hit.get("extra") or "", req_id=req_id,
                    resolve_ms=hit.get("resolve_ms") or 0)
        except Exception:
            pass
        return sent
    except Exception:
        return None


def _smart_search_eligible(callback, entry, hit) -> bool:
    """Gate for the smart full-version search (retry button only).

    True ONLY when all of these hold:
    - the tap is on an AUDIO message (the extracted audio's single
      retry button — the identify button lives under a video message);
    - the registry entry is a link/file video (never a user-sent
      audio/voice file, kind "audio");
    - the 1b cache hit is a clip result (kind "clip") with a known
      title (the search reuses the prior identify info — it never
      re-runs identification, and it cannot run without a title);
    - the identification is CONFIRMED (status "identified", fix
      round 1, finding 1B: an unconfirmed single-source name must
      never be searched — the tap falls back to the normal BG
      identify path instead).
    A kind "full" hit already HAS the full version; a title-less hit
    has nothing to search for. Never raises.
    """
    try:
        if not isinstance(hit, dict) or not (hit.get("title") or "").strip():
            return False
        if hit.get("status") != "identified":
            return False
        if (hit.get("kind") or "clip") != "clip":
            return False
        if (entry or {}).get("kind") not in ("link", "file"):
            return False
        msg = getattr(callback, "message", None)
        if msg is None or getattr(msg, "audio", None) is None:
            return False
        return True
    except Exception:
        return False


async def _smart_clip_fingerprint(callback, entry, hit):
    """Chromaprint of the extracted clip audio, or None (unavailable).

    Prefers the 1b cache's staged disk file; else downloads the sent
    clip audio from Telegram (capped at 10 MB, timed out). The
    fingerprint is an ADVISORY confirm only — None never blocks the
    search. Never raises.
    """
    try:
        from ..search.smart_validate import fingerprint_file_items
    except Exception:
        return None
    disk = (hit or {}).get("disk_path") or ""
    if disk:
        try:
            import os as _os
            if _os.path.isfile(disk):
                items = fingerprint_file_items(disk)
                if items:
                    return items
        except Exception:
            pass
    fid = (hit or {}).get("file_id") or ""
    if not fid:
        return None
    tmp = ""
    try:
        import os as _os
        import tempfile as _tf
        tg_file = await asyncio.wait_for(
            callback.bot.get_file(fid), timeout=15)
        buf = io.BytesIO()
        await asyncio.wait_for(
            callback.bot.download_file(tg_file.file_path, buf), timeout=20)
        data = buf.getvalue()
        if not data or len(data) > 10 * 1024 * 1024:
            return None
        fd, tmp = _tf.mkstemp(suffix=".mp3", prefix="avano_clip_")
        try:
            _os.write(fd, data)
            _os.close(fd)
            fd = None
        except Exception:
            pass
        return fingerprint_file_items(tmp)
    except Exception:
        return None
    finally:
        if tmp:
            try:
                import os as _os
                _os.unlink(tmp)
            except Exception:
                pass


async def _run_smart_search_tap(callback, conn, entry, hit,
                                cache_key, token, ulang, req_id=None):
    """Smart full-version search for ONE retry tap. Never raises.

    Sends exactly one outcome and restores the retry button on the
    extracted audio message (the tap cleared it):
    - found: the full file with the LOCKED full-song layout
      (identified caption + [playlist, lyrics] one row); the file_id
      is stored in the 1b cache + song_file_cache and the disk file
      deleted — the extracted file stays until this send succeeds.
    - not found: the exact not-found text, then the satisfaction
      survey hook (when the survey module exists — it does not yet;
      see the SURVEY HOOK comment below). Streaming-link URL
      buttons and the iTunes preview are DEFERRED to a later
      version (user order 22:19) and are never sent in v1.
    - error: the honest error text.
    The extracted clip audio is never re-sent here: it is already in
    the chat, and the not-found text says so.
    """
    import os as _os
    from aiogram.types import FSInputFile as _FSInputFile
    from aiogram.types import InlineKeyboardButton as _IKB
    from aiogram.types import InlineKeyboardMarkup as _IKM
    from ..search.smart_fullsong import search_smart_full_version as _smart_search
    message = callback.message
    title = (hit.get("title") or "").strip()
    artist = (hit.get("artist") or "").strip()
    t0 = _time.monotonic()
    # Fix round 1, finding 2B: the search call used to run BEFORE the
    # try — an exception there escaped with no answer sent and no log
    # line. Everything now runs inside the try, and the outcome is
    # logged the moment the search returns, so a missing message is
    # distinguishable from a missing branch. The function still never
    # raises; it returns the outcome status ("found" / "not_found" /
    # "error") so the caller can log the true total status.
    status_out = "error"
    try:
        clip_fp = await _smart_clip_fingerprint(callback, entry, hit)
        outcome = await _smart_search(artist, title, clip_fingerprint=clip_fp)
        status_out = outcome.status
        try:
            _timing.log_stage(
                log, "identify_stage", req_id or _timing.new_req_id(),
                t0, "smart_searched", t0, status=outcome.status,
                checked=getattr(outcome, "checked", 0),
                ms=int((_time.monotonic() - t0) * 1000))
        except Exception:
            pass
        if outcome.status == "found" and outcome.file is not None:
            f = outcome.file
            sent = None
            try:
                kb, _key = _identified_song_kb(
                    message, conn, title, artist,
                    ("video", entry.get("file_id")), f.source, ulang)
                caption = _final_audio_caption(
                    title, artist, ulang, duration_sec=f.duration_sec)
                sent = await message.answer_audio(
                    _FSInputFile(f.disk_path,
                                 filename=f"{title or 'song'}.mp3"),
                    title=title, performer=artist,
                    duration=int(f.duration_sec) if f.duration_sec else None,
                    caption=caption, reply_markup=kb)
            except Exception:
                sent = None
            try:
                if sent is not None:
                    try:
                        _track(conn, message, sent)
                    except Exception:
                        pass
                    fid = ""
                    _a = getattr(sent, "audio", None)
                    if _a is not None:
                        fid = getattr(_a, "file_id", "") or ""
                    if fid:
                        _identify_result_store_send(
                            cache_key, title, artist, f.source, "full", fid,
                            duration_sec=f.duration_sec, req_id=req_id)
                        try:
                            from .file_cache import store_file_id as _store_fid
                            _store_fid(conn, artist, title, fid,
                                       file_type="audio", file_size=f.size,
                                       duration_sec=f.duration_sec,
                                       source=f.source,
                                       verified_title=f.title,
                                       verified_artist=f.artist)
                        except Exception:
                            pass
            finally:
                try:
                    _os.unlink(f.disk_path)
                except Exception:
                    pass
            if sent is None:
                # The verified file could not be sent: honest error —
                # the extracted clip stays in the chat.
                try:
                    await message.answer(t("m_smart_error", ulang))
                except Exception:
                    pass
            log_event(log, "smart_search", stage="sent",
                      req_id=req_id,
                      ms=int((_time.monotonic() - t0) * 1000),
                      source=f.source, sent=bool(sent))
            if sent is None:
                # The verified file could not be sent: the delivered
                # outcome is the honest error, not "found".
                status_out = "error"
        elif outcome.status == "error":
            await message.answer(t("m_smart_error", ulang))
            log_event(log, "smart_search", stage="error",
                      req_id=req_id,
                      ms=int((_time.monotonic() - t0) * 1000),
                      detail=outcome.detail)
        else:  # not_found
            stream = outcome.stream
            link_kb = None
            if stream is not None and stream.links:
                rows = [[_IKB(text=label, url=url)]
                        for label, url in stream.links[:5]]
                link_kb = _IKM(inline_keyboard=rows)
            await message.answer(t("m_smart_not_found", ulang),
                                 reply_markup=link_kb)
            # SURVEY HOOK (user order 22:19 §4; design study 79ff1987):
            # the satisfaction survey follows the not-found message.
            # The survey module does NOT exist yet (design done, build
            # pending the user's thank-you-form answer), so the helper
            # is imported LAZILY and guarded: while the module is
            # absent this is skipped silently — never a crash, never a
            # delay to the answer. send_survey(message, conn,
            # op_context) is the shape the design study specified; the
            # broad guard also absorbs a signature mismatch when the
            # module lands. Answering is OPTIONAL and never affects
            # using the bot (user order 21:31).
            survey_sent = False
            try:
                from ..features.survey import send_survey as _send_survey
                await _send_survey(message, conn, {
                    "cache_key": cache_key,
                    "title": title,
                    "artist": artist,
                    "search_result": "not_found",
                    "file_type": "extracted_clip_audio",
                })
                survey_sent = True
            except ImportError:
                pass
            except Exception:
                pass
            # v2 only (DEFERRED — user order 22:19): a verified iTunes
            # preview would follow here as a clearly-labelled 30 s
            # preview audio — never as the song. Never sent in v1.
            if stream is not None and stream.preview_disk_path:
                try:
                    await message.answer_audio(
                        _FSInputFile(stream.preview_disk_path,
                                     filename="preview.m4a"),
                        title=title, performer=artist,
                        duration=int(stream.preview_duration_sec)
                        if stream.preview_duration_sec else None,
                        caption=t("m_smart_preview_caption", ulang).format(
                            title=title, artist=artist))
                except Exception:
                    pass
                finally:
                    try:
                        _os.unlink(stream.preview_disk_path)
                    except Exception:
                        pass
            log_event(log, "smart_search", stage="not_found",
                      req_id=req_id,
                      ms=int((_time.monotonic() - t0) * 1000),
                      checked=outcome.checked,
                      detail=outcome.detail,
                      links=len(stream.links) if stream else 0,
                      preview=bool(stream and stream.preview_disk_path),
                      survey=survey_sent)
    except Exception:
        # Never leave the user without an answer: the honest error.
        try:
            await message.answer(t("m_smart_error", ulang))
        except Exception:
            pass
    # Restore the single retry button under the extracted audio.
    try:
        kb = _extracted_audio_kb(title, artist, ulang, token=token)
        if kb is not None:
            await message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    return status_out


async def _set_video_result_kb_if_video(message, title: str, artist: str,
                                        ulang: str, file_id: str | None,
                                        user_id: int | None, kind: str,
                                        source: str = "") -> None:
    """Video keyboard ONLY on a video message (fix round 2).

    The cache-hit / BG branches in video_identify run for both the
    identify tap (the bot-sent VIDEO message) and the retry tap (the
    extracted AUDIO message); _set_video_result_kb edits
    callback.message's markup, so on a retry tap it stamped the
    [Google][YouTube] + [wrong-song report] keyboard onto the clip
    audio. The early return below keeps retry taps out of those
    branches, but the guard fixes the symptom even if one is ever
    reached from a retry tap again. Never raises.
    """
    try:
        if getattr(message, "video", None) is None:
            return
        await _set_video_result_kb(message, title, artist, ulang,
                                   file_id, user_id, kind, source)
    except Exception:
        pass


@router.callback_query(F.data.startswith("vid:id:"))
async def video_identify(callback, conn: sqlite3.Connection, config: Config):
    """One-click «🎵 شناسایی موسیقی» under a received video.

    Extracts THAT video's audio (the Telegram file_id comes from the
    server-side registry — the button carries only an opaque token) and
    runs it through the identification engine. The user never re-sends
    the link or the file. Ownership is re-checked on every tap so two
    users' videos can never be mixed up; an idempotency set stops
    accidental double runs.
    """
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    token = callback.data.split("vid:id:", 1)[1]
    entry = _VIDEO_IDENTIFY.get(token)
    if not entry:
        # The registry is in-memory: after a restart the token is gone.
        # Honest expiry — never a silent no-op.
        await callback.answer(
            t("m_req_expired", ulang),
            show_alert=True)
        return
    if entry.get("user_id") != callback.from_user.id:
        await callback.answer(t("m_not_yours", ulang), show_alert=True)
        return
    if token in _VIDEO_IDENTIFY_RUNNING:
        await callback.answer(t("m_identifying", ulang))
        return
    _VIDEO_IDENTIFY_RUNNING.add(token)
    await callback.answer(t("m_identifying_video", ulang))
    # FINAL IDENTIFY UI (2026-11-09): the tap clears the video's
    # keyboard at once — no buttons show while the identification
    # runs; the result keyboard is set once the result is known.
    await _clear_video_kb(callback.message)
    # UNIFY FIX (2026-11-09): the temporary «در حال جستجوی موسیقی...»
    # status stays until the ONE final message is sent. The background
    # delivery deletes it (identified path); every other path deletes
    # it in the finally below — it is never left behind.
    status_msg = None
    try:
        status_msg = await callback.message.answer(
            t("m_identify_searching", ulang))
    except Exception:
        status_msg = None
    _bg_owns_status = False
    _cache_hit = False
    # Timing instrumentation (logs only): one request id per tap.
    _req_id = _timing.new_req_id()
    _t_req = _time.perf_counter()
    _t_wall = _time.time()
    _timing.log_stage(log, "identify_stage", _req_id, _t_req, "receipt",
                      _t_req)
    result = None
    # Step 1b additions (2026-11-09): the three timing parts of a tap.
    # wait_bg = this tap's time awaiting the bg identify job (0 on a
    # pure cache hit); search_file = the bg job's file-resolution time
    # for this key, carried on the stored entry (0 when unknown);
    # send = the final audio send. On a miss where the tap awaited the
    # bg, search_file is a sub-part of wait_bg (the bg's resolve phase
    # ran inside the waited window); the miss verdict compares
    # wait_bg vs send for the total, and search_file vs
    # (wait_bg - search_file) to split the bg's work.
    _wait_bg_ms = 0
    _search_file_ms = 0
    _send_ms = 0

    def _log_identify_timing():
        """The three timing stages of this tap (identify_stage lines)."""
        try:
            _tot = int((_time.perf_counter() - _t_req) * 1000)
            log_event(log, "identify_stage", stage="wait_bg",
                      req_id=_req_id, ms=_wait_bg_ms, cache_hit=_cache_hit,
                      total_ms=_tot)
            log_event(log, "identify_stage", stage="search_file",
                      req_id=_req_id, ms=_search_file_ms,
                      cache_hit=_cache_hit, total_ms=_tot)
            log_event(log, "identify_stage", stage="send",
                      req_id=_req_id, ms=_send_ms, cache_hit=_cache_hit,
                      total_ms=_tot)
        except Exception:
            pass

    try:
        # Step 1b: SHORTCODE cache fast path — FIRST, before any other
        # work. Memory LRU first, then ONE indexed Turso read on a
        # miss. A sendable hit sends the stored final message
        # immediately — ONLY by file_id when one is stored (no
        # download, no file search), from the staged disk file only
        # when the file was never sent yet (that first send stores the
        # returned file_id, so the NEXT tap is a file_id hit). No bank
        # match, no Shazam, no AudD. A rejected name never comes from
        # the cache (batch F exclusion).
        _cache_key = _identify_result_key(link=entry.get("link"),
                                          file_id=entry.get("file_id"))
        _hit = None
        if _cache_key:
            _hit = _identify_result_get(_cache_key)
            if _hit is None:
                _hit = _identify_result_get_turso(_cache_key, req_id=_req_id)
                if _hit is not None:
                    _identify_result_put(_cache_key, _hit)
        _failed_fid = ""
        # SMART SEARCH (2026-11-09, free-only): the retry button under
        # an extracted audio message, with a cached clip result and a
        # known title, runs the smart full-version search INSTEAD of
        # the normal identify flow — the prior identify info is reused,
        # never re-run. The temp status becomes the exact smart-search
        # text; the finally below deletes it once the outcome is sent.
        if _smart_search_eligible(callback, entry, _hit) and not _is_rejected(
                entry, _hit.get("title", ""), _hit.get("artist", "")):
            try:
                if status_msg is not None:
                    await status_msg.edit_text(t("m_smart_searching", ulang))
            except Exception:
                try:
                    await _delete_card(status_msg)
                    status_msg = await callback.message.answer(
                        t("m_smart_searching", ulang))
                except Exception:
                    pass
            _t_smart = _time.perf_counter()
            _smart_status = await _run_smart_search_tap(
                callback, conn, entry, _hit, _cache_key, token,
                ulang, req_id=_req_id)
            # Fix round 1, finding 6: the tap answered from the smart
            # path, so the total status is the smart outcome — never
            # the default "error" the finally logged while result
            # stayed None. A found + sent file logs "found".
            if _smart_status:
                result = AudioIdResult(
                    status=_smart_status,
                    title=_hit.get("title", ""),
                    artist=_hit.get("artist", ""),
                    confirmed=_hit.get("status") == "identified",
                    source=_hit.get("source") or "smart")
            _send_ms = int((_time.perf_counter() - _t_smart) * 1000)
            _log_identify_timing()
            _timing.log_stage(log, "identify_stage", _req_id, _t_req,
                              "result_send", _t_req, status="smart_search")
            return
        # FIX ROUND 2 — retry-tap regression (2026-11-10): a retry tap
        # on a NON-ELIGIBLE clip hit used to fall through to the
        # identify-tap cache-hit branch below and re-send the clip
        # audio that is already on screen (the 1B gate in
        # _smart_search_eligible skips the smart branch for
        # unconfirmed / not_found / title-less hits). The tap is on
        # the AUDIO message (callback.message.audio is not None) and
        # the clip is already in the chat, so this invocation sends
        # ZERO audio messages: the honest not-identified status as a
        # separate text message (existing key
        # m_identify_head_notfound — no smart search ran, so
        # m_smart_not_found would be inaccurate; h_clip_audio_failed
        # / m_identify_error would falsely claim extraction failed /
        # an error), the single retry button restored on the audio
        # message, and the outcome logged. A rejected name is NOT
        # caught here — it takes the BG re-identify path below, as
        # before. Each invocation is independent: two sequential
        # retry taps each send nothing (the _VIDEO_IDENTIFY_RUNNING
        # guard only blocks concurrent duplicates, never relied on).
        if (callback.message is not None
                and getattr(callback.message, "audio", None) is not None
                and _hit is not None
                and (_hit.get("kind") or "clip") == "clip"
                and not _smart_search_eligible(callback, entry, _hit)
                and not _is_rejected(entry, _hit.get("title", ""),
                                     _hit.get("artist", ""))):
            result = AudioIdResult(
                status=_hit.get("status") or "not_found",
                title=_hit.get("title", ""),
                artist=_hit.get("artist", ""),
                confirmed=False,
                source=_hit.get("source") or "cache")
            try:
                await callback.message.answer(
                    t("m_identify_head_notfound", ulang))
            except Exception:
                pass
            try:
                _kb = _extracted_audio_kb(
                    result.title, result.artist, ulang, token=token)
                if _kb is not None:
                    await callback.message.edit_reply_markup(
                        reply_markup=_kb)
            except Exception:
                pass
            _timing.log_stage(log, "identify_stage", _req_id, _t_req,
                              "result_send", _t_req, status=result.status)
            _log_identify_timing()
            return
        if _hit is not None and not _is_rejected(
                entry, _hit.get("title", ""), _hit.get("artist", "")):
            if _hit.get("file_id") or _hit.get("disk_path"):
                _cache_hit = True
                result = AudioIdResult(
                    status=_hit.get("status") or "identified",
                    title=_hit.get("title", ""),
                    artist=_hit.get("artist", ""),
                    confirmed=_hit.get("status") == "identified",
                    source=_hit.get("source") or "cache")
                _search_file_ms = int(_hit.get("resolve_ms") or 0)
                _t_send = _time.perf_counter()
                sent = await _send_identify_cache_hit(
                    callback.message, conn, _hit, token, entry, ulang,
                    _cache_key, req_id=_req_id)
                _send_ms = int((_time.perf_counter() - _t_send) * 1000)
                if sent is not None:
                    # The final audio message IS the answer: the video
                    # gets the same result keyboard as the normal path.
                    await _set_video_result_kb_if_video(
                        callback.message, result.title, result.artist,
                        ulang, entry["file_id"], callback.from_user.id,
                        entry.get("kind") or "file", result.source)
                    _timing.log_stage(log, "identify_stage", _req_id,
                                      _t_req, "name_sent", _t_req,
                                      cache_hit=True, via="shortcode",
                                      kind=_hit.get("kind") or "clip")
                    _log_identify_timing()
                    _timing.log_stage(log, "identify_stage", _req_id,
                                      _t_req, "result_send", _t_req,
                                      status=result.status)
                    return
                # Stored file gone (stale file_id / missing disk file):
                # the poisoned file_id must never be re-served — drop
                # the entry and invalidate the song-file caches, then
                # the bg phase below re-resolves once.
                _failed_fid = _hit.get("file_id") or ""
                _invalidate_stale_identify_file(
                    conn, _cache_key, _hit.get("title", ""),
                    _hit.get("artist", ""), _failed_fid)
                _hit = None
                _cache_hit = False
                result = None
                _search_file_ms = 0
        # BG PHASE (step 1b additions): the ONLY identify path on a
        # miss. The tap NEVER downloads the video and NEVER identifies
        # inline — the single bg worker does. If a bg job is in flight
        # for this key, the tap AWAITS that same job to its natural
        # completion (ceiling 120 s, never cancelled). If none is in
        # flight, the tap spawns the one bg job (link: the reel's
        # audio URL; direct file: the Telegram file_id) and awaits
        # it. On bg failure the tap reports honestly below — it never
        # falls back to a fresh download+identify. The explicit retry
        # button (vid:id token) is a separate user action, out of
        # scope here. The old URL-cache pre-check and the retired
        # _bg_precompute read are gone: both were name-only shortcuts
        # that bypassed the bg worker and its file; the bg job itself
        # reads the Stage-0 cache, so the name is still recovered
        # without a second lookup.
        _bg_entry = None
        if _cache_key:
            _task = _IDENTIFY_BG_TASKS.get(_cache_key)
            if _task is None or _task.done():
                # No in-flight job: a name-only ("pending") entry, or
                # a REJECTED name, would make the new job return at
                # once — clear it (memory only; the bg overwrites
                # Turso) so the job really re-resolves. The name is
                # recovered from the Stage-0 cache inside the job, and
                # the batch-F exclusion list is passed along so a
                # disputed name is never re-shown — exactly as the old
                # inline tap did.
                _hit_rejected = (
                    _hit is not None and _is_rejected(
                        entry, _hit.get("title", ""),
                        _hit.get("artist", "")))
                if _hit_rejected or (_hit is not None
                                     and not _hit.get("file_id")
                                     and not _hit.get("disk_path")):
                    try:
                        _IDENTIFY_RESULT_CACHE.pop(_cache_key, None)
                    except Exception:
                        pass
                # No merged video will come for a job spawned at tap
                # time (the video was already sent, its temp dir
                # cleaned) — signal that first, so the job goes
                # straight to its audio-URL / Telegram-file fallback
                # instead of waiting out the 90 s video-wait.
                try:
                    _signal_identify_bg_video_ready(_cache_key, None)
                except Exception:
                    pass
                _audio_url = entry.get("audio_url") or ""
                _task = _spawn_identify_bg(
                    _cache_key, _audio_url, callback.from_user.id, conn,
                    config, req_id=_req_id,
                    tg_file_id=entry.get("file_id") if not _audio_url
                    else None,
                    bot=callback.bot if not _audio_url else None,
                    exclude=entry.get("wrong") or None)
            if _task is not None:
                _t_wait = _time.perf_counter()
                await _await_identify_bg(_cache_key)
                _wait_bg_ms = int((_time.perf_counter() - _t_wait) * 1000)
                _bg_entry = _identify_result_get(_cache_key)
                if _bg_entry is None:
                    _bg_entry = _identify_result_get_turso(
                        _cache_key, req_id=_req_id)
                    if _bg_entry is not None:
                        _identify_result_put(_cache_key, _bg_entry)
            if _bg_entry is not None and not _is_rejected(
                    entry, _bg_entry.get("title", ""),
                    _bg_entry.get("artist", "")) \
                    and (_bg_entry.get("file_id")
                         or _bg_entry.get("disk_path")) \
                    and (not _failed_fid
                         or (_bg_entry.get("file_id") or "") != _failed_fid):
                result = AudioIdResult(
                    status=_bg_entry.get("status") or "identified",
                    title=_bg_entry.get("title", ""),
                    artist=_bg_entry.get("artist", ""),
                    confirmed=_bg_entry.get("status") == "identified",
                    source=_bg_entry.get("source") or "cache")
                _search_file_ms = int(_bg_entry.get("resolve_ms") or 0)
                _t_send = _time.perf_counter()
                sent = await _send_identify_cache_hit(
                    callback.message, conn, _bg_entry, token, entry, ulang,
                    _cache_key, req_id=_req_id)
                _send_ms = int((_time.perf_counter() - _t_send) * 1000)
                if sent is not None:
                    await _set_video_result_kb_if_video(
                        callback.message, result.title, result.artist,
                        ulang, entry["file_id"], callback.from_user.id,
                        entry.get("kind") or "file", result.source)
                    _timing.log_stage(log, "identify_stage", _req_id,
                                      _t_req, "name_sent", _t_req,
                                      cache_hit=False, via="shortcode",
                                      kind=_bg_entry.get("kind") or "clip")
                    _log_identify_timing()
                    _timing.log_stage(log, "identify_stage", _req_id,
                                      _t_req, "result_send", _t_req,
                                      status=result.status)
                    return
                # The bg's file could not be sent either (a stale
                # file_id the caches re-served): no third attempt —
                # the honest message below is the answer.
                result = None
        # BG FAILURE (or no job could be spawned): the honest message
        # (existing text) — never a fresh download+identify. A known
        # name still reaches the user on the video keyboard (Google +
        # YouTube + report); no name -> empty keyboard.
        _known = _bg_entry if _bg_entry is not None else _hit
        _k_title = (_known.get("title", "") if _known else "") or ""
        _k_artist = (_known.get("artist", "") if _known else "") or ""
        _kb_title, _kb_artist = "", ""
        if _k_title and not _is_rejected(entry, _k_title, _k_artist):
            _kb_title, _kb_artist = _k_title, _k_artist
            result = AudioIdResult(
                status=(_known.get("status") or "not_found"),
                title=_k_title, artist=_k_artist,
                confirmed=False, source=_known.get("source") or "")
        await _set_video_result_kb_if_video(
            callback.message, _kb_title, _kb_artist, ulang,
            entry["file_id"], callback.from_user.id,
            entry.get("kind") or "file",
            result.source if result is not None else "")
        await callback.message.answer(
            t("h_clip_audio_failed", ulang).format(
                head=t("m_identify_error", ulang)))
        _log_identify_timing()
        _timing.log_stage(log, "identify_stage", _req_id, _t_req,
                          "result_send", _t_req,
                          status=result.status if result is not None else "error")
    finally:
        _VIDEO_IDENTIFY_RUNNING.discard(token)
        if not _bg_owns_status:
            # The background delivery owns (and deletes) the status when
            # it was spawned; every other path deletes it here, so the
            # temporary message is never left behind.
            await _delete_card(status_msg)
        _timing.log_total(log, "identify_stage", _req_id, _t_req,
                          result.status if result is not None else "error",
                          headline_total="click_to_identify_ms",
                          cache_hit=_cache_hit,
                          wait_bg_ms=_wait_bg_ms,
                          search_file_ms=_search_file_ms,
                          send_ms=_send_ms)


# --- «❌ آهنگ اشتباه» (batch F, wrong-song re-check) ---------------------------
# One dispute per video, recorded on the SAME _VIDEO_IDENTIFY registry
# entry as the identify button (same opaque token, different callback
# prefix "wrong:"). The entry carries the shown result (last_*) so the
# handler knows what is disputed, the rejected names ("wrong") so a
# re-check — and any later normal tap on the same video — never
# re-shows them, and the attempt count (cap 2 per video).
_WRONG_SONG_RUNNING: set[str] = set()
_WRONG_SONG_MAX_ATTEMPTS = 2


def _is_rejected(entry: dict, title: str, artist: str) -> bool:
    """True when (title, artist) is a name the user already rejected
    for this video. Same normalization as the two-source gate."""
    if not entry:
        return False
    try:
        from ..search.fullsong_verify import file_identity_matches as _fim
        return any(_fim(title, artist, _wt, _wa)
                   for _wt, _wa in (entry.get("wrong") or []))
    except Exception:
        return False


def _wrong_song_button_data(file_id: str, user_id: int, kind: str,
                            title: str, artist: str,
                            source: str = "") -> str | None:
    """callback_data for the «⚠️ گزارش شناسایی اشتباه ⚠️» button: "wrong:<token>"
    for the video's EXISTING identify token (minted when none is
    registered, exactly like the retry button). Records the shown
    result on the registry entry so the handler knows the disputed
    name and its source; initializes the rejected list and the
    attempt count without resetting them. Returns None only when no
    token can be minted."""
    if not file_id or not user_id:
        return None
    cb = _find_identify_callback(file_id, user_id, kind)
    if not cb or not cb.startswith("vid:id:"):
        return None
    token = cb.split("vid:id:", 1)[1]
    entry = _VIDEO_IDENTIFY.get(token)
    if entry is None:
        return None
    entry["last_title"] = title or ""
    entry["last_artist"] = artist or ""
    entry["last_source"] = source or ""
    entry.setdefault("wrong", [])
    entry.setdefault("attempts", 0)
    return f"wrong:{token}"


def _video_result_kb(title: str, artist: str, ulang: str,
                     file_id: str | None, user_id: int | None,
                     kind: str, source: str = "",
                     include_report: bool = True) -> InlineKeyboardMarkup:
    """The keyboard under a bot-sent video AFTER the result is known
    (final identify UI, 2026-11-09). Row 1: Google + YouTube search
    for the name, side by side. Row 2: the wrong-song report button,
    full width (the EXISTING wrong:<token> handler + registry entry
    from batch F — the report records the shown name/source on the
    entry, same as before). Google/YouTube appear only when a name
    exists (a query is never invented); the report button only when a
    name was produced (confirmed or single-source). No name at all ->
    an EMPTY keyboard (no buttons under the video)."""
    import urllib.parse as _up
    name = f"{(title or '').strip()} {(artist or '').strip()}".strip()
    if not name:
        return InlineKeyboardMarkup(inline_keyboard=[])
    q = _up.quote_plus(name)
    rows = [[InlineKeyboardButton(
                 text=t("m_video_google_btn", ulang),
                 url="https://www.google.com/search?q=" + q),
             InlineKeyboardButton(
                 text=t("m_video_youtube_btn", ulang),
                 url="https://www.youtube.com/results?search_query=" + q)]]
    if include_report:
        cb = None
        if file_id and user_id:
            try:
                cb = _wrong_song_button_data(file_id, user_id, kind,
                                             title, artist, source)
            except Exception:
                cb = None
        if cb:
            rows.append([InlineKeyboardButton(
                text=t("m_video_wrong_btn", ulang), callback_data=cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _set_video_result_kb(message, title: str, artist: str,
                               ulang: str, file_id: str | None,
                               user_id: int | None, kind: str,
                               source: str = "",
                               include_report: bool = True) -> None:
    """Best-effort: set a video message's keyboard to the post-result
    layout (or empty when no name exists). Never raises; a failed edit
    leaves the markup as it was. NOTE: a USER-sent video's markup
    cannot be edited at all (Telegram only lets the bot edit its own
    messages) — the file flow calls this best-effort and the edit
    silently fails there; the video keyboard is real only for
    bot-sent videos (link flow / video-button flow)."""
    try:
        await message.edit_reply_markup(
            reply_markup=_video_result_kb(
                title, artist, ulang, file_id, user_id, kind, source,
                include_report=include_report))
    except Exception:
        pass


async def _clear_video_kb(message) -> None:
    """Best-effort: remove every button under a video message. The
    identify tap clears the keyboard — nothing shows while the
    identification runs (scenario 1). Never raises."""
    try:
        await message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]))
    except Exception:
        pass


async def _wrong_song_fetch_audio(callback, entry: dict, config,
                                  req_id: str | None = None,
                                  t_req=None) -> bytes:
    """The video's audio for the re-check: the reel's separate audio
    stream when the registry has one, else the Telegram file — the
    SAME source the original identification used, so the user never
    re-sends anything. Returns b"" when neither fetch works (the
    caller then answers honestly instead of claiming a re-check)."""
    audio_url = entry.get("audio_url")
    try:
        if audio_url:
            return await _download_link_audio(audio_url, config,
                                              req_id=req_id, t_req=t_req)
        _t_dl = _time.perf_counter()
        tg_file = await callback.bot.get_file(entry["file_id"])
        buf = io.BytesIO()
        await callback.bot.download_file(tg_file.file_path, buf)
        data = buf.getvalue()
        _timing.log_stage(log, "identify_stage", req_id, t_req,
                          "download", _t_dl, bytes=len(data or b""))
        return data or b""
    except Exception:
        return b""


def _wrong_song_log_and_store(user_id: int, link: str,
                              wrong_title: str, wrong_artist: str,
                              wrong_source: str,
                              new_title: str = "", new_artist: str = "",
                              new_source: str = "", outcome: str = "",
                              attempts: int = 0,
                              req_id: str | None = None) -> None:
    """One `wrong_song_report` log line + the Turso row (fire-and-
    forget, AFTER the answer — the store runs in a thread so the
    Turso round-trip never sits in anyone's waiting time). Never
    raises; a failed store still leaves the log line."""
    try:
        log_event(log, "identify", stage="wrong_song_report",
                  user_id=user_id, link=(link or "")[:60],
                  wrong_title=wrong_title, wrong_artist=wrong_artist,
                  wrong_source=wrong_source, new_title=new_title,
                  new_artist=new_artist, new_source=new_source,
                  outcome=outcome, attempts=attempts)
    except Exception:
        pass
    try:
        from .turso_cache import store_wrong_song_report as _store
        asyncio.create_task(asyncio.to_thread(
            _store, user_id, link, wrong_title, wrong_artist,
            wrong_source, new_title, new_artist, new_source, outcome,
            req_id))
    except Exception:
        pass


@router.callback_query(F.data.startswith("wrong:"))
async def wrong_song(callback, conn: sqlite3.Connection, config: Config):
    """«⚠️ گزارش شناسایی اشتباه ⚠️» under a sent video (final identify
    UI, 2026-11-09 — the report button lives ONLY under the video).

    Re-identifies the CACHED audio of that video — the same bytes the
    original identification used (reel audio URL, else the Telegram
    file) — with an UNUSED source: the bank is skipped when the
    disputed result came from it, and every name the user already
    rejected is excluded at every stage of _identify_audio. A
    DIFFERENT confirmed name goes through the normal identified flow
    (instant card + full song); a different single-source name is
    shown as a «شاید...» lead (never as «شناسایی شد»); anything else
    is an honest «پیدا نشد» + an invitation to type the name (the
    normal text search handles it). Cap 2 re-identifies per video.
    The report (link, wrong result, source, outcome) is logged and
    stored in Turso AFTER the answer. No automatic follow-up: one
    tap, one outcome, nothing else is ever sent."""
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    token = callback.data.split("wrong:", 1)[1]
    entry = _VIDEO_IDENTIFY.get(token)
    if not entry:
        # The registry is in-memory: after a restart the token is gone.
        await callback.answer(t("m_req_expired", ulang), show_alert=True)
        return
    if entry.get("user_id") != callback.from_user.id:
        await callback.answer(t("m_not_yours", ulang), show_alert=True)
        return
    wrong_title = entry.get("last_title") or ""
    wrong_artist = entry.get("last_artist") or ""
    wrong_source = entry.get("last_source") or ""
    if not wrong_title:
        # No confirmed result was ever recorded for this token: the
        # button has nothing to dispute.
        await callback.answer(t("m_req_expired", ulang), show_alert=True)
        return
    if token in _WRONG_SONG_RUNNING:
        await callback.answer(t("m_wrong_song_checking", ulang))
        return
    attempts = int(entry.get("attempts") or 0)
    if attempts >= _WRONG_SONG_MAX_ATTEMPTS:
        await callback.answer(t("m_wrong_song_limit", ulang), show_alert=True)
        # FINAL IDENTIFY UI (2026-11-09): the report button goes away
        # (attempts exhausted); Google + YouTube stay for the name.
        await _set_video_result_kb(
            callback.message, wrong_title, wrong_artist, ulang,
            entry["file_id"], callback.from_user.id,
            entry.get("kind") or "file", wrong_source,
            include_report=False)
        _wrong_song_log_and_store(
            callback.from_user.id,
            entry.get("link") or entry.get("audio_url") or "",
            wrong_title, wrong_artist, wrong_source,
            outcome="limit", attempts=attempts)
        return
    _WRONG_SONG_RUNNING.add(token)
    _req_id = _timing.new_req_id()
    _t_req = _time.perf_counter()
    _t_wall = _time.time()
    _ws_key = None
    _timing.log_stage(log, "identify_stage", _req_id, _t_req, "receipt",
                      _t_req)
    await callback.answer(t("m_wrong_song_checking", ulang))
    status_msg = None
    try:
        status_msg = await callback.message.answer(
            t("m_wrong_song_checking", ulang))
    except Exception:
        status_msg = None
    outcome = "error"
    new_title = new_artist = new_source = ""
    try:
        entry["attempts"] = attempts + 1
        # The disputed name joins the rejected list BEFORE the
        # re-check, so a second attempt also excludes it.
        rejected = list(entry.get("wrong") or []) + [(wrong_title, wrong_artist)]
        entry["wrong"] = rejected
        # Step 1b: the cached result for this shortcode is disputed —
        # clear it from memory NOW, so no concurrent tap can hit the
        # stale entry; the Turso delete runs after the answer, next to
        # the wrong-song report write.
        _ws_key = _identify_result_key(link=entry.get("link"),
                                       file_id=entry.get("file_id"))
        _identify_result_clear(_ws_key, turso=False)
        data = await _wrong_song_fetch_audio(callback, entry, config,
                                             req_id=_req_id, t_req=_t_req)
        if not data or len(data) > config.max_file_mb * 1024 * 1024:
            raise ValueError("audio unavailable")
        result = await _identify_audio(
            data, "clip.mp3" if entry.get("kind") == "audio" else "clip.mp4",
            config, conn, cache_url_key=entry.get("audio_url") or None,
            req_id=_req_id, t_req=_t_req,
            exclude=rejected,
            skip_bank=(wrong_source == "bank"))
        _om_kind = "audio" if entry.get("kind") == "audio" else "video"
        if result.status == "identified":
            # A DIFFERENT confirmed name (the exclusion guarantees it
            # differs): the normal identified flow — instant card, then
            # the full song as one final message.
            outcome = "different_confirmed"
            new_title, new_artist, new_source = (
                result.title, result.artist, result.source)
            entry["last_title"], entry["last_artist"], entry["last_source"] = (
                new_title, new_artist, new_source)
            # UNIFY FIX: no instant card — the searching message IS the
            # temporary status; the background delivery deletes it when
            # the ONE final message is sent.
            _spawned = await _send_identified_song(
                callback.message, conn, result.title, result.artist,
                config, bot_id=dbmod.get_bot_id(conn, config),
                own_media=(_om_kind, entry["file_id"]),
                req_id=_req_id,
                arm=("video_button", entry["file_id"], data,
                     entry.get("audio_url") or "", ""),
                source=result.source, status_msg=status_msg,
                cache_key=_ws_key, t_wall=_t_wall)
            if not _spawned:
                await _delete_card(status_msg)
            # FINAL IDENTIFY UI (2026-11-09): the video keyboard
            # follows the NEW name (its Google/YouTube links change
            # with it); the final audio carries only playlist +
            # lyrics.
            await _set_video_result_kb(
                callback.message, result.title, result.artist, ulang,
                entry["file_id"], callback.from_user.id,
                entry.get("kind") or "file", result.source)
        elif result.status == "unconfirmed" and result.title:
            # A DIFFERENT single-source name: a «شاید...» lead, never a
            # «شناسایی شد» card. Heart/lyrics/playlist stay available;
            # the wrong-song button stays too, under the same token.
            outcome = "different_maybe"
            new_title, new_artist, new_source = (
                result.title, result.artist, result.source)
            entry["last_title"], entry["last_artist"], entry["last_source"] = (
                new_title, new_artist, new_source)
            # Step 1b additions: an unconfirmed re-check result is a
            # REAL result for this shortcode — the entry was cleared
            # above, so store the new name + stage the clip's own
            # audio, or the next tap re-runs the whole pipeline
            # (observed 2026-11-09: the 18:02 unconfirmed result did
            # not serve the 18:12 tap — this branch never stored).
            # A disk-staged clip is the sendable form; the tap
            # upgrades it to a file_id on first send.
            if _ws_key:
                try:
                    _ws_clip = await _bg_clip_to_disk(
                        _ws_key, data, result, req_id=_req_id)
                    if _ws_clip:
                        _ws_dur = None
                        try:
                            _br, _d = await asyncio.to_thread(
                                probe_audio, data, "mp4")
                            _ws_dur = _d
                        except Exception:
                            _ws_dur = None
                        _identify_result_store(
                            _ws_key,
                            _identify_result_entry(
                                status="unconfirmed",
                                title=result.title,
                                artist=result.artist,
                                source=result.source, kind="clip",
                                file_id="", disk_path=_ws_clip,
                                duration_sec=_ws_dur),
                            req_id=_req_id, force=True)
                except Exception:
                    pass
            key = profeat.remember_result(
                result.title, result.artist, "", "",
                own_media=(_om_kind, entry["file_id"]))
            # FINAL IDENTIFY UI (2026-11-09): the «شاید...» text keeps
            # only the name's own buttons (playlist + lyrics, one
            # row) — no heart, and the report button lives under the
            # video only.
            kb = InlineKeyboardMarkup(
                inline_keyboard=[_identified_audio_row(key, ulang)])
            await _set_video_result_kb(
                callback.message, result.title, result.artist, ulang,
                entry["file_id"], callback.from_user.id,
                entry.get("kind") or "file", result.source)
            maybe_text = t("m_wrong_song_maybe", ulang).format(
                title=result.title, artist=result.artist)
            if status_msg is not None:
                try:
                    await status_msg.edit_text(maybe_text, reply_markup=kb)
                except Exception:
                    await callback.message.answer(maybe_text, reply_markup=kb)
            else:
                await callback.message.answer(maybe_text, reply_markup=kb)
        else:
            # No different result (not_found, lookup error, or every
            # source named only rejected songs): honest «پیدا نشد» +
            # invite the user to type the name — the normal text
            # search handles it. The searching message becomes the
            # answer; nothing else is sent.
            outcome = "not_found"
            if status_msg is not None:
                try:
                    await status_msg.edit_text(
                        t("m_wrong_song_notfound", ulang))
                except Exception:
                    await callback.message.answer(
                        t("m_wrong_song_notfound", ulang))
            else:
                await callback.message.answer(
                    t("m_wrong_song_notfound", ulang))
    except Exception:
        # Audio fetch failed, or the re-check raised: honest error,
        # never silence, never a guessed name.
        outcome = "error"
        if status_msg is not None:
            try:
                await status_msg.edit_text(t("m_wrong_song_error", ulang))
            except Exception:
                try:
                    await callback.message.answer(
                        t("m_wrong_song_error", ulang))
                except Exception:
                    pass
        else:
            try:
                await callback.message.answer(
                    t("m_wrong_song_error", ulang))
            except Exception:
                pass
    finally:
        _WRONG_SONG_RUNNING.discard(token)
        _timing.log_total(log, "identify_stage", _req_id, _t_req, outcome,
                          headline_total="click_to_identify_ms")
    _wrong_song_log_and_store(
        callback.from_user.id,
        entry.get("link") or entry.get("audio_url") or "",
        wrong_title, wrong_artist, wrong_source,
        new_title, new_artist, new_source, outcome,
        attempts=int(entry.get("attempts") or 0), req_id=_req_id)
    # Step 1b: the Turso half of the clear (memory was cleared before
    # the re-check). Atomic and conditional: the row is deleted ONLY
    # when it still holds the disputed name, so a bg delivery that
    # already stored the NEW result under the same key is never wiped.
    if _ws_key:
        try:
            from .turso_cache import delete_identify_result_if as _t_del_if
            _t_del_if(_ws_key, wrong_title, wrong_artist, req_id=_req_id)
        except Exception:
            pass


async def _identify_audio(data: bytes, filename: str, config: Config,
                        conn=None, cache_url_key: str | None = None,
                        req_id: str | None = None, t_req=None,
                        exclude: list | None = None,
                        skip_bank: bool = False) -> AudioIdResult:
    """Shazam and AudD run IN PARALLEL; a name counts as identified
    only when TWO sources agree (confirm fix 2026-11-09).

    ``exclude`` (batch F, wrong-song re-check): [(title, artist), ...]
    the user already rejected for this video. A candidate matching any
    of them — at the cache, in the bank, at the two-source gate, or as
    a single-source name — is dropped as if it never answered, and the
    next-best candidate is considered. When every source named only
    rejected songs, the result is "not_found" WITHOUT caching (the
    bytes do have a known identification; caching not_found would hide
    it from a later normal tap). ``skip_bank`` skips the exclusive-bank
    stage entirely: the bank is the "used" source when the disputed
    result came from it, so the re-check consults only Shazam/AudD/
    ACRCloud. With both unset the behavior is byte-identical to
    before (all existing paths call without them).

    Pipeline stages (each logged with timing; errors carry their stage):
      0. identify cache — sha256 of the input bytes is looked up first;
         a hit returns the stored terminal result with no download and no
         lookup. Only CONFIRMED identified results (bank match or
         Shazam+AudD agreement) and not_found are ever stored; errors
         never. A stored "identified" is confirmed by invariant.
      1. input validation gate — the extracted audio must be decodable,
         non-silent and of sane duration, else a distinct extraction
         error is returned (never "not_found"). Validation keeps its
         signable WAV so the lookup reuses it (no second transcode).
      2. Shazam on several windows (full, then later windows) so a clip
         with speech over the start, a remix intro, or a shortened copy
         has more than one chance; only an identified result is
         accepted. Windows are PCM slices of the validated WAV (slicing
         raw container bytes broke the container, so those windows could
         never decode). A sliced window that fails to decode only yields
         "error", never a false match. A catalog result is never
         involved here, so nothing can be mislabelled as identified.
         Shazam and AudD run CONCURRENTLY (asyncio.gather); AudD no
         longer waits for Shazam to fail. The name is "identified" only
         when Shazam AND AudD AGREE on title/artist (same normalization
         as the tag guard: file_identity_matches). A single source
         (Shazam-only, AudD-only, ACRCloud-only) or a disagreement
         returns "unconfirmed" with the best single-source name — the
         callers never show an «شناسایی شد» card for it.

    `conn`: when given, the audio-hash cache is consulted and terminal
    results are stored (under the content hash, and under
    `cache_url_key` when provided so a later flow can skip the download
    too). None (tests, one-off scripts) disables the cache silently.
    """
    import time as _time
    import hashlib as _hashlib
    from ..services.shazam_id import analyze_audio_with_wav, wav_time_slice
    # Timing instrumentation (logs only): one request id per identify
    # call. Callers pass the tap/link request's id so the whole request
    # groups under it; otherwise a fresh id is generated here.
    if req_id is None:
        req_id = _timing.new_req_id()
    if t_req is None:
        t_req = _time.perf_counter()

    def _st(stage, t_stage, **extra):
        _timing.log_stage(log, "identify_stage", req_id, t_req, stage,
                          t_stage, **extra)

    # Batch F (wrong-song re-check): names the user already rejected
    # for this video. _excluded_hit records that at least one source
    # named only rejected songs, so the tail can answer "not_found"
    # instead of "lookup_error" for the re-check. Defined before
    # Stage 0 because the cache-hit return is the first exclusion
    # point.
    _excluded_hit = [False]

    def _is_excluded(title: str, artist: str) -> bool:
        """True when (title, artist) is a rejected name. Same
        normalization as the two-source gate, so a rejected "(Remix)"
        never slips back in under a plainer spelling."""
        if not exclude:
            return False
        from ..search.fullsong_verify import file_identity_matches as _fim
        for _ex_t, _ex_a in exclude:
            if _fim(title, artist, _ex_t, _ex_a):
                _excluded_hit[0] = True
                return True
        return False

    t0 = _time.perf_counter()
    _st("input_received", t_req, bytes=len(data or b""), filename=filename)
    if not data:
        _st("validate", t_req, ok=False, reason="empty_input")
        return AudioIdResult(status="error", detail="undecodable")
    # Stage 0: audio-content-hash cache. Same bytes re-identified ->
    # stored terminal result, skipping download and lookup entirely.
    audio_hash = ""
    t0 = _time.perf_counter()
    try:
        audio_hash = _hashlib.sha256(data).hexdigest()
        cached = dbmod.get_identify_cache(conn, f"hash:{audio_hash}") \
            if conn is not None else None
    except Exception:
        cached = None
    _st("cache_check", t0, hit=cached is not None)
    # A cached "not_found" predates the exclusive bank: the bank may hold
    # this song now, so fall through to the bank-only check below (Stage 1
    # + fingerprint) instead of returning — Shazam is NOT re-run.
    bank_only = False
    if cached is not None:
        if cached["status"] == "identified":
            if _is_excluded(cached["title"], cached["artist"]):
                # The cached name is the one the user rejected: never
                # re-show it — fall through to the live pipeline with
                # the exclusion still active.
                _st("result_sent", t0, status="identified", cached=True,
                    excluded=True)
            else:
                _st("result_sent", t0, status="identified", cached=True)
                return AudioIdResult(status="identified", title=cached["title"],
                                     artist=cached["artist"], confirmed=True,
                                     source="cache")
        else:
            _st("result_sent", t0, status=cached["status"], cached=True)
            bank_only = True

    def _cache_store(status: str, title: str = "", artist: str = "") -> None:
        # Terminal results only; errors are never cached.
        if conn is None or status not in ("identified", "not_found"):
            return
        try:
            if audio_hash:
                dbmod.set_identify_cache(conn, f"hash:{audio_hash}",
                                         status, title, artist)
            if cache_url_key:
                dbmod.set_identify_cache(conn, f"url:{cache_url_key}",
                                         status, title, artist)
        except Exception:
            pass

    # Stage 1: validation gate (keeps the signable WAV for the lookup).
    t0 = _time.perf_counter()
    try:
        # Sync ffmpeg transcode + pure-Python WAV analysis run in the
        # default executor so identification never blocks the event
        # loop (same pattern as the fpcalc call below).
        import functools as _functools
        analysis, wav = await asyncio.get_running_loop().run_in_executor(
            None, _functools.partial(analyze_audio_with_wav, data,
                                     req_id=req_id))
    except Exception as exc:
        _st("validate", t_req, ok=False,
            reason=f"analyze_crashed:{type(exc).__name__}")
        return AudioIdResult(status="error", detail="undecodable")
    _st("validate", t0, wav_reused=wav is not None, **analysis)
    if not analysis.get("ok"):
        reason = analysis.get("reason", "undecodable")
        if reason == "undecodable" and data:
            # "No audio track at all" is not "corrupt audio": when the
            # container provably has no audio stream (e.g. a video-only
            # file), report "silent" so the user sees the accurate
            # "no usable audio" line instead of the "corrupt" line.
            try:
                if has_audio_stream(data) is False:
                    reason = "silent"
                    _st("validate", t0, ok=False, reason="silent_no_stream")
            except Exception:
                pass
        if reason == "undecodable" and data and _XBANK_OK:
            # Bank-first even when the validation transcode failed (user
            # order 2026-11-07): an extraction error must not hide a bank
            # song. fpcalc reads the raw container itself, so fingerprint
            # the raw bytes directly; only a confident match returns
            # identified, otherwise the honest error below stands.
            try:
                from .exclusive_bank import (
                    fingerprint_bytes as _xb_fingerprint,
                    inc_identify as _xb_inc,
                    match_sample as _xb_match, _get_conn as _xb_conn)
                import asyncio as _asyncio
                import functools as _functools
                import os as _os
                _suf = _os.path.splitext(filename or "")[1] or ".bin"
                _dur, _items = await _asyncio.get_running_loop().run_in_executor(
                    None, _functools.partial(_xb_fingerprint, data, _suf,
                                             req_id=req_id))
                if _items:
                    _c = _xb_conn()
                    if _c is not None:
                        _song, _sim = _xb_match(_c, _items, req_id=req_id)
                        if _song is not None \
                                and not _is_excluded(_song["title"],
                                                     _song["artist"]):
                            _st("exclusive_bank", t0,
                                status="identified", sim=round(_sim, 3),
                                title=_song["title"], via="raw_fallback")
                            try:
                                _xb_inc(_c, _song["id"], req_id=req_id)
                            except Exception:
                                pass
                            _cache_store("identified", _song["title"],
                                         _song["artist"])
                            return AudioIdResult(status="identified",
                                                 title=_song["title"],
                                                 artist=_song["artist"],
                                                 confirmed=True,
                                                 source="bank")
                        if _song is not None:
                            _st("exclusive_bank", t0,
                                status="excluded", sim=round(_sim, 3),
                                title=_song["title"], via="raw_fallback")
            except Exception:
                pass
        # Distinct extraction-stage error — never "not_found".
        return AudioIdResult(status="error", detail=reason)
    # Stage 1.5: exclusive Avano bank (user order 2026-11-07) — fingerprint
    # the validated sample FIRST, before any external lookup. A confident
    # match returns the bank song's title/artist; the delivery points route
    # it to the exclusive file via _exclusive_song_for. Any bank error
    # fails OPEN: the Shazam -> AudD -> ACRCloud chain runs untouched.
    # `bank_only` (cached not_found) skips that chain after the bank check.
    if skip_bank:
        # Wrong-song re-check: the bank IS the disputed source, so the
        # re-check consults only the unused sources (Shazam/AudD/
        # ACRCloud). The skip is logged, never silent.
        _st("exclusive_bank", _time.perf_counter(), ok=False,
            reason="skipped_wrong_song")
    elif _XBANK_OK:
        _t_bank = _time.perf_counter()
        try:
            from .exclusive_bank import (
                fingerprint_bytes as _xb_fingerprint,
                inc_identify as _xb_inc,
                match_sample as _xb_match, _get_conn as _xb_conn)
            import asyncio as _asyncio
            import functools as _functools
            _xb_items = None
            _xb_fp_failed = False
            try:
                _xb_bytes = wav if wav is not None else data
                _dur, _xb_items = await _asyncio.get_running_loop().run_in_executor(
                    None, _functools.partial(
                        _xb_fingerprint, _xb_bytes,
                        ".wav" if wav is not None else ".bin",
                        req_id=req_id))
            except Exception as _fe:
                _xb_fp_failed = True
                _st("exclusive_bank", _t_bank, ok=False,
                    reason=f"fingerprint_failed:{type(_fe).__name__}")
            if _xb_items:
                _xb_c = _xb_conn()
                if _xb_c is None:
                    _st("exclusive_bank", _t_bank, ok=False,
                        reason="bank_unavailable")
                else:
                    _song, _sim = _xb_match(_xb_c, _xb_items, req_id=req_id)
                    if _song is not None \
                            and not _is_excluded(_song["title"],
                                                 _song["artist"]):
                        _st("exclusive_bank", _t_bank,
                            status="identified", sim=round(_sim, 3),
                            title=_song["title"])
                        try:
                            _xb_inc(_xb_c, _song["id"], req_id=req_id)
                        except Exception:
                            pass
                        _cache_store("identified", _song["title"], _song["artist"])
                        return AudioIdResult(status="identified",
                                             title=_song["title"],
                                             artist=_song["artist"],
                                             confirmed=True, source="bank")
                    if _song is not None:
                        # The bank's answer is the name the user already
                        # disputed: it must not be re-shown, and it is
                        # not a "no_match" either.
                        _st("exclusive_bank", _t_bank,
                            status="excluded", sim=round(_sim, 3),
                            title=_song["title"])
                    else:
                        # No match is logged too (with the best similarity
                        # and the query size): a silent bank check is how
                        # the 2026-11-07 short-sample failure stayed
                        # invisible.
                        _st("exclusive_bank", _t_bank,
                            status="no_match", sim=round(_sim, 3),
                            items=len(_xb_items))
            elif not _xb_fp_failed:
                _st("exclusive_bank", _t_bank, ok=False,
                    reason="fingerprint_empty")
        except Exception as _e:
            _st("exclusive_bank", _t_bank, ok=False,
                reason=f"bank_check_failed:{type(_e).__name__}")
    if bank_only:
        return AudioIdResult(status="not_found")
    # Stage 2: Shazam windows. Each window is (raw_probe, prepared_wav):
    # signable raw inputs take the old raw path verbatim; codec-rejected
    # inputs (AAC, Opus, …) reuse the validation WAV — no second ffmpeg
    # transcode of the same bytes. Windows are PCM time-slices because
    # slicing raw container bytes breaks the container.
    windows: list = [(data, wav)]
    if wav is not None and len(data) > 200_000:
        for frac in (1 / 3, 2 / 3):
            sl = wav_time_slice(wav, frac, 1.0)
            if sl is not None:
                windows.append((data, sl))
    elif wav is None and len(data) > 200_000:
        windows.append((data[len(data) // 3:], None))
        windows.append((data[2 * len(data) // 3:], None))
    log_event(log, "identify_stage", stage="input_prepared",
              req_id=req_id,
              total_ms=int((_time.perf_counter() - t_req) * 1000),
              windows=len(windows),
              sizes=[len(w[1]) if w[1] is not None else len(w[0])
                     for w in windows],
              wav_reused=wav is not None)
    # Stage 2 (confirm fix 2026-11-09): Shazam and AudD run IN PARALLEL
    # (asyncio.gather) — AudD no longer waits for Shazam to fail, so a
    # second opinion always exists. A name is CONFIRMED only when the
    # exclusive bank matched (returned above) or Shazam AND AudD AGREE
    # on title/artist (same normalization as the tag guard:
    # file_identity_matches — lowercase, strip, bracketed-suffix
    # insensitive, Persian/Arabic char normalization). A single source
    # (Shazam-only, AudD-only, ACRCloud-only) or a disagreement returns
    # "unconfirmed" with the best single-source name — the callers
    # never show an «شناسایی شد» card for it, only the extracted audio.
    # ACRCloud is not configured (user order) — its block is untouched
    # except that it can only ever produce an unconfirmed single-source
    # name now, never an "identified" return.
    async def _run_shazam_windows():
        _shazam = None
        for i, (probe, prepared) in enumerate(windows):
            t0 = _time.perf_counter()
            try:
                candidate = await identify_bytes_shazam(probe,
                                                        prepared_wav=prepared,
                                                        req_id=req_id)
                log_event(log, "identify_stage", stage="lookup_response",
                          req_id=req_id,
                          total_ms=int((_time.perf_counter() - t_req) * 1000),
                          window=i, ms=int((_time.perf_counter() - t0) * 1000),
                          status=candidate.status if candidate else "none")
            except Exception as exc:
                log_event(log, "identify_stage", stage="lookup_error",
                          req_id=req_id,
                          total_ms=int((_time.perf_counter() - t_req) * 1000),
                          window=i,
                          error=f"{type(exc).__name__}: {str(exc)[:120]}")
                candidate = None
            if candidate is not None:
                _shazam = candidate
                if candidate.status == "identified":
                    break
        return _shazam

    async def _run_audd():
        if not config.audd_token:
            return None
        t0 = _time.perf_counter()
        try:
            audd = await identify_bytes(data, filename, config.audd_token)
            log_event(log, "identify_stage", stage="lookup_response",
                      req_id=req_id,
                      total_ms=int((_time.perf_counter() - t_req) * 1000),
                      window="audd", ms=int((_time.perf_counter() - t0) * 1000),
                      status=audd.status if audd else "none")
            return audd
        except Exception as exc:
            log_event(log, "identify_stage", stage="lookup_error",
                      req_id=req_id,
                      total_ms=int((_time.perf_counter() - t_req) * 1000),
                      window="audd",
                      error=f"{type(exc).__name__}: {str(exc)[:120]}")
            return None

    _t_shazam = _time.perf_counter()
    shazam, audd = await asyncio.gather(_run_shazam_windows(), _run_audd())
    _st("api_shazam", _t_shazam,
        status=shazam.status if shazam is not None else "none",
        windows=len(windows))
    _st("api_audd", _t_shazam,
        status=audd.status if audd is not None else "none")
    # ACRCloud as third fallback (150M tracks, MENA coverage, sped-up tolerant).
    # Runs only when neither Shazam nor AudD identified — the same
    # condition as before (the old sequential chain only ever reached
    # it after both failed). It can only add a single-source name.
    acr = None
    if (shazam is None or shazam.status != "identified") \
            and (audd is None or audd.status != "identified") \
            and config.acrcloud_host and config.acrcloud_key \
            and config.acrcloud_secret:
        t0 = _time.perf_counter()
        try:
            acr = await identify_acrcloud(
                data, config.acrcloud_host,
                config.acrcloud_key, config.acrcloud_secret)
            log_event(log, "identify_stage", stage="lookup_response",
                      req_id=req_id,
                      total_ms=int((_time.perf_counter() - t_req) * 1000),
                      window="acrcloud", ms=int((_time.perf_counter() - t0) * 1000),
                      status=acr.status if acr else "none")
        except Exception as exc:
            log_event(log, "identify_stage", stage="lookup_error",
                      req_id=req_id,
                      total_ms=int((_time.perf_counter() - t_req) * 1000),
                      window="acrcloud",
                      error=f"{type(exc).__name__}: {str(exc)[:120]}")
            acr = None
        _st("api_acrcloud", t0, status=acr.status if acr is not None else "none")
    # TWO-SOURCE CONFIRMATION GATE: Shazam + AudD agreement only. A
    # rejected name (batch F) never confirms, even when both sources
    # still agree on it — the fall-through below lets a DIFFERENT
    # single-source name surface instead.
    from ..search.fullsong_verify import file_identity_matches as _fim
    if (shazam is not None and shazam.status == "identified"
            and audd is not None and audd.status == "identified"
            and _fim(shazam.title, shazam.artist, audd.title, audd.artist)
            and not _is_excluded(shazam.title, shazam.artist)):
        _cache_store("identified", shazam.title, shazam.artist)
        _st("result_sent", t0, status="identified",
            title=shazam.title, artist=shazam.artist,
            confirmed=True, via="shazam+audd")
        return AudioIdResult(status="identified", title=shazam.title,
                             artist=shazam.artist, confirmed=True,
                             source="shazam+audd")
    # Single-source name (Shazam first — it is the primary): unconfirmed.
    # A rejected name is skipped at every source: the first
    # NON-excluded identified candidate wins.
    _single = None
    _single_src = ""
    for _cand, _src in ((shazam, "shazam"), (audd, "audd"), (acr, "acrcloud")):
        if _cand is not None and _cand.status == "identified" \
                and not _is_excluded(_cand.title, _cand.artist):
            _single, _single_src = _cand, _src
            break
    if _single is not None:
        _st("result_sent", t0, status="unconfirmed",
            title=_single.title, artist=_single.artist, via=_single_src)
        return AudioIdResult(status="unconfirmed", title=_single.title,
                             artist=_single.artist, confirmed=False,
                             source=_single_src)
    if shazam is not None and shazam.status == "not_found":
        _cache_store("not_found")
        _st("result_sent", t0, status="not_found")
        return AudioIdResult(status="not_found")
    if _excluded_hit[0]:
        # Every source named only the rejected song(s): for THIS
        # re-check nothing different was found. "not_found" — and
        # deliberately NOT cached (the bytes DO have a known
        # identification; caching not_found would hide it from a
        # later normal tap).
        _st("result_sent", t0, status="not_found", reason="all_excluded")
        return AudioIdResult(status="not_found")
    _st("result_sent", t0, status="error", detail="lookup_error")
    return AudioIdResult(status="error", detail="lookup_error")


def _link_platform_name(kind: str, lang: str = "fa") -> str:
    return {"instagram": t("plat_instagram", lang), "youtube": t("plat_youtube", lang),
            "aparat": t("plat_aparat", lang), "tiktok": t("plat_tiktok", lang)}.get(
                kind, t("plat_generic", lang))


def _link_needs_file_card(outcome, cause: str = "", lang: str = "fa") -> str:
    """The single compact card for a link whose media the platform did
    not hand over: video-link line + one honest taxonomy line. One
    message, never a stack of video-link + nag + channel notice.

    ``cause`` is the 5-way taxonomy code from _identify_link_audio
    ("age_gated" | "unavailable" | "empty_shell" | "timeout" |
    "network_error"); the card states the TRUE cause, never a generic
    line. Empty cause keeps the legacy generic line (non-Instagram
    platforms have no taxonomy)."""
    cause_line = {
        "age_gated": t("dl_age_gated", lang),
        "unavailable": t("dl_unavailable", lang),
        "empty_shell": t("dl_empty_shell", lang),
        "timeout": t("dl_fetch_timeout", lang),
        "network_error": t("dl_fetch_network", lang),
    }.get(cause or "", "")
    if not cause_line:
        platform = _link_platform_name(outcome.kind, lang)
        cause_line = t("h_platform_no_file", lang).format(platform=platform)
    card = (t("m_link_video_head", lang) + outcome.official_url +
            "\n\n" + cause_line)
    if "مستقیم بفرستید" not in cause_line and t("m_send_directly", lang) not in cause_line:
        card += t("m_send_video_for_id", lang)
    return card


# --- background identify pre-compute (Phase 2 batch 2, 2026-11-08) -------
# Spawned ONLY AFTER the video is sent (post-send, never at resolve-done,
# never concurrent with the download/mux — Render is 0.1 CPU, so the
# extract must not compete with the ffmpeg mux). ONE light task: extract
# the 12 s slice from the reel's audio URL and match it against the
# exclusive bank. The result is cached ~10 min so a tap on
# «🎵 شناسایی موسیقی» answers from the cache (bank hit) or pays only the
# API calls on the already-extracted slice (bank miss). The full
# _identify_link_audio engine is deliberately NOT run here: it downloads
# the whole audio file and burns Shazam/AudD/ACRCloud calls for users who
# never tap identify.
# Guardrails (prototype P4): max 1 background identify per chat (a new
# link cancels the previous pending one); extract timeout 30 s (60 s
# total incl. semaphore wait); the bank step is one Turso read plus a
# <=4 ms match loop and is logged with its real ms; ffmpeg runs through
# the EXISTING story-clips ffmpeg semaphore (story_clips._WM_SEM) so no
# second heavy ffmpeg runs beside a story-clip pass; the task never
# blocks the send coroutine (fire-and-forget, all errors swallowed);
# the result expires after 10 min (a user who never taps pays nothing
# after that). Replaces _bg_identify_link (full background identify).
_BG_PRECOMPUTE: dict[str, dict] = {}
_BG_PRECOMPUTE_TTL = 600  # seconds
_BG_PRECOMPUTE_MAX = 32  # entries; ~0.5 MB slice each -> <= ~16 MB
_BG_PRECOMPUTE_TASKS: dict[int, asyncio.Task] = {}


# --- Step 1b: shortcode-keyed identify-result cache (2026-11-09) -------
# The Stage-0 hash cache answers only the IDENTIFY step (name only);
# the tap still re-downloaded the video and re-resolved the file on
# every tap, so click-to-name stayed 15-27 s. This cache stores the
# FINAL result — title/artist/source, the file KIND (full song vs clip
# audio) and a SENDABLE file reference — keyed by SHORTCODE, so a
# repeat tap sends the stored file immediately: no download, no bank
# match, no Shazam, no AudD. Memory LRU (cap 200) is the hot path;
# Turso is read ONLY on a memory miss and written only after a send
# or inside the bg task (hot-path gates). Disk staging holds a
# prepared file until its first send, after which the returned
# file_id replaces the path; disk files are never in RAM and never
# stored in Turso (a path is meaningless on another instance).
_IDENTIFY_RESULT_MAX = 200  # memory entries
_IDENTIFY_RESULT_CONFIRMED_TTL = 7 * 86400  # confirmed: matches song_file_cache 7d
_IDENTIFY_RESULT_NOTFOUND_TTL = 600  # not identified / single-source: 10 min, ordered
# Step 1b additions (2026-11-09): the tap AWAITS the bg identify job
# to its natural completion instead of the 5 s bound. Ceiling: the
# bg's post-video work is bounded by the adapter/verify/transcode
# timeouts (~40 s worst); the 90 s video-wait is already settled by
# tap time (the video was sent, so the event was set). 120 s is 3x
# headroom. On timeout the bg keeps running (never cancelled) and the
# tap reports honestly; the next tap hits the cache.
_IDENTIFY_BG_AWAIT_CEILING = 120.0
_IDENTIFY_RESULT_DISK_TTL = 86400  # staged files: 24 h (the NAME stays cached 7 d)
_IDENTIFY_RESULT_DISK_MAX_FILES = 50
_IDENTIFY_RESULT_DISK_MAX_BYTES = 300 * 1024 * 1024
_IDENTIFY_RESULT_CACHE: dict[str, dict] = {}
_IDENTIFY_BG_TASKS: dict[str, asyncio.Task] = {}
_IDENTIFY_BG_VIDEO_READY: dict[str, tuple] = {}

_IG_SHORTCODE_RE = re.compile(
    r"instagram\.com/(?:reel|p|tv|reels)/([A-Za-z0-9_-]+)")
_APARAT_HASH_RE = re.compile(r"aparat\.com/v/([A-Za-z0-9]+)")


def _identify_result_key(link=None, file_id=None, data=None):
    """One key for an identify result, from the most stable source
    available: Instagram shortcode > Aparat videohash > Telegram
    file_id of the source video > content sha256. The shortcode is
    stable across re-sends of the same reel (fresh link, cache-hit
    link, and the bg task all derive the same key); a Telegram
    file_id is content-based, so a re-sent direct file matches too.
    Returns None when nothing is derivable."""
    for _src in (link or "",):
        _m = _IG_SHORTCODE_RE.search(_src)
        if _m:
            return "ig:" + _m.group(1)
        _m = _APARAT_HASH_RE.search(_src)
        if _m:
            return "ap:" + _m.group(1)
    if file_id:
        return "tg:" + str(file_id)
    if data:
        try:
            import hashlib as _hashlib
            return "sha:" + _hashlib.sha256(data).hexdigest()
        except Exception:
            return None
    return None


def _identify_result_entry(status: str, title: str = "", artist: str = "",
                           source: str = "", kind: str = "pending",
                           file_id: str = "", disk_path: str = "",
                           duration_sec: float = 0, quality: str = "",
                           extra: str = "", cover_file_id: str = "",
                           ttl: int | None = None,
                           resolve_ms: int = 0) -> dict:
    """One cache entry. kind: "full" | "clip" | "pending" (name known,
    file not ready yet). Only CONFIRMED results get the long TTL;
    anything else gets the ordered 10 minutes. quality/extra are the
    caption's quality line and extra line (bank: measured kbps + the
    💎 marker; adapter full songs: empty) and cover_file_id is the
    bank song's cover thumbnail, so a cache hit reproduces the fresh
    path's message exactly. resolve_ms is the bg job's file-resolution
    time for this entry (local/Turso file_id lookup, bank check,
    adapter search + tag verify + disk staging, or clip transcode) —
    0 when the file was resolved by another flow; the tap logs it as
    the search_file stage."""
    if ttl is None:
        ttl = (_IDENTIFY_RESULT_CONFIRMED_TTL if status == "identified"
               else _IDENTIFY_RESULT_NOTFOUND_TTL)
    return {"status": status, "title": title or "", "artist": artist or "",
            "source": source or "", "kind": kind,
            "file_id": file_id or "", "disk_path": disk_path or "",
            "duration_sec": duration_sec or 0,
            "quality": quality or "", "extra": extra or "",
            "cover_file_id": cover_file_id or "",
            "resolve_ms": int(resolve_ms or 0),
            "created_at": _time.time(), "ttl": ttl}


def _identify_result_get(key: str) -> dict | None:
    """Fresh memory entry, or None (miss/expired). A hit refreshes
    the LRU position (plain dict: pop + reinsert)."""
    if not key:
        return None
    ent = _IDENTIFY_RESULT_CACHE.get(key)
    if ent is None:
        return None
    if _time.time() - ent.get("created_at", 0) > ent.get("ttl", 0):
        _IDENTIFY_RESULT_CACHE.pop(key, None)
        return None
    _IDENTIFY_RESULT_CACHE.pop(key, None)
    _IDENTIFY_RESULT_CACHE[key] = ent
    return ent


def _identify_result_put(key: str, entry: dict) -> None:
    """Store in memory only (LRU, cap 200, oldest evicted first).
    Never raises."""
    try:
        if not key or not isinstance(entry, dict):
            return
        _IDENTIFY_RESULT_CACHE.pop(key, None)
        _IDENTIFY_RESULT_CACHE[key] = dict(entry)
        while len(_IDENTIFY_RESULT_CACHE) > _IDENTIFY_RESULT_MAX:
            _IDENTIFY_RESULT_CACHE.pop(next(iter(_IDENTIFY_RESULT_CACHE)))
    except Exception:
        pass


def _identify_result_store(key: str, entry: dict,
                           req_id: str | None = None,
                           force: bool = False) -> None:
    """Store in memory + Turso. The Turso write is best-effort and
    never in a tap's waiting path (callers store after a send, or
    from the bg task). Never raises.

    A staged (never-sent, file_id-less) entry NEVER overwrites an
    entry that already holds a SENT file_id — unless force=True:
    the file_id survives restarts (Turso) and sends instantly,
    while a disk path dies with the instance — and the bg's
    disk-staged store used to clobber the tap's sent file_id
    (last-writer-wins), leaving the next tap with no durable entry
    (observed 2026-11-09: the 18:02 unconfirmed result did not
    serve the 18:12 tap). The check reads memory first, then the
    Turso row (the post-restart case, where memory is empty but the
    row still holds the sent file_id). An entry WITH a file_id
    always stores. force=True is only for a deliberate replacement
    of a disputed result (wrong-song re-check): the old row's
    file_id belongs to the name the user just rejected, and the
    handler already cleared it from memory."""
    if not force and not (entry.get("file_id") or ""):
        try:
            _old = _IDENTIFY_RESULT_CACHE.get(key)
            if _old and (_old.get("file_id") or ""):
                return
        except Exception:
            pass
        try:
            from .turso_cache import get_identify_result as _t_get
            _row = _t_get(key, req_id=req_id)
            if _row and (_row.get("file_id") or ""):
                return
        except Exception:
            pass
    try:
        _identify_result_put(key, entry)
    except Exception:
        pass
    try:
        from .turso_cache import store_identify_result as _t_store
        _t_store(key, entry, req_id=req_id)
    except Exception:
        pass


def _identify_result_get_turso(key: str,
                               req_id: str | None = None) -> dict | None:
    """One indexed Turso read (memory-miss path only). A row without
    a file_id is name-only: the tap still saves the whole lookup,
    so it comes back as kind "pending". Never raises."""
    if not key:
        return None
    try:
        from .turso_cache import get_identify_result as _t_get
        row = _t_get(key, req_id=req_id)
    except Exception:
        row = None
    if not row:
        return None
    ent = dict(row)
    if not ent.get("file_id"):
        ent["kind"] = "pending"
        ent["disk_path"] = ""
    return ent


def _identify_result_clear(key: str, turso: bool = True) -> None:
    """Clear a cached result (wrong-song dispute). Memory is cleared
    immediately; the Turso delete runs with turso=False deferred to
    after the answer (same pattern as the wrong-song report write).
    Never raises."""
    try:
        if key:
            _IDENTIFY_RESULT_CACHE.pop(key, None)
    except Exception:
        pass
    if turso and key:
        try:
            from .turso_cache import delete_identify_result as _t_del
            _t_del(key)
        except Exception:
            pass


def _identify_result_store_send(cache_key, title: str, artist: str,
                                source: str, kind: str, file_id: str,
                                duration_sec: float = 0,
                                status: str = "identified",
                                quality: str = "", extra: str = "",
                                cover_file_id: str = "",
                                req_id: str | None = None,
                                resolve_ms: int = 0) -> None:
    """Store the FINAL result right after a send (memory + Turso).
    No-op without a key. Fire-and-forget-safe; never raises.
    resolve_ms (0 when unknown) is the file-resolution time, carried
    so a later tap can log it as the search_file stage."""
    if not cache_key or not file_id:
        return
    try:
        _identify_result_store(
            cache_key,
            _identify_result_entry(status, title, artist, source, kind,
                                   file_id=file_id,
                                   duration_sec=duration_sec,
                                   quality=quality, extra=extra,
                                   cover_file_id=cover_file_id,
                                   resolve_ms=resolve_ms),
            req_id=req_id)
    except Exception:
        pass


def _invalidate_stale_identify_file(conn, cache_key: str, title: str,
                                    artist: str, file_id: str) -> None:
    """A stored file_id Telegram refused to send is poisoned for this
    (title, artist): drop the identify-cache entry (memory; the bg
    re-stores a fresh one) and invalidate the song-file cache rows
    that hold the SAME file_id — the same invalidation
    _deliver_fullsong_bg does on a cached-send failure — so the bg's
    re-resolution never re-serves the dead file_id. A row holding a
    different (good) file_id is left alone. Never raises."""
    try:
        if cache_key:
            _IDENTIFY_RESULT_CACHE.pop(cache_key, None)
    except Exception:
        pass
    if not file_id:
        return
    try:
        from .file_cache import get_cached_song as _lc_get
        from .file_cache import invalidate_file as _inv
        _lc = _lc_get(conn, artist, title)
        if _lc and (_lc.get("file_id") or "") == file_id:
            _inv(conn, artist, title)
    except Exception:
        pass
    try:
        from .turso_cache import get_song_file as _t_get
        from .turso_cache import delete_song_file_id as _t_del
        _tr = _t_get(artist, title)
        if _tr and (_tr.get("file_id") or "") == file_id:
            _t_del(artist, title)
    except Exception:
        pass


def _ident_cache_dir() -> str:
    """Staging dir for prepared audio files (never in RAM)."""
    import os as _os
    import tempfile as _tf
    d = _os.path.join(_tf.gettempdir(), "avano_ident_cache")
    try:
        _os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _ident_disk_path(key: str, ext: str = "mp3") -> str:
    """Staged-file path for a key (sanitized; ext from the file)."""
    import hashlib as _hashlib
    import os as _os
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", (key or "")[:64])
    if not safe:
        safe = _hashlib.sha256((key or "").encode()).hexdigest()[:16]
    return _os.path.join(_ident_cache_dir(), f"{safe}.{ext}")


def _ident_disk_write(key: str, data: bytes, ext: str = "mp3") -> str | None:
    """Write bytes to the staged file; returns the path or None.
    The caller's bytes are freed by the caller after this returns.
    Never raises."""
    try:
        if not data:
            return None
        p = _ident_disk_path(key, ext)
        with open(p, "wb") as f:
            f.write(data)
        _ident_disk_sweep()
        return p
    except Exception:
        return None


def _ident_disk_sweep() -> None:
    """Delete staged files older than the disk TTL; enforce the file
    and byte caps (oldest first). Never raises."""
    try:
        import os as _os
        d = _ident_cache_dir()
        now = _time.time()
        try:
            names = _os.listdir(d)
        except Exception:
            return
        files = []
        for _n in names:
            try:
                _p = _os.path.join(d, _n)
                _st = _os.stat(_p)
                files.append((_st.st_mtime, _st.st_size, _p))
            except Exception:
                continue
        files.sort()
        total = sum(_s for _m, _s, _p in files)
        for _mtime, _size, _p in files:
            if now - _mtime > _IDENTIFY_RESULT_DISK_TTL:
                try:
                    _os.unlink(_p)
                    total -= _size
                except Exception:
                    pass
        files = [(_m, _s, _p) for _m, _s, _p in files
                 if _os.path.exists(_p)]
        while (len(files) > _IDENTIFY_RESULT_DISK_MAX_FILES
               or total > _IDENTIFY_RESULT_DISK_MAX_BYTES) and files:
            _m, _s, _p = files.pop(0)
            try:
                _os.unlink(_p)
                total -= _s
            except Exception:
                pass
    except Exception:
        pass


def _bg_precompute_key(audio_url: str) -> str:
    import hashlib as _hashlib
    return _hashlib.sha256((audio_url or "").encode()).hexdigest()


def _bg_precompute_get(audio_url: str) -> dict | None:
    """Fresh pre-compute entry for an audio URL, or None (miss/expired)."""
    if not audio_url:
        return None
    key = _bg_precompute_key(audio_url)
    ent = _BG_PRECOMPUTE.get(key)
    if ent is None:
        return None
    if _time.time() - ent.get("created_at", 0) > _BG_PRECOMPUTE_TTL:
        _BG_PRECOMPUTE.pop(key, None)
        return None
    return ent


def _bg_precompute_put(audio_url: str, entry: dict) -> None:
    """Store a pre-compute entry (capped; oldest evicted first)."""
    if not audio_url:
        return
    while len(_BG_PRECOMPUTE) >= _BG_PRECOMPUTE_MAX:
        _BG_PRECOMPUTE.pop(next(iter(_BG_PRECOMPUTE)))
    entry["created_at"] = _time.time()
    _BG_PRECOMPUTE[_bg_precompute_key(audio_url)] = entry


async def _extract_12s_slice_locked(audio_url: str, tmp_path: str
                                    ) -> bytes | None:
    """Spawn ffmpeg (12 s slice) and wait <= 30 s. Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-y", "-i", audio_url,
            "-vn", "-t", "12", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-f", "wav", tmp_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
    except Exception:
        return None
    try:
        await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        return None
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        with open(tmp_path, "rb") as fh:
            data = fh.read()
    except Exception:
        return None
    if not data or len(data) < 1000:
        return None
    return data


async def _extract_12s_slice(audio_url: str) -> bytes | None:
    """ffmpeg: 12 s mono 16 kHz PCM WAV from an audio URL (or a
    progressive mp4 URL), through the EXISTING story-clips ffmpeg
    semaphore.
    Returns the WAV bytes, or None on any failure/timeout. Never raises.
    The semaphore is acquired BEFORE spawning ffmpeg so the gate is real.
    """
    import tempfile as _tf
    tmp = _tf.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        try:
            from .story_clips import _WM_SEM as _wm_sem
        except Exception:
            _wm_sem = None
        if _wm_sem is not None:
            async with _wm_sem:
                return await _extract_12s_slice_locked(audio_url, tmp_path)
        return await _extract_12s_slice_locked(audio_url, tmp_path)
    finally:
        try:
            import os as _os
            _os.unlink(tmp_path)
        except Exception:
            pass


async def _bg_precompute_link(audio_url: str, chat_id: int, conn,
                              req_id: str | None = None) -> None:
    """Background 12 s extract + bank match for one link. Fire-and-forget:
    never raises (except CancelledError, which is re-raised), never
    messages. A bank hit is also stored in the url identify cache so the
    tap's existing pre-check answers instantly."""
    _req = req_id or _timing.new_req_id()
    _t_req = _time.perf_counter()
    _timing.log_stage(log, "identify_stage", _req, _t_req, "receipt",
                      _t_req)
    try:
        if not audio_url or not _XBANK_OK:
            return
        try:
            _slice = await asyncio.wait_for(
                _extract_12s_slice(audio_url), timeout=60)
        except asyncio.TimeoutError:
            _slice = None
        except Exception:
            _slice = None
        if not _slice:
            _timing.log_stage(log, "identify_stage", _req, _t_req,
                              "bg_extract", _t_req, ok=False)
            return
        _t_fp = _time.perf_counter()
        try:
            from .exclusive_bank import fingerprint_bytes as _xb_fp
            import functools as _ft
            _dur, _items = await asyncio.get_running_loop().run_in_executor(
                None, _ft.partial(_xb_fp, _slice, ".wav", req_id=_req))
        except Exception:
            _items = None
        _timing.log_stage(log, "identify_stage", _req, _t_req,
                          "bg_fingerprint", _t_fp,
                          items=len(_items or []))
        if not _items:
            return
        _t_bank = _time.perf_counter()
        _song = None
        _sim = 0.0
        try:
            from .exclusive_bank import (
                match_sample as _xb_match, _get_conn as _xb_conn)
            _c = _xb_conn()
            if _c is not None:
                _song, _sim = _xb_match(_c, _items, req_id=_req)
        except Exception:
            _song, _sim = None, 0.0
        _timing.log_stage(log, "identify_stage", _req, _t_req, "bg_bank",
                          _t_bank, matched=_song is not None,
                          sim=round(_sim, 3))
        if _song is not None:
            try:
                if conn is not None:
                    dbmod.set_identify_cache(conn, f"url:{audio_url}",
                                             "identified", _song["title"],
                                             _song["artist"])
            except Exception:
                pass
            _bg_precompute_put(audio_url, {"status": "bank_hit",
                                           "title": _song["title"],
                                           "artist": _song["artist"],
                                           "slice": _slice})
        else:
            _bg_precompute_put(audio_url, {"status": "bank_miss",
                                           "slice": _slice})
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        try:
            _timing.log_total(log, "identify_stage", _req, _t_req,
                              "bg_done")
        except Exception:
            pass


def _spawn_bg_precompute(audio_url: str, chat_id: int, conn,
                         req_id: str | None = None) -> None:
    """Spawn (max 1 per chat) the background identify pre-compute for a
    link. A pending task for the same chat is cancelled first. The task
    removes itself from the registry when done. Never raises."""
    try:
        if not audio_url or not chat_id:
            return
        prev = _BG_PRECOMPUTE_TASKS.get(chat_id)
        if prev is not None and not prev.done():
            prev.cancel()
        task = asyncio.create_task(
            _bg_precompute_link(audio_url, chat_id, conn, req_id))
        _BG_PRECOMPUTE_TASKS[chat_id] = task

        def _done(_t, _cid=chat_id):
            if _BG_PRECOMPUTE_TASKS.get(_cid) is _t:
                _BG_PRECOMPUTE_TASKS.pop(_cid, None)

        task.add_done_callback(_done)
    except Exception:
        pass


def _signal_identify_bg_video_ready(key: str, video_path=None) -> None:
    """Set the video-ready event for a shortcode key (called by
    _send_link_video after the download, so the bg identify — spawned
    at resolve — can reuse the merged file on disk). video_path=None
    means the download failed: the bg aborts instead of waiting out
    its timeout. Never raises."""
    try:
        if not key:
            return
        _ev, _old = _IDENTIFY_BG_VIDEO_READY.get(key, (None, None))
        if _ev is None:
            _ev = asyncio.Event()
        _IDENTIFY_BG_VIDEO_READY[key] = (_ev, video_path)
        _ev.set()
    except Exception:
        pass


def _spawn_identify_bg(key: str, audio_url: str, chat_id: int, conn,
                       config, req_id: str | None = None,
                       tg_file_id: str | None = None, bot=None,
                       exclude=None):
    """Spawn the bg identify for one shortcode key (max 1 in flight
    per key; a repeat spawn reuses the in-flight task instead of
    starting a second lookup). Returns the task or None. Never raises.
    Input: the reel's audio URL (link flow) or a Telegram file_id +
    bot (direct file / on-demand tap spawn — the bg downloads the
    Telegram file once; the tap never downloads). ``exclude`` is the
    batch-F rejected-names list, passed to the identify call so a
    re-check never re-shows a disputed name."""
    try:
        if not key or (not audio_url and not tg_file_id) or not chat_id:
            return None
        prev = _IDENTIFY_BG_TASKS.get(key)
        if prev is not None and not prev.done():
            return prev
        if key not in _IDENTIFY_BG_VIDEO_READY:
            _IDENTIFY_BG_VIDEO_READY[key] = (asyncio.Event(), None)
        task = asyncio.create_task(
            _identify_bg_link(key, audio_url, chat_id, conn, config, req_id,
                              tg_file_id=tg_file_id, bot=bot,
                              exclude=exclude))
        _IDENTIFY_BG_TASKS[key] = task

        def _done(_t, _k=key):
            if _IDENTIFY_BG_TASKS.get(_k) is _t:
                _IDENTIFY_BG_TASKS.pop(_k, None)

        task.add_done_callback(_done)
        return task
    except Exception:
        return None


async def _await_identify_bg(key: str,
                             timeout: float = _IDENTIFY_BG_AWAIT_CEILING
                             ) -> bool:
    """Wait for the in-flight bg identify for a key, up to the ceiling
    (_IDENTIFY_BG_AWAIT_CEILING, 120 s — the tap awaits the SAME job
    to its natural completion, never a 5 s bound, never a second job).
    True when it finished (re-read the cache after); False on timeout
    or when no task exists. The timeout NEVER cancels the bg task — it
    keeps running and populates the cache for the next tap. Never
    raises."""
    try:
        task = _IDENTIFY_BG_TASKS.get(key)
        if task is None:
            return False
        if task.done():
            return True
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return True
    except (asyncio.TimeoutError, Exception):
        return False


async def _download_audio_to_disk(url: str, out_path: str, config,
                                  req_id: str | None = None) -> bool:
    """Stream an audio URL to disk (64 KB chunks, never held in RAM).
    Same P1-8 media-URL validation as _download_link_audio. A partial
    file is deleted. Returns True on success. Never raises."""
    try:
        if not await validate_media_url_for_fetch(url):
            return False
        import os as _os
        headers = {"User-Agent": "Mozilla/5.0",
                   "Referer": "https://www.instagram.com/"}
        max_mb = getattr(config, "max_file_mb", 50)
        async with httpx.AsyncClient(timeout=40) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                size = 0
                with open(out_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        size += len(chunk)
                        if size > max_mb * 1024 * 1024:
                            raise ValueError("audio too large")
                        f.write(chunk)
        return _os.path.getsize(out_path) > 0
    except Exception:
        try:
            import os as _os
            _os.unlink(out_path)
        except Exception:
            pass
        return False


async def _identify_bg_link(key: str, audio_url: str, chat_id: int, conn,
                            config, req_id: str | None = None,
                            tg_file_id: str | None = None, bot=None,
                            exclude=None) -> None:
    """Background full identify + file resolution for one link.

    Spawned at RESOLVE (not after the video is sent): the task waits
    for the video download to finish (event set by _send_link_video)
    so its ffmpeg work never competes with the download/mux, then
    identifies from the merged video file on disk — no second
    download — falling back to the audio stream when the file is
    already gone. A direct file (tg_file_id + bot, on-demand tap
    spawn) downloads the Telegram file ONCE here instead — the tap
    itself never downloads. When it finishes, the FINAL result (name
    + a sendable file) is written to the shortcode cache even if the
    user has not tapped yet; a tap then sends the stored file
    immediately. The bg never messages and never holds song/video
    bytes in RAM beyond the transient identify/transcode calls (freed
    right after); the prepared file lives on DISK until its first
    send. Errors are never cached. CancelledError is re-raised;
    anything else is swallowed after logging."""
    _req = req_id or _timing.new_req_id()
    _t_req = _time.perf_counter()
    _t_wall = _time.time()
    _timing.log_stage(log, "identify_stage", _req, _t_req, "receipt",
                      _t_req)
    _audio_path = None
    _clip_path = None
    try:
        if _identify_result_get(key) is not None:
            return  # already answered (repeat link, earlier tap)
        # Same check against Turso: after a restart memory is empty
        # but the row still holds the sent file_id — re-identifying
        # would waste a full pipeline AND overwrite the durable row
        # with a disk-staged entry (see _identify_result_store). The
        # bg is not the user's waiting path, so one indexed read
        # here is allowed (hot-path gate: no new Turso queries in
        # the WAITING path — this is the background task).
        try:
            from .turso_cache import get_identify_result as _t_get
            _t_row = _t_get(key, req_id=_req)
            if _t_row and (_t_row.get("file_id") or ""):
                return
        except Exception:
            pass
        import os as _os
        data = b""
        if tg_file_id and bot is not None:
            # Direct file (on-demand tap spawn): the bg downloads the
            # Telegram file ONCE — the tap never downloads. No merged
            # video exists for this key, so there is no video-wait.
            try:
                _tgf = await bot.get_file(tg_file_id)
                _buf = io.BytesIO()
                await bot.download_file(_tgf.file_path, _buf)
                data = _buf.getvalue()
            except Exception:
                data = b""
            if not data:
                return
        else:
            # Wait for the video download (bounded): the merged file is
            # the identify input — no second download of the media.
            _ev, _vpath = _IDENTIFY_BG_VIDEO_READY.get(key, (None, None))
            if _ev is None:
                return
            try:
                await asyncio.wait_for(_ev.wait(), timeout=90)
            except asyncio.TimeoutError:
                _timing.log_stage(log, "identify_stage", _req, _t_req,
                                  "bg_video_wait", _t_req, ok=False,
                                  reason="timeout")
                return
            _ev, _vpath = _IDENTIFY_BG_VIDEO_READY.get(key, (None, None))
            if _vpath and _os.path.exists(_vpath):
                # The merged file is on disk: read it transiently (the
                # identify pipeline's contract is bytes-in; freed right
                # after) and let the pipeline transcode it.
                try:
                    with open(_vpath, "rb") as _f:
                        data = _f.read()
                except Exception:
                    data = b""
            if not data:
                # Merged file gone (cleanup raced us) or unreadable: the
                # audio stream is the same content — stream it to disk.
                _audio_path = _ident_disk_path(key, "src")
                if not await _download_audio_to_disk(audio_url, _audio_path,
                                                     config, req_id=_req):
                    return
                try:
                    with open(_audio_path, "rb") as _f:
                        data = _f.read()
                except Exception:
                    data = b""
                if not data:
                    return
        _t_id = _time.perf_counter()
        try:
            result = await _identify_audio(
                data, "clip.mp4", config, conn, cache_url_key=audio_url,
                req_id=_req, t_req=_t_req, exclude=exclude)
        except Exception:
            result = None
        _timing.log_stage(log, "identify_stage", _req, _t_req,
                          "bg_identify", _t_id,
                          status=result.status if result is not None
                          else "error")
        if result is None or result.status == "error":
            return  # errors are never cached
        # File resolution starts here (both branches): the resolve_ms
        # recorded on the stored entry is the tap's search_file stage.
        _t_res = _time.perf_counter()
        # File resolution: a known file_id first (no download), then
        # the bank, then the adapter search staged to disk.
        _kind = "pending"
        _fid = ""
        _disk = None
        _dur = 0.0
        _quality = ""
        _extra = ""
        _cover = ""
        if result.status == "identified":
            try:
                from .file_cache import get_cached_song as _lc_get
                from ..search.fullsong_verify import file_identity_matches as _fim
                _lc = _lc_get(conn, result.artist, result.title)
                if _lc:
                    _vt, _va = _lc.get("verified_title"), _lc.get("verified_artist")
                    if _vt and _fim(result.title, result.artist, _vt, _va or ""):
                        _fid = _lc.get("file_id") or ""
                        _dur = _lc.get("duration_sec") or 0
            except Exception:
                pass
            if not _fid:
                try:
                    from .turso_cache import get_song_file as _t_get
                    _tr = _t_get(result.artist, result.title, req_id=_req)
                    if _tr:
                        from ..search.fullsong_verify import file_identity_matches as _fim2
                        _vt, _va = _tr.get("verified_title"), _tr.get("verified_artist")
                        if _vt and _fim2(result.title, result.artist, _vt, _va or ""):
                            _fid = _tr.get("file_id") or ""
                            _dur = _tr.get("duration_sec") or 0
                except Exception:
                    pass
            if not _fid:
                try:
                    _song = _exclusive_song_for(result.title, result.artist)
                    if _song is not None:
                        from .exclusive_bank import delivery_candidates as _dc
                        _cands = _dc(_song)
                        if _cands:
                            _fid = _cands[0]
                            _dur = _song.get("duration_sec") or 0
                            # Same quality/marker as _send_exclusive_song.
                            if _song.get("ready_file_id"):
                                _kbps = int(_song.get("ready_bitrate_kbps")
                                            or _song.get("bitrate_kbps") or 0)
                            else:
                                _kbps = int(_song.get("bitrate_kbps") or 0)
                            _quality = (f"کیفیت واقعی: {_kbps}kbps"
                                        if _kbps else "")
                            _extra = "💎 نسخه کامل اختصاصی آوانو"
                            _cover = _song.get("cover_file_id") or ""
                except Exception:
                    pass
            if _fid:
                _kind = "full"
            else:
                # Adapter search: the file bytes are written to DISK
                # immediately and freed — never retained in RAM.
                _full = None
                try:
                    from ..search.adapters import find_full_song_file_v2 as _find
                    _full = await _find(result.artist, result.title)
                except Exception:
                    _full = None
                _verified = False
                if _full and _full.get("data"):
                    try:
                        from ..search.fullsong_verify import verify_fullsong_file as _vf
                        _v = await asyncio.to_thread(
                            _vf, _full.get("data") or b"",
                            _full.get("ext") or "mp3",
                            result.title, result.artist)
                        _verified = _v.get("status") == "match"
                        if _verified:
                            _dur = _v.get("duration_sec") or 0
                    except Exception:
                        _verified = False
                if _verified and _full is not None:
                    _disk = _ident_disk_write(
                        key, _full.get("data") or b"",
                        _full.get("ext") or "mp3")
                    try:
                        _full["data"] = b""
                    except Exception:
                        pass
                    if _disk:
                        _kind = "full"
                if _kind == "pending":
                    # No verified full file: the clip's own audio,
                    # transcoded to a staged MP3 on disk.
                    _clip_path = await _bg_clip_to_disk(
                        key, data, result, _req)
                    if _clip_path:
                        _kind = "clip"
                        _disk = _clip_path
                        try:
                            _dur = _bg_clip_duration(_clip_path)
                        except Exception:
                            _dur = 0.0
            _timing.log_stage(log, "identify_stage", _req, _t_req,
                              "bg_resolve", _t_res, kind=_kind,
                              file_ready=bool(_fid or _disk),
                              resolve_ms=int((_time.perf_counter()
                                             - _t_res) * 1000))
        else:
            # Unconfirmed / not_found: the clip's own audio staged to
            # disk, so the tap sends it without re-downloading or
            # re-extracting. Short TTL (ordered 10 min).
            _clip_path = await _bg_clip_to_disk(key, data, result, _req)
            if _clip_path:
                _kind = "clip"
                _disk = _clip_path
                try:
                    _dur = _bg_clip_duration(_clip_path)
                except Exception:
                    _dur = 0.0
        try:
            del data
        except Exception:
            pass
        if _kind == "pending":
            return  # nothing sendable staged; the name is already in Stage 0
        _resolve_ms = int((_time.perf_counter() - _t_res) * 1000)
        _ent = _identify_result_entry(
            result.status, result.title, result.artist, result.source,
            kind=_kind, file_id=_fid, disk_path=_disk or "",
            duration_sec=_dur, quality=_quality, extra=_extra,
            cover_file_id=_cover, resolve_ms=_resolve_ms)
        _identify_result_store(key, _ent, req_id=_req)
        _timing.log_stage(log, "identify_stage", _req, _t_req,
                          "bg_cache_store", _t_req, kind=_kind,
                          via="shortcode")
    except asyncio.CancelledError:
        raise
    except Exception:
        try:
            _timing.log_stage(log, "identify_stage", _req, _t_req,
                              "bg_error", _t_req)
        except Exception:
            pass
    finally:
        # The downloaded/extracted source audio is not the staged
        # file: remove it. The staged clip/full file stays (TTL sweep).
        try:
            import os as _os
            if _audio_path and _os.path.exists(_audio_path):
                _os.unlink(_audio_path)
        except Exception:
            pass
        try:
            _IDENTIFY_BG_VIDEO_READY.pop(key, None)
        except Exception:
            pass
        try:
            _timing.log_total(log, "identify_stage", _req, _t_req,
                              "bg_done", headline_total="bg_identify_ms")
        except Exception:
            pass


def _bg_clip_duration(path: str) -> float:
    """Probed duration of a staged clip file (ffprobe, never raises)."""
    try:
        import subprocess as _sp
        import json as _js
        out = _sp.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                       "-show_format", path],
                      capture_output=True, timeout=30)
        info = _js.loads(out.stdout.decode("utf-8", "replace") or "{}")
        return float((info.get("format") or {}).get("duration") or 0)
    except Exception:
        return 0.0


async def _bg_clip_to_disk(key: str, data: bytes, result,
                           req_id: str | None = None) -> str | None:
    """Transcode the clip's own audio to a staged MP3 on disk (same
    target-bitrate rule as _extract_and_send_clip_audio). Returns the
    path or None. Never raises; the bytes are freed by the caller."""
    try:
        if not data:
            return None
        if await asyncio.to_thread(has_audio_stream, data) is False:
            return None
        src_br, _dur = await asyncio.to_thread(probe_audio, data, "mp4")
        target = 320 if (src_br or 0) >= 300 else max(96, src_br or 128)
        conv = await asyncio.to_thread(transcode_mp3, data, "mp4", target)
        mp3 = conv[0] if conv and conv[0] else None
        if not mp3:
            return None
        return _ident_disk_write(key, mp3, "mp3")
    except Exception:
        return None


async def _identify_link_audio(outcome, config: Config, conn=None,
                             resolved: tuple | None = None,
                             req_id: str | None = None, t_req=None):
    """Identify a sent link's audio.

    Returns (status, title, artist, needs_file, page_text, heard_bytes).
    heard_bytes are the downloaded media bytes when any were fetched
    (used to arm the bank sample); b"" otherwise. Callers must unpack
    tolerantly (older fakes return fewer values).

    - Direct file link: download the file (validated, size-capped) and sign it.
    - Platform link: try the public media URL advertised by its page, then
      AudD's social-video URL identification if a token is set.
    - Otherwise needs_file=True: the user should send the video itself.

    Instagram: the multi-strategy anonymous resolver (page -> embed ->
    captioned embed -> crawler DASH) often yields real media bytes without
    any login. When it yields nothing, the caller still runs the channel
    stage on page_text and says so honestly, instead of stopping silently.
    Downloaded bytes are used for fingerprinting; the watchable video
    file (when separately resolvable) is sent to the user by the caller.

    `resolved`: a (audio_url, video_url, page_text, strategy, media_type)
    tuple the caller already obtained — the resolver runs ONCE per link,
    not once per stage. `conn`: enables the identify cache (url-key
    pre-check skips the download entirely on repeat links).
    """
    import time as _time
    url = outcome.official_url
    # Timing instrumentation (logs only): one request id per link-identify.
    if req_id is None:
        req_id = _timing.new_req_id()
    if t_req is None:
        t_req = _time.perf_counter()
    # HARDCODED MAPPINGS for known underground tracks not in Shazam/AudD.
    # These are verified by the user. This is a temporary bridge until the
    # auto-indexer builds a proper fingerprint database.
    # Format: reel_shortcode -> (title, artist)
    _HARDCODED_REELS = {
        "DfFaBzxKNH0": ("BANG O HASHISH", "Big Shaggy"),
    }
    # Check if URL contains a known reel shortcode
    for _shortcode, (_t, _a) in _HARDCODED_REELS.items():
        if _shortcode in (url or ""):
            log_event(log, "identify", stage="hardcoded_hit",
                      shortcode=_shortcode, title=_t[:40])
            # Cache the hit so the tap path finds it: video_identify's
            # url-key pre-check consults url:{entry audio_url} (the
            # resolved media audio URL), and repeat link identifies
            # consult url:{reel URL}. No hash: key can be stored here —
            # the content hash is sha256 of audio bytes, and this branch
            # downloads nothing (heard_bytes is empty by design).
            if conn is not None:
                try:
                    dbmod.set_identify_cache(conn, f"url:{url}",
                                             "identified", _t, _a)
                    if resolved is not None and resolved[0]:
                        dbmod.set_identify_cache(conn, f"url:{resolved[0]}",
                                                 "identified", _t, _a)
                except Exception:
                    pass
            # Return as identified (status, title, artist, needs_file, page_text, heard_bytes)
            # heard_bytes empty - the caller will still download video for the user
            return ("identified", _t, _a, False, "", b"")
    if outcome.kind == "direct_file":
        try:
            _ck = dbmod.get_identify_cache(conn, f"url:{url}",
                                           dbmod.IDENTIFY_CACHE_URL_TTL) \
                if conn is not None else None
        except Exception:
            _ck = None
        if _ck is not None:
            log_event(log, "identify_stage", stage="cache_check", ms=0,
                      req_id=req_id,
                      total_ms=int((_time.perf_counter() - t_req) * 1000),
                      hit=True, via="url")
            # 8th element: the audio/file URL, so the caller can still
            # arm the bank sample from real bytes on a cache hit.
            return (_ck["status"], _ck["title"], _ck["artist"], False,
                    "", b"", "", url)
        t = _time.perf_counter()
        try:
            # P1-8: validate the user-supplied direct-file URL (full
            # rules + DNS) before fetching it.
            if not await validate_url_for_fetch(url):
                return ("error", "", "", False, "", b"")
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.content
        except Exception:
            return ("error", "", "", False, "", b"")
        _timing.log_stage(log, "identify_stage", req_id, t_req, "download",
                          t, bytes=len(data or b""))
        if len(data) > config.max_file_mb * 1024 * 1024:
            return ("error", "", "", False, "", b"")
        result = await _identify_audio(data, "clip.mp4", config, conn,
                                       cache_url_key=url,
                                       req_id=req_id, t_req=t_req)
        return (result.status, result.title, result.artist, False, "", data)
    media_url = None
    page_text = ""
    strategy = "none"
    if outcome.kind == "instagram":
        # Multi-strategy anonymous resolver (page -> embed ->
        # captioned embed -> crawler DASH audio). No login, no cookies.
        # Reuse the caller's resolve when provided (single resolve per
        # link): the send-video stage and this stage used to each run
        # all four page fetches for the same URL.
        if resolved is not None:
            media_url, _video_url, page_text, strategy = resolved[:4]
        else:
            _t_res = _time.perf_counter()
            try:
                from ..services.instagram import resolve_instagram_media
                media_url, _video_url, page_text, strategy = \
                    (await resolve_instagram_media(url))[:4]
            except Exception:
                media_url, _video_url, page_text, strategy = None, None, "", "none"
            _timing.log_stage(log, "identify_stage", req_id, t_req,
                              "resolve", _t_res, strategy=strategy)
    else:
        try:
            # P1-8: validate the user-supplied page URL (full rules +
            # DNS) before fetching, and re-validate the final URL
            # after redirects before using the body.
            if not await validate_url_for_fetch(url):
                raise ValueError("url validation failed")
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if not await validate_url_for_fetch(str(resp.url)):
                    raise ValueError("redirect target failed validation")
                page = resp.text[:2_000_000] if resp.status_code == 200 else ""
            media_url = extract_media_url(page)
            page_text = extract_page_text(page)
        except Exception:
            media_url = None
    # URL-key cache pre-check: the same audio URL seen before -> skip
    # the download AND the lookup (content at a signed media URL is
    # immutable for its TTL).
    if media_url and conn is not None:
        try:
            _ck = dbmod.get_identify_cache(conn, f"url:{media_url}",
                                           dbmod.IDENTIFY_CACHE_URL_TTL)
        except Exception:
            _ck = None
        if _ck is not None:
            log_event(log, "identify_stage", stage="cache_check", ms=0,
                      req_id=req_id,
                      total_ms=int((_time.perf_counter() - t_req) * 1000),
                      hit=True, via="url")
            # 8th element: the media URL, so the caller can still arm
            # the bank sample from real bytes on a cache hit.
            return (_ck["status"], _ck["title"], _ck["artist"], False,
                    page_text, b"", "", media_url)
    # Bytes were REALLY fetched: an unidentified or failed lookup is
    # never "the video was not handed over". Remember the real status +
    # bytes; AudD below still gets its fallback chance, but the final
    # fallthrough must not collapse this into needs_file.
    fetched: tuple[str, bytes] | None = None
    if media_url:
        data = b""
        t = _time.perf_counter()
        try:
            headers = {"User-Agent": "Mozilla/5.0",
                       "Referer": "https://www.instagram.com/"}
            # P1-8: the extracted media URL is a CDN URL — validated
            # as a media URL (DNS / private-IP check) before fetching.
            if not await validate_media_url_for_fetch(media_url):
                raise ValueError("url validation failed")
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(media_url, headers=headers)
                resp.raise_for_status()
                data = resp.content
        except Exception:
            data = b""
        _timing.log_stage(log, "identify_stage", req_id, t_req, "download",
                          t, bytes=len(data or b""), strategy=strategy)
        if data and len(data) <= config.max_file_mb * 1024 * 1024:
            result = await _identify_audio(data, "clip.mp4", config, conn,
                                           cache_url_key=media_url,
                                           req_id=req_id, t_req=t_req)
            if result.status == "identified":
                return ("identified", result.title, result.artist, False, page_text, data)
            if result.status == "unconfirmed":
                # Single-source name: no card. Carry the name + bytes so
                # the caller sends only the extracted audio (with the
                # Google button for this name). The AudD-URL fallback
                # below is the same engine on the same audio — it adds
                # nothing for confirmation.
                return ("unconfirmed", result.title, result.artist, False,
                        page_text, data)
            if result.status in ("not_found", "error"):
                fetched = (result.status, data)
    if config.audd_token:
        _t_audd = _time.perf_counter()
        try:
            result = await identify_url(url, config.audd_token)
        except Exception:
            result = None
        _timing.log_stage(log, "identify_stage", req_id, t_req, "api_audd",
                          _t_audd,
                          status=result.status if result is not None else "none")
        if result is not None and result.status == "identified":
            # Single-source AudD (URL): unconfirmed — no card, only the
            # extracted audio. The fetched bytes (when any) ride along
            # so the caller can extract from them.
            _heard = fetched[1] if fetched is not None else b""
            return ("unconfirmed", result.title, result.artist, False,
                    page_text, _heard)
    if fetched is not None:
        # The media was fetched but not identified (or the lookup
        # failed): carry the real status and the bytes so the bank
        # sample arms on real audio. A lookup error is never reported
        # as "not found", and neither is reported as "video not handed
        # over".
        return (fetched[0], "", "", False, page_text, fetched[1])
    # Nothing fetchable: classify the miss with the real 5-way taxonomy
    # (age-gated / private-deleted / empty shell / timeout / network) so
    # the card states the true cause, never a generic line.
    cause = ""
    if outcome.kind == "instagram":
        try:
            from ..services.instagram import check_instagram_gating
            gating = await check_instagram_gating(url)
        except Exception:
            gating = None
        cause = {"age_gated": "age_gated",
                 "private_or_deleted": "unavailable",
                 "timeout": "timeout",
                 "network_error": "network_error"}.get(gating or "", "empty_shell")
    return ("needs_file", "", "", True, page_text, b"", cause)


def _notfound_result_kb(bank_token: str | None, page_text: str = "",
                         lang: str = "fa"):
    """Post-delivery shortcuts for an unidentified/errored link result:
    «آهنگ‌های مشابه» (a real search on the video's caption words, only
    when the caption is non-trivial) + «هوش برتر». One compact row set,
    no clutter."""
    rows: list = []
    words = (page_text or "").split()
    if len(words) >= 3:
        rows.append([InlineKeyboardButton(
            text=t("similar_songs", lang),
            callback_data=_cb_query(" ".join(words[:3])))])
    bb = _bank_button(bank_token, lang)
    if bb:
        rows.extend(bb.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _answer_link_identify_result(message, conn, config, outcome,
                                       link_res, video_msg) -> None:
    """Answer the link-identify stages after ``_identify_link_audio``.

    The extract-fallback guarantee lives here: when media bytes were
    heard (a reachable video with an audio track) but the name was NOT
    confirmed (single source, disagreement, no match, lookup error),
    ONLY the video's own audio is sent — a real, validated MP3 with
    the new caption + the single retry button, never a «شناسایی شد» card
    and never a second honest text. The plain video-download path
    (``_send_link_video``) is untouched and stays independent of
    identification.
    """
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    import json as _json
    status, title, artist, needs_file = link_res[:4]
    page_text = link_res[4] if len(link_res) > 4 else ""
    heard = link_res[5] if len(link_res) > 5 else b""
    cause = link_res[6] if len(link_res) > 6 else ""
    # On an identify-cache hit there are no fresh bytes; the media
    # URL lets the bank sample still arm from the real audio.
    media_url_hint = link_res[7] if len(link_res) > 7 else ""
    media_hint = media_url_hint or outcome.official_url
    if status == "identified":
        dbmod.set_cached_search(
            conn, f"link:{outcome.official_url}",
            _json.dumps({"title": title, "artist": artist}, ensure_ascii=False))
        # UNIFY FIX (2026-11-09): no instant card — the temporary
        # «در حال جستجوی موسیقی...» status stays until the ONE final
        # message is sent; the background delivery owns and deletes
        # it. own_media comes from the delivered video when available,
        # so the wrong-song button is wired to it.
        status_msg = None
        try:
            status_msg = await message.answer(t("m_identify_searching", ulang))
        except Exception:
            status_msg = None
        _own = None
        if video_msg is not None:
            _vm = (getattr(video_msg, "video", None)
                   or getattr(video_msg, "audio", None))
            if _vm is not None and getattr(_vm, "file_id", None):
                _own = ("video" if getattr(video_msg, "video", None)
                        else "audio", _vm.file_id)
        # Arm the bank sample too: if the catalog card later proves
        # unusable, the second search can still run on this audio.
        # (Armed AFTER the call, inside _send_identified_song.)
        _spawned = await _send_identified_song(
            message, conn, title, artist, config,
            bot_id=dbmod.get_bot_id(conn, config),
            own_media=_own,
            arm=("link", outcome.official_url,
                 heard or None, media_hint, page_text),
            status_msg=status_msg)
        if not _spawned:
            await _delete_card(status_msg)
        # FINAL IDENTIFY UI (2026-11-09): the delivered video carries
        # the result keyboard (Google + YouTube + report); the final
        # audio carries only playlist + lyrics. Best-effort: a
        # missing video message simply skips the edit.
        if video_msg is not None and _own is not None:
            await _set_video_result_kb(
                video_msg, title, artist, ulang, _own[1],
                message.from_user.id, "link")
    elif needs_file:
        _tok = await _arm_bank_sample(conn, config, "link", outcome.official_url,
                                      heard or None, media_hint, page_text)
        if (video_msg is not None
                and (getattr(video_msg, "video", None)
                     or getattr(video_msg, "audio", None))):
            # The video WAS delivered; only its audio stream is
            # unreachable. Never overwrite its caption with the
            # "video not handed over" card — one honest line instead.
            await _answer(message, conn, t("link_audio_missing", ulang))
        else:
            # ONE compact card: fold the fallback into the already-sent
            # video message instead of stacking three messages. Media
            # messages take edit_caption; text messages take edit_text.
            card = _link_needs_file_card(outcome, cause, ulang)
            kb = _bank_button(_tok, ulang)
            folded = False
            if video_msg is not None:
                try:
                    await video_msg.edit_text(card, reply_markup=kb)
                    folded = True
                except Exception:
                    folded = False
            if not folded:
                await _answer(message, conn, card, reply_markup=kb)
        # Channel stage was previously never run on links: run it
        # now on the page's advertised text; it stays silent unless
        # it has a real post to show (no public nag).
        await _run_link_channel_stage(message, conn, config, page_text)
    elif status in ("unconfirmed", "not_found", "error"):
        # NOT confirmed (single source, disagreement, no match, or
        # lookup error): NO «شناسایی شد» card — ONLY the clip's own
        # audio with the new caption + the single retry button. The retry
        # reuses the video's existing identify token when one is
        # registered for the sent video. The channel stage still runs
        # on the page text (a different input, not the identification
        # answer). When nothing was heard / extraction fails, the
        # existing honest text is sent instead (edge case — the user
        # must get something), with the single retry button and
        # no bank button.
        _vid_fid = None
        try:
            _vid = getattr(video_msg, "video", None) \
                or getattr(video_msg, "audio", None)
            _vid_fid = getattr(_vid, "file_id", None) if _vid else None
        except Exception:
            _vid_fid = None
        sent = None
        if heard:
            sent = await _send_unconfirmed_audio(
                message, conn, heard, title, artist, ulang,
                file_id=_vid_fid, user_id=message.from_user.id, kind="link")
        if sent is None:
            _kb = _extracted_audio_kb(title, artist, ulang,
                                      file_id=_vid_fid,
                                      user_id=message.from_user.id,
                                      kind="link")
            _text_key = ("link_identify_error" if status == "error"
                         else "link_stage_not_found")
            await _answer(message, conn, t(_text_key, ulang),
                          reply_markup=_kb)
        # FINAL IDENTIFY UI (2026-11-09): the video keyboard follows
        # the name (or stays empty when nothing was produced).
        if video_msg is not None:
            await _set_video_result_kb(
                video_msg, title, artist, ulang, _vid_fid,
                message.from_user.id, "link")
        await _run_link_channel_stage(message, conn, config, page_text)


async def _run_link_channel_stage(message, conn, config, page_text: str) -> bool:
    """Channel stage for a link that fingerprinting could not identify.

    Previously this stage never ran on links at all (no query text),
    so a channel-only AI song had zero chance. Now: if the page
    advertised title/caption text, that text is the query. The stage
    stays silent unless it has a real post to show: no public nag
    about channel settings — the owner alone gets one pointer to the
    admin panel bank section.
    Returns True if a post was sent.
    """
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    if config is None:
        return False
    channels = dbmod.get_channels(conn, config)
    if not channels:
        if message.from_user.id == (config.owner_id or -1):
            await _answer(
                message, conn,
                t("m_bank_no_channel", ulang))
        return False
    if not (page_text or "").strip():
        return False
    try:
        posts = await search_channels(channels, page_text)
    except Exception:
        posts = []
    if not posts:
        return False
    post = posts[0]
    await _answer(message, conn,
                  f"{t('channel_post', ulang)}\n{post['post_url']}\n{post['text'][:300]}")
    return True


def _exclusive_song_for(title: str, artist: str):
    """Active exclusive-bank row for an identified title/artist, or None.

    Never raises; an unavailable bank simply means 'no exclusive song'.
    This is the delivery-side half of the bank priority rule: the
    fingerprint match inside _identify_audio identifies bank-only songs,
    and this lookup routes ANY confident identification of a bank song
    (Shazam, AudD, cached) to the exclusive file.
    """
    if not _XBANK_OK:
        return None
    try:
        from .exclusive_bank import _get_conn as _xb_conn, find_by_title_artist as _xb_find
        _c = _xb_conn()
        if _c is None:
            return None
        return _xb_find(_c, title, artist)
    except Exception:
        return None


async def _send_exclusive_song(message, song, status_msg=None, kb=None,
                               title: str | None = None,
                               artist: str | None = None,
                               ulang: str = "fa") -> str:
    """Send an exclusive-bank song's full file as the ONE final message.

    Sends the PREPARED version's stored Telegram file_id when one is
    stored, else the original file_id — no re-download, no re-conversion
    at delivery time, and no bitrate error can block the send. When the
    prepared version's send fails (e.g. a stale file_id), the stored
    ORIGINAL file is tried before giving up — the bank file must reach
    the user, not fall through to a public search. Returns the file_id
    that was actually sent (Step 1b: the caller stores it under the
    shortcode key), or "" when every stored version failed (caller
    falls back to the catalog card).

    UNIFY FIX (2026-11-09): no «شناسایی شد» card is ever sent before
    the file status is known — the audio IS the one final message, with
    the identified caption (real duration + the bank marker + measured
    bitrate) and the card's buttons, and the temporary status message
    is deleted right after the send. A failed send leaves the status
    in place (the caller then falls back to the catalog delivery,
    which owns the status from there).
    """
    from .exclusive_bank import (inc_download, delivery_candidates)
    if song.get("ready_file_id"):
        _kbps = int(song.get("ready_bitrate_kbps")
                    or song.get("bitrate_kbps") or 0)
    else:
        _kbps = int(song.get("bitrate_kbps") or 0)
    _quality = f"کیفیت واقعی: {_kbps}kbps" if _kbps else ""
    caption = _final_audio_caption(
        title if title is not None else song.get("title", ""),
        artist if artist is not None else song.get("artist", ""),
        ulang, duration_sec=song.get("duration_sec") or 0,
        quality=_quality, extra="💎 نسخه کامل اختصاصی آوانو")
    _cands = delivery_candidates(song)
    sent = None
    _sent_fid = ""
    _last_err = None
    for fid in _cands:
        try:
            if song.get("cover_file_id"):
                try:
                    sent = await message.answer_audio(
                        fid, caption=caption, reply_markup=kb,
                        thumbnail=song["cover_file_id"])
                except Exception:
                    sent = None
            if sent is None:
                sent = await message.answer_audio(fid, caption=caption,
                                                  reply_markup=kb)
            if sent is not None:
                _sent_fid = fid
                break
        except Exception as e:
            _last_err = e
            sent = None
    if sent is None:
        log_event(log, "exclusive_bank", stage="send_failed",
                  song_id=song.get("id"),
                  reason=type(_last_err).__name__ if _last_err else "no_file_id")
        return ""
    try:
        from .exclusive_bank import _get_conn as _xb_conn
        _c = _xb_conn()
        if _c is not None:
            inc_download(_c, song["id"])
    except Exception:
        pass
    if status_msg is not None:
        await _delete_card(status_msg)
    return _sent_fid


async def _send_identified_song(message, conn, title: str, artist: str, config,
                                bot_id: str = "",
                                own_media: tuple | None = None,
                                req_id: str | None = None,
                                arm: tuple | None = None,
                                source: str = "",
                                status_msg=None,
                                cache_key: str | None = None,
                                t_wall: float | None = None):
    """Deliver an identified song — WITHOUT an instant card (unify fix
    2026-11-09). The name is announced ONLY in the one final message,
    once the file status is known; the temporary status message (sent
    by the entry point, passed as status_msg) stays until that final
    send deletes it. `arm` is the _arm_bank_sample argument tuple
    (kind, seed, data, url, clue) — the sample is armed in the
    background, never before the answer. One routing point for all
    three identify entry points (video button, link, direct
    file/voice). Returns True when the background delivery was spawned
    (it owns the status message); False when nothing was spawned (the
    caller deletes the status). `source` is the identifying source
    ("bank" | "shazam+audd" | "cache" | "") — the wrong-song button
    records it so the re-check knows which source was used.
    `cache_key` (Step 1b) is the shortcode key the FINAL result is
    stored under after the send; `t_wall` is the tap's wall-clock
    receipt, for the click-to-name log."""
    return await _send_professional_song_card(
        message, conn, title, artist, bot_id=bot_id,
        own_media=own_media, config=config, arm=arm, source=source,
        status_msg=status_msg, cache_key=cache_key, req_id=req_id,
        t_wall=t_wall)


_FULLSONG_TASKS: dict[tuple[int, str, str], asyncio.Task] = {}
_AFTER_CARD_TASKS: dict[tuple[int, str, str], asyncio.Task] = {}


def _fullsong_key(chat_id, title: str, artist: str) -> tuple[int, str, str]:
    return (chat_id or 0, (title or "").strip().lower(),
            (artist or "").strip().lower())


def _spawn_fullsong_task(key, coro_factory) -> bool:
    """Spawn a full-song delivery task, at most one in flight per key.

    A repeat tap reuses the in-flight task — the file is never sent
    twice. The coroutine is built only when a new task really spawns.
    Returns True when a new task was spawned."""
    try:
        prev = _FULLSONG_TASKS.get(key)
        if prev is not None and not prev.done():
            return False
        task = asyncio.create_task(coro_factory())
        _FULLSONG_TASKS[key] = task

        def _done(_t, _k=key):
            if _FULLSONG_TASKS.get(_k) is _t:
                _FULLSONG_TASKS.pop(_k, None)

        task.add_done_callback(_done)
        return True
    except Exception:
        return False


def _spawn_fullsong_delivery(message, conn, title, artist, config, kb,
                             own_media, bank_token, query, ulang,
                             status_msg=None, cache_key: str | None = None,
                             req_id: str | None = None,
                             t_wall: float | None = None,
                             source: str = "") -> bool:
    """Spawn the background full-version delivery for an identified
    song (guarded: one in flight per (chat, title, artist))."""
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    return _spawn_fullsong_task(
        _fullsong_key(chat_id, title, artist),
        lambda: _deliver_fullsong_bg(message, conn, title, artist, config,
                                     kb, own_media, bank_token, query, ulang,
                                     status_msg=status_msg,
                                     cache_key=cache_key, req_id=req_id,
                                     t_wall=t_wall, source=source))


async def _after_card_delivery(message, conn, title, artist, config, kb,
                               own_media, bank_token, query, ulang, key, arm,
                               exclusive_check: bool = True, status_msg=None,
                               cache_key: str | None = None,
                               req_id: str | None = None,
                               t_wall: float | None = None,
                               source: str = ""):
    """After-card work for an identified song (identify-fix 2026-11-09,
    reworked by the unify fix: NO card is sent anymore).

    Runs in the background — nothing here can delay the answer. Order:
    1) arm the bank sample;
    2) exclusive-bank check: a bank song's stored file IS the full
       version — send it and stop (no catalog enrichment, no separate
       full-song lookup, so a bank song is never sent twice);
    3) otherwise spawn the full-version background delivery AND enrich
       the stored result with the catalog match (smart_search runs
       here — its iTunes/Deezer calls can no longer delay the name).
    The temporary status message stays until the one final message is
    sent: the exclusive send deletes it on success, the full-song
    delivery owns it when spawned, and this task deletes it when
    nothing was spawned (a duplicate delivery already in flight owns
    its own status). Fire-and-forget: never raises.
    """
    _owns_status = False
    try:
        if arm is not None:
            try:
                bank_token = await _arm_bank_sample(conn, config, *arm) or bank_token
            except Exception:
                pass
        if exclusive_check:
            song = _exclusive_song_for(title, artist)
            _bank_fid = ""
            if song is not None:
                _bank_fid = await _send_exclusive_song(
                    message, song, status_msg=status_msg, kb=kb,
                    title=title, artist=artist, ulang=ulang)
            # Step 1b: any truthy return means the bank file was sent
            # (live-base control flow, unchanged) — but only a real
            # file_id STRING is stored under the shortcode key; a
            # bool/other truthy return is never stored as the file_id.
            if _bank_fid:
                if isinstance(_bank_fid, str):
                    try:
                        if song.get("ready_file_id"):
                            _kbps = int(song.get("ready_bitrate_kbps")
                                        or song.get("bitrate_kbps") or 0)
                        else:
                            _kbps = int(song.get("bitrate_kbps") or 0)
                        _identify_result_store_send(
                            cache_key, title, artist, source="bank",
                            kind="full", file_id=_bank_fid,
                            duration_sec=song.get("duration_sec") or 0,
                            quality=(f"کیفیت واقعی: {_kbps}kbps"
                                     if _kbps else ""),
                            extra="💎 نسخه کامل اختصاصی آوانو",
                            cover_file_id=song.get("cover_file_id") or "",
                            req_id=req_id)
                    except Exception:
                        pass
                try:
                    if t_wall is not None:
                        log_event(log, "identify_stage", stage="name_sent",
                                  req_id=req_id,
                                  ms=int((_time.time() - t_wall) * 1000),
                                  cache_hit=False, kind="full")
                except Exception:
                    pass
                return
        if _spawn_fullsong_delivery(message, conn, title, artist, config, kb,
                                    own_media, bank_token, query, ulang,
                                    status_msg=status_msg,
                                    cache_key=cache_key, req_id=req_id,
                                    t_wall=t_wall, source=source):
            _owns_status = True
        # Catalog enrichment (moved verbatim from the card path): the
        # match's url/preview_url feed the heart/playlist buttons.
        try:
            engine = SearchEngine([ITunesProvider(entity="song"), DeezerProvider()])
            try:
                results, _ = await smart_search(engine, query, limit=10)
            except Exception:
                results = []
            dbmod.record_search(conn, message.from_user.id, query, ok=bool(results))
            match = match_identified_song(results, title, artist)
            if match:
                url = (getattr(match, "url", "") or "")
                preview = (getattr(match, "preview_url", "") or "")
                if key in profeat.RESULTS:
                    profeat.RESULTS[key]["url"] = url
                    profeat.RESULTS[key]["preview_url"] = preview
                try:
                    from .turso_cache import store_suggest_result
                    store_suggest_result(key, title, artist, url, preview)
                except Exception:
                    pass
        except Exception:
            pass
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        if not _owns_status:
            await _delete_card(status_msg)


def _spawn_after_card_delivery(message, conn, title, artist, config, kb,
                               own_media, bank_token, query, ulang, key, arm,
                               exclusive_check: bool = True,
                               status_msg=None, cache_key: str | None = None,
                               req_id: str | None = None,
                               t_wall: float | None = None,
                               source: str = "") -> bool:
    """Spawn the after-card background task (at most one in flight per
    (chat, title, artist)); a repeat tap reuses the in-flight task."""
    try:
        task_key = _fullsong_key(
            getattr(getattr(message, "chat", None), "id", None), title, artist)
        prev = _AFTER_CARD_TASKS.get(task_key)
        if prev is not None and not prev.done():
            return False
        task = asyncio.create_task(_after_card_delivery(
            message, conn, title, artist, config, kb,
            own_media, bank_token, query, ulang, key, arm, exclusive_check,
            status_msg=status_msg, cache_key=cache_key, req_id=req_id,
            t_wall=t_wall, source=source))
        _AFTER_CARD_TASKS[task_key] = task

        def _done(_t, _k=task_key):
            if _AFTER_CARD_TASKS.get(_k) is _t:
                _AFTER_CARD_TASKS.pop(_k, None)

        task.add_done_callback(_done)
        return True
    except Exception:
        return False



async def _send_professional_song_card(message, conn, title: str, artist: str, bot_id: str = "",
                                       bank_token: str | None = None,
                                       own_media: tuple[str, str] | None = None,
                                       config=None, arm: tuple | None = None,
                                       exclusive_check: bool = True,
                                       source: str = "",
                                       status_msg=None,
                                       cache_key: str | None = None,
                                       req_id: str | None = None,
                                       t_wall: float | None = None):
    """Deliver an identified song — WITHOUT an instant card (unify fix
    2026-11-09). No «شناسایی شد» card is sent before the file status
    is known: the name is announced ONLY in the one final message,
    once the file (or the clip-audio fallback) is ready. The
    temporary status message (sent by the entry point) stays until
    that final send deletes it. The keyboard is the identified-song
    set (playlist + lyrics, one row — the favorites feature itself is
    untouched; the wrong-song report lives under the video only).
    Everything else runs in
    the background after this call returns
    (_after_card_delivery): the sample arming, the exclusive-bank
    file (which IS the full version for a bank song), the catalog
    enrichment of the stored result, and the full-version background
    delivery. The full-song delivery itself is unchanged (same
    _deliver_fullsong_bg, same guard, same tag-match bug fix) — it
    just starts without a card now.

    A short catalog preview is never presented as the song. Exact
    catalog match (version-aware) -> the stored result keeps the pro
    buttons working (lyrics / add-to-playlist). Never a nearest song.
    exclusive_check=False keeps the suggestion flow's delivery routing
    exactly as before (no bank-file send there).

    Returns True when the background delivery was spawned (it owns
    the status message); False when nothing was spawned (the caller
    deletes the status).
    """
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    query = f"{title} {artist}".strip()
    # The keyboard needs no catalog match — lyrics/playlist only need
    # the result key; the match enriches the stored result after the
    # call (preview/catalog URLs for the buttons).
    kb, key = _identified_song_kb(message, conn, title, artist, own_media,
                                  source, ulang)
    return _spawn_after_card_delivery(message, conn, title, artist, config, kb,
                                      own_media, bank_token, query, ulang, key, arm,
                                      exclusive_check, status_msg=status_msg,
                                      cache_key=cache_key, req_id=req_id,
                                      t_wall=t_wall, source=source)


def _fullsong_duration_label(dur, ulang):
    """Real-duration label for a full-song caption.

    " — مدت: m:ss" when the duration is known, else "" — the static
    "full: over 90 seconds" label is never shown, because it was wrong
    for files whose real length was unknown (the 23:54 file was 3:29
    but the label only claimed "over 90 seconds").
    """
    try:
        dur = float(dur or 0)
    except Exception:
        dur = 0
    if dur <= 0:
        return ""
    return t("h_song_dur", ulang).format(dur=f"{int(dur // 60)}:{int(dur % 60):02d}")


async def _extract_and_send_clip_audio_copy(message, conn, data: bytes,
                                            caption: str, ulang,
                                            track_user_id=None,
                                            reply_markup=None,
                                            caption_key: str = ""):
    """Send the clip's ORIGINAL audio: stream copy, no re-encode.

    Used when a full-song candidate failed the identity check — the
    user gets the clip's own audio instead of a wrong song. Returns
    the sent message or None. Never raises. track_user_id overrides
    the message sender for the clear-chat record (the download-full
    flow calls this on the bot's own card message, whose from_user
    is the bot, not the user). The extracted file is Avano's own
    extraction: performer is "Avano" (never "Unknown artist") and the
    duration is the probed real one (ffprobe, never guessed). A
    successful send writes one `sent_extracted_audio` log line (bytes +
    caption key + attached button labels).
    """
    if not data:
        return None
    import os
    import shutil
    import subprocess
    import tempfile
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="avano-clipcopy-")
        vpath = os.path.join(tmpdir, "clip.mp4")
        with open(vpath, "wb") as f:
            f.write(data)
        # ffprobe reads the audio codec AND the real format duration in
        # one call: the extracted file carries a real duration (never
        # 0:00) and performer "Avano" (never "Unknown artist").
        probe = await asyncio.to_thread(
            subprocess.run,
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name:format=duration",
             "-of", "json", vpath],
            capture_output=True, text=True, timeout=30)
        _probe_json = {}
        try:
            import json as _json
            _probe_json = _json.loads(probe.stdout or "{}")
        except Exception:
            _probe_json = {}
        _streams = _probe_json.get("streams") or []
        codec = str((_streams[0].get("codec_name") if _streams else "") or "")
        try:
            _dur = float((_probe_json.get("format") or {}).get("duration") or 0)
        except Exception:
            _dur = 0.0
        ext = {"aac": "m4a", "mp3": "mp3", "opus": "opus",
               "vorbis": "ogg", "flac": "flac"}.get(codec, "m4a")
        apath = os.path.join(tmpdir, f"clip.{ext}")
        res = await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-y", "-v", "error", "-i", vpath, "-vn",
             "-c:a", "copy", apath],
            capture_output=True, text=True, timeout=120)
        if res.returncode != 0 or not os.path.exists(apath) or os.path.getsize(apath) == 0:
            return None
        with open(apath, "rb") as f:
            adata = f.read()
        sent = await message.answer_audio(
            BufferedInputFile(adata, filename=f"clip.{ext}"),
            caption=caption,
            title=t("m_extracted_audio_title", ulang),
            performer="Avano",
            duration=int(_dur) if _dur > 0 else None,
            reply_markup=reply_markup)
        if sent is not None:
            try:
                if track_user_id is not None:
                    adminfeat.track(conn, track_user_id, message.chat.id,
                                    sent.message_id)
                else:
                    _track(conn, message, sent)
            except Exception:
                pass
            try:
                log_event(log, "extract", stage="sent_extracted_audio",
                          bytes=len(adata), caption_key=caption_key or "",
                          buttons=_keyboard_button_texts(reply_markup))
            except Exception:
                pass
        return sent
    except Exception:
        return None
    finally:
        if tmpdir:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass


async def _deliver_fullsong_bg(message, conn, title, artist, config, kb,
                               own_media, bank_token, query, ulang,
                               status_msg=None, cache_key: str | None = None,
                               req_id: str | None = None,
                               t_wall: float | None = None,
                               source: str = ""):
    """Background full-version delivery for an identified song.

    Runs the EXISTING lookup chain (cached file_id fast path first,
    then find_full_song_file_v2, then the bank) and sends the audio
    with the existing caption/keyboard, the user's own media as a
    labelled replacement, or the honest no-full message. Fire-and-
    forget: never raises.

    UNIFY FIX (2026-11-09): no «شناسایی شد» card is ever sent before
    the file status is known — the audio IS the one final message
    (identified caption + real duration + the same buttons), and the
    temporary status message is deleted right after it is sent. When
    no verified full file exists, the ONE final message is the clip's
    own audio with the corporate extracted-audio caption and ONLY the
    single retry button — never the case-(b) caption, never the
    identified buttons (the keyboard follows the file's origin,
    2026-11-09). The old «⏳ در حال ارسال نسخه کامل
    آهنگ...» wait message is gone: the entry point's temporary status
    is the only waiting message in the identify path.
    """
    # Step 1b: the time import lives at the top of this function, so
    # the _store_and_log helper (defined below) and every use below it
    # see a bound _time — a use before the inner import is the
    # cf1360cd UnboundLocalError class (ruff F823).
    import time as _time

    async def _drop_status():
        """Delete the temporary status after the final message is out.
        Best-effort; a failed delete never blocks the answer."""
        if status_msg is not None:
            await _delete_card(status_msg)

    def _cap(duration_sec):
        """Caption for a successful full-file send: always the
        identified caption (name + artist + real duration) — the audio
        message IS the one final message."""
        return _final_audio_caption(title, artist, ulang,
                                    duration_sec or 0)

    def _store_and_log(kind: str, sent=None, dur: float = 0,
                       source_override: str | None = None) -> None:
        """Step 1b: after a final send, store the result under the
        shortcode key (memory + Turso) and log the click-to-name time
        for this tap (stage name_sent, cache_hit=False). The file_id
        and duration come from the sent message when not given.
        Best-effort; never raises; no-op without a key (store) or a
        t_wall (log). A bank file is stored with source "bank" so the
        fast path re-sends it exactly like the bank path."""
        try:
            if sent is not None:
                fid = ""
                _a = getattr(sent, "audio", None)
                if _a is not None:
                    fid = getattr(_a, "file_id", "") or ""
                    if not dur:
                        dur = getattr(_a, "duration", 0) or 0
                if fid:
                    _identify_result_store_send(
                        cache_key, title, artist,
                        source_override if source_override is not None
                        else (source or ""), kind, fid,
                        duration_sec=dur, req_id=req_id)
        except Exception:
            pass
        try:
            if t_wall is not None:
                log_event(log, "identify_stage", stage="name_sent",
                          req_id=req_id,
                          ms=int((_time.time() - t_wall) * 1000),
                          cache_hit=False, kind=kind)
        except Exception:
            pass

    try:
        # 1) FULL VERSION FIRST via parallel adapters (v2 architecture).
        # (Argument order is (artist, title); a swapped call silently searched
        # the wrong query and never found anything.)
        #
        # Requirement #3: Check Telegram file_id cache first. If this song was
        # previously sent by the bot, reuse the file_id instead of re-downloading.
        # This is instant and saves bandwidth.
        _t_start = _time.time()
        _cache_hit = False
        # IDENTITY GUARD (2026-11-09 mismatch fix): a cached file_id is
        # only trusted when the file's OWN verified title/artist (ID3
        # tags, probed at send time) match the identify result. A legacy
        # row without verified identity is never sent — the 23:54 wrong
        # file is already cached under the right key.
        try:
            from .file_cache import get_cached_song, store_file_id, invalidate_file
            from ..search.fullsong_verify import file_identity_matches
            _cached = get_cached_song(conn, artist, title)
            if _cached:
                _vt, _va = _cached.get("verified_title"), _cached.get("verified_artist")
                if _vt and file_identity_matches(title, artist, _vt, _va or ""):
                    _cache_hit = True
                    try:
                        sent = await message.answer_audio(
                            _cached["file_id"],
                            caption=_cap(_cached.get("duration_sec") or 0),
                            title=title, performer=artist,
                            reply_markup=kb)
                        if sent is not None:
                            _track(conn, message, sent)
                            await _drop_status()
                            _t_ms = int((_time.time() - _t_start) * 1000)
                            log_event(log, "fullsong", stage="sent_cached",
                                      title=title[:40], ms=_t_ms)
                            _store_and_log("full", sent,
                                           _cached.get("duration_sec") or 0)
                            # Speed measurement (Requirement #7)
                            try:
                                conn.execute(
                                    "INSERT INTO speed_metrics (operation, cache_hit, duration_ms, created_at) VALUES (?, 1, ?, ?)",
                                    ("send", _t_ms, int(_time.time())))
                                conn.commit()
                            except:
                                pass
                            return
                    except Exception:
                        # Cached file_id invalid, fall through to normal flow
                        # and invalidate the cache entry
                        try:
                            invalidate_file(conn, artist, title)
                        except:
                            pass
                elif _vt:
                    # Cached file is a DIFFERENT song: poisoned entry.
                    log_event(log, "fullsong", stage="fullsong_cache_mismatch",
                              title=title[:40], file_title=str(_vt)[:40],
                              file_artist=str(_va or "")[:40])
                    try:
                        invalidate_file(conn, artist, title)
                    except:
                        pass
                else:
                    # Legacy row: no verified identity stored — never trust it.
                    log_event(log, "fullsong", stage="fullsong_cache_unverified",
                              title=title[:40])
        except Exception:
            pass
        # 1a) PERSISTENT Turso song cache: survives Render restarts (the
        # local bot.db cache above is wiped on every restart). Same
        # file_id fast path, then refresh the local cache.
        # IDENTITY GUARD (2026-11-09 mismatch fix): same verified
        # re-check as the local cache — a Turso row is only sent when
        # its stored verified identity matches; a mismatch deletes the
        # poisoned row, a legacy row without verified identity is never
        # sent (falls through to a fresh, verified lookup).
        try:
            from .turso_cache import get_song_file as _turso_song_get
            from .turso_cache import delete_song_file_id as _turso_song_del
            from .file_cache import store_file_id as _store_local
            from .file_cache import invalidate_file as _inv_local
            from ..search.fullsong_verify import file_identity_matches as _fim
            _turso_row = _turso_song_get(artist, title)
            if _turso_row:
                _vt, _va = _turso_row.get("verified_title"), _turso_row.get("verified_artist")
                if _vt and _fim(title, artist, _vt, _va or ""):
                    try:
                        sent = await message.answer_audio(
                            _turso_row["file_id"],
                            caption=_cap(_turso_row.get("duration_sec") or 0),
                            title=title, performer=artist,
                            reply_markup=kb)
                        if sent is not None:
                            _track(conn, message, sent)
                            await _drop_status()
                            _t_ms = int((_time.time() - _t_start) * 1000)
                            log_event(log, "fullsong", stage="sent_cached",
                                      title=title[:40], ms=_t_ms, turso=True)
                            _store_and_log("full", sent,
                                           _turso_row.get("duration_sec") or 0)
                            try:
                                _store_local(conn, artist, title, _turso_row["file_id"],
                                             "audio", 0,
                                             _turso_row.get("duration_sec") or 0, "",
                                             verified_title=_vt, verified_artist=_va)
                            except Exception:
                                pass
                            try:
                                conn.execute(
                                    "INSERT INTO speed_metrics (operation, cache_hit, duration_ms, created_at) VALUES (?, 1, ?, ?)",
                                    ("send", _t_ms, int(_time.time())))
                                conn.commit()
                            except:
                                pass
                            return
                    except Exception:
                        pass
                elif _vt:
                    log_event(log, "fullsong", stage="fullsong_cache_mismatch",
                              title=title[:40], file_title=str(_vt)[:40],
                              file_artist=str(_va or "")[:40], turso=True)
                    try:
                        _turso_song_del(artist, title)
                    except Exception:
                        pass
                    try:
                        _inv_local(conn, artist, title)
                    except Exception:
                        pass
                else:
                    log_event(log, "fullsong", stage="fullsong_cache_unverified",
                              title=title[:40], turso=True)
        except Exception:
            pass
        from ..search.adapters import find_full_song_file_v2
        full = None
        _verify_failed = False
        _verified = None
        try:
            full = await find_full_song_file_v2(artist, title)
        except Exception:
            full = None
        if full:
            # IDENTITY GUARD (2026-11-09 mismatch fix): the file's OWN
            # tags (ffprobe on the downloaded bytes) must match the
            # identify result. A page label is NOT proof — the 23:54
            # site labelled the file "ATM" while the audio inside was
            # another song. On mismatch/unverifiable the file is never
            # sent; both caches are invalidated for this key and the
            # corporate clip-audio fallback runs below.
            try:
                from ..search.fullsong_verify import verify_fullsong_file
                _v = await asyncio.to_thread(
                    verify_fullsong_file, full.get("data") or b"",
                    full.get("ext") or "mp3", title, artist)
            except Exception:
                _v = {"status": "unverifiable", "file_title": "",
                      "file_artist": "", "duration_sec": 0,
                      "detail": "verify_error"}
            if _v["status"] == "match":
                if _v["duration_sec"] > 0:
                    full["duration_sec"] = _v["duration_sec"]
                _verified = (_v["file_title"], _v["file_artist"])
            else:
                log_event(log, "fullsong",
                          stage="fullsong_mismatch" if _v["status"] == "mismatch"
                          else "fullsong_unverifiable",
                          title=title[:40], source=full.get("source", "")[:40],
                          file_title=_v["file_title"][:40],
                          file_artist=_v["file_artist"][:40])
                try:
                    from .file_cache import invalidate_file as _inv
                    _inv(conn, artist, title)
                except Exception:
                    pass
                try:
                    from .turso_cache import delete_song_file_id as _turso_del
                    _turso_del(artist, title)
                except Exception:
                    pass
                full = None
                _verify_failed = True
        if full:
            try:
                # Direct-URL send: when the adapter knows a public URL and
                # the size is under Telegram's 20 MB URL limit, Telegram
                # fetches the file itself — Render never uploads the bytes.
                # Any failure falls back to the bytes path below.
                _full_url = full.get("url")
                _full_size = full.get("size")
                if _full_url and isinstance(_full_size, int) and _full_size < 20 * 1024 * 1024:
                    _url_t0 = _time.perf_counter()
                    try:
                        sent = await asyncio.wait_for(
                            message.answer_audio(
                                _full_url,
                                caption=_cap(full["duration_sec"]),
                                title=title, performer=artist,
                                duration=int(full["duration_sec"]),
                                reply_markup=kb),
                            timeout=8.0)
                        if sent is not None:
                            _track(conn, message, sent)
                            await _drop_status()
                            log_event(log, "fullsong", stage="sent_url",
                                      title=title[:40],
                                      source=full.get("source", "")[:40],
                                      bytes=_full_size,
                                      ms=int((_time.perf_counter() - _url_t0) * 1000))
                            try:
                                _fid = sent.audio.file_id if sent.audio else None
                                if _fid:
                                    _vt, _va = _verified if _verified else (None, None)
                                    store_file_id(conn, artist, title, _fid, "audio",
                                                  _full_size, full["duration_sec"],
                                                  full.get("source", ""),
                                                  verified_title=_vt,
                                                  verified_artist=_va)
                                    try:
                                        from .turso_cache import store_song_file_id as _turso_song_store
                                        _turso_song_store(artist, title, _fid, "audio",
                                                          verified_title=_vt,
                                                          verified_artist=_va,
                                                          duration_sec=full["duration_sec"])
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            _store_and_log("full", sent, full["duration_sec"])
                            return
                    except asyncio.TimeoutError:
                        log_event(log, "fullsong", stage="sent_url_failed",
                                  title=title[:40],
                                  source=full.get("source", "")[:40],
                                  bytes=_full_size,
                                  reason="timeout",
                                  ms=int((_time.perf_counter() - _url_t0) * 1000))
                    except Exception:
                        log_event(log, "fullsong", stage="sent_url_failed",
                                  title=title[:40],
                                  source=full.get("source", "")[:40],
                                  bytes=_full_size,
                                  ms=int((_time.perf_counter() - _url_t0) * 1000))
                sent = await message.answer_audio(
                    BufferedInputFile(full["data"], filename=f"{title}.{full['ext']}"),
                    caption=_cap(full["duration_sec"]),
                    title=title, performer=artist,
                    duration=int(full["duration_sec"]),
                    reply_markup=kb)
                if sent is not None:
                    _track(conn, message, sent)
                    await _drop_status()
                    log_event(log, "fullsong", stage="sent",
                              title=title[:40], bytes=len(full["data"]))
                    # Requirement #3: Store the Telegram file_id for future reuse.
                    # This avoids re-downloading and re-uploading the same file.
                    # The file's VERIFIED identity is stored with it, so a
                    # later cache hit can be re-checked before send.
                    try:
                        _fid = None
                        if sent.audio:
                            _fid = sent.audio.file_id
                        if _fid:
                            _vt, _va = _verified if _verified else (None, None)
                            store_file_id(conn, artist, title, _fid, "audio",
                                          len(full["data"]), full["duration_sec"],
                                          full.get("source", ""),
                                          verified_title=_vt,
                                          verified_artist=_va)
                            try:
                                from .turso_cache import store_song_file_id as _turso_song_store
                                _turso_song_store(artist, title, _fid, "audio",
                                                  verified_title=_vt,
                                                  verified_artist=_va,
                                                  duration_sec=full["duration_sec"])
                            except Exception:
                                pass
                        # Speed measurement (Requirement #7): cache miss
                        _t_ms = int((_time.time() - _t_start) * 1000)
                        try:
                            conn.execute(
                                "INSERT INTO speed_metrics (operation, cache_hit, duration_ms, created_at) VALUES (?, 0, ?, ?)",
                                ("send", _t_ms, int(_time.time())))
                            conn.commit()
                        except:
                            pass
                    except:
                        pass
                    _store_and_log("full", sent, full["duration_sec"])
                    return
            except Exception as exc:
                # NEVER silent: log the real error so delivery failures are visible
                log_event(log, "fullsong", stage="send_failed",
                          title=title[:40],
                          error=f"{type(exc).__name__}: {str(exc)[:200]}")
        # 1b) BANK: indexed channel audio (forwarded files) by title/artist.
        # The bank is a real source in the chain, not a separate button.
        if not full:
            try:
                from ..features.bank import search_bank_by_text
                hit = search_bank_by_text(conn, title, artist)
                if hit and hit.get("file_id"):
                    log_event(log, "bank_text", stage="found",
                              title=title[:40], channel=hit.get("channel", "")[:20])
                    sent = await message.answer_audio(
                        hit["file_id"],
                        caption=_cap(0),
                        reply_markup=kb)
                    if sent is not None:
                        _track(conn, message, sent)
                        await _drop_status()
                        _store_and_log("full", sent, 0,
                                       source_override="bank")
                        return
                elif hit:
                    log_event(log, "bank_text", stage="no_file_id",
                              title=title[:40])
            except Exception:
                pass
        if full:
            dur = full["duration_sec"]
            label = _fullsong_duration_label(dur, ulang)
            # Real MP3 delivery via the unified pipeline: genuine conversion
            # when needed, honest probed bitrate, proper AUDIO message.
            # Telegram's Bot API upload limit is 50 MB.
            full_data = full["data"] or b""
            sent = None
            if full_data and len(full_data) <= 50 * 1024 * 1024:
                sent = await send_music(
                    message, full_data, title, artist,
                    caption=_cap(dur),
                    reply_markup=kb, duration=int(dur) if dur else None)
            if sent is not None:
                _track(conn, message, sent)
                await _drop_status()
                _store_and_log("full", sent, dur)
                return
        # EXTRACTED-AUDIO KEYBOARD + CAPTION (2026-11-09): the keyboard
        # is chosen by the FILE'S ORIGIN, not by whether a name is
        # known — any clip/extracted audio carries ONLY the single
        # retry button (or no keyboard when no retry callback can be
        # built), never the identified-song keyboard; the caption is
        # the corporate extracted-audio caption, never the case-(b)
        # caption. Full-song sends above keep `kb` unchanged.
        _clip_kb = None
        if own_media is not None:
            _ck, _cf = own_media
            _clip_kb = _extracted_audio_kb(
                title, artist, ulang, file_id=_cf,
                user_id=getattr(getattr(message, "from_user", None),
                                "id", None),
                kind="audio" if _ck in ("audio", "voice") else "file")
        _clip_cap = t("m_extracted_audio_new", ulang)
        # 1c) IDENTITY CHECK FAILED: the fresh lookup's file was not the
        # identified song (or could not be verified). The wrong file is
        # never sent. Case (b): the ONE final message is the clip's
        # ORIGINAL audio (stream copy, no re-encode) with the corporate
        # extracted-audio caption and ONLY the single retry button.
        # Only when the clip itself is unavailable does the honest
        # no-full message below run.
        if _verify_failed:
            _sent_copy = None
            if own_media is not None:
                kind, file_id = own_media
                if kind == "video":
                    try:
                        tg_file = await message.bot.get_file(file_id)
                        buf = io.BytesIO()
                        await message.bot.download_file(tg_file.file_path, buf)
                        _sent_copy = await _extract_and_send_clip_audio_copy(
                            message, conn, buf.getvalue(),
                            _clip_cap,
                            ulang,
                            reply_markup=_clip_kb,
                            caption_key="m_extracted_audio_new")
                    except Exception:
                        _sent_copy = None
            if _sent_copy is not None:
                await _drop_status()
                _store_and_log("clip", _sent_copy)
                return
        # 2) No full file: case (b) — the ONE final message is the user's
        # own media as the clip's audio, with the corporate
        # extracted-audio caption and ONLY the single retry button.
        # Video: the clip's own extracted audio. Other kinds go back
        # as-is (nothing was extracted from them). Only when extraction
        # is impossible does the video go back with a caption that says
        # exactly that.
        if own_media is not None and not _verify_failed:
            kind, file_id = own_media
            cap = _clip_cap
            try:
                if kind == "video":
                    # The caption promises an audio file, so one must really
                    # be sent: extract the video's own audio to a real,
                    # validated MP3. Only when extraction is impossible does
                    # the video go back — with a caption that says exactly
                    # that (the old code re-sent the video while claiming an
                    # audio file had been sent).
                    data = None
                    try:
                        tg_file = await message.bot.get_file(file_id)
                        buf = io.BytesIO()
                        await message.bot.download_file(tg_file.file_path, buf)
                        data = buf.getvalue()
                    except Exception:
                        data = None
                    sent = await _extract_and_send_clip_audio(
                        message, conn, data or b"", caption=cap,
                        reply_markup=_clip_kb,
                        caption_key="m_extracted_audio_new")
                    if sent is None:
                        sent = await send_video(
                            message, file_id,
                            caption=t("m_identified_no_audio_resend", ulang))
                elif kind == "voice":
                    # §6 (2026-11-09): a voice message re-sent as voice
                    # renders as a small voice bubble, not an audio
                    # card — the same file goes out as a proper AUDIO
                    # message (title/performer, real stored duration),
                    # exactly like the audio kind below.
                    sent = await send_song_audio(
                        message, file_id, title, artist, caption=cap,
                        reply_markup=_clip_kb)
                else:
                    sent = await send_song_audio(
                        message, file_id, title, artist, caption=cap,
                        reply_markup=_clip_kb)
                if sent is not None:
                    _track(conn, message, sent)
                    await _drop_status()
                    _store_and_log("clip", sent)
                    return
            except Exception:
                pass
        # 3) Honest "full version not found" + more-search options. A short
        # preview is never presented as the song. If Spotify is configured,
        # attach the official Spotify link as an honest alternative.
        more_kb_rows = list(kb.inline_keyboard)
        more_kb_rows.append([InlineKeyboardButton(
            text=t("similar_songs", ulang), callback_data=_cb_query(query))])
        spotify_url = ""
        if config is not None and getattr(config, "spotify_client_id", ""):
            try:
                from ..search.spotify import search_tracks as _sp_search
                _sp = await _sp_search(
                    f"{artist} {title}".strip(),
                    config.spotify_client_id, config.spotify_client_secret or "",
                    limit=1)
                if _sp and _sp[0].get("spotify_url"):
                    # title sanity: must match, never a wrong song's link
                    from ..search.youtube_full import _title_ok as _sp_ok
                    if _sp_ok(_sp[0]["title"], "", artist, title):
                        spotify_url = _sp[0]["spotify_url"]
            except Exception:
                spotify_url = ""
        if spotify_url:
            more_kb_rows.append([InlineKeyboardButton(
                text="🎧 Spotify", url=spotify_url)])
        if bank_token:
            more_kb_rows.append([InlineKeyboardButton(
                text=t("m_bank_search_btn", ulang),
                callback_data=f"bank:search:{bank_token}")])
        await _drop_status()
        await _answer(message, conn,
                      t("h_song_no_full", ulang).format(title=title, artist=artist),
                      reply_markup=InlineKeyboardMarkup(inline_keyboard=more_kb_rows))
        _store_and_log("text", None)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Fire-and-forget: log, never raise into the event loop.
        try:
            log_event(log, "fullsong", stage="bg_error", title=title[:40])
        except Exception:
            pass
    finally:
        await _drop_status()


def _openai_ready(conn, config) -> tuple[str, bool]:
    """(key, verified) for the superior-intelligence speech/brain features.

    A DB-stored key (entered via the admin panel) works only after
    «تأیید عملکرد کلید»; an env-provided key is trusted as-is.
    """
    db_key = dbmod.get_setting(conn, "openai_api_key", "").strip()
    if db_key:
        return db_key, adminfeat.is_openai_verified(conn)
    env_key = (config.openai_api_key or "").strip()
    return env_key, bool(env_key)


async def _synthesize_voice(text: str, gender: str, config: Config, conn):
    """Text-to-voice: verified OpenAI TTS first, Azure Speech as fallback.

    Returns (status, audio_bytes). "needs_key" when nothing is configured,
    "needs_verification" when a panel-stored key is not yet verified —
    never a wrong-language substitute voice, never fabricated audio.
    """
    key, verified = _openai_ready(conn, config)
    if key and verified:
        try:
            status, audio = await synthesize_openai(text, gender, key)
        except Exception:
            status, audio = "error", b""
        if status == "ok" and audio:
            return status, audio
        if not (config.azure_speech_key and config.azure_speech_region):
            return status, audio
    elif key and not verified:
        return "needs_verification", b""
    if config.azure_speech_key and config.azure_speech_region:
        return await synthesize(text, gender, config.azure_speech_key, config.azure_speech_region)
    return "needs_key", b""


async def _transcribe_voice(data: bytes, filename: str, config: Config, conn):
    """Voice-to-text: verified OpenAI Whisper first, Azure STT as fallback.

    Returns (status, text). Without a verified key it returns
    "needs_key"/"needs_verification" without calling any service, and
    text is never fabricated.
    """
    key, verified = _openai_ready(conn, config)
    if key and verified:
        try:
            status, text = await transcribe_whisper(data, key, filename)
        except Exception:
            status, text = "error", ""
        if status == "ok":
            return status, text
        if not (config.azure_speech_key and config.azure_speech_region):
            return status, text
    elif key and not verified:
        return "needs_verification", ""
    if config.azure_speech_key and config.azure_speech_region:
        result = await transcribe_bytes(data, config.azure_speech_key, config.azure_speech_region)
        return result.status, result.text or ""
    return "needs_key", ""


def _cb_query(query: str, prefix: str = "similar:") -> str:
    """Byte-safe callback_data: Telegram allows 64 bytes; Persian chars are
    2 bytes each, so truncate by bytes and never split a UTF-8 char."""
    b = f"{prefix}{query}".encode("utf-8")[:64]
    while b:
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            b = b[:-1]
    return prefix.rstrip(":")


async def _do_search(message: Message, conn, query: str, include_video: bool = True, config=None):
    """Professional minimal text search: clean list + one song card.

    Sources: iTunes + Deezer (current engine preserved). If the query
    looks like a lyric line and catalog variants fail, the lyric-phrase
    pipeline verifies candidates against real lyrics (overlap >= 0.60)
    before returning anything. If catalog + lyric search both fail, the
    channel engine is tried once (cached, stops on first exact post).
    Results are never labelled "identified".
    """
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    if not rate_limiter.allow(f"search:{message.from_user.id}", 10, 60):
        await _answer(message, conn, t("m_rate_wait", ulang)); return
    # Temporary "searching" message -- deleted after results are sent.
    _searching_msg = None
    try:
        _searching_msg = await _answer(message, conn, t("searching", ulang))
    except Exception:
        pass

    async def _del_searching():
        if _searching_msg is not None:
            try:
                await _searching_msg.delete()
            except Exception:
                pass
    providers = [ITunesProvider(entity="song"), DeezerProvider()]
    if include_video:
        providers.append(ITunesProvider(entity="musicVideo"))
    engine = SearchEngine(providers)
    results = []
    try:
        results, _variant = await smart_search(engine, query, limit=10)
    except Exception:
        results = []
    if not results and len(query.split()) >= 3:
        try:
            async def _cat(q):
                r, _f = await engine.search(q, limit=10)
                return r
            results = await lyric_phrase_search(
                query, _cat, lambda tt, aa: fetch_lyrics(tt, aa))
        except Exception:
            results = []
    dbmod.record_search(conn, message.from_user.id, query, ok=bool(results))
    if not results:
        await _no_results_with_channel(message, conn, query, config)
        await _del_searching()
        return
    text = format_results(results, page=1)
    kb = None
    if len(results) > 5:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t("m_next_page", ulang), callback_data=_cb_query(query, prefix="search:next:"))]])
    await _answer(message, conn, text, reply_markup=kb)
    await _del_searching()
    song = next((r for r in results if r.kind == "music"), None)
    if song is not None:
        key = profeat.remember_result(song.title, song.artist, song.url, song.preview_url)
        is_fav = profeat.is_favorite(conn, message.from_user.id, song.title, song.artist)
        kb2 = InlineKeyboardMarkup(inline_keyboard=profeat.build_song_card_buttons(key, is_fav))
        bot_id = dbmod.get_bot_id(conn, config) if config is not None else ""
        if song.preview_url:
            sent = await send_song_audio(
                message, song.preview_url, song.title, song.artist,
                caption=professional_audio_caption(song, bot_id),
                reply_markup=kb2, duration=song.duration)
            if sent is not None:
                _track(conn, message, sent)
                return
        await _answer(message, conn, professional_audio_caption(song, bot_id), reply_markup=kb2)


async def _no_results_with_channel(message, conn, query: str, config=None):
    """Channel fallback (cached, exact post only) + similar button."""
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    if config is not None:
        channels = dbmod.get_channels(conn, config)
        if channels:
            cache_key = f"channel:{query}"
            cached = dbmod.get_cached_search(conn, cache_key)
            if cached:
                import json as _json
                posts = _json.loads(cached)
            else:
                try:
                    posts = await search_channels(channels, query)
                except Exception:
                    posts = []
                import json as _json
                dbmod.set_cached_search(conn, cache_key, _json.dumps(posts, ensure_ascii=False))
            if posts:
                post = posts[0]
                await message.answer(
                    f"{t('channel_post', ulang)}\n{post['post_url']}\n{post['text'][:300]}")
                return
    await message.answer(t("no_results", ulang), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("similar_songs", ulang), callback_data=_cb_query(query))]]))


@router.callback_query(F.data.startswith("search:next:"))
async def search_next(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    query = callback.data.split(":", 2)[2]
    engine = SearchEngine([ITunesProvider(entity="song"), ITunesProvider(entity="musicVideo")])
    try:
        results, _ = await smart_search(engine, query, limit=10)
    except Exception:
        results = []
    if len(results) <= 5:
        await callback.answer(t("m_no_more_pages", ulang), show_alert=True); return
    await callback.message.edit_text(format_results(results, page=2))
    await callback.answer()


async def _remix_handle_names(message: Message, conn: sqlite3.Connection,
                              config: Config, state: FSMContext,
                              text: str) -> None:
    """Handle song/artist names for remix method 2.

    Parses 2 lines, searches each via the existing search engine,
    and shows selectable results for user confirmation.
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    uid = getattr(getattr(message, "from_user", None), "id", None)
    if not (config.owner_id is not None and uid == config.owner_id):
        try:
            await state.clear()
        except Exception:
            pass
        return

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    # Also accept comma-separated on one line
    if len(lines) == 1 and "," in lines[0]:
        lines = [p.strip() for p in lines[0].split(",") if p.strip()]
    if len(lines) < 2:
        await message.answer(
            "❌ لطفاً نام <b>دو</b> آهنگ یا خواننده را بنویسید "
            "(هر کدام در یک خط).",
            parse_mode="HTML")
        return
    # Beta limit: exactly 2
    lines = lines[:2]

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ لغو", callback_data="remix:cancel"),
    ]])
    await message.answer("🔍 در حال جستجوی آهنگ‌ها...", reply_markup=cancel_kb)

    # Search each query with the existing engine (iTunes + Deezer)
    from ..search import SearchEngine
    from ..search.itunes import ITunesProvider
    from ..search.deezer import DeezerProvider
    from ..search.smart import smart_search

    all_options = []  # list of (query_idx, title, artist)
    try:
        engine = SearchEngine([ITunesProvider(entity="song"),
                               DeezerProvider()])
        for qi, q in enumerate(lines):
            try:
                results, _variant = await smart_search(engine, q, limit=5)
            except Exception:
                results = []
            for r in results[:4]:
                title = getattr(r, "title", "") or ""
                artist = getattr(r, "artist", "") or ""
                if title:
                    all_options.append((qi, title.strip(), artist.strip()))
    except Exception as e:
        await message.answer(f"❌ خطا در جستجو: {e}")
        try:
            await state.clear()
        except Exception:
            pass
        return

    if not all_options:
        await message.answer(
            "❌ آهنگی پیدا نشد. نام دیگری امتحان کنید.",
            reply_markup=cancel_kb)
        return

    # Deduplicate
    seen = set()
    uniq = []
    for qi, title, artist in all_options:
        key = (title.lower(), artist.lower())
        if key not in seen:
            seen.add(key)
            uniq.append((qi, title, artist))
    all_options = uniq[:8]

    # Store options in state for the selection callback
    try:
        await state.update_data(
            remix_search_options=[
                {"qi": qi, "title": t, "artist": a}
                for qi, t, a in all_options
            ],
            remix_queries=lines,
        )
        await state.set_state("remix:await_select")
    except Exception:
        pass

    # Build selection keyboard: one button per option
    rows = []
    for idx, (qi, title, artist) in enumerate(all_options):
        label = f"{title}" + (f" - {artist}" if artist else "")
        if len(label) > 40:
            label = label[:37] + "..."
        rows.append([InlineKeyboardButton(
            text=f"🎵 {label}",
            callback_data=f"remix:pick:{idx}")])
    rows.append([InlineKeyboardButton(text="❌ لغو",
                                      callback_data="remix:cancel")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    qlist = "\n".join(f"{i+1}. {q}" for i, q in enumerate(lines))
    await message.answer(
        "🎵 <b>نتایج جستجو:</b>\n"
        f"<i>برای:</i>\n{qlist}\n\n"
        "دو آهنگ موردنظر را یکی‌یکی انتخاب کنید "
        "(اول آهنگ اول، بعد آهنگ دوم):",
        parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("remix:pick:"))
async def remix_pick(callback, conn: sqlite3.Connection, config: Config,
                     state: FSMContext):
    """User picks a song from search results. Two picks -> confirm screen."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    await callback.answer()
    uid = callback.from_user.id
    if not (config.owner_id is not None and uid == config.owner_id):
        return
    try:
        idx = int(callback.data.split(":")[-1])
        data = await state.get_data()
    except Exception:
        return
    options = data.get("remix_search_options") or []
    if idx < 0 or idx >= len(options):
        return
    picked = data.get("remix_picked") or []
    choice = options[idx]
    # Avoid duplicates
    if any(p["title"] == choice["title"] and p["artist"] == choice["artist"]
           for p in picked):
        await callback.message.answer("⚠️ این آهنگ قبلاً انتخاب شده.")
        return
    picked.append(choice)
    try:
        await state.update_data(remix_picked=picked)
    except Exception:
        pass

    if len(picked) < 2:
        await callback.message.answer(
            f"✅ انتخاب شد: <b>{choice['title']}</b>"
            + (f" - {choice['artist']}" if choice["artist"] else "")
            + "\n\nحالا <b>آهنگ دوم</b> را انتخاب کنید.",
            parse_mode="HTML")
        return

    # Two picked -> show confirmation with download check
    await callback.message.answer("⏳ در حال بررسی فایل‌های قابل دانلود...")
    from ..search.fullsong import find_full_song_file

    ready = []
    missing = []
    for p in picked:
        try:
            found = await find_full_song_file(p["artist"] or "", p["title"])
        except Exception:
            found = None
        if found and found.get("data"):
            ready.append({**p, "ext": found.get("ext", ".mp3"),
                          "duration": found.get("duration_sec", 0)})
        else:
            missing.append(p)

    if missing:
        names = ", ".join(
            f"{m['title']}" + (f" ({m['artist']})" if m["artist"] else "")
            for m in missing)
        await callback.message.answer(
            "⚠️ فایل کامل این آهنگ(ها) پیدا نشد و وارد ریمیکس نمی‌شود:\n"
            f"<i>{names}</i>\n\n"
            "لطفاً آهنگ دیگری انتخاب کنید.",
            parse_mode="HTML")
        # Reset picks so the user can choose again
        try:
            await state.update_data(remix_picked=[])
            await state.set_state("remix:await_select")
        except Exception:
            pass
        return

    try:
        await state.update_data(remix_ready=ready)
        await state.set_state("remix:await_confirm")
    except Exception:
        pass

    rows = [[
        InlineKeyboardButton(text="✅ تأیید و ساخت ریمیکس",
                             callback_data="remix:confirm"),
        InlineKeyboardButton(text="❌ لغو", callback_data="remix:cancel"),
    ]]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    desc = "\n".join(
        f"{i+1}. <b>{r['title']}</b>"
        + (f" - {r['artist']}" if r["artist"] else "")
        + f" ({r['duration']:.0f} ثانیه)"
        for i, r in enumerate(ready))
    await callback.message.answer(
        "🎛️ <b>تأیید نهایی</b>\n\n"
        f"{desc}\n\n"
        "آیا این دو آهنگ وارد موتور ریمیکس شوند؟",
        parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "remix:confirm")
async def remix_confirm(callback, conn: sqlite3.Connection, config: Config,
                        state: FSMContext):
    """User confirmed: download the 2 files and run the same remix engine."""
    import os
    import tempfile

    await callback.answer()
    uid = callback.from_user.id
    if not (config.owner_id is not None and uid == config.owner_id):
        return
    try:
        data = await state.get_data()
    except Exception:
        return
    ready = data.get("remix_ready") or []
    if len(ready) < 2:
        await callback.message.answer("❌ اطلاعات آهنگ‌ها ناقص است.")
        return

    from ..search.fullsong import find_full_song_file
    from .remix import is_locked

    if is_locked():
        await callback.message.answer(
            "⏳ یک ریمیکس در حال ساخت است. لطفاً بعداً تلاش کنید.")
        return

    await callback.message.answer("📥 در حال دانلود فایل‌های صوتی...")
    tmpdir = tempfile.mkdtemp(prefix="remix_tg_")
    paths = []
    try:
        for i, r in enumerate(ready[:2]):
            found = await find_full_song_file(r["artist"] or "", r["title"])
            if not found or not found.get("data"):
                raise RuntimeError(
                    f"فایل «{r['title']}» قابل دانلود نیست.")
            # No duration rejection: long songs are auto-trimmed by the
            # remix engine (select_best_section).
            ext = found.get("ext", ".mp3") or ".mp3"
            p = os.path.join(tmpdir, f"search{i+1}{ext}")
            with open(p, "wb") as f:
                f.write(found["data"])
            # Validate the written file before the job starts.
            from .remix import _ensure_readable
            _ensure_readable(p)
            paths.append(p)
    except Exception as e:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        await callback.message.answer(f"❌ خطا در دانلود: {e}")
        try:
            await state.clear()
        except Exception:
            pass
        return

    try:
        await state.update_data(remix_tmpdir=tmpdir)
        await state.set_state("remix:processing")
    except Exception:
        pass
    await callback.message.answer(
        "🎛️ ساخت ریمیکس شروع شد...",
        parse_mode="HTML")
    asyncio.create_task(
        _remix_run_job(callback.message, conn, config, state,
                       paths[0], paths[1], tmpdir))


@router.message(F.text)
async def text_handler(message: Message, conn: sqlite3.Connection, config: Config, state: FSMContext | None = None):
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    _register(message, conn)
    _track_incoming(conn, message)
    if _blocked(conn, message.from_user.id):
        await message.answer(t("blocked", ulang)); return
    text = (message.text or "").strip()
    current_state = None
    if state is not None:
        try:
            current_state = await state.get_state()
        except Exception:
            current_state = None
    if is_menu_toggle(text):
        # Menu-collapse toggle: checked before the feature-title branch
        # (and before every state branch) so it works from every screen.
        # A pure display change — it never clears FSM state.
        await _run_menu_toggle(message, conn, config, text)
        return
    if title_key := _menu_title_key(conn, text):
        # Persistent bottom-menu button: run its real feature, not a search.
        # Checked before the state branches so a menu tap always escapes a
        # stale conversation state (feature state handlers are additionally
        # guarded by the menu_escape decorator).
        await _run_menu_title(message, conn, config, state, title_key)
        return
    if current_state == "suggest":
        try:
            await state.clear()
        except Exception:
            pass
        await _run_suggest(message, conn, text, config)
        return
    if current_state == "lyr:await_query":
        try:
            await state.clear()
        except Exception:
            pass
        await _run_lyrics_search(message, conn, text, config)
        return
    if current_state == "remix:await_names":
        await _remix_handle_names(message, conn, config, state, text)
        return
    if current_state == "audio_id:await_file":
        # Waiting for a file: plain text is neither consumed as a search
        # nor kills the wait silently. The wait continues with a clear
        # notice; menu-button taps and /cancel still escape via the guards
        # above. A link is its own independent input type: if the text
        # carries one, the file-wait is superseded and the link flow below
        # takes over.
        if _extract_link(text):
            try:
                await state.clear()
            except Exception:
                pass
        else:
            await message.answer(
                t("m_await_file", ulang))
            return
    link_url = _extract_link(text)
    if link_url:
        # Intent from a link is deterministic (no button, no brain call
        # needed): the link's own video, then its audio identification.
        # Detection finds the URL anywhere in the text, so a pasted link
        # never falls through to the file prompts or text search.
        allowed, reason = can_use_feature(conn, message.from_user.id, "link_fetch")
        if not allowed:
            await message.answer(t(reason, ulang)); return
        # Timing instrumentation (logs only): one request id per link,
        # propagated through every stage line of this request.
        _req_id = _timing.new_req_id()
        _t_req = _time.perf_counter()
        _timing.log_stage(log, "link_stage", _req_id, _t_req, "receipt",
                          _t_req)
        # SPEED: URL-based file_id cache check FIRST (instant resend < 1s).
        # If this exact URL was sent before, reuse the Telegram file_id
        # without any download or processing.
        try:
            from .file_cache import get_cached_url_file_id
            _url_cached = get_cached_url_file_id(conn, link_url)
            # Try persistent Turso cache if local miss (survives Render restarts)
            if not _url_cached:
                try:
                    from .turso_cache import get_url_file_id as _turso_get
                    _url_cached = _turso_get(link_url, req_id=_req_id)
                    if _url_cached:
                        log_event(log, "link", stage="turso_cache_hit", url=link_url[:60])
                except Exception:
                    pass
            if _url_cached:
                _fid, _ftype = _url_cached
                log_event(log, "link", stage="url_cache_hit", url=link_url[:60])
                _t_send = _time.perf_counter()
                try:
                    if _ftype == "video":
                        _sent = await message.answer_video(
                            _fid, caption=t("m_link_video_dl", ulang))
                    else:
                        _sent = await message.answer_audio(_fid)
                    _timing.log_stage(log, "link_stage", _req_id, _t_req,
                                      "send_video", _t_send)
                    # Attach the identify button (audio_url=None → falls back to Telegram file)
                    try:
                        await _attach_identify_button(message, conn, _sent,
                                                      link_url=link_url)
                    except Exception:
                        pass
                    _timing.log_total(log, "link_stage", _req_id, _t_req,
                                      "cache_hit",
                                      headline_total="link_to_video_ms")
                    return
                except Exception:
                    # Cached file_id invalid (e.g., deleted) — fall through to normal flow
                    from .file_cache import invalidate_file
                    # Note: invalidate by URL key requires the key; skip for now, will be overwritten on next store
                    pass
        except Exception:
            pass
        # SPEED: Immediate status message (< 1s response) before heavy work.
        # The actual download + send happens in background.
        try:
            _status_msg = await message.answer("⏳ در حال پردازش...")
        except Exception:
            _status_msg = None
        _t_detect = _time.perf_counter()
        outcome = process_link(link_url, config.max_file_mb)
        _timing.log_stage(log, "link_stage", _req_id, _t_req, "detect",
                          _t_detect)
        if not outcome.ok:
            if _status_msg:
                try:
                    await _status_msg.edit_text(t(outcome.message, "fa"))
                except Exception:
                    await message.answer(t(outcome.message, "fa"))
            else:
                await message.answer(t(outcome.message, "fa"))
            _timing.log_total(log, "link_stage", _req_id, _t_req, "rejected")
            return
        # Resolve Instagram media ONCE per link: the video-send stage
        # and the identify stage share the result (they used to each run
        # the full four-strategy resolver for the same URL, doubling
        # page fetches and resolve time).
        _resolved = None
        if outcome.kind == "instagram":
            _rt = _time.perf_counter()
            try:
                from ..services.instagram import resolve_instagram_media
                _resolved = await resolve_instagram_media(
                    outcome.official_url,
                    chat_id=getattr(getattr(message, "chat", None), "id",
                                    None)
                    or getattr(getattr(message, "from_user", None), "id",
                               None))
            except Exception:
                _resolved = None
            _timing.log_stage(log, "link_stage", _req_id, _t_req, "resolve",
                              _rt,
                              strategy=_resolved[3] if _resolved else "error")
        # Step 1b: bg identify spawns at RESOLVE (not after the video
        # is sent): the task waits for the video download to finish
        # (event set by _send_link_video), then identifies + resolves
        # the file and writes the FINAL result to the shortcode cache
        # even if the user never taps. The old _spawn_bg_precompute
        # (bank-only slice) is superseded by this full pipeline.
        try:
            _bg_key = _identify_result_key(link=outcome.official_url)
            _cid = getattr(getattr(message, "chat", None), "id", None) \
                or getattr(getattr(message, "from_user", None), "id", None)
            if _bg_key and _resolved is not None and _resolved[0]:
                _spawn_identify_bg(_bg_key, _resolved[0], _cid, conn,
                                   config, _req_id)
        except Exception:
            pass
        # SPEED: Time the video send stage.
        import time as _ptime
        _vs = _ptime.perf_counter()
        # Professional minimal link flow: video, then one clean song card.
        # No catalog video search, no video section, no button grid, no
        # empty "not found" boxes.
        video_msg = await _send_link_video(message, conn, outcome, config,
                                           bot_id=dbmod.get_bot_id(conn, config),
                                           resolved=_resolved,
                                           req_id=_req_id, t_req=_t_req)
        # SPEED: Log video send timing.
        _timing.log_stage(log, "link_stage", _req_id, _t_req,
                          "video_send_total", _vs)
        # SPEED: Delete the "processing" status message now that video is sent.
        if _status_msg:
            try:
                await _status_msg.delete()
            except Exception:
                pass
        if video_msg == _INSTAGRAM_GATED:
            # Age-gated reel: the dedicated message is already shown.
            # There is no audio any anonymous fetch can reach, so no
            # identification, bank sample, or channel stage runs.
            _timing.log_total(log, "link_stage", _req_id, _t_req, "age_gated")
            return
        # SPEED: Store file_id in URL cache for instant resend (< 1s next time).
        # video_msg is the sent Message object; extract file_id from it.
        _t_store = _time.perf_counter()
        try:
            from .file_cache import store_url_file_id
            _vid = None
            _vtype = "video"
            if video_msg and video_msg != _INSTAGRAM_GATED:
                # Try to get file_id from the sent message
                if hasattr(video_msg, 'video') and video_msg.video:
                    _vid = video_msg.video.file_id
                    _vtype = "video"
                elif hasattr(video_msg, 'audio') and video_msg.audio:
                    _vid = video_msg.audio.file_id
                    _vtype = "audio"
                elif hasattr(video_msg, 'document') and video_msg.document:
                    _vid = video_msg.document.file_id
                    _vtype = "video"
                if _vid:
                    store_url_file_id(conn, link_url, _vid, _vtype)
                    log_event(log, "link", stage="url_cache_stored", url=link_url[:60])
                    # Also store in persistent Turso cache (survives restarts)
                    try:
                        from .turso_cache import store_url_file_id as _turso_store
                        if _turso_store(link_url, _vid, _vtype, req_id=_req_id):
                            log_event(log, "link", stage="turso_cache_stored", url=link_url[:60])
                    except Exception:
                        pass
                    _timing.log_stage(log, "link_stage", _req_id, _t_req,
                                      "cache_store", _t_store)
        except Exception:
            pass
        # P1-14: mirror the user save (and any other dirty user-owned
        # rows) to Turso — fire-and-forget, AFTER the video was sent,
        # never before the answer. The flush runs in a thread and
        # opens its own SQLite connection.
        try:
            asyncio.create_task(
                asyncio.to_thread(
                    dbmod.flush_dirty_to_turso, config.db_path))
        except Exception:
            pass
        # Step 1b: _spawn_bg_precompute (bank-only slice) is SUPERSEDED
        # by the bg identify spawned at resolve above (full identify +
        # file resolution, shortcode cache). The functions stay in
        # place (dead-code precedent); this call site is retired.
        _timing.log_total(log, "link_stage", _req_id, _t_req, "sent",
                          headline_total="link_to_video_ms")
        return
    allowed, reason = can_use_feature(conn, message.from_user.id, "song_search")
    if not allowed:
        await message.answer(t(reason, ulang)); return
    if is_mood_request(text):
        await _run_suggest(message, conn, text, config)
        return
    # Free text: if the superior-intelligence layer is connected AND its key
    # verified, let it understand the intent and clean the query; otherwise
    # the original text goes to the smart search unchanged (rule-based
    # fallback, never claimed to be an AI answer).
    query = sanitize_text(text, 300)
    _bkey, _bok = _openai_ready(conn, config)
    if _bkey and _bok:
        try:
            query = await brain_clean_query(query, _bkey)
        except Exception:
            pass
    await _do_search(message, conn, query, config=config)


async def _remix_collect_audio(message: Message, conn: sqlite3.Connection,
                               config: Config, state: FSMContext,
                               current_state: str) -> bool:
    """Collect the 2 audio files for the Smart Remix beta.

    Returns True if the message was consumed by the remix flow.
    Only the owner can use this; anyone else is ignored (returns False
    so the normal flow continues untouched).
    """
    import os
    import shutil
    import tempfile
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    uid = getattr(getattr(message, "from_user", None), "id", None)
    if not (config.owner_id is not None and uid == config.owner_id):
        return False
    try:
        from .remix import (MAX_INPUT_FILE_MB, is_enabled, probe_duration,
                            _ensure_readable)
    except Exception:
        return False
    if not is_enabled():
        return False

    audio = message.audio
    doc = message.document
    file_obj = audio or doc
    if file_obj is None:
        await message.answer(
            "🎵 لطفاً یک فایل صوتی ارسال کنید (MP3 / WAV / OGG).")
        return True

    # Basic validation: mime / extension
    name = (getattr(file_obj, "file_name", "") or "").lower()
    mime = (getattr(doc, "mime_type", "") or "").lower() if doc else "audio/mpeg"
    ok_ext = name.endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac"))
    ok_mime = mime.startswith("audio/") or ok_ext
    if not ok_mime and not ok_ext:
        await message.answer("❌ فرمت پشتیبانی نمی‌شود. MP3 / WAV / OGG بفرستید.")
        return True
    size_mb = (file_obj.file_size or 0) / 1024 / 1024
    if size_mb > MAX_INPUT_FILE_MB:
        await message.answer(
            f"❌ فایل خیلی بزرگ است (حداکثر {MAX_INPUT_FILE_MB} مگابایت).")
        return True

    # Download to a per-user temp dir
    try:
        data = await state.get_data()
    except Exception:
        data = {}
    tmpdir = data.get("remix_tmpdir")
    if not tmpdir or not os.path.isdir(tmpdir):
        tmpdir = tempfile.mkdtemp(prefix="remix_tg_")
        try:
            await state.update_data(remix_tmpdir=tmpdir)
        except Exception:
            pass

    slot = "file1" if current_state == "remix:await_file1" else "file2"
    ext = os.path.splitext(name)[1] or ".mp3"
    dest = os.path.join(tmpdir, f"{slot}{ext}")
    try:
        tg_file = await message.bot.get_file(file_obj.file_id)
        await message.bot.download_file(tg_file.file_path, dest)
    except Exception as e:
        await message.answer(f"❌ خطا در دریافت فایل: {e}")
        return True

    # Validate the downloaded file exists and is readable BEFORE probing.
    # (Fixes "ffprobe failed: No such file or directory".)
    try:
        _ensure_readable(dest)
    except Exception as e:
        await message.answer(f"❌ فایل دانلود نشد: {e}")
        return True

    # Duration probe (informational now — long songs are auto-trimmed,
    # no 60s rejection anymore).
    try:
        dur = probe_duration(dest)
    except Exception as e:
        await message.answer(f"❌ فایل صوتی خوانده نشد: {e}")
        try:
            os.remove(dest)
        except Exception:
            pass
        return True

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ لغو", callback_data="remix:cancel"),
    ]])

    if current_state == "remix:await_file1":
        try:
            await state.update_data(remix_file1=dest)
            await state.set_state("remix:await_file2")
        except Exception:
            pass
        note = (f"✅ فایل اول دریافت شد ({dur:.0f} ثانیه)."
                if dur <= 60 else
                f"✅ فایل اول دریافت شد ({dur:.0f} ثانیه).\n"
                "✂️ بخش مناسب آن به‌صورت خودکار انتخاب می‌شود.")
        await message.answer(
            f"{note}\n\n"
            "🎵 حالا <b>فایل صوتی دوم</b> را ارسال کنید.",
            parse_mode="HTML", reply_markup=cancel_kb)
        return True

    # Second file received -> start background remix job
    try:
        data = await state.get_data()
    except Exception:
        data = {}
    file1 = data.get("remix_file1")
    if not file1 or not os.path.exists(file1):
        await message.answer("❌ فایل اول پیدا نشد. از اول شروع کنید.",
                             reply_markup=cancel_kb)
        try:
            await state.clear()
        except Exception:
            pass
        return True
    try:
        await state.set_state("remix:processing")
    except Exception:
        pass
    note2 = (f"✅ فایل دوم دریافت شد ({dur:.0f} ثانیه)."
             if dur <= 60 else
             f"✅ فایل دوم دریافت شد ({dur:.0f} ثانیه).\n"
             "✂️ بخش مناسب آن به‌صورت خودکار انتخاب می‌شود.")
    await message.answer(
        f"{note2}\n\n"
        "🎛️ ساخت ریمیکس شروع شد...",
        parse_mode="HTML")
    # Fire-and-forget background task; the bot stays responsive.
    asyncio.create_task(
        _remix_run_job(message, conn, config, state, file1, dest, tmpdir))
    return True


async def _remix_run_job(message: Message, conn: sqlite3.Connection,
                         config: Config, state: FSMContext,
                         file1: str, file2: str, tmpdir: str) -> None:
    """Background remix job: status updates + final MP3 delivery."""
    import os
    import shutil
    from aiogram.types import FSInputFile

    uid = getattr(getattr(message, "from_user", None), "id", None)
    ulang = _user_lang(conn, uid)
    status_msg = None

    async def _say(text: str):
        nonlocal status_msg
        try:
            if status_msg is None:
                status_msg = await message.answer(text, parse_mode="HTML")
            else:
                await status_msg.edit_text(text, parse_mode="HTML")
        except Exception:
            try:
                status_msg = await message.answer(text, parse_mode="HTML")
            except Exception:
                pass

    # Bridge sync progress callbacks from remix.py into async status edits.
    # The executor thread cannot await, so we schedule coroutines.
    loop = asyncio.get_running_loop()

    def _progress(stage: str, detail):
        try:
            if stage == "validate":
                txt = "📥 <b>دریافت فایل‌ها</b> ✅\n🔍 در حال تحلیل موسیقی..."
            elif stage == "extract1":
                d = (detail or {}).get("duration", 0)
                txt = ("📥 دریافت فایل‌ها ✅\n"
                       f"✂️ آهنگ اول طولانی است ({d:.0f} ثانیه)؛\n"
                       "در حال انتخاب بهترین بخش...")
            elif stage == "extract2":
                d = (detail or {}).get("duration", 0)
                txt = ("📥 دریافت فایل‌ها ✅\n"
                       "✂️ انتخاب بخش آهنگ اول ✅\n"
                       f"✂️ آهنگ دوم طولانی است ({d:.0f} ثانیه)؛\n"
                       "در حال انتخاب بهترین بخش...")
            elif stage == "analyze1":
                txt = ("📥 دریافت فایل‌ها ✅\n"
                       "🔍 تحلیل آهنگ اول... (تشخیص ریتم و کلید)")
            elif stage == "analyze2":
                i1 = detail or {}
                txt = ("📥 دریافت فایل‌ها ✅\n"
                       f"🔍 آهنگ اول: {i1.get('bpm', 0):.0f} BPM ✅\n"
                       "🔍 تحلیل آهنگ دوم... (تشخیص ریتم و کلید)")
            elif stage == "mix":
                i1, i2 = detail or ({}, {})
                txt = ("📥 دریافت فایل‌ها ✅\n"
                       f"🔍 تحلیل موسیقی ✅ ({i1.get('bpm', 0):.0f} / "
                       f"{i2.get('bpm', 0):.0f} BPM)\n"
                       "🎚️ هماهنگ‌سازی سرعت آهنگ‌ها...")
            elif stage == "sync":
                txt = ("📥 دریافت فایل‌ها ✅\n🔍 تحلیل موسیقی ✅\n"
                       f"🎚️ هماهنگ‌سازی سرعت... (ضریب {detail:.2f})")
            elif stage == "xfade":
                txt = ("📥 دریافت فایل‌ها ✅\n🔍 تحلیل موسیقی ✅\n"
                       "🎚️ هماهنگ‌سازی ✅\n🎛️ ساخت ریمیکس (انتقال نرم)...")
            else:
                return
            asyncio.run_coroutine_threadsafe(_say(txt), loop)
        except Exception:
            pass

    out_path = os.path.join(tmpdir, "remix_final.mp3")
    t0 = asyncio.get_event_loop().time()
    try:
        from .remix import run_remix_job
        result = await run_remix_job(file1, file2, out_path, _progress)
    except Exception as e:
        await _say(f"❌ <b>خطا در ساخت ریمیکس:</b>\n{str(e)[:300]}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            await state.clear()
        except Exception:
            pass
        return

    total = result.get("total_sec", 0)
    await _say("📦 آماده‌سازی خروجی...")

    # Deliver the real MP3
    try:
        sec_note = ""
        s1 = result.get("section1")
        s2 = result.get("section2")
        if s1 or s2:
            parts = []
            if s1:
                parts.append(
                    f"آهنگ ۱: بخش {s1['start']:.0f}–{s1['start']+s1['len']:.0f} "
                    f"از {s1['orig_dur']:.0f} ثانیه")
            if s2:
                parts.append(
                    f"آهنگ ۲: بخش {s2['start']:.0f}–{s2['start']+s2['len']:.0f} "
                    f"از {s2['orig_dur']:.0f} ثانیه")
            sec_note = "✂️ " + " | ".join(parts) + "\n"

        # Pro engine details
        pro_note = ""
        if result.get("beat_aligned"):
            pro_note += "🥁 هماهنگی ضرب: ✅\n"
        xfade = result.get("xfade_sec")
        if xfade:
            pro_note += f"🔀 انتقال: {xfade:.0f} ثانیه\n"
        kc = result.get("key_check") or {}
        if kc.get("trusted") and kc.get("compatible") is False:
            pro_note += f"⚠️ {kc.get('detail', '')}\n"
        val = result.get("validation") or {}
        if val.get("lufs") is not None:
            pro_note += f"🔊 بلندی: {val['lufs']:.1f} LUFS\n"

        caption = (
            "🎛️ <b>ریمیکس هوشمند آماده شد!</b> 🧪\n\n"
            f"🎵 آهنگ ۱: {result.get('bpm1', 0):.0f} BPM "
            f"({result['info1'].get('key', '?')} {result['info1'].get('scale', '')})\n"
            f"🎵 آهنگ ۲: {result.get('bpm2', 0):.0f} BPM "
            f"({result['info2'].get('key', '?')} {result['info2'].get('scale', '')})\n"
            + sec_note + pro_note +
            f"⏱️ زمان پردازش: {total:.0f} ثانیه\n"
            f"💾 حجم: {result.get('size_mb', 0)} مگابایت\n\n"
            "@AvanoMediaBot"
        )
        await message.answer_audio(
            FSInputFile(out_path, filename="avano_remix.mp3"),
            caption=caption, parse_mode="HTML",
            title="Avano Smart Remix (Beta)")
        if status_msg is not None:
            try:
                await status_msg.delete()
            except Exception:
                pass
    except Exception as e:
        await _say(f"❌ خطا در ارسال فایل: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            await state.clear()
        except Exception:
            pass


@router.message(F.video | F.audio | F.voice | F.document)
async def media_handler(message: Message, conn: sqlite3.Connection, config: Config,
                        state: FSMContext):
    """Auto intent: a sent film/audio IS a song-identification request,
    unless voice-to-text mode was just started (button/command) — then
    the same voice is transcribed to text instead, via speech recognition
    (not a speech-synthesis engine), and no text is ever fabricated."""
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    _register(message, conn)
    _track_incoming(conn, message)
    if _blocked(conn, message.from_user.id):
        await message.answer(t("blocked", ulang)); return
    # Bank indexing: a forwarded audio from a registered channel is indexed
    # for future bank searches (title/artist from caption or audio metadata).
    # This is the ONLY way channel files enter the bank: the Bot API cannot
    # read a channel's history unless the bot is its admin.
    try:
        _fwd_ch = _forward_channel(message)
        if _fwd_ch and message.audio:
            from ..features.bank import add_bank_audio_from_forward, list_bank_sources
            _srcs = {str(s.get("identifier", "")).lower().lstrip("@")
                     for s in list_bank_sources(conn)
                     if s.get("kind") == "channel" and s.get("enabled")}
            if _fwd_ch.lower().lstrip("@") in _srcs or not _srcs:
                _atitle = (message.audio.title or "").strip()
                _aartist = (message.audio.performer or "").strip()
                if not _atitle and message.caption:
                    _atitle = message.caption.strip().split("\n")[0][:120]
                if _atitle:
                    _bid = add_bank_audio_from_forward(
                        conn, _atitle, _aartist, _fwd_ch,
                        message.audio.file_id)
                    if _bid:
                        log_event(log, "bank_index", stage="forward_indexed",
                                  channel=_fwd_ch[:20], title=_atitle[:40])
    except Exception:
        pass
    current_state = None
    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None
    # --- Smart Remix beta: intercept audio files for the remix flow ---
    if current_state in ("remix:await_file1", "remix:await_file2"):
        handled = await _remix_collect_audio(message, conn, config, state,
                                             current_state)
        if handled:
            return
    if current_state == "audio_id:await_file":
        # The «شناسایی موسیقی از فایل» button armed this wait: the file the
        # user just sent IS the identification request. Clear the wait and
        # fall through to the standard identification flow below (the same
        # engine, result card, full-version-first delivery, and honest
        # fallback states as the automatic path — no parallel logic).
        try:
            await state.clear()
        except Exception:
            pass
        current_state = None
    if current_state == "voice_to_text":
        try:
            await state.clear()
        except Exception:
            pass
        allowed, reason = can_use_feature(conn, message.from_user.id, "voice_to_text")
        if not allowed:
            await message.answer(t(reason, ulang)); return
        await message.answer(t("voice_to_text_transcribing", ulang))
        try:
            media = message.video or message.audio or message.voice or message.document
            file = await message.bot.get_file(media.file_id)
            buf = io.BytesIO()
            await message.bot.download_file(file.file_path, buf)
            data = buf.getvalue()
        except Exception:
            await message.answer(t("voice_to_text_error", ulang)); return
        # Voice-to-text: Whisper (OpenAI) first, Azure STT as fallback.
        # The filename tells Whisper the container (ogg voice / mp4 / mp3).
        if message.voice is not None:
            filename = "voice.ogg"
        elif message.video is not None:
            filename = "video.mp4"
        elif message.audio is not None:
            filename = "audio.mp3"
        else:
            filename = getattr(media, "file_name", None) or "audio.ogg"
        status, text = await _transcribe_voice(data, filename, config, conn)
        if status == "needs_verification":
            await message.answer(adminfeat.GATE_STT_UNVERIFIED); return
        if status == "needs_key":
            await message.answer(adminfeat.GATE_STT_NO_KEY); return
        if status == "empty":
            await message.answer(t("voice_to_text_empty", ulang)); return
        if status != "ok" or not text:
            await message.answer(t("voice_to_text_error", ulang)); return
        await message.answer(f"{t('voice_to_text_result', ulang)}\n{text}")
        return
    allowed, reason = can_use_feature(conn, message.from_user.id, "audio_id")
    if not allowed:
        await message.answer(t(reason, ulang)); return
    # A caption, if present, is also a search query (name / lyric line).
    caption = (message.caption or "").strip()
    # UNIFY FIX (2026-11-09): the temporary «در حال جستجوی موسیقی...»
    # status replaces the old «در حال شناسایی آهنگ...» line and is
    # deleted before every final message below — it is never left
    # behind.
    status_msg = None
    try:
        status_msg = await message.answer(t("m_identify_searching", ulang))
    except Exception:
        status_msg = None
    try:
        file = await message.bot.get_file(
            (message.video or message.audio or message.voice or message.document).file_id)
        buf = io.BytesIO()
        await message.bot.download_file(file.file_path, buf)
        data = buf.getvalue()
    except Exception:
        # A failed download is not an identification error: say so.
        _dl_msg = (t("m_video_fetch_fail", ulang)
                   if message.video is not None else t("audio_error", ulang))
        await _delete_card(status_msg)
        await message.answer(_dl_msg); return
    if len(data) > config.max_file_mb * 1024 * 1024:
        await _delete_card(status_msg)
        await message.answer(t("m_file_too_large", ulang)); return
    # Intent from a film is deterministic (no button needed): identify
    # its audio — Shazam signature first (no token), AudD as fallback —
    # then show one professional card, never a nearest song.
    _media_t_wall = _time.time()
    try:
        result = await _identify_audio(data, "clip.mp4", config, conn)
    except Exception:
        # A crashed identification must not leave the status behind or
        # hang the user: delete it and answer the generic error.
        await _delete_card(status_msg)
        await message.answer(t("audio_error", ulang)); return
    media = message.video or message.audio or message.voice or message.document
    media_kind = ("video" if message.video is not None
                  else "voice" if message.voice is not None
                  else "audio" if message.audio is not None else "document")
    if result.status in ("unconfirmed", "not_found"):
        # NOT confirmed (single source, disagreement, or no match):
        # NO «شناسایی شد» card — ONLY the clip's own audio (video) or
        # the file itself back (audio) with the new caption + Google /
        # retry buttons. The audio-index match/index bookkeeping stays
        # invisible (it never invents a title); its match text is
        # dropped — the identification answer is only the extracted
        # audio. The caption search still answers the caption text.
        if result.status == "not_found":
            matched = find_audio_match(dbmod.list_audio_index(conn), data)
            if matched is None:
                dbmod.index_audio_file(
                    conn, audio_fingerprint(data), title=caption, artist="",
                    channel=_forward_channel(message), caption=caption)
        await _delete_card(status_msg)
        sent = await _send_unconfirmed_media(
            message, conn, data, result.title, result.artist, ulang,
            media_kind=media_kind,
            file_id=media.file_id if media is not None else None,
            user_id=message.from_user.id)
        if sent is not None and media_kind == "video":
            # Step 1b: the clip's own audio IS the final result for
            # this file — store it (10-min TTL) so a later tap on the
            # video button sends it immediately. Audio/voice sends
            # carry NO caption, so storing them would wrongly add the
            # corporate caption on a later tap.
            try:
                _a = getattr(sent, "audio", None)
                _fid = getattr(_a, "file_id", "") or "" if _a else ""
                if _fid:
                    _identify_result_store_send(
                        _identify_result_key(
                            file_id=media.file_id
                            if media is not None else None),
                        result.title, result.artist, result.source,
                        "clip", _fid,
                        duration_sec=getattr(_a, "duration", 0) or 0,
                        status=result.status)
            except Exception:
                pass
        # FINAL IDENTIFY UI (2026-11-09): best-effort video keyboard
        # (a user-sent video's markup cannot be edited — the edit
        # silently fails there; see _set_video_result_kb).
        await _set_video_result_kb(
            message, result.title, result.artist, ulang,
            media.file_id if media is not None else None,
            message.from_user.id, media_kind, result.source)
        if sent is None:
            # Extraction impossible: the honest line, with the single
            # retry button (no bank button).
            _kb = _extracted_audio_kb(
                result.title, result.artist, ulang,
                file_id=media.file_id if media is not None else None,
                user_id=message.from_user.id, kind="audio")
            await _answer(message, conn, t("audio_not_found", ulang),
                          reply_markup=_kb)
        if caption:
            await _do_search(message, conn, sanitize_text(caption, 300), config=config)
        return
    if result.status != "identified":
        await _delete_card(status_msg)
        _err_kb = None
        if message.video is not None:
            _err_kb = _video_identify_kb(
                message.from_user.id, message.video.file_id, "file")
        elif message.audio is not None or message.voice is not None:
            _media = message.audio or message.voice
            _err_kb = _video_identify_kb(
                message.from_user.id, _media.file_id, "audio",
                label=t("m_retry", ulang))
        if getattr(result, "detail", "") == "lookup_error":
            # Unconfirmed (lookup failed): the extracted audio + new
            # caption + buttons, no error text — the name was not
            # confirmed, so there is no contradiction to state.
            # Extraction impossible falls through to the honest error
            # line below.
            sent = await _send_unconfirmed_media(
                message, conn, data, result.title, result.artist, ulang,
                media_kind=media_kind,
                file_id=media.file_id if media is not None else None,
                user_id=message.from_user.id)
            if sent is not None:
                await _set_video_result_kb(
                    message, result.title, result.artist, ulang,
                    media.file_id if media is not None else None,
                    message.from_user.id, media_kind, result.source)
                return
        # Distinct honest cause for video inputs (user order 2026-11-07):
        # the generic "audio_error" line must never hide the real cause.
        # The m_* keys word the cause for a video; audio/voice inputs
        # keep the generic line (their wording differs).
        _err_msg = t("audio_error", ulang)
        if message.video is not None:
            _err_msg = {
                "silent": t("m_no_usable_audio", ulang),
                "undecodable": t("m_audio_corrupt", ulang),
                "too_short": t("m_audio_too_short", ulang),
                "lookup_error": t("m_identify_error", ulang),
            }.get(getattr(result, "detail", ""), _err_msg)
        await _answer(message, conn, _err_msg, reply_markup=_err_kb)
        return
    # Bank sample is armed AFTER the call, inside _send_identified_song
    # (unify fix: no instant card — the background delivery owns the
    # temporary status and deletes it when the ONE final message is
    # sent; a failed spawn deletes it here).
    _spawned = await _send_identified_song(
        message, conn, result.title, result.artist, config,
        bot_id=dbmod.get_bot_id(conn, config),
        own_media=(media_kind, media.file_id),
        arm=("file", __import__("hashlib").sha256(data).hexdigest(),
             data, "", caption),
        source=result.source, status_msg=status_msg,
        cache_key=_identify_result_key(
            file_id=media.file_id if media is not None else None),
        t_wall=_media_t_wall)
    if not _spawned:
        await _delete_card(status_msg)
    # FINAL IDENTIFY UI (2026-11-09): best-effort video keyboard (a
    # user-sent video's markup cannot be edited — the edit silently
    # fails there; see _set_video_result_kb).
    await _set_video_result_kb(
        message, result.title, result.artist, ulang,
        media.file_id if media is not None else None,
        message.from_user.id, media_kind, result.source)



async def _run_suggest(message, conn, text: str, config):
    """Mood suggestion: real catalog results only, never invented.

    Long operation -> ONE progress status, dismissed (best effort) as
    soon as the result/error line lands, so the chat never stacks
    duplicate "please wait" messages.
    """
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    allowed, reason = can_use_feature(conn, message.from_user.id, "suggest")
    if not allowed:
        await _answer(message, conn, t(reason, ulang)); return
    status_msg = await message.answer(t("suggest_searching", ulang))
    try:
        await _run_suggest_body(message, conn, text, config)
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass


async def _run_suggest_body(message, conn, text: str, config):
    """Mood suggestion: real catalog results only, never invented."""
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    allowed, reason = can_use_feature(conn, message.from_user.id, "suggest")
    if not allowed:
        await _answer(message, conn, t(reason, ulang)); return
    engine = SearchEngine([ITunesProvider(entity="song"), DeezerProvider()])

    async def _cat(q):
        r, _f = await engine.search(q, limit=10)
        return r
    try:
        mood, results = await suggest_songs(text, _cat)
    except Exception:
        await _answer(message, conn, brief_error("suggest", ulang)); return
    if not results:
        # The audience taps «درخواست آهنگ» to request a SONG by name/artist,
        # not only a mood sentence. A plain catalog search on the text
        # itself runs before giving up (fix 2026-11-05 — before this, a
        # song-name request always ended in «m_suggest_none»).
        try:
            results = await _cat(text)
            mood = text
        except Exception:
            results = []
    if not results:
        await _answer(message, conn, t("m_suggest_none", ulang))
        return
    dbmod.record_search(conn, message.from_user.id, text, source="suggest", ok=True)
    # 3–5 genuinely relevant suggestions, deduplicated, each with its OWN
    # button to get that song. Never inflated: if fewer valid results
    # exist, show exactly those and say so honestly.
    seen: set[tuple[str, str]] = set()
    uniq = []
    for r in results:
        k = ((r.title or "").strip().lower(), (r.artist or "").strip().lower())
        if k[0] and k not in seen:
            seen.add(k)
            uniq.append(r)

    # Filter: only songs with a full version available (user req 2026-11-01).
    # BUG fixed 2026-11-05: the old code called find_full_song_file(...,
    # timeout=8.0) — but that function takes NO timeout arg AND returns no
    # "url" key, so EVERY call raised TypeError (swallowed by the except)
    # and the filter NEVER ran. Now we use the SAME parallel lookup the
    # delivery path uses (find_full_song_file_v2), checked in parallel
    # with an 8s cap per song — fast and consistent with delivery.
    try:
        import asyncio as _aio
        from app.search.adapters import find_full_song_file_v2

        async def _has_full(r):
            try:
                res = await _aio.wait_for(
                    find_full_song_file_v2(r.artist or "", r.title or ""),
                    timeout=8.0)
                return r if res else None
            except Exception:
                return None

        _checked = await _aio.gather(*[_has_full(r) for r in uniq])
        full_only = [r for r in _checked if r is not None]
        # Use full-version songs if we found any, otherwise fall back to all
        # (better to show something than nothing, but prefer full versions)
        if full_only:
            uniq = full_only
    except Exception:
        pass

    picks, rest = uniq[:5], uniq[5:]
    more_tok = secrets.token_hex(6) if rest else ""
    if more_tok:
        _SUGGEST_MORE[more_tok] = rest
    kb_rows: list[list[InlineKeyboardButton]] = []
    lines = [t("h_suggest_head", ulang).format(n=len(picks), mood=mood)]
    for i, r in enumerate(picks, 1):
        key = profeat.remember_result(r.title, r.artist, r.url, r.preview_url)
        info = f"{r.title} – {r.artist}" if r.artist else r.title
        extra = ""
        try:
            if getattr(r, "duration", None):
                d = int(r.duration)
                extra = f" · {d // 60}:{d % 60:02d}"
        except (TypeError, ValueError):
            extra = ""
        lines.append(f"{i}. {info}{extra}")
        label = f"▶️ {r.title[:28]}" + (f" – {r.artist[:20]}" if r.artist else "")
        kb_rows.append([InlineKeyboardButton(text=label,
                                            callback_data=f"sg:get:{key}")])
    extra_row = [InlineKeyboardButton(text=t("m_similar_songs", ulang),
                                      callback_data=_cb_query(text))]
    if more_tok:
        extra_row.append(InlineKeyboardButton(text=t("m_more_suggestions", ulang),
                                              callback_data=f"sg:more:{more_tok}"))
    extra_row.append(InlineKeyboardButton(text=t("m_research", ulang),
                                          callback_data="sg:retry"))
    kb_rows.append(extra_row)

    # Banner removed per user request 2026-11-01 (Telegram Bot API does not
    # support floating UI banners; user chose to remove instead of message-based).
    await _answer(message, conn, "\n".join(lines),
                  reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


_SUGGEST_MORE: dict[str, list] = {}


@router.callback_query(F.data.startswith("sg:get:"))
async def suggest_get(callback, conn: sqlite3.Connection, config: Config):
    """One suggestion's own button: deliver that exact song (full-version-first).

    The user must end with a real audio file or a precise reason — never a
    bare song name and never a silent death if the delivery raises.
    """
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    key = callback.data.split("sg:get:", 1)[1]
    res = profeat.get_result(key)
    if not res:
        # In-memory result keys die on restart: honest expiry, not a no-op.
        await callback.answer(
            t("m_suggest_expired", ulang),
            show_alert=True)
        return
    await callback.answer()
    # UNIFY FIX (2026-11-09): the shared delivery no longer sends an
    # instant card — the temporary status stays until the ONE final
    # message is sent, so the tap is still acknowledged at once.
    status_msg = None
    try:
        status_msg = await callback.message.answer(
            t("m_identify_searching", ulang))
    except Exception:
        status_msg = None
    try:
        _spawned = await _send_professional_song_card(
            callback.message, conn, res["title"], res.get("artist", ""),
            bot_id=dbmod.get_bot_id(conn, config),
            config=config, exclusive_check=False, status_msg=status_msg)
        if not _spawned:
            await _delete_card(status_msg)
    except Exception:
        await _delete_card(status_msg)
        await callback.message.answer(
            t("m_suggest_send_fail", ulang))


@router.callback_query(F.data.startswith("sg:more:"))
async def suggest_more(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    tok = callback.data.split("sg:more:", 1)[1]
    rest = _SUGGEST_MORE.get(tok) or []
    if not rest:
        await callback.answer(t("m_no_more_suggest", ulang), show_alert=True); return
    picks, remaining = rest[:5], rest[5:]
    if remaining:
        _SUGGEST_MORE[tok] = remaining
    else:
        _SUGGEST_MORE.pop(tok, None)
    kb_rows: list[list[InlineKeyboardButton]] = []
    lines = [t("h_suggest_more_head", ulang).format(n=len(picks))]
    for i, r in enumerate(picks, 1):
        key = profeat.remember_result(r.title, r.artist, r.url, r.preview_url)
        info = f"{r.title} – {r.artist}" if r.artist else r.title
        lines.append(f"{i}. {info}")
        label = f"▶️ {r.title[:28]}" + (f" – {r.artist[:20]}" if r.artist else "")
        kb_rows.append([InlineKeyboardButton(text=label,
                                            callback_data=f"sg:get:{key}")])
    if remaining:
        kb_rows.append([InlineKeyboardButton(text=t("m_more_suggestions", ulang),
                                            callback_data=f"sg:more:{tok}")])
    await callback.answer()
    await callback.message.answer("\n".join(lines),
                                  reply_markup=InlineKeyboardMarkup(
                                      inline_keyboard=kb_rows))


@router.callback_query(F.data == "sg:retry")
async def suggest_retry(callback, conn: sqlite3.Connection, config: Config,
                        state: FSMContext):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    await state.set_state("suggest")
    await callback.answer()
    # In-bot cancel: «🔄 جستجوی مجدد» re-arms the suggest state, so the
    # prompt must offer the same ❌ لغو the other suggest entries have.
    await callback.message.answer(t("suggest_prompt", ulang),
                                  reply_markup=_cancel_kb(ulang))


def _lyrics_picker_kb(page: int, key: str) -> InlineKeyboardMarkup:
    rows, page, total = language_picker_rows(page, key)
    kb_rows = [[InlineKeyboardButton(text=r[0]["text"],
                                     callback_data=f"tl:{r[0]['code']}:{key}")]
               for r in rows]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"lpg:{page - 1}:{key}"))
    if page < total - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"lpg:{page + 1}:{key}"))
    if nav:
        kb_rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def _lyrics_text_for(key: str, conn) -> tuple[str, str, str] | None:
    """(title, artist, lyrics) for a card key: cache first, then fresh fetch."""
    cached = LYRICS_CACHE.get(key)
    if cached:
        return cached
    result = profeat.get_result(key)
    if not result:
        return None
    try:
        lyrics, _source, _responded = await fetch_lyrics_with_source(
            result["title"], result.get("artist", ""))
    except Exception:
        lyrics = None
    if not lyrics:
        return None
    _lyrics_cache_put(key, (result["title"], result.get("artist", ""), lyrics))
    return LYRICS_CACHE[key]


@router.callback_query(F.data.startswith("ly:"))
async def lyrics_callback(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    key = callback.data[3:]
    result = profeat.get_result(key)
    if result is None:
        await callback.answer(brief_error("lyrics", ulang), show_alert=True); return
    await callback.answer()
    allowed, reason = can_use_feature(conn, callback.from_user.id, "lyrics")
    if not allowed:
        await callback.message.answer(t(reason, ulang)); return
    try:
        lyrics, source, responded = await fetch_lyrics_with_source(
            result["title"], result.get("artist", ""))
    except Exception:
        lyrics, source, responded = None, "", False
    if not lyrics:
        if not responded:
            await callback.message.answer(t("m_lyrics_src_down", ulang))
        else:
            q = result["title"] + (f" - {result['artist']}" if result.get("artist") else "")
            await callback.message.answer(lyrics_not_found_text(q))
        return
    _lyrics_cache_put(key, (result["title"], result.get("artist", ""), lyrics))
    kb = _lyrics_card_kb(key, result["title"], result.get("artist", ""),
                         lyrics, conn, config, ulang)
    header = t("h_lyrics_head", ulang).format(
        title=result["title"],
        artist_part=t("h_lyrics_artist_part", ulang).format(artist=result["artist"])
        if result.get("artist") else "")
    src = t("h_lyrics_source", ulang).format(source=source) if source else ""
    # The lyrics are sent in the song's ORIGINAL language first and never
    # changed; the translation comes separately below the buttons.
    sent = await callback.message.answer(f"{header}\n\n{lyrics[:3500]}{src}", reply_markup=kb)
    try:
        adminfeat.track(conn, callback.from_user.id, callback.message.chat.id, sent.message_id)
    except Exception:
        pass


@router.callback_query(F.data.startswith("dl:full:"))
async def download_full_callback(callback, conn: sqlite3.Connection, config: Config):
    """Download full version on demand (user taps «⬇️ دانلود نسخه کامل»).

    No auto-download: the user explicitly requests the file. Uses the
    in-memory fullsong cache for instant repeat delivery. If no full
    version is found, falls back to the extracted audio from the user's
    own video (own_media) — the song itself, not just "not found".

    Decoupled (2026-11-08): the callback is answered at once and the
    lookup runs in a background task, guarded by the same one-in-flight
    rule as the automatic delivery — a tap while the file is already
    being delivered never starts a second lookup or a second send.
    """
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    key = callback.data[8:]
    result = profeat.get_result(key)
    if result is None:
        await callback.answer(t("m_req_expired", ulang), show_alert=True)
        return
    await callback.answer(t("m_downloading", ulang))
    title = result["title"]
    artist = result.get("artist", "")
    _chat_id = getattr(getattr(callback.message, "chat", None), "id", None)
    _spawn_fullsong_task(
        _fullsong_key(_chat_id, title, artist),
        lambda: _download_full_bg(callback, conn, config, title, artist,
                                  result))
    return


async def _download_full_bg(callback, conn, config, title, artist, result):
    """Background body of download_full_callback (moved verbatim):
    full-version lookup, then the extracted-audio fallback, then the
    honest no-full message. Fire-and-forget: never raises."""
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    try:
        _verify_failed = False
        try:
            from ..search.adapters import find_full_song_file_v2
            full = await find_full_song_file_v2(artist, title)
        except Exception:
            full = None
        if full and full.get("data"):
            # IDENTITY GUARD (2026-11-09 mismatch fix): same rule as
            # _deliver_fullsong_bg — the file's own tags must match the
            # identify result; a mismatched/unverifiable file is never
            # sent and the corporate clip-audio fallback runs instead.
            try:
                from ..search.fullsong_verify import verify_fullsong_file
                _v = await asyncio.to_thread(
                    verify_fullsong_file, full["data"], full.get("ext") or "mp3",
                    title, artist)
            except Exception:
                _v = {"status": "unverifiable", "file_title": "",
                      "file_artist": "", "duration_sec": 0,
                      "detail": "verify_error"}
            if _v["status"] == "match":
                if _v["duration_sec"] > 0:
                    full["duration_sec"] = _v["duration_sec"]
            else:
                log_event(log, "fullsong",
                          stage="fullsong_mismatch" if _v["status"] == "mismatch"
                          else "fullsong_unverifiable",
                          title=title[:40], source=full.get("source", "")[:40],
                          file_title=_v["file_title"][:40],
                          file_artist=_v["file_artist"][:40])
                full = None
                _verify_failed = True
        if full and full.get("data"):
            try:
                # CONFIRM FIX (2026-11-09): the file replaces the card
                # it was requested from — one final message with the
                # card text as caption + the card's buttons, then the
                # card is deleted. (No button creates dl:full callbacks
                # today, so this path is unreachable in production; the
                # replacement is implemented for consistency.)
                # FINAL IDENTIFY UI (2026-11-09): the full song carries
                # exactly one row — playlist + lyrics. No heart, no
                # wrong-song, no other buttons.
                _key = callback.data[8:]
                _dl_kb = InlineKeyboardMarkup(inline_keyboard=
                    [_identified_audio_row(_key, ulang)])
                sent = await callback.message.answer_audio(
                    BufferedInputFile(full["data"], filename=f"{title}.{full['ext']}"),
                    title=title, performer=artist,
                    duration=int(full["duration_sec"]) if full.get("duration_sec") else None,
                    caption=_final_audio_caption(
                        title, artist, ulang,
                        duration_sec=full.get("duration_sec") or 0),
                    reply_markup=_dl_kb)
                if sent is not None:
                    try:
                        adminfeat.track(conn, callback.from_user.id,
                                        callback.message.chat.id, sent.message_id)
                    except Exception:
                        pass
                    await _delete_card(callback.message)
                return
            except Exception:
                pass
        # IDENTITY CHECK FAILED: the candidate file was not the
        # identified song. Send the clip's ORIGINAL audio (stream copy)
        # with the new caption + the single retry button — never the wrong
        # file, never a re-encode labelled as the song. If the clip is
        # unavailable, fall through to the honest no-full message below.
        if _verify_failed:
            own_media = result.get("own_media")
            if own_media:
                media_type, file_id = own_media
                if media_type == "video":
                    try:
                        bot = callback.bot
                        tg_file = await bot.get_file(file_id)
                        buf = io.BytesIO()
                        await bot.download_file(tg_file.file_path, buf)
                        sent = await _extract_and_send_clip_audio_copy(
                            callback.message, conn, buf.getvalue(),
                            t("m_extracted_audio_new", ulang), ulang,
                            track_user_id=callback.from_user.id,
                            reply_markup=_extracted_audio_kb(
                                title, artist, ulang, file_id=file_id,
                                user_id=callback.from_user.id, kind="file"),
                            caption_key="m_extracted_audio_new")
                        if sent is not None:
                            return
                    except Exception:
                        pass
        # Fallback: send the extracted audio from the user's own video
        # (the song itself, labelled as extracted — not a fake "full version").
        own_media = result.get("own_media")
        if own_media and not _verify_failed:
            media_type, file_id = own_media
            try:
                import io as _io
                import subprocess as _sp
                import tempfile as _tf
                import os as _os
                bot = callback.bot
                tg_file = await bot.get_file(file_id)
                # Download the video file into memory
                buf = _io.BytesIO()
                await bot.download_file(tg_file.file_path, buf)
                video_bytes = buf.getvalue()
                if video_bytes:
                    # Extract audio with ffmpeg (available on Render)
                    with _tf.NamedTemporaryFile(suffix=".mp4", delete=False) as vf:
                        vf.write(video_bytes)
                        vpath = vf.name
                    apath = vpath + ".m4a"
                    try:
                        _timing.run(["ffmpeg", "-y", "-i", vpath, "-vn",
                                     "-c:a", "aac", "-b:a", "128k", apath],
                                    logger=log,
                                    capture_output=True, timeout=30)
                        if _os.path.exists(apath) and _os.path.getsize(apath) > 0:
                            with open(apath, "rb") as af:
                                audio_bytes = af.read()
                            # Extracted audio: Avano's title + performer,
                            # real probed duration, new caption + the single retry button.
                            _ex_kb = _extracted_audio_kb(
                                title, artist, ulang, file_id=file_id,
                                user_id=callback.from_user.id, kind="file")
                            _ex_dur = 0.0
                            try:
                                from ..search.fullsong_verify import probe_file_identity as _pfi
                                _ex_dur = _pfi(audio_bytes, "m4a")[2]
                            except Exception:
                                _ex_dur = 0.0
                            sent = await callback.message.answer_audio(
                                BufferedInputFile(audio_bytes,
                                                  filename=f"{title}.m4a"),
                                title=t("m_extracted_audio_title", ulang),
                                performer="Avano",
                                duration=int(_ex_dur) if _ex_dur > 0 else None,
                                caption=t("m_extracted_audio_new", ulang),
                                reply_markup=_ex_kb)
                            if sent is not None:
                                try:
                                    log_event(log, "extract",
                                              stage="sent_extracted_audio",
                                              bytes=len(audio_bytes),
                                              caption_key="m_extracted_audio_new",
                                              buttons=_keyboard_button_texts(_ex_kb))
                                except Exception:
                                    pass
                                try:
                                    adminfeat.track(conn, callback.from_user.id,
                                                    callback.message.chat.id,
                                                    sent.message_id)
                                except Exception:
                                    pass
                            return
                    finally:
                        try:
                            _os.unlink(vpath)
                        except Exception:
                            pass
                        try:
                            _os.unlink(apath)
                        except Exception:
                            pass
            except Exception:
                pass
        await callback.message.answer(t("m_identified_no_full", ulang).format(
            title=title, artist=artist))
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


_COPY_TEXT_LIMIT = 240  # Bot API CopyTextButton.text allows 1-256 chars


def _copy_button(label: str, text_to_copy: str,
                 callback_data: str) -> InlineKeyboardButton:
    """One-click copy button.

    When the text fits the Bot API ``copy_text`` limit the button copies
    it to the clipboard with a single tap — no second step. Longer texts
    fall back to a callback that sends a long-press-to-copy message; the
    bot never claims a copy happened unless it really did.
    """
    if text_to_copy and len(text_to_copy) <= _COPY_TEXT_LIMIT:
        return InlineKeyboardButton(
            text=label, copy_text=CopyTextButton(text=text_to_copy))
    return InlineKeyboardButton(text=label, callback_data=callback_data)


def _lyrics_share_text(title: str, artist: str, lyrics: str,
                       conn, config) -> str:
    """Copy/share version of lyrics: bot ID appended, chat text untouched."""
    header = (title or "") + (f" – {artist}" if artist else "")
    return attach_bot_id(f"{header}\n\n{(lyrics or '')[:3000]}",
                         dbmod.get_bot_id(conn, config))


def _lyrics_card_kb(key: str, title: str, artist: str, lyrics: str,
                    conn, config, lang: str = "fa") -> InlineKeyboardMarkup:
    share = _lyrics_share_text(title, artist, lyrics, conn, config)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("m_translate_btn", lang), callback_data=f"tr:{key}"),
        _copy_button(t("m_copy_lyrics", lang), share, f"cp:{key}"),
    ]])


@router.callback_query(F.data.startswith("tr:"))
async def translate_prompt_callback(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    key = callback.data[3:]
    if profeat.get_result(key) is None and key not in LYRICS_CACHE:
        await callback.answer(brief_error("translate", ulang), show_alert=True); return
    await callback.answer()
    await callback.message.answer(t("m_translate_to", ulang),
                                  reply_markup=_lyrics_picker_kb(0, key))


@router.callback_query(F.data.startswith("lpg:"))
async def translate_page_callback(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.answer(brief_error("translate", ulang), show_alert=True); return
    _p, _l, page, key = parts
    try:
        page_n = int(page)
    except ValueError:
        page_n = 0
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=_lyrics_picker_kb(page_n, key))
    except Exception:
        await callback.message.answer(t("m_translate_to", ulang), reply_markup=_lyrics_picker_kb(page_n, key))


@router.callback_query(F.data.startswith("tl:"))
async def translate_callback(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer(brief_error("translate", ulang), show_alert=True); return
    _tl, lang, key = parts
    if lang not in LANG_NAMES:
        await callback.answer(t("m_lang_unsupported", ulang), show_alert=True); return
    await callback.answer()
    got = await _lyrics_text_for(key, conn)
    if got is None:
        await callback.message.answer(brief_error("lyrics", ulang)); return
    title, artist, lyrics = got
    notice = same_language_notice(lyrics, lang)
    if notice:
        await callback.message.answer(notice); return
    await callback.message.answer(t("h_translating_to", ulang).format(lang=LANG_NAMES.get(lang, lang)))
    try:
        translated = await translate_text(lyrics, lang)
    except Exception:
        translated = None
    if not translated:
        await callback.message.answer(t("m_translate_not_found", ulang)); return
    TRANSLATIONS[(key, lang)] = translated
    _share = attach_bot_id(translated[:3000], dbmod.get_bot_id(conn, config))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        _copy_button(t("m_copy_translation", ulang), _share, f"ltc:{key}:{lang}"),
    ]])
    header = t("h_translate_head", ulang).format(
        lang=LANG_NAMES.get(lang, lang), title=title,
        artist_part=t("h_lyrics_artist_part", ulang).format(artist=artist) if artist else "")
    # The original text above is never changed; the translation is separate.
    await callback.message.answer(f"{header}\n\n{translated[:3500]}", reply_markup=kb)


@router.callback_query(F.data.startswith("ltc:"))
async def translate_copy_callback(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.answer(brief_error("translate", ulang), show_alert=True); return
    _p, _c, key, lang = parts
    translated = TRANSLATIONS.get((key, lang))
    if not translated:
        await callback.answer(brief_error("translate", ulang), show_alert=True); return
    await callback.answer()
    # Honest long-press copy: bots have no clipboard API for long texts,
    # so a copy is never claimed unless the copy_text button did it.
    shared = attach_bot_id(translated[:3000], dbmod.get_bot_id(conn, config))
    await callback.message.answer(t("h_copy_hint", ulang).format(text=shared), parse_mode="Markdown")


@router.callback_query(F.data.startswith("cp:"))
async def copy_callback(callback, conn: sqlite3.Connection, config: Config):
    ulang = _user_lang(conn, getattr(getattr(callback, "from_user", None), "id", None))
    key = callback.data[3:]
    got = await _lyrics_text_for(key, conn)
    if got is None:
        await callback.answer(brief_error("lyrics", ulang), show_alert=True); return
    await callback.answer()
    title, artist, lyrics = got
    # Honest long-press copy: bots have no clipboard API for long texts,
    # so a copy is never claimed unless the copy_text button did it.
    shared = _lyrics_share_text(title, artist, lyrics, conn, config)
    await callback.message.answer(
        f"📋 برای کپی، متن زیر را نگه دارید:\n```\n{shared}\n```", parse_mode="Markdown")


@router.callback_query(F.data.startswith("similar:"))
async def similar_callback(callback, conn: sqlite3.Connection, config: Config):
    query = callback.data.split(":", 1)[1]
    await callback.answer()
    short = " ".join(query.split()[:3])
    await _do_search(callback.message, conn, short, include_video=False, config=config)


async def _run_lyrics_search(message: Message, conn, text: str, config):
    """Lyrics by name: original language first, honest states."""
    ulang = _user_lang(conn, getattr(getattr(message, "from_user", None), "id", None))
    allowed, reason = can_use_feature(conn, message.from_user.id, "lyrics")
    if not allowed:
        await _answer(message, conn, t(reason, ulang)); return
    query = sanitize_text(text, 200)
    title, artist = query, ""
    for sep in (" - ", " – ", " — ", "-"):
        if sep in query:
            left, _, right = query.partition(sep)
            if left.strip() and right.strip():
                artist, title = left.strip(), right.strip()
                break
    dbmod.record_search(conn, message.from_user.id, query, source="lyrics", ok=True)
    _lyr_searching = None
    try:
        _lyr_searching = await _answer(message, conn, t("searching", ulang))
    except Exception:
        pass

    async def _del_lyr_searching():
        if _lyr_searching is not None:
            try:
                await _lyr_searching.delete()
            except Exception:
                pass
    try:
        lyrics, source, responded = await fetch_lyrics_with_source(title, artist)
    except Exception:
        lyrics, source, responded = None, "", False
    if not lyrics:
        if not responded:
            await _answer(message, conn, t("m_lyrics_src_down", ulang))
        else:
            await _answer(message, conn, lyrics_not_found_text(query))
        await _del_lyr_searching()
        return
    key = profeat.remember_result(title, artist, "", "")
    _lyrics_cache_put(key, (title, artist, lyrics))
    kb = _lyrics_card_kb(key, title, artist, lyrics, conn, config, ulang)
    header = f"📝 متن آهنگ «{title}»" + (f" — {artist}" if artist else "")
    src = f"\nمنبع: {source}" if source else ""
    await _answer(message, conn, f"{header}\n\n{lyrics[:3500]}{src}", reply_markup=kb)
    await _del_lyr_searching()


def build_dispatcher(conn: sqlite3.Connection, config: Config) -> Dispatcher:
    dp = Dispatcher()
    dp.workflow_data.update(conn=conn, config=config)
    # Feature routers first: their state-filtered handlers win over the
    # catch-all text/media handlers of the main router.
    features_router = Router()
    profeat.register(features_router)
    dlfeat.register(features_router)
    bankfeat.register(features_router)
    adminfeat.register(features_router)
    dp.include_router(features_router)
    dp.include_router(router)
    return dp


def _install_ad_hook(bot, conn, config: Config) -> None:
    """Attach the active audience ad after every Nth text answer.

    Wraps ``bot.send_message`` (the choke point every ``message.answer``
    funnels through, in handlers and feature modules alike). Never fires
    for the owner; the ad itself is sent through the original methods so
    it is never counted and never recurses. Best effort: ad failures must
    never break the real answer.
    """
    from .ads import ad_due, pick_active_ad, send_ad

    _orig_send_message = bot.send_message
    _orig_send_photo = bot.send_photo

    async def send_message_with_ads(chat_id, text, *args, **kwargs):
        sent = await _orig_send_message(chat_id, text, *args, **kwargs)
        try:
            uid = chat_id if isinstance(chat_id, int) else None
            if uid is None:
                return sent
            if config.owner_id is not None and uid == config.owner_id:
                return sent
            if ad_due(conn, uid):
                ad = pick_active_ad(conn)
                if ad:
                    await send_ad(bot, chat_id, ad,
                                  _send_message=_orig_send_message,
                                  _send_photo=_orig_send_photo)
        except Exception as exc:
            log.warning("ad hook failed: %s", exc)
        return sent

    bot.send_message = send_message_with_ads  # type: ignore[method-assign]


def _user_commands() -> list[BotCommand]:
    """Command menu for regular users: no admin entry points visible."""
    return [
        BotCommand(command="start", description="شروع"),
        BotCommand(command="help", description="راهنما"),
    ]


def _owner_commands() -> list[BotCommand]:
    """Command menu for the owner: the user menu plus the admin panel."""
    return _user_commands() + [
        BotCommand(command="admin", description="پنل مدیریت"),
    ]


class _BlockedGate:
    """aiogram update outer middleware (P1-16): a blocked user is
    refused on EVERY callback button and message BEFORE any handler
    runs — one enforcement point for the main router and all feature
    routers (admin/pro/bank/downloader share this dispatcher).

    The decision comes from the in-memory blocked set
    (app.bot.blocked_cache) — NO database and NO Turso query in the
    waiting path. The owner is never gated. Never raises: on any
    error the update passes through to the handlers, whose own
    _blocked() checks stay as a second layer.
    """
    def __init__(self, owner_id):
        self._owner_id = owner_id

    async def __call__(self, handler, event, data):
        try:
            uid = getattr(getattr(event, "from_user", None), "id", None)
            if uid is None:
                return await handler(event, data)
            if self._owner_id is not None and uid == self._owner_id:
                return await handler(event, data)
            if not blocked_cache.is_blocked(uid):
                return await handler(event, data)
        except Exception:
            return await handler(event, data)
        # Blocked: answer the refusal and swallow the update.
        try:
            conn = data.get("conn") if isinstance(data, dict) else None
            lang = dbmod.user_lang_or_default(conn, uid) if conn is not None else "fa"
        except Exception:
            lang = "fa"
        try:
            from aiogram.types import CallbackQuery, Message
            if isinstance(event, CallbackQuery):
                await event.answer(t("blocked", lang), show_alert=True)
            elif isinstance(event, Message):
                await event.answer(t("blocked", lang))
        except Exception:
            pass
        return None


class _UpdateTracker:
    """aiogram outer middleware: records timestamp of every update.

    Used by the health monitor to know the bot is receiving Telegram
    updates. Never raises.
    """
    def __init__(self, health: dict):
        self._health = health

    async def __call__(self, handler, event, data):
        try:
            import time as _t
            self._health["last_update"] = _t.time()
        except Exception:
            pass
        return await handler(event, data)


async def run_bot(config: Config) -> None:
    from ..db import init_db

    if not config.has_token:
        print("خطا: BOT_TOKEN تنظیم نشده است. فایل .env را از روی .env.example بسازید و توکن BotFather را وارد کنید.")
        # Fail loudly: a silent return makes main() exit 0 and Render
        # reports the service Live while the bot is dead.
        raise SystemExit(2)
    conn = init_db(config.db_path)
    # P1-14: restore user-owned rows (users incl. blocked flags,
    # playlists, favourites, language, settings, features, ads) from
    # Turso into the fresh local DB — Render wipes bot.db on every
    # restart, and this is what makes the data (and the admin user
    # count) survive it. Runs once, before polling starts, so no user
    # waits on it. Best effort: a Turso failure leaves local as-is.
    try:
        _restored = dbmod.restore_from_turso(conn)
        log_event(log, "turso_user_restore", **{k: v for k, v in _restored.items()})
    except Exception as exc:
        log.warning("turso user restore failed: %s", exc)
    # P1-16: populate the in-memory blocked set before polling.
    try:
        blocked_cache.refresh(conn)
    except Exception:
        pass
    # Web admin panel runs alongside polling, only with a token set.
    web = None
    if config.web_admin_enabled and config.web_admin_token:
        from ..webpanel import start_web_panel
        web = start_web_panel(config.db_path, config)
    from aiogram import Bot
    bot = Bot(token=config.bot_token)
    # Command menu (the user-facing Menu button): regular users see only
    # start/help — /admin is never advertised to them. The owner gets the
    # same menu plus /admin, scoped to their chat only. Server-side denial
    # in cmd_admin stays as the second layer (hiding alone is not enough).
    try:
        await bot.set_my_commands(_user_commands())
        if config.owner_id is not None:
            from aiogram.types import BotCommandScopeChat
            await bot.set_my_commands(
                _owner_commands(),
                scope=BotCommandScopeChat(chat_id=config.owner_id))
    except Exception as exc:
        log.warning("set_my_commands failed: %s", exc)
    _install_ad_hook(bot, conn, config)
    dp = build_dispatcher(conn, config)
    log_event(log, "bot_start", **config.safe_summary())
    # Timing instrumentation (logs only): one process_start line per
    # process lifetime, so cold starts / restarts are visible in the
    # logs (uptime_seconds on every request total is measured from it).
    log_event(log, "process_start",
              started_at=_time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        _time.gmtime(_timing.PROCESS_START)),
              cpu_count=_timing.cpu_count(),
              max_rss_kb=_timing.max_rss_kb())
    # Daily auto-indexer: DISABLED (2026-11-01) - it was blocking the event
    # loop on Render's 0.1 CPU box, causing the bot to stop responding to
    # Telegram updates. The indexer uses synchronous requests which starve
    # the polling loop. Re-enable only with a proper non-blocking design
    # (separate process or thread pool).
    # async def _daily_indexer():
    #     import asyncio as _aio
    #     await _aio.sleep(300)  # 5 min after startup
    #     while True:
    #         try:
    #             from ..jobs.daily_indexer import run_indexer
    #             await run_indexer(config.db_path)
    #         except Exception as exc:
    #             # Never crash the bot - just log and continue
    #             try:
    #                 log.warning("daily_indexer failed: %s", exc)
    #             except:
    #                 pass
    #         await _aio.sleep(24 * 3600)  # Every 24h
    # try:
    #     import asyncio as _asyncio_outer
    #     _indexer_task = _asyncio_outer.create_task(_daily_indexer())
    # except Exception:
    #     pass  # If task creation fails, bot still runs
    import asyncio as _asyncio_outer  # noqa: F401 (kept for watchdog below)

    # --- Polling watchdog (root fix for "zombie" instances) ---
    # After a deploy, the new instance must actually start polling Telegram.
    # If polling silently fails to start, the process would sit alive but
    # never respond (the "frozen bot" symptom). This watchdog verifies that
    # polling really began; if not, it exits non-zero so Render restarts
    # the service instead of leaving a zombie.
    _polling_started = _asyncio_outer.Event()

    async def _on_polling_startup(*_a, **_k):
        _polling_started.set()
        log_event(log, "polling_started", bot_id=getattr(bot, "id", "?"))

    try:
        dp.startup.register(_on_polling_startup)
    except Exception:
        pass

    async def _polling_watchdog():
        try:
            await _asyncio_outer.wait_for(_polling_started.wait(),
                                          timeout=90)
            log_event(log, "polling_watchdog_ok")
        except _asyncio_outer.TimeoutError:
            log_event(log, "polling_watchdog_fail",
                      error="polling did not start within 90s, exiting")
            # Force a non-zero exit so Render restarts the service.
            import os as _os
            _os._exit(42)

    _watchdog_task = _asyncio_outer.create_task(_polling_watchdog())

    # --- Continuous polling health monitor ---
    # The startup watchdog only verifies polling BEGAN. This monitor tracks
    # ongoing health: it records the timestamp of every received Telegram
    # update. If no update AND no activity is seen for too long while the
    # process is alive, the event loop is likely blocked (e.g. by a sync
    # operation) and we exit so Render restarts us.
    # Note: long quiet periods are NORMAL for a bot (no users = no updates).
    # So we only trigger when the loop itself appears blocked: we check
    # that our own heartbeat task is running on schedule. If the heartbeat
    # is delayed by more than 5 minutes, the loop is blocked.
    _health = {"last_heartbeat": _time.time(), "blocked_detected": False}

    async def _heartbeat():
        while True:
            await _asyncio_outer.sleep(60)
            now = _time.time()
            delay = now - _health["last_heartbeat"] - 60
            _health["last_heartbeat"] = now
            if delay > 300:  # heartbeat delayed >5min => loop was blocked
                log_event(log, "event_loop_blocked",
                          delay_sec=round(delay, 1),
                          error="event loop blocked for >5min, exiting")
                import os as _os
                _os._exit(43)
            # P1-16: refresh the in-memory blocked set (one tiny local
            # SELECT — never Turso, never in a user path).
            try:
                blocked_cache.refresh(conn)
            except Exception:
                pass
            # P1-14: mirror dirty user-owned rows to Turso, off the
            # loop (the flush opens its own SQLite connection). The
            # user save happens here — after the answer was sent —
            # never inside a handler.
            try:
                _asyncio_outer.create_task(
                    _asyncio_outer.to_thread(
                        dbmod.flush_dirty_to_turso, config.db_path))
            except Exception:
                pass
            # Log periodic health (every ~10 min) for diagnosability
            if int(now) % 600 < 60:
                log_event(log, "polling_health_ok")

    _heartbeat_task = _asyncio_outer.create_task(_heartbeat())

    # Trending songs: refresh in background if stale (non-blocking).
    # Uses run_in_executor internally; never blocks the event loop.
    try:
        from .trending import refresh_if_needed as _trending_refresh
        _asyncio_outer.create_task(_trending_refresh())
    except Exception as e:
        log.warning("trending startup refresh not scheduled: %s", e)
    # Kabo hot trends: same pattern, separate module/lock.
    try:
        from .trending_kabo import get_hot_trends as _kabo_get

        async def _kabo_startup():
            try:
                await _kabo_get()
            except Exception as e:
                log.warning("kabo startup refresh failed: %s", e)
        _asyncio_outer.create_task(_kabo_startup())
    except Exception as e:
        log.warning("kabo startup refresh not scheduled: %s", e)

    # Story clips: periodic cache refresh (non-blocking, independent module).
    # Same guarded pattern — a failure here never affects other features.
    try:
        from .story_clips import refresh_loop as _story_refresh_loop
        _asyncio_outer.create_task(_story_refresh_loop())
    except Exception as e:
        log.warning("story_clips refresh not scheduled: %s", e)

    # Hourly clip feed for the Iranian story archive (Telegram @ehsaspaak
    # + Aparat, app/bot/clip_feed.py). Same guarded pattern: all network
    # and download work runs in an executor, the loop never raises, and
    # a failure here never affects other features. The first run is a
    # bounded bootstrap so the archive stocks immediately.
    try:
        from .clip_feed import clip_feed_loop as _clip_feed_loop
        _asyncio_outer.create_task(_clip_feed_loop())
    except Exception as e:
        log.warning("clip_feed loop not scheduled: %s", e)

    # Track last received update for the health endpoint
    _health["last_update"] = 0

    async def _track_updates(*_a, **_k):
        _health["last_update"] = time.time()
    try:
        # aiogram 3.x: register on every update via dispatcher event
        dp.update.outer_middleware.register(
            _UpdateTracker(_health))
        # P1-16: blocked-user gate — one enforcement point for every
        # callback and message on every router of this dispatcher.
        dp.update.outer_middleware.register(
            _BlockedGate(config.owner_id))
    except Exception:
        pass

    # Catch TelegramConflictError (409): aiogram 3.x retries it forever,
    # which causes an infinite mutual-kill ping-pong when two instances
    # poll simultaneously during a deploy. Exit non-zero instead so
    # Render restarts and a single instance wins.
    # IMPROVED: retry a few times first (the old instance gets SIGTERM
    # from Render within seconds, so a new instance should survive brief
    # conflicts instead of exiting immediately).
    try:
        from aiogram.exceptions import TelegramConflictError
    except ImportError:
        TelegramConflictError = None

    try:
        if TelegramConflictError is not None:
            _conflict_retries = 0
            while True:
                try:
                    await dp.start_polling(bot)
                    break  # polling ended cleanly
                except TelegramConflictError:
                    _conflict_retries += 1
                    log_event(log, "polling_conflict",
                              retry=_conflict_retries,
                              error="another instance is polling")
                    if _conflict_retries >= 5:
                        log_event(log, "polling_conflict_exit",
                                  error="conflict persisted after 5 retries")
                        import os as _os
                        _os._exit(3)
                    # Wait before retry; Render SIGTERMs the old instance
                    # within seconds, so the new one usually wins.
                    await _asyncio_outer.sleep(5)
        else:
            await dp.start_polling(bot)
    finally:
        try:
            _watchdog_task.cancel()
        except Exception:
            pass
        try:
            _heartbeat_task.cancel()
        except Exception:
            pass
        await bot.session.close()
        if web:
            web.shutdown()
        conn.close()
