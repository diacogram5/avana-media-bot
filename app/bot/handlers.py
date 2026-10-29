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

import io
import re
import secrets
import sqlite3

import httpx
from aiogram import Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile, BotCommand, CopyTextButton, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
)

from .. import db as dbmod
from ..bot.audio_send import send_song_audio
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
from ..security import RateLimiter, resolve_short_link, sanitize_text
from ..services.audio_id import AudioIdResult, identify_bytes, identify_url
from ..services.brain import brain_clean_query
from ..search.audio_match import fingerprint as audio_fingerprint, find_match as find_audio_match
from ..services.link_media import extract_media_url, extract_page_text
from ..services.openai_speech import synthesize_openai, transcribe_whisper
from ..services.shazam_id import identify_bytes_shazam
from ..services.stt import transcribe_bytes
from ..services.tts import synthesize
from .logic import (
    attach_bot_id, brief_error, can_use_feature, capabilities_menu,
    format_results, identified_card_acceptable, link_video_delivery,
    match_identified_song, menu_title_key, professional_audio_caption,
    welcome_text,
)

log = get_logger(__name__)
router = Router()
rate_limiter = RateLimiter()
# Lyrics fetched for cards the user opened: key -> (title, artist, lyrics).
LYRICS_CACHE: dict[str, tuple[str, str, str]] = {}
# Translations shown: (key, lang) -> translated text.
TRANSLATIONS: dict[tuple[str, str], str] = {}


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


def _web_button(conn, config):
    """Professional web-fallback button, only when WEB_URL is configured."""
    url = dbmod.get_web_url(conn, config)
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("menu_human_voice_web", "fa"), url=url)]])


def is_owner(message, config: Config) -> bool:
    uid = getattr(message.from_user, "id", None)
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
    # Persistent bottom menu (web parity): the 10 main feature buttons stay
    # at the bottom of the chat, each wired to its real function. Inline
    # keyboards remain only for supplementary card operations.
    await _answer(message, conn, _with_signature(conn, config, welcome_text(conn)),
                  reply_markup=_main_reply_kb(conn))


def _main_reply_kb(conn) -> ReplyKeyboardMarkup:
    """Permanent bottom menu built from the admin-managed feature list.

    Reflects renames/toggles/reorders automatically: whatever the admin
    panel shows as visible+enabled is what the user gets at the bottom.

    Keyboard policy (Telegram standards):
    - The reply keyboard is the navigation layer: it STAYS visible during
      navigation and one-shot hints. It is never removed mid-conversation
      (removing it would strand the user with no way back).
    - Contextual keyboards are INLINE only (quality pickers, pagination,
      confirmations, per-song buttons) — they never replace the bottom menu.
    - Every multi-step flow offers in-bot «❌ لغو» / «🏠 منوی اصلی»; the
      phone's hardware back button sends nothing to the bot and is never
      relied upon.
    - After an operation completes or is cancelled, the UI returns to the
      one predictable standard state: this main-menu keyboard + a short
      status line. The keyboard is re-sent only when the state changes —
      never re-sent identically as filler.
    """
    titles = [f["title"] for f in capabilities_menu(conn)]
    rows = []
    for i in range(0, len(titles), 2):
        rows.append([KeyboardButton(text=t) for t in titles[i:i + 2]])
    if not rows:
        rows = [[KeyboardButton(text="✨ قابلیت‌ها")]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True,
                               is_persistent=True)


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
    await callback.answer()
    rows = []
    for f in capabilities_menu(conn):
        cb = _menu_callback(f["key"])
        if cb:
            rows.append([InlineKeyboardButton(text=f["title"], callback_data=cb)])
    if not rows:
        await callback.message.answer("در حال حاضر قابلیتی فعال نیست.")
        return
    sent = await callback.message.answer(
        "✨ قابلیت‌ها:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    try:
        adminfeat.track(conn, callback.from_user.id, callback.message.chat.id, sent.message_id)
    except Exception:
        pass


@router.callback_query(F.data == "menu:link")
async def menu_link_hint(callback, conn: sqlite3.Connection, config: Config):
    await callback.answer()
    await callback.message.answer(
        "🔗 لینک اینستاگرام، تیک‌تاک، آپارات یا یوتیوب را بفرستید تا ویدیوی همان لینک و آهنگ آن بررسی شود.")


@router.callback_query(F.data == "menu:file")
async def menu_file_hint(callback, conn: sqlite3.Connection, config: Config):
    await callback.answer()
    await callback.message.answer(
        "🎵 فایل صوتی، ویدیو یا ویس را بفرستید تا آهنگ آن شناسایی شود.")


async def _run_menu_title(message: Message, conn: sqlite3.Connection,
                          config: Config, state: FSMContext | None,
                          key: str) -> None:
    """Run the real feature behind a persistent-menu button title.

    Reply-keyboard buttons arrive as plain text; this maps the (possibly
    admin-renamed) title back to its feature key and performs the same
    operation the inline «قابلیت‌ها» menu would trigger.
    """
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
            "🔍 نام آهنگ، نام خواننده، بخشی از متن ترانه یا لینک را بفرستید؛ "
            "فیلم یا فایل صوتی هم خودکار شناسایی می‌شود.")
    elif key == "suggest":
        allowed, reason = can_use_feature(conn, uid, "suggest")
        if not allowed:
            await message.answer(t(reason, "fa")); return
        if state is not None:
            await state.set_state("suggest")
        await message.answer(t("suggest_prompt", "fa"),
                             reply_markup=_cancel_kb())
    elif key == "downloader":
        allowed, reason = can_use_feature(conn, uid, "downloader")
        if not allowed:
            await message.answer(t(reason, "fa")); return
        if state is not None:
            await state.set_state(dlfeat.DlStates.await_url)
        await message.answer(f"{dlfeat.TITLE}\n\n{dlfeat.EXPLAINER}\n\n{dlfeat.PROMPT_SEND_LINK}",
                             reply_markup=_cancel_kb())
    elif key == "professional":
        allowed, reason = can_use_feature(conn, uid, "professional")
        if not allowed:
            await message.answer(t(reason, "fa")); return
        text, kb = profeat._render_pro_view(conn, uid)
        await message.answer(text, reply_markup=kb)
    elif key == "lyrics":
        allowed, reason = can_use_feature(conn, uid, "lyrics")
        if not allowed:
            await message.answer(t(reason, "fa")); return
        if state is not None:
            await state.set_state("lyr:await_query")
        await message.answer("📝 نام آهنگ و خواننده را بنویسید، مثلاً: مهدی احمدوند - دوست دارم",
                             reply_markup=_cancel_kb())
    elif key == "clear_chat":
        text, kb = adminfeat.clear_view()
        await message.answer(text, reply_markup=kb)
    elif key == "history":
        items = dbmod.recent_searches(conn, uid, limit=15)
        if not items:
            await message.answer("🕘 تاریخچه جستجوهای اخیر\n\nهنوز جستجویی ثبت نشده است.")
            return
        import datetime as _dt
        lines = ["🕘 تاریخچه جستجوهای اخیر"]
        for it in items:
            ts = _dt.datetime.fromtimestamp(it["created_at"]).strftime("%m-%d %H:%M")
            lines.append(f"• {it['query'][:60]} · {ts}")
        await message.answer("\n".join(lines))
    elif key == "help_support":
        await message.answer(t("help", "fa"))
    elif key == "link_fetch":
        await message.answer(
            "🔗 لینک اینستاگرام، تیک‌تاک، آپارات یا یوتیوب را بفرستید تا ویدیوی همان لینک و آهنگ آن بررسی شود.")
    elif key == "audio_id":
        allowed, reason = can_use_feature(conn, uid, "audio_id")
        if not allowed:
            await message.answer(t(reason, "fa")); return
        # Explicit pending-file state: the button now arms a real wait for
        # a file instead of sending a dead hint. The state is honoured by
        # media_handler (file -> identification) and text_handler (a text
        # mid-wait gets a clear "still waiting" notice, never a silent die).
        if state is not None:
            await state.set_state("audio_id:await_file")
        await message.answer(
            "🎵 فایل صوتی، ویدیو یا ویس را بفرستید تا آهنگ آن شناسایی شود.",
            reply_markup=_cancel_kb())
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
        reply_markup=_main_reply_kb(conn))


def _cancel_kb() -> InlineKeyboardMarkup:
    """In-bot «لغو»: every multi-step flow offers it, so the user never
    needs the phone's hardware back button (which sends nothing to the
    bot) and can never be trapped in a dead-end step."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ لغو", callback_data="flow:cancel")]])


@router.callback_query(F.data == "flow:cancel")
async def flow_cancel(callback, conn: sqlite3.Connection, config: Config,
                      state: FSMContext | None = None):
    """Cancel any in-progress flow: clear the conversation state and land
    back on the one predictable standard state — the persistent main-menu
    keyboard plus a short status line."""
    await callback.answer()
    if state is not None:
        try:
            await state.clear()
        except Exception:
            pass
    await callback.message.answer(
        "لغو شد؛ به منوی اصلی برگشتید.",
        reply_markup=_main_reply_kb(conn))


@router.callback_query(F.data == "menu:search")
async def menu_search_hint(callback, conn: sqlite3.Connection, config: Config):
    await callback.answer()
    await callback.message.answer(
        "🔍 نام آهنگ، نام خواننده، بخشی از متن ترانه یا لینک را بفرستید؛ "
        "فیلم یا فایل صوتی هم خودکار شناسایی می‌شود.")


@router.callback_query(F.data == "menu:lyrics")
async def menu_lyrics(callback, conn: sqlite3.Connection, config: Config, state: FSMContext):
    allowed, reason = can_use_feature(conn, callback.from_user.id, "lyrics")
    if not allowed:
        await callback.answer(t(reason, "fa"), show_alert=True); return
    await state.set_state("lyr:await_query")
    await callback.answer()
    # In-bot cancel: without it this entry point (inline «قابلیت‌ها» menu)
    # leaves the user in lyr:await_query with no visible way back.
    await callback.message.answer("📝 نام آهنگ و خواننده را بنویسید، مثلاً: مهدی احمدوند - دوست دارم",
                                  reply_markup=_cancel_kb())


@router.callback_query(F.data == "menu:history")
async def menu_history(callback, conn: sqlite3.Connection, config: Config):
    await callback.answer()
    items = dbmod.recent_searches(conn, callback.from_user.id, limit=15)
    if not items:
        await callback.message.answer("🕘 تاریخچه جستجوهای اخیر\n\nهنوز جستجویی ثبت نشده است.")
        return
    import datetime as _dt
    lines = ["🕘 تاریخچه جستجوهای اخیر"]
    for it in items:
        ts = _dt.datetime.fromtimestamp(it["created_at"]).strftime("%m-%d %H:%M")
        lines.append(f"• {it['query'][:60]} · {ts}")
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data == "menu:help")
async def menu_help(callback, conn: sqlite3.Connection, config: Config):
    await callback.answer()
    await callback.message.answer(t("help", "fa"))


@router.callback_query(F.data.startswith("menu:custom:"))
async def menu_custom(callback, conn: sqlite3.Connection, config: Config):
    key = callback.data.split(":", 2)[2]
    feat = dbmod.get_feature(conn, key)
    if not feat or not feat.get("enabled"):
        await callback.answer(t("feature_disabled", "fa"), show_alert=True); return
    await callback.answer()
    await callback.message.answer(feat.get("message") or feat.get("title") or "")


@router.message(Command("help"))
async def cmd_help(message: Message, conn: sqlite3.Connection, config: Config):
    _register(message, conn)
    # One useful button, not a stats button: voice-to-text.
    rows = [[InlineKeyboardButton(text=t("menu_voice_to_text", "fa"),
                             callback_data="voice_to_text:start")],
            [InlineKeyboardButton(text=t("menu_request_song", "fa"),
                             callback_data="suggest:start")],
            [InlineKeyboardButton(text=t("menu_playlists", "fa"),
                             callback_data="playlists:list")]]
    web = _web_button(conn, config)
    if web is not None:
        rows.extend(web.inline_keyboard)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    # Honest brain status: the superior-intelligence layer is OpenAI and
    # needs OPENAI_API_KEY; without it the bot says so, never fakes an
    # AI answer. Basic rule-based search still works without the key.
    brain_line = ("🧠 هوش برتر: وصل است (OpenAI)" if config.openai_api_key
                  else "🧠 هوش برتر: وصل نیست؛ برای فهم هوشمند، OPENAI_API_KEY لازم است.")
    await _answer(message, conn, _with_signature(
        conn, config, t("help", "fa") + "\n\n" + brain_line + "\n\n" + t("voice_usage", "fa")),
                    reply_markup=kb)


@router.message(Command("voicetotext", "stt"))
async def cmd_voice_to_text(message: Message, conn: sqlite3.Connection,
                            config: Config, state: FSMContext):
    """Button/command entry for voice-to-text: the next voice is transcribed."""
    _register(message, conn)
    if _blocked(conn, message.from_user.id):
        await _answer(message, conn, t("blocked", "fa")); return
    allowed, reason = can_use_feature(conn, message.from_user.id, "voice_to_text")
    if not allowed:
        await _answer(message, conn, t(reason, "fa")); return
    await state.set_state("voice_to_text")
    await _answer(message, conn, t("voice_to_text_prompt", "fa"),
                  reply_markup=_cancel_kb())


@router.callback_query(F.data == "voice_to_text:start")
async def voice_to_text_button(callback, conn: sqlite3.Connection,
                               config: Config, state: FSMContext):
    allowed, reason = can_use_feature(conn, callback.from_user.id, "voice_to_text")
    if not allowed:
        await callback.answer(t(reason, "fa"), show_alert=True); return
    await state.set_state("voice_to_text")
    await callback.answer()
    await callback.message.answer(t("voice_to_text_prompt", "fa"),
                                  reply_markup=_cancel_kb())



@router.message(Command("suggest", "request"))
async def cmd_suggest(message: Message, conn: sqlite3.Connection, config: Config, state: FSMContext):
    _register(message, conn)
    await state.set_state("suggest")
    await _answer(message, conn, t("suggest_prompt", "fa"),
                  reply_markup=_cancel_kb())


@router.callback_query(F.data == "suggest:start")
async def suggest_button(callback, conn: sqlite3.Connection, config: Config, state: FSMContext):
    await state.set_state("suggest")
    await callback.answer()
    await _answer(callback.message, conn, t("suggest_prompt", "fa"),
                  reply_markup=_cancel_kb())


@router.message(Command("playlists"))
async def cmd_playlists(message: Message, conn: sqlite3.Connection, config: Config):
    _register(message, conn)
    pls = dbmod.list_playlists(conn, message.from_user.id)
    if not pls:
        await _answer(message, conn, t("playlists_empty", "fa")); return
    lines = ["🎶 پلی‌لیست‌های شما:"]
    for pl in pls:
        items = dbmod.playlist_items(conn, pl["id"])
        lines.append(f"• {pl['name']} ({len(items)} آهنگ)" + (f" — {pl['description']}" if pl.get("description") else ""))
    await _answer(message, conn, "\n".join(lines))


@router.callback_query(F.data == "playlists:list")
async def playlists_button(callback, conn: sqlite3.Connection, config: Config):
    await callback.answer()
    pls = dbmod.list_playlists(conn, callback.from_user.id)
    if not pls:
        await callback.message.answer(t("playlists_empty", "fa")); return
    lines = ["🎶 پلی‌لیست‌های شما:"]
    for pl in pls:
        lines.append(f"• {pl['name']} ({len(dbmod.playlist_items(conn, pl['id']))} آهنگ)")
    await callback.message.answer("\n".join(lines))


@router.message(Command("newplaylist"))
async def cmd_newplaylist(message: Message, command: CommandObject, conn: sqlite3.Connection, config: Config):
    _register(message, conn)
    name = (command.args or "").strip()
    if not name:
        await _answer(message, conn, "نام پلی‌لیست را بنویسید؛ مثلاً: /newplaylist غمگین"); return
    dbmod.create_playlist(conn, message.from_user.id, name)
    await _answer(message, conn, t("playlist_created", "fa"))


@router.message(Command("favorites"))
async def cmd_favorites(message: Message, conn: sqlite3.Connection, config: Config):
    _register(message, conn)
    favs = dbmod.list_favorites(conn, message.from_user.id)
    if not favs:
        await _answer(message, conn, "😕 هنوز آهنگی به علاقه‌مندی‌ها اضافه نشده است."); return
    lines = ["❤️ علاقه‌مندی‌های شما:"]
    for f in favs:
        lines.append(f"• {f['title']} – {f['artist']}" if f["artist"] else f"• {f['title']}")
    await _answer(message, conn, "\n".join(lines))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, conn: sqlite3.Connection, state: FSMContext):
    await state.clear()
    await _answer(message, conn, t("cancelled", "fa"),
                  reply_markup=_main_reply_kb(conn))


@router.message(Command("admin"))
async def cmd_admin(message: Message, conn: sqlite3.Connection, config: Config):
    _register(message, conn)
    if not is_owner(message, config):
        await message.answer(t("not_admin", "fa"))
        return
    feats = dbmod.list_features(conn)
    buttons = [[InlineKeyboardButton(
        text=f"{'✅' if f['enabled'] else '❌'} {f['title']}",
        callback_data=f"feat:toggle:{f['key']}")] for f in feats]
    users = dbmod.list_users(conn, limit=10)
    lines = ["🛡 پنل مدیریت (فقط مالک)", f"👥 کاربران: {dbmod.count_users(conn)}",
             f"🆔 آیدی ربات: {'تنظیم شده' if dbmod.get_bot_id(conn, config) else 'تنظیم نشده'}",
             f"🌐 وب: {'تنظیم شده' if dbmod.get_web_url(conn, config) else 'تنظیم نشده'}",
             f"📡 چنل‌ها: {len(dbmod.get_channels(conn, config))} چنل"]
    for u in users:
        lines.append(f"• {u['telegram_id']} {u.get('username') or ''} "
                     f"{'⛔ مسدود' if u.get('blocked') else ''}")
    buttons.append([InlineKeyboardButton(text="🛡 پنل مدیریت کامل", callback_data="adm:panel")])
    buttons.append([InlineKeyboardButton(text="🏦 بانک هوش برتر", callback_data="bank:panel")])
    await _answer(message, conn, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("feat:toggle:"))
async def feat_toggle(callback, conn: sqlite3.Connection, config: Config):
    if not is_owner(callback, config):
        await callback.answer(t("not_admin", "fa"), show_alert=True)
        return
    key = callback.data.split(":", 2)[2]
    feat = dbmod.get_feature(conn, key)
    if feat:
        dbmod.set_feature(conn, key, enabled=not bool(feat["enabled"]), by_id=callback.from_user.id)
    await callback.answer("✅ تغییر کرد.")
    await callback.message.edit_text("🧩 وضعیت قابلیت تغییر کرد. برای مشاهده دوباره /admin را بزنید.")


@router.message(Command("voice"))
async def cmd_voice(message: Message, command: CommandObject,
                    conn: sqlite3.Connection, config: Config):
    _register(message, conn)
    if _blocked(conn, message.from_user.id):
        await _answer(message, conn, t("blocked", "fa")); return
    allowed, reason = can_use_feature(conn, message.from_user.id, "tts")
    if not allowed:
        await _answer(message, conn, t(reason, "fa")); return
    args = (command.args or "").strip()
    if not args:
        await _answer(message, conn, t("voice_usage", "fa")); return
    first, _, rest = args.partition(" ")
    gender = "female" if first in ("زن", "خانم", "female") else "male" if first in ("مرد", "آقا", "male") else "female"
    text = rest.strip() if rest.strip() else args
    # Text-to-voice: verified OpenAI TTS first, Azure as fallback.
    # With neither key, it honestly says the speech service is not connected.
    status, audio = await _synthesize_voice(text, gender, config, conn)
    if status == "needs_verification":
        await _answer(message, conn,
                      "⚠️ کلید هوش برتر ذخیره شده، اما عملکرد آن در «پنل مدیریت ← تنظیمات» "
                      "تأیید نشده است. تا تأیید عملکرد، ویس طبیعی فعال نمی‌شود.")
        return
    if status == "needs_key":
        await _answer(message, conn, t("tts_needs_key", "fa"), reply_markup=_web_button(conn, config)); return
    if status != "ok" or not audio:
        await _answer(message, conn, t("tts_error", "fa")); return
    sent = await message.answer_voice(BufferedInputFile(audio, filename="voice.mp3"),
                                      caption=f"{t('tts_ready', 'fa')}\n{text[:200]}")
    _track(conn, message, sent)


async def _send_link_video(message: Message, conn, outcome, config=None,
                           bot_id: str = ""):
    """Send the video of the sent link itself for download — no video search.

    Direct public video file: send that same file. Instagram: the real
    video file via the anonymous resolver when fetchable, else the
    official-link card. Other platforms: the official video via its
    official open link. Never fetch private or protected platform
    content. Returns the sent Message (or None) so the caller can fold
    a later fallback into the same card instead of stacking messages.
    """
    from aiogram.types import BufferedInputFile

    delivery, text = link_video_delivery(outcome)
    if delivery == "direct_video_file":
        sent = await send_video(message, outcome.official_url,
                                caption="🎬 ویدیوی همان لینک برای دانلود")
        if sent is not None:
            _track(conn, message, sent)
            await _attach_identify_button(message, conn, sent)
            return sent
        await _answer(message, conn, text)
        return None
    elif delivery == "platform_official" and outcome.kind == "instagram":
        sent = await _send_instagram_video_file(message, conn, outcome, config)
        if sent == _INSTAGRAM_GATED:
            # Dedicated age-gate line; the caller must not overwrite it
            # with the generic card or run identification stages on it.
            await _answer(message, conn, dlfeat.AGE_GATED)
            return _INSTAGRAM_GATED
        if sent is not None:
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
                                     config=None):
    """Fetch the reel's real video file (anonymous resolver) and send it.

    Returns the sent Message, or None when no fetchable video exists —
    the caller then falls back to the official-link card. Size-capped;
    the bytes must look like a real video container.
    """
    from aiogram.types import BufferedInputFile

    try:
        from ..services.instagram import resolve_instagram_media
        audio_url, video_url, _page_text, _strategy = \
            await resolve_instagram_media(outcome.official_url)
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
    try:
        headers = {"User-Agent": "Mozilla/5.0",
                   "Referer": "https://www.instagram.com/"}
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.get(video_url, headers=headers)
            resp.raise_for_status()
            data = resp.content
    except Exception:
        return None
    max_mb = getattr(config, "max_file_mb", 50) if config is not None else 50
    if not data or len(data) > max_mb * 1024 * 1024 or not _looks_like_video(data):
        return None
    sent = await send_video(
        message, BufferedInputFile(data, filename="video.mp4"),
        caption="🎬 ویدیوی همان لینک برای دانلود")
    if sent is not None:
        _track(conn, message, sent)
        # Pass the reel's separate audio-stream URL so the one-click
        # «🎵 شناسایی موسیقی» button identifies the reel's REAL audio —
        # the sent video file is the video-only DASH stream (no audio).
        await _attach_identify_button(message, conn, sent, audio_url=audio_url)
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


def _bank_button(token: str | None):
    if not token:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔎 جست‌وجو در بانک هوش برتر",
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

# Sentinel returned by _send_instagram_video_file when the reel is
# age/region-gated (oEmbed gating signals): the dedicated message is
# already handled by the caller, and no identification stages may run
# on a reel no anonymous fetch can ever reach.
_INSTAGRAM_GATED = "instagram_age_gated"


def _video_identify_kb(user_id: int, file_id: str, kind: str,
                       label: str = "🎵 شناسایی موسیقی",
                       audio_url: str | None = None) -> InlineKeyboardMarkup:
    """Build the one-click music-identification button for a video — or,
    with kind="audio" and a «🔄 تلاش مجدد» label, the retry button for an
    audio/voice file whose identification failed.

    ``kind`` is "link" (video fetched from a sent link), "file" (video
    the user sent directly), or "audio" (audio/voice the user sent). The
    registry entry is keyed by an opaque token; the Telegram file_id is
    resolved server-side on click. ``audio_url`` (link kind only) points
    at the reel's separate audio stream — the callback identifies THOSE
    bytes, because the sent video file itself is video-only."""
    token = secrets.token_hex(8)
    _VIDEO_IDENTIFY[token] = {"user_id": user_id, "file_id": file_id,
                              "kind": kind, "audio_url": audio_url}
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label,
                             callback_data=f"vid:id:{token}")]])


async def _attach_identify_button(message: Message, conn, sent,
                                  audio_url: str | None = None) -> None:
    """Attach the one-click identification button under a bot-sent video
    message (best effort; the video must have a downloadable file_id)."""
    try:
        video = getattr(sent, "video", None)
        file_id = getattr(video, "file_id", None) if video else None
        if not file_id or not message.from_user:
            return
        kb = _video_identify_kb(message.from_user.id, file_id, "link",
                                audio_url=audio_url)
        await sent.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


async def _download_link_audio(url: str, config) -> bytes:
    """Download a reel's separate audio stream for identification.

    Same validation as the video path: size cap, real container bytes —
    never a renamed or empty payload."""
    headers = {"User-Agent": "Mozilla/5.0",
               "Referer": "https://www.instagram.com/"}
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.content
    max_mb = getattr(config, "max_file_mb", 50)
    if not data or len(data) > max_mb * 1024 * 1024 \
            or not _looks_like_audio(data):
        raise ValueError("audio bytes failed validation")
    return data


async def _answer_identify_clip_fallback(message, conn, config, data: bytes,
                                         file_id: str) -> None:
    """Not-identified flow for the one-click video button: the clip's own
    audio is extracted to a real MP3 and sent, then the honest message
    with the bank button. Never a guessed song."""
    mp3 = None
    try:
        src_br, _dur = probe_audio(data, "mp4")
        target = 320 if (src_br or 0) >= 300 else max(96, src_br or 128)
        conv = transcode_mp3(data, "mp4", target)
        if conv[0]:
            mp3 = conv[0]
    except Exception:
        mp3 = None
    if mp3:
        sent = await send_music(
            message, mp3, "صدای استخراج‌شده از ویدیو", "",
            caption=("نام و نسخه اصلی آهنگ شناسایی نشد؛ اما صدای موجود در "
                     "ویدیو به‌صورت فایل صوتی برای شما آماده شد."))
        if sent is not None:
            _track(conn, message, sent)
    _tok = await _arm_bank_sample(conn, config, "video_button", file_id,
                                  data, "", "")
    await _answer(message, conn,
                  "نام و نسخه اصلی آهنگ شناسایی نشد؛ اما صدای موجود در ویدیو به‌صورت فایل صوتی برای شما آماده شد.",
                  reply_markup=_bank_button(_tok))


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
    token = callback.data.split("vid:id:", 1)[1]
    entry = _VIDEO_IDENTIFY.get(token)
    if not entry:
        # The registry is in-memory: after a restart the token is gone.
        # Honest expiry — never a silent no-op.
        await callback.answer(
            "این درخواست منقضی شده است؛ لطفاً فایل را دوباره بفرستید.",
            show_alert=True)
        return
    if entry.get("user_id") != callback.from_user.id:
        await callback.answer("این دکمه برای شما نیست.", show_alert=True)
        return
    if token in _VIDEO_IDENTIFY_RUNNING:
        await callback.answer("در حال شناسایی…")
        return
    _VIDEO_IDENTIFY_RUNNING.add(token)
    await callback.answer("🎧 در حال شناسایی آهنگ از روی صدای ویدیو…")
    try:
        # For link-fetched videos the sent file is the video-ONLY stream:
        # identify the reel's real audio from its separate audio URL.
        # User-sent files have no audio_url and fall back to the Telegram
        # file, exactly as before.
        audio_url = entry.get("audio_url")
        try:
            if audio_url:
                data = await _download_link_audio(audio_url, config)
            else:
                tg_file = await callback.bot.get_file(entry["file_id"])
                buf = io.BytesIO()
                await callback.bot.download_file(tg_file.file_path, buf)
                data = buf.getvalue()
        except Exception:
            await callback.message.answer(
                "⚠️ دریافت فایل ویدیو ممکن نشد؛ دوباره تلاش کنید.")
            return
        if not data or len(data) > config.max_file_mb * 1024 * 1024:
            await callback.message.answer(
                "⚠️ فایل ویدیو معتبر نیست یا بزرگ‌تر از حد مجاز است.")
            return
        # Distinct honest cause when the video provably has no audio track.
        try:
            audible = has_audio_stream(data)
        except Exception:
            audible = None
        if audible is False:
            await callback.message.answer(
                "🔇 این ویدیو صدای قابل‌شناسایی ندارد.")
            return
        result = await _identify_audio(
            data, "clip.mp3" if entry.get("kind") == "audio" else "clip.mp4",
            config)
        if result.status == "identified":
            _tok = await _arm_bank_sample(conn, config, "video_button",
                                          entry["file_id"], data, "", "")
            await _send_professional_song_card(
                callback.message, conn, result.title, result.artist,
                bot_id=dbmod.get_bot_id(conn, config), bank_token=_tok,
                own_media=("video", entry["file_id"]))
        elif result.status == "not_found":
            await _answer_identify_clip_fallback(
                callback.message, conn, config, data, entry["file_id"])
        else:
            # Distinct extraction/lookup causes — never one generic line.
            _detail_msg = {
                "silent": "🔇 صدای قابل‌استفاده‌ای در این ویدیو پیدا نشد.",
                "undecodable": "⚠️ صدای این ویدیو خراب یا غیرقابل پردازش است.",
                "too_short": "⚠️ صدای این ویدیو برای شناسایی خیلی کوتاه است.",
            }.get(result.detail)
            await callback.message.answer(
                _detail_msg or t("audio_error", "fa"))
    finally:
        _VIDEO_IDENTIFY_RUNNING.discard(token)


async def _identify_audio(data: bytes, filename: str, config: Config) -> AudioIdResult:
    """Shazam signature first (no token), AudD with a token as fallback.

    Pipeline stages (each logged with timing; errors carry their stage):
      1. input validation gate — the extracted audio must be decodable,
         non-silent and of sane duration, else a distinct extraction
         error is returned (never "not_found");
      2. Shazam on several windows (full, then later windows) so a clip
         with speech over the start, a remix intro, or a shortened copy
         has more than one chance; only an identified result is
         accepted. A sliced window that fails to decode only yields
         "error", never a false match. A catalog result is never
         involved here, so nothing can be mislabelled as identified.
    """
    import time as _time
    from ..services.shazam_id import analyze_audio
    t0 = _time.time()
    log_event(log, "identify_stage", stage="input_received",
              bytes=len(data or b""), filename=filename)
    if not data:
        log_event(log, "identify_stage", stage="validate", ok=False,
                  reason="empty_input")
        return AudioIdResult(status="error", detail="undecodable")
    # Stage 1: validation gate.
    t = _time.time()
    try:
        analysis = analyze_audio(data)
    except Exception as exc:
        log_event(log, "identify_stage", stage="validate", ok=False,
                  reason=f"analyze_crashed:{type(exc).__name__}")
        return AudioIdResult(status="error", detail="undecodable")
    log_event(log, "identify_stage", stage="validate",
              ms=int((_time.time() - t) * 1000), **analysis)
    if not analysis.get("ok"):
        reason = analysis.get("reason", "undecodable")
        # Distinct extraction-stage error — never "not_found".
        return AudioIdResult(status="error", detail=reason)
    # Stage 2: Shazam windows.
    shazam = None
    windows = [data]
    if data and len(data) > 200_000:
        windows.append(data[len(data) // 3:])
        windows.append(data[2 * len(data) // 3:])
    log_event(log, "identify_stage", stage="input_prepared",
              windows=len(windows), sizes=[len(w) for w in windows])
    for i, window in enumerate(windows):
        t = _time.time()
        try:
            candidate = await identify_bytes_shazam(window)
            log_event(log, "identify_stage", stage="lookup_response",
                      window=i, ms=int((_time.time() - t) * 1000),
                      status=candidate.status if candidate else "none")
        except Exception as exc:
            log_event(log, "identify_stage", stage="lookup_error",
                      window=i,
                      error=f"{type(exc).__name__}: {str(exc)[:120]}")
            candidate = None
        if candidate is not None:
            shazam = candidate
            if candidate.status == "identified":
                break
    if shazam is not None and shazam.status == "identified":
        log_event(log, "identify_stage", stage="result_sent",
                  ms=int((_time.time() - t0) * 1000), status="identified",
                  title=shazam.title, artist=shazam.artist)
        return AudioIdResult(status="identified", title=shazam.title, artist=shazam.artist)
    if config.audd_token:
        t = _time.time()
        try:
            audd = await identify_bytes(data, filename, config.audd_token)
            log_event(log, "identify_stage", stage="lookup_response",
                      window="audd", ms=int((_time.time() - t) * 1000),
                      status=audd.status if audd else "none")
        except Exception as exc:
            log_event(log, "identify_stage", stage="lookup_error",
                      window="audd",
                      error=f"{type(exc).__name__}: {str(exc)[:120]}")
            audd = None
        if audd is not None and audd.status == "identified":
            log_event(log, "identify_stage", stage="result_sent",
                      ms=int((_time.time() - t0) * 1000), status="identified",
                      title=audd.title, artist=audd.artist)
            return audd
    if shazam is not None and shazam.status == "not_found":
        log_event(log, "identify_stage", stage="result_sent",
                  ms=int((_time.time() - t0) * 1000), status="not_found")
        return AudioIdResult(status="not_found")
    log_event(log, "identify_stage", stage="result_sent",
              ms=int((_time.time() - t0) * 1000), status="error",
              detail="lookup_error")
    return AudioIdResult(status="error", detail="lookup_error")


def _link_platform_name(kind: str) -> str:
    return {"instagram": "اینستاگرام", "youtube": "یوتیوب",
            "aparat": "آپارات", "tiktok": "تیک‌تاک"}.get(kind, "این سرویس")


def _link_needs_file_card(outcome) -> str:
    """The single compact card for a link whose media the platform did
    not hand over: video-link line + one honest line. One message,
    never a stack of video-link + nag + channel notice."""
    platform = _link_platform_name(outcome.kind)
    return ("🎬 ویدیوی همان لینک:\n" + outcome.official_url +
            f"\n\n⚠️ {platform} فایل ویدیو را به بات نداد؛ "
            "برای شناسایی آهنگ، خود ویدیو را مستقیم بفرستید.")


async def _identify_link_audio(outcome, config: Config):
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
    """
    url = outcome.official_url
    if outcome.kind == "direct_file":
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.content
        except Exception:
            return ("error", "", "", False, "", b"")
        if len(data) > config.max_file_mb * 1024 * 1024:
            return ("error", "", "", False, "", b"")
        result = await _identify_audio(data, "clip.mp4", config)
        return (result.status, result.title, result.artist, False, "", data)
    media_url = None
    page_text = ""
    strategy = "none"
    if outcome.kind == "instagram":
        # Multi-strategy anonymous resolver (page -> embed ->
        # captioned embed -> crawler DASH audio). No login, no cookies.
        try:
            from ..services.instagram import resolve_instagram_media
            media_url, _video_url, page_text, strategy = await resolve_instagram_media(url)
        except Exception:
            media_url, _video_url, page_text, strategy = None, None, "", "none"
    else:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                page = resp.text[:2_000_000] if resp.status_code == 200 else ""
            media_url = extract_media_url(page)
            page_text = extract_page_text(page)
        except Exception:
            media_url = None
    if media_url:
        try:
            headers = {"User-Agent": "Mozilla/5.0",
                       "Referer": "https://www.instagram.com/"}
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(media_url, headers=headers)
                resp.raise_for_status()
                data = resp.content
            if len(data) <= config.max_file_mb * 1024 * 1024:
                result = await _identify_audio(data, "clip.mp4", config)
                if result.status == "identified":
                    return ("identified", result.title, result.artist, False, page_text, data)
        except Exception:
            pass
    if config.audd_token:
        try:
            result = await identify_url(url, config.audd_token)
        except Exception:
            result = None
        if result is not None and result.status == "identified":
            return ("identified", result.title, result.artist, False, page_text, b"")
    return ("needs_file", "", "", True, page_text, b"")


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
    if config is None:
        return False
    channels = dbmod.get_channels(conn, config)
    if not channels:
        if message.from_user.id == (config.owner_id or -1):
            await _answer(
                message, conn,
                "📡 بانک هوش برتر هنوز چنلی ندارد؛ از «🛡 پنل مدیریت» ← "
                "«🏦 بانک هوش برتر» چنل اضافه و ایندکس کنید.")
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
                  f"{t('channel_post', 'fa')}\n{post['post_url']}\n{post['text'][:300]}")
    return True


async def _send_professional_song_card(message, conn, title: str, artist: str, bot_id: str = "",
                                       bank_token: str | None = None,
                                       own_media: tuple[str, str] | None = None):
    """One clean card for an identified song.

    Delivery order (full-version-first):
    1. The FULL same-version file from existing sources, sent as a real
       Telegram audio message (never a document, never a short preview).
    2. Only if no full version exists: the user's own media re-sent as a
       clearly-labelled replacement.
    3. Otherwise an honest "full version not found" message with the
       similar-songs and bank buttons. A short catalog preview is never
       presented as the song.

    - Exact catalog match (version-aware) -> the card keeps the pro
      buttons (heart / lyrics / add-to-playlist). Never a nearest song.
    """
    query = f"{title} {artist}".strip()
    engine = SearchEngine([ITunesProvider(entity="song"), DeezerProvider()])
    try:
        results, _ = await smart_search(engine, query, limit=10)
    except Exception:
        results = []
    dbmod.record_search(conn, message.from_user.id, query, ok=bool(results))
    match = match_identified_song(results, title, artist)
    key = profeat.remember_result(
        title, artist,
        (getattr(match, "url", "") or "") if match else "",
        (getattr(match, "preview_url", "") or "") if match else "")
    is_fav = profeat.is_favorite(conn, message.from_user.id, title, artist)
    kb = InlineKeyboardMarkup(inline_keyboard=profeat.build_song_card_buttons(key, is_fav))
    # 1) FULL VERSION FIRST. (Argument order is (artist, title); a swapped
    # call silently searched the wrong query and never found anything.)
    full = None
    try:
        full = await find_full_song_file(artist, title)
    except Exception:
        full = None
    if full:
        dur = full["duration_sec"]
        label = (f" — مدت: {int(dur // 60)}:{int(dur % 60):02d}" if dur < 90
                 else " — مدت کامل: بیش از ۹۰ ثانیه")
        # Real MP3 delivery via the unified pipeline: genuine conversion
        # when needed, honest probed bitrate, proper AUDIO message.
        # Telegram's Bot API upload limit is 50 MB.
        full_data = full["data"] or b""
        sent = None
        if full_data and len(full_data) <= 50 * 1024 * 1024:
            sent = await send_music(
                message, full_data, title, artist,
                caption=f"🎵 نسخه کامل آهنگ پیدا و آماده شد{label}\n{title} – {artist}",
                reply_markup=kb, duration=int(dur) if dur else None)
        if sent is not None:
            _track(conn, message, sent)
            return
    # 2) No full file: the user's own media as a labelled replacement (if any).
    # Kept button-free and minimal; the 🔎 second search belongs to the
    # not-identified flows (media/link not-found), where it is offered.
    if own_media is not None:
        kind, file_id = own_media
        cap = ("آهنگ شناسایی شد، اما نسخه کامل آن برای دانلود پیدا نشد؛ صدای موجود در ویدیوی شما "
               "به‌عنوان فایل صوتی ارسال شد. می‌توانید برای یافتن نسخه کامل یا نسخه‌های دیگر، "
               "جست‌وجوی تکمیلی انجام دهید.")
        try:
            if kind == "video":
                sent = await send_video(message, file_id, caption=cap)
            elif kind == "voice":
                sent = await message.answer_voice(file_id, caption=cap)
            else:
                sent = await send_song_audio(
                    message, file_id, title, artist, caption=cap)
            if sent is not None:
                _track(conn, message, sent)
                return
        except Exception:
            pass
    # 3) Honest "full version not found" + more-search options. A short
    # preview is never presented as the song.
    more_kb_rows = list(kb.inline_keyboard)
    more_kb_rows.append([InlineKeyboardButton(
        text=t("similar_songs", "fa"), callback_data=f"similar:{query[:60]}")])
    if bank_token:
        more_kb_rows.append([InlineKeyboardButton(
            text="🔎 جست‌وجو در بانک هوش برتر",
            callback_data=f"bank:search:{bank_token}")])
    await _answer(message, conn,
                  f"«{title}» از {artist} شناسایی شد، اما نسخه کامل آن برای دانلود پیدا نشد.",
                  reply_markup=InlineKeyboardMarkup(inline_keyboard=more_kb_rows))


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


async def _do_search(message: Message, conn, query: str, include_video: bool = True, config=None):
    """Professional minimal text search: clean list + one song card.

    Sources: iTunes + Deezer (current engine preserved). If the query
    looks like a lyric line and catalog variants fail, the lyric-phrase
    pipeline verifies candidates against real lyrics (overlap >= 0.60)
    before returning anything. If catalog + lyric search both fail, the
    channel engine is tried once (cached, stops on first exact post).
    Results are never labelled "identified".
    """
    if not rate_limiter.allow(f"search:{message.from_user.id}", 10, 60):
        await _answer(message, conn, "⏳ لطفاً کمی صبر کنید و دوباره تلاش کنید."); return
    await _answer(message, conn, t("searching", "fa"))
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
        return
    text = format_results(results, page=1)
    kb = None
    if len(results) > 5:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➡️ صفحه بعد", callback_data=f"search:next:{query[:60]}")]])
    await _answer(message, conn, text, reply_markup=kb)
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
                    f"{t('channel_post', 'fa')}\n{post['post_url']}\n{post['text'][:300]}")
                return
    await message.answer(t("no_results", "fa"), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("similar_songs", "fa"), callback_data=f"similar:{query[:60]}")]]))


@router.callback_query(F.data.startswith("search:next:"))
async def search_next(callback, conn: sqlite3.Connection, config: Config):
    query = callback.data.split(":", 2)[2]
    engine = SearchEngine([ITunesProvider(entity="song"), ITunesProvider(entity="musicVideo")])
    try:
        results, _ = await smart_search(engine, query, limit=10)
    except Exception:
        results = []
    if len(results) <= 5:
        await callback.answer("صفحه دیگری نیست.", show_alert=True); return
    await callback.message.edit_text(format_results(results, page=2))
    await callback.answer()


@router.message(F.text)
async def text_handler(message: Message, conn: sqlite3.Connection, config: Config, state: FSMContext | None = None):
    _register(message, conn)
    _track_incoming(conn, message)
    if _blocked(conn, message.from_user.id):
        await message.answer(t("blocked", "fa")); return
    text = (message.text or "").strip()
    current_state = None
    if state is not None:
        try:
            current_state = await state.get_state()
        except Exception:
            current_state = None
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
                "⏳ هنوز منتظر فایل صوتی، ویدیو یا ویس هستم؛ برای لغو «❌ لغو» را بزنید.")
            return
    link_url = _extract_link(text)
    if link_url:
        # Intent from a link is deterministic (no button, no brain call
        # needed): the link's own video, then its audio identification.
        # Detection finds the URL anywhere in the text, so a pasted link
        # never falls through to the file prompts or text search.
        allowed, reason = can_use_feature(conn, message.from_user.id, "link_fetch")
        if not allowed:
            await message.answer(t(reason, "fa")); return
        outcome = process_link(link_url, config.max_file_mb)
        if not outcome.ok:
            await message.answer(t(outcome.message, "fa")); return
        # Professional minimal link flow: video, then one clean song card.
        # No catalog video search, no video section, no button grid, no
        # empty "not found" boxes.
        video_msg = await _send_link_video(message, conn, outcome, config,
                                           bot_id=dbmod.get_bot_id(conn, config))
        if video_msg == _INSTAGRAM_GATED:
            # Age-gated reel: the dedicated message is already shown.
            # There is no audio any anonymous fetch can reach, so no
            # identification, bank sample, or channel stage runs.
            return
        # Cached identification first: same link must not re-burn
        # Shazam/AudD lookups (token/API cost).
        import json as _json
        cached = dbmod.get_cached_search(conn, f"link:{outcome.official_url}")
        if cached:
            try:
                cached_obj = _json.loads(cached)
            except ValueError:
                cached_obj = {}
            if cached_obj.get("title"):
                await _send_professional_song_card(
                    message, conn, cached_obj["title"], cached_obj.get("artist", ""),
                    bot_id=dbmod.get_bot_id(conn, config))
                return
        _link_res = await _identify_link_audio(outcome, config)
        status, title, artist, needs_file = _link_res[:4]
        page_text = _link_res[4] if len(_link_res) > 4 else ""
        heard = _link_res[5] if len(_link_res) > 5 else b""
        if status == "identified":
            dbmod.set_cached_search(
                conn, f"link:{outcome.official_url}",
                _json.dumps({"title": title, "artist": artist}, ensure_ascii=False))
            # Arm the bank sample too: if the catalog card later proves
            # unusable, the second search can still run on this audio.
            _tok = await _arm_bank_sample(conn, config, "link", outcome.official_url,
                                          heard or None, outcome.official_url, page_text)
            await _send_professional_song_card(message, conn, title, artist,
                                               bot_id=dbmod.get_bot_id(conn, config),
                                               bank_token=_tok)
        elif needs_file:
            _tok = await _arm_bank_sample(conn, config, "link", outcome.official_url,
                                          heard or None, outcome.official_url, page_text)
            # ONE compact card: fold the fallback into the already-sent
            # video message instead of stacking three messages. Media
            # messages take edit_caption; text messages take edit_text.
            card = _link_needs_file_card(outcome)
            kb = _bank_button(_tok)
            folded = False
            if video_msg is not None:
                try:
                    if getattr(video_msg, "video", None) or getattr(video_msg, "audio", None):
                        await video_msg.edit_caption(caption=card, reply_markup=kb)
                    else:
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
        else:
            _tok = await _arm_bank_sample(conn, config, "link", outcome.official_url,
                                          heard or None, outcome.official_url, page_text)
            await _answer(message, conn, t("link_stage_not_found", "fa"),
                          reply_markup=_bank_button(_tok))
            await _run_link_channel_stage(message, conn, config, page_text)
        return
    allowed, reason = can_use_feature(conn, message.from_user.id, "song_search")
    if not allowed:
        await message.answer(t(reason, "fa")); return
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


@router.message(F.video | F.audio | F.voice | F.document)
async def media_handler(message: Message, conn: sqlite3.Connection, config: Config,
                        state: FSMContext):
    """Auto intent: a sent film/audio IS a song-identification request,
    unless voice-to-text mode was just started (button/command) — then
    the same voice is transcribed to text instead, via speech recognition
    (not a speech-synthesis engine), and no text is ever fabricated."""
    _register(message, conn)
    _track_incoming(conn, message)
    if _blocked(conn, message.from_user.id):
        await message.answer(t("blocked", "fa")); return
    current_state = None
    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None
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
            await message.answer(t(reason, "fa")); return
        await message.answer(t("voice_to_text_transcribing", "fa"))
        try:
            media = message.video or message.audio or message.voice or message.document
            file = await message.bot.get_file(media.file_id)
            buf = io.BytesIO()
            await message.bot.download_file(file.file_path, buf)
            data = buf.getvalue()
        except Exception:
            await message.answer(t("voice_to_text_error", "fa")); return
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
            await message.answer(t("voice_to_text_empty", "fa")); return
        if status != "ok" or not text:
            await message.answer(t("voice_to_text_error", "fa")); return
        await message.answer(f"{t('voice_to_text_result', 'fa')}\n{text}")
        return
    allowed, reason = can_use_feature(conn, message.from_user.id, "audio_id")
    if not allowed:
        await message.answer(t(reason, "fa")); return
    # A caption, if present, is also a search query (name / lyric line).
    caption = (message.caption or "").strip()
    await message.answer(t("audio_identifying", "fa"))
    try:
        file = await message.bot.get_file(
            (message.video or message.audio or message.voice or message.document).file_id)
        buf = io.BytesIO()
        await message.bot.download_file(file.file_path, buf)
        data = buf.getvalue()
    except Exception:
        await message.answer(t("audio_error", "fa")); return
    if len(data) > config.max_file_mb * 1024 * 1024:
        await message.answer("⚠️ فایل بزرگ‌تر از حد مجاز است."); return
    # Intent from a film is deterministic (no button needed): identify
    # its audio — Shazam signature first (no token), AudD as fallback —
    # then show one professional card, never a nearest song.
    result = await _identify_audio(data, "clip.mp4", config)
    media = message.video or message.audio or message.voice or message.document
    media_kind = ("video" if message.video is not None
                  else "voice" if message.voice is not None
                  else "audio" if message.audio is not None else "document")
    if result.status == "not_found":
        # Audio-content fallback BEFORE the honest "not found" return:
        # 1) match against files the bot has actually seen (e.g. a
        #    forwarded channel audio file) by chunk content — this is
        #    the only channel-file matching that does not require
        #    scraping private channels. A match is labelled as a
        #    channel/file match, never as a catalog identification.
        # 2) otherwise index this file so a later identical/shortened
        #    upload can match it. Indexing never invents a title.
        matched = find_audio_match(dbmod.list_audio_index(conn), data)
        if matched is not None:
            label = matched.get("title") or matched.get("caption") or ""
            ch = matched.get("channel") or ""
            post = matched.get("post_url") or ""
            await _answer(message, conn,
                          f"{t('audio_index_match', 'fa')}\n{label}"
                          + (f" (چنل: {ch})" if ch else "")
                          + (f"\n{post}" if post else ""))
        else:
            dbmod.index_audio_file(
                conn, audio_fingerprint(data), title=caption, artist="",
                channel=_forward_channel(message), caption=caption)
        # Arm the second search on this same audio sample.
        _tok = await _arm_bank_sample(conn, config, "file",
                                      __import__("hashlib").sha256(data).hexdigest(),
                                      data, "", caption)
        _rows: list = []
        _bb = _bank_button(_tok)
        if _bb:
            _rows.extend(_bb.inline_keyboard)
        if message.video is not None:
            # One-click retry on this same video: no re-send needed.
            _rows.extend(_video_identify_kb(
                message.from_user.id, message.video.file_id,
                "file").inline_keyboard)
        elif message.audio is not None or message.voice is not None:
            # Same-file retry for audio/voice: the registry resolves the
            # Telegram file_id server-side, so the user never re-sends.
            _media = message.audio or message.voice
            _rows.extend(_video_identify_kb(
                message.from_user.id, _media.file_id, "audio",
                label="🔄 تلاش مجدد").inline_keyboard)
        await _answer(message, conn, t("audio_not_found", "fa"),
                      reply_markup=InlineKeyboardMarkup(inline_keyboard=_rows) if _rows else None)
        # AI/channel-only fallback: the user's own file, labelled honestly
        # (never claimed identified), sent back for download with bot ID.
        try:
            cap = t("ai_channel_note", "fa")
            if message.video is not None:
                sent = await send_video(message, media.file_id, caption=cap)
            elif message.audio is not None:
                sent = await send_song_audio(
                    message, media.file_id, "", "", caption=cap)
            else:
                sent = None
            if sent is not None:
                _track(conn, message, sent)
        except Exception:
            pass
        if caption:
            await _do_search(message, conn, sanitize_text(caption, 300), config=config)
        return
    if result.status != "identified":
        _err_kb = None
        if message.video is not None:
            _err_kb = _video_identify_kb(
                message.from_user.id, message.video.file_id, "file")
        elif message.audio is not None or message.voice is not None:
            _media = message.audio or message.voice
            _err_kb = _video_identify_kb(
                message.from_user.id, _media.file_id, "audio",
                label="🔄 تلاش مجدد")
        await _answer(message, conn, t("audio_error", "fa"),
                      reply_markup=_err_kb)
        return
    _tok = await _arm_bank_sample(conn, config, "file",
                                  __import__("hashlib").sha256(data).hexdigest(),
                                  data, "", caption)
    await _send_professional_song_card(
        message, conn, result.title, result.artist,
        bot_id=dbmod.get_bot_id(conn, config), bank_token=_tok,
        own_media=(media_kind, media.file_id))



async def _run_suggest(message, conn, text: str, config):
    """Mood suggestion: real catalog results only, never invented."""
    allowed, reason = can_use_feature(conn, message.from_user.id, "suggest")
    if not allowed:
        await _answer(message, conn, t(reason, "fa")); return
    engine = SearchEngine([ITunesProvider(entity="song"), DeezerProvider()])

    async def _cat(q):
        r, _f = await engine.search(q, limit=10)
        return r
    try:
        mood, results = await suggest_songs(text, _cat)
    except Exception:
        await _answer(message, conn, brief_error("suggest")); return
    if not results:
        await _answer(message, conn, "برای این حال‌وهوا آهنگ مناسبی پیدا نشد؛ جور دیگری بنویسید، مثلاً: «شاد برای رانندگی».")
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
    picks, rest = uniq[:5], uniq[5:]
    more_tok = secrets.token_hex(6) if rest else ""
    if more_tok:
        _SUGGEST_MORE[more_tok] = rest
    kb_rows: list[list[InlineKeyboardButton]] = []
    lines = [f"🎧 {len(picks)} پیشنهاد برای: {mood}"]
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
    extra_row = [InlineKeyboardButton(text="🎵 آهنگ‌های مشابه",
                                      callback_data=f"similar:{text[:50]}")]
    if more_tok:
        extra_row.append(InlineKeyboardButton(text="➕ پیشنهادهای بیشتر",
                                              callback_data=f"sg:more:{more_tok}"))
    extra_row.append(InlineKeyboardButton(text="🔄 جستجوی مجدد",
                                          callback_data="sg:retry"))
    kb_rows.append(extra_row)
    await _answer(message, conn, "\n".join(lines),
                  reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


_SUGGEST_MORE: dict[str, list] = {}


@router.callback_query(F.data.startswith("sg:get:"))
async def suggest_get(callback, conn: sqlite3.Connection, config: Config):
    """One suggestion's own button: deliver that exact song (full-version-first).

    The user must end with a real audio file or a precise reason — never a
    bare song name and never a silent death if the delivery raises.
    """
    key = callback.data.split("sg:get:", 1)[1]
    res = profeat.get_result(key)
    if not res:
        # In-memory result keys die on restart: honest expiry, not a no-op.
        await callback.answer(
            "این پیشنهاد منقضی شده است؛ لطفاً دوباره درخواست بدهید.",
            show_alert=True)
        return
    await callback.answer()
    try:
        await _send_professional_song_card(
            callback.message, conn, res["title"], res.get("artist", ""),
            bot_id=dbmod.get_bot_id(conn, config))
    except Exception:
        await callback.message.answer(
            "⚠️ ارسال فایل این آهنگ ممکن نشد؛ دوباره تلاش کنید.")


@router.callback_query(F.data.startswith("sg:more:"))
async def suggest_more(callback, conn: sqlite3.Connection, config: Config):
    tok = callback.data.split("sg:more:", 1)[1]
    rest = _SUGGEST_MORE.get(tok) or []
    if not rest:
        await callback.answer("پیشنهاد بیشتری نیست.", show_alert=True); return
    picks, remaining = rest[:5], rest[5:]
    if remaining:
        _SUGGEST_MORE[tok] = remaining
    else:
        _SUGGEST_MORE.pop(tok, None)
    kb_rows: list[list[InlineKeyboardButton]] = []
    lines = [f"🎧 {len(picks)} پیشنهاد بیشتر:"]
    for i, r in enumerate(picks, 1):
        key = profeat.remember_result(r.title, r.artist, r.url, r.preview_url)
        info = f"{r.title} – {r.artist}" if r.artist else r.title
        lines.append(f"{i}. {info}")
        label = f"▶️ {r.title[:28]}" + (f" – {r.artist[:20]}" if r.artist else "")
        kb_rows.append([InlineKeyboardButton(text=label,
                                            callback_data=f"sg:get:{key}")])
    if remaining:
        kb_rows.append([InlineKeyboardButton(text="➕ پیشنهادهای بیشتر",
                                            callback_data=f"sg:more:{tok}")])
    await callback.answer()
    await callback.message.answer("\n".join(lines),
                                  reply_markup=InlineKeyboardMarkup(
                                      inline_keyboard=kb_rows))


@router.callback_query(F.data == "sg:retry")
async def suggest_retry(callback, conn: sqlite3.Connection, config: Config,
                        state: FSMContext):
    await state.set_state("suggest")
    await callback.answer()
    # In-bot cancel: «🔄 جستجوی مجدد» re-arms the suggest state, so the
    # prompt must offer the same ❌ لغو the other suggest entries have.
    await callback.message.answer(t("suggest_prompt", "fa"),
                                  reply_markup=_cancel_kb())


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
    LYRICS_CACHE[key] = (result["title"], result.get("artist", ""), lyrics)
    return LYRICS_CACHE[key]


@router.callback_query(F.data.startswith("ly:"))
async def lyrics_callback(callback, conn: sqlite3.Connection, config: Config):
    key = callback.data[3:]
    result = profeat.get_result(key)
    if result is None:
        await callback.answer(brief_error("lyrics"), show_alert=True); return
    await callback.answer()
    allowed, reason = can_use_feature(conn, callback.from_user.id, "lyrics")
    if not allowed:
        await callback.message.answer(t(reason, "fa")); return
    try:
        lyrics, source, responded = await fetch_lyrics_with_source(
            result["title"], result.get("artist", ""))
    except Exception:
        lyrics, source, responded = None, "", False
    if not lyrics:
        if not responded:
            await callback.message.answer("منابع متن آهنگ در دسترس نیستند. چند لحظه دیگر دوباره تلاش کنید.")
        else:
            q = result["title"] + (f" - {result['artist']}" if result.get("artist") else "")
            await callback.message.answer(lyrics_not_found_text(q))
        return
    LYRICS_CACHE[key] = (result["title"], result.get("artist", ""), lyrics)
    kb = _lyrics_card_kb(key, result["title"], result.get("artist", ""),
                         lyrics, conn, config)
    header = f"📝 متن آهنگ «{result['title']}»"
    if result.get("artist"):
        header += f" — {result['artist']}"
    src = f"\nمنبع: {source}" if source else ""
    # The lyrics are sent in the song's ORIGINAL language first and never
    # changed; the translation comes separately below the buttons.
    sent = await callback.message.answer(f"{header}\n\n{lyrics[:3500]}{src}", reply_markup=kb)
    try:
        adminfeat.track(conn, callback.from_user.id, callback.message.chat.id, sent.message_id)
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
                    conn, config) -> InlineKeyboardMarkup:
    share = _lyrics_share_text(title, artist, lyrics, conn, config)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 ترجمه", callback_data=f"tr:{key}"),
        _copy_button("📋 کپی متن آهنگ", share, f"cp:{key}"),
    ]])


@router.callback_query(F.data.startswith("tr:"))
async def translate_prompt_callback(callback, conn: sqlite3.Connection, config: Config):
    key = callback.data[3:]
    if profeat.get_result(key) is None and key not in LYRICS_CACHE:
        await callback.answer(brief_error("translate"), show_alert=True); return
    await callback.answer()
    await callback.message.answer("ترجمه به:",
                                  reply_markup=_lyrics_picker_kb(0, key))


@router.callback_query(F.data.startswith("lpg:"))
async def translate_page_callback(callback, conn: sqlite3.Connection, config: Config):
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.answer(brief_error("translate"), show_alert=True); return
    _p, _l, page, key = parts
    try:
        page_n = int(page)
    except ValueError:
        page_n = 0
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=_lyrics_picker_kb(page_n, key))
    except Exception:
        await callback.message.answer("ترجمه به:", reply_markup=_lyrics_picker_kb(page_n, key))


@router.callback_query(F.data.startswith("tl:"))
async def translate_callback(callback, conn: sqlite3.Connection, config: Config):
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer(brief_error("translate"), show_alert=True); return
    _tl, lang, key = parts
    if lang not in LANG_NAMES:
        await callback.answer("این زبان پشتیبانی نمی‌شود.", show_alert=True); return
    await callback.answer()
    got = await _lyrics_text_for(key, conn)
    if got is None:
        await callback.message.answer(brief_error("lyrics")); return
    title, artist, lyrics = got
    notice = same_language_notice(lyrics, lang)
    if notice:
        await callback.message.answer(notice); return
    await callback.message.answer(f"در حال ترجمه به {LANG_NAMES.get(lang, lang)}…")
    try:
        translated = await translate_text(lyrics, lang)
    except Exception:
        translated = None
    if not translated:
        await callback.message.answer("ترجمه این متن پیدا نشد؛ دوباره تلاش کنید."); return
    TRANSLATIONS[(key, lang)] = translated
    _share = attach_bot_id(translated[:3000], dbmod.get_bot_id(conn, config))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        _copy_button("📋 کپی ترجمه", _share, f"ltc:{key}:{lang}"),
    ]])
    header = f"🌐 ترجمه ({LANG_NAMES.get(lang, lang)}): {title}"
    if artist:
        header += f" – {artist}"
    # The original text above is never changed; the translation is separate.
    await callback.message.answer(f"{header}\n\n{translated[:3500]}", reply_markup=kb)


@router.callback_query(F.data.startswith("ltc:"))
async def translate_copy_callback(callback, conn: sqlite3.Connection, config: Config):
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.answer(brief_error("translate"), show_alert=True); return
    _p, _c, key, lang = parts
    translated = TRANSLATIONS.get((key, lang))
    if not translated:
        await callback.answer(brief_error("translate"), show_alert=True); return
    await callback.answer()
    # Honest long-press copy: bots have no clipboard API for long texts,
    # so a copy is never claimed unless the copy_text button did it.
    shared = attach_bot_id(translated[:3000], dbmod.get_bot_id(conn, config))
    await callback.message.answer(f"📋 برای کپی، متن زیر را نگه دارید:\n```\n{shared}\n```", parse_mode="Markdown")


@router.callback_query(F.data.startswith("cp:"))
async def copy_callback(callback, conn: sqlite3.Connection, config: Config):
    key = callback.data[3:]
    got = await _lyrics_text_for(key, conn)
    if got is None:
        await callback.answer(brief_error("lyrics"), show_alert=True); return
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
    allowed, reason = can_use_feature(conn, message.from_user.id, "lyrics")
    if not allowed:
        await _answer(message, conn, t(reason, "fa")); return
    query = sanitize_text(text, 200)
    title, artist = query, ""
    for sep in (" - ", " – ", " — ", "-"):
        if sep in query:
            left, _, right = query.partition(sep)
            if left.strip() and right.strip():
                artist, title = left.strip(), right.strip()
                break
    dbmod.record_search(conn, message.from_user.id, query, source="lyrics", ok=True)
    await _answer(message, conn, t("searching", "fa"))
    try:
        lyrics, source, responded = await fetch_lyrics_with_source(title, artist)
    except Exception:
        lyrics, source, responded = None, "", False
    if not lyrics:
        if not responded:
            await _answer(message, conn, "منابع متن آهنگ در دسترس نیستند. چند لحظه دیگر دوباره تلاش کنید.")
        else:
            await _answer(message, conn, lyrics_not_found_text(query))
        return
    key = profeat.remember_result(title, artist, "", "")
    LYRICS_CACHE[key] = (title, artist, lyrics)
    kb = _lyrics_card_kb(key, title, artist, lyrics, conn, config)
    header = f"📝 متن آهنگ «{title}»" + (f" — {artist}" if artist else "")
    src = f"\nمنبع: {source}" if source else ""
    await _answer(message, conn, f"{header}\n\n{lyrics[:3500]}{src}", reply_markup=kb)


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


async def run_bot(config: Config) -> None:
    from ..db import init_db

    if not config.has_token:
        print("خطا: BOT_TOKEN تنظیم نشده است. فایل .env را از روی .env.example بسازید و توکن BotFather را وارد کنید.")
        return
    conn = init_db(config.db_path)
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
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        if web:
            web.shutdown()
        conn.close()
