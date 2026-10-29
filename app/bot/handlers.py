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
import sqlite3

import httpx
from aiogram import Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from .. import db as dbmod
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
from ..security import RateLimiter, sanitize_text
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
    match_identified_song, professional_audio_caption, welcome_text,
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


@router.message(Command("start"))
async def cmd_start(message: Message, conn: sqlite3.Connection, config: Config):
    _register(message, conn)
    # Minimal: welcome text + one «قابلیت‌ها» button. The menu itself is
    # dynamic (DB features table); new capabilities surface a button only
    # when added by the admin. No always-visible command clutter.
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✨ قابلیت‌ها", callback_data="menu:open")]])
    await _answer(message, conn, welcome_text(conn), reply_markup=kb)


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
    await callback.message.answer("📝 نام آهنگ و خواننده را بنویسید، مثلاً: مهدی احمدوند - دوست دارم")


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
    await _answer(message, conn, t("help", "fa") + "\n\n" + brain_line + "\n\n" + t("voice_usage", "fa"),
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
    await _answer(message, conn, t("voice_to_text_prompt", "fa"))


@router.callback_query(F.data == "voice_to_text:start")
async def voice_to_text_button(callback, conn: sqlite3.Connection,
                               config: Config, state: FSMContext):
    allowed, reason = can_use_feature(conn, callback.from_user.id, "voice_to_text")
    if not allowed:
        await callback.answer(t(reason, "fa"), show_alert=True); return
    await state.set_state("voice_to_text")
    await callback.answer()
    await callback.message.answer(t("voice_to_text_prompt", "fa"))



@router.message(Command("suggest", "request"))
async def cmd_suggest(message: Message, conn: sqlite3.Connection, config: Config, state: FSMContext):
    _register(message, conn)
    await state.set_state("suggest")
    await _answer(message, conn, t("suggest_prompt", "fa"))


@router.callback_query(F.data == "suggest:start")
async def suggest_button(callback, conn: sqlite3.Connection, config: Config, state: FSMContext):
    await state.set_state("suggest")
    await callback.answer()
    await _answer(callback.message, conn, t("suggest_prompt", "fa"))


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
    await _answer(message, conn, t("cancelled", "fa"))


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


async def _send_link_video(message: Message, conn, outcome, bot_id: str = "") -> None:
    """Send the video of the sent link itself for download — no video search.

    Direct public video file: send that same file. Platform link: send
    the official video via its official open link. Never fetch private
    or protected platform content.
    """
    delivery, text = link_video_delivery(outcome)
    if delivery == "direct_video_file":
        send_video = getattr(message, "answer_video", None)
        if send_video is not None:
            try:
                sent = await send_video(outcome.official_url,
                                        caption=attach_bot_id("🎬 ویدیوی همان لینک برای دانلود", bot_id))
                _track(conn, message, sent)
                return
            except Exception:
                pass
        await _answer(message, conn, text)
    elif delivery == "platform_official":
        await _answer(message, conn, text)
    else:  # official_link only (e.g. a direct audio file): show the link, clean.
        await _answer(message, conn, outcome.official_url)


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


async def _identify_audio(data: bytes, filename: str, config: Config) -> AudioIdResult:
    """Shazam signature first (no token), AudD with a token as fallback.

    Shazam is tried on several windows (full, then later windows) so a
    clip with speech over the start, a remix intro, or a shortened
    copy has more than one chance; only an identified result is
    accepted. Windows are tried through the handlers-level
    identify_bytes_shazam (same function the shazam_id multi-window
    helper uses) so a sliced window that fails to decode only yields
    "error", never a false match. A catalog result is never involved
    here, so nothing can be mislabelled as identified.
    """
    shazam = None
    windows = [data]
    if data and len(data) > 200_000:
        windows.append(data[len(data) // 3:])
        windows.append(data[2 * len(data) // 3:])
    for window in windows:
        try:
            candidate = await identify_bytes_shazam(window)
        except Exception:
            candidate = None
        if candidate is not None:
            shazam = candidate
            if candidate.status == "identified":
                break
    if shazam is not None and shazam.status == "identified":
        return AudioIdResult(status="identified", title=shazam.title, artist=shazam.artist)
    if config.audd_token:
        try:
            audd = await identify_bytes(data, filename, config.audd_token)
        except Exception:
            audd = None
        if audd is not None and audd.status == "identified":
            return audd
    if shazam is not None and shazam.status == "not_found":
        return AudioIdResult(status="not_found")
    return AudioIdResult(status="error")


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

    REAL LIMIT, stated plainly: Instagram pages in practice do NOT
    advertise a public media file (JS-rendered / login-walled), so
    media_url is usually None and no audio is available to fingerprint.
    That — not a search bug — is why an Instagram link alone used to
    end at "send the video". The caller must therefore still run the
    channel stage on page_text (below) and say so, instead of stopping
    silently. Downloaded bytes are used only for fingerprinting and
    then discarded.
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
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(media_url)
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
    advertised title/caption text, that text is the query; if no
    channels are configured, that is said briefly instead of skipping
    silently; a found post is labelled as a channel post, never as an
    identified catalog song. Returns True if a post was sent.
    """
    if config is None:
        return False
    channels = dbmod.get_channels(conn, config)
    if not channels:
        await _answer(message, conn, t("channel_not_configured", "fa"))
        return False
    if not (page_text or "").strip():
        await _answer(message, conn, t("channel_no_result", "fa"))
        return False
    try:
        posts = await search_channels(channels, page_text)
    except Exception:
        posts = []
    if not posts:
        await _answer(message, conn, t("channel_no_result", "fa"))
        return False
    post = posts[0]
    await _answer(message, conn,
                  f"{t('channel_post', 'fa')}\n{post['post_url']}\n{post['text'][:300]}")
    return True


async def _send_professional_song_card(message, conn, title: str, artist: str, bot_id: str = "",
                                       bank_token: str | None = None,
                                       own_media: tuple[str, str] | None = None):
    """One clean card for an identified song.

    - Exact catalog match (version-aware) -> audio card with the pro
      buttons (heart / lyrics / add-to-playlist). Never a nearest song.
    - Identified but no catalog file -> the FULL same-version file first
      (find_full_song_file); only if none, the user's own media re-sent
      as a clearly-labelled replacement; then the honest message plus
      the similar-songs and bank buttons.
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
    if match is not None:
        if match.preview_url:
            send_audio = getattr(message, "answer_audio", None)
            if send_audio is not None:
                try:
                    sent = await send_audio(match.preview_url,
                                            caption=professional_audio_caption(match, bot_id),
                                            reply_markup=kb)
                    _track(conn, message, sent)
                    return
                except Exception:
                    pass
        await _answer(message, conn, professional_audio_caption(match, bot_id), reply_markup=kb)
        return
    # Identified, but no catalog file: full-version-first.
    full = None
    try:
        full = await find_full_song_file(title, artist)
    except Exception:
        full = None
    if full:
        dur = full["duration_sec"]
        label = (f" — مدت: {int(dur // 60)}:{int(dur % 60):02d}" if dur < 90
                 else " — مدت کامل: بیش از ۹۰ ثانیه")
        try:
            sent = await message.answer_audio(
                BufferedInputFile(full["data"], filename=f"{title[:60]}.{full['ext']}"),
                caption=attach_bot_id(
                    f"🎵 نسخه کامل آهنگ پیدا و آماده شد{label}\n{title} – {artist}", bot_id),
                reply_markup=kb)
            _track(conn, message, sent)
            return
        except Exception:
            pass
    # No full file: the user's own media as a labelled replacement (if any).
    # Kept button-free and minimal; the 🔎 second search belongs to the
    # not-identified flows (media/link not-found), where it is offered.
    if own_media is not None:
        kind, file_id = own_media
        cap = attach_bot_id(
            "آهنگ شناسایی شد، اما فایل اصلی آن برای پخش پیدا نشد؛ صدای موجود در ویدیوی شما "
            "به‌عنوان فایل صوتی ارسال شد. می‌توانید برای یافتن نسخه کامل یا نسخه‌های دیگر، "
            "جست‌وجوی تکمیلی انجام دهید.", bot_id)
        try:
            if kind == "video":
                sent = await message.answer_video(file_id, caption=cap)
            elif kind == "voice":
                sent = await message.answer_voice(file_id, caption=cap)
            else:
                sent = await message.answer_audio(file_id, caption=cap)
            _track(conn, message, sent)
            return
        except Exception:
            pass
    await _answer(message, conn,
                  f"{title} – {artist}\nآهنگ شناسایی شد، اما فایلش برای پخش پیدا نشد.")


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
            send_audio = getattr(message, "answer_audio", None)
            if send_audio is not None:
                try:
                    sent = await send_audio(song.preview_url,
                                            caption=professional_audio_caption(song, bot_id),
                                            reply_markup=kb2)
                    _track(conn, message, sent)
                    return
                except Exception:
                    pass
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
    if _blocked(conn, message.from_user.id):
        await message.answer(t("blocked", "fa")); return
    text = (message.text or "").strip()
    current_state = None
    if state is not None:
        try:
            current_state = await state.get_state()
        except Exception:
            current_state = None
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
    if text.startswith("http://") or text.startswith("https://") or "://" in text:
        # Intent from a link is deterministic (no button, no brain call
        # needed): the link's own video, then its audio identification.
        allowed, reason = can_use_feature(conn, message.from_user.id, "link_fetch")
        if not allowed:
            await message.answer(t(reason, "fa")); return
        outcome = process_link(text, config.max_file_mb)
        if not outcome.ok:
            await message.answer(t(outcome.message, "fa")); return
        # Professional minimal link flow: video, then one clean song card.
        # No catalog video search, no video section, no button grid, no
        # empty "not found" boxes.
        await _send_link_video(message, conn, outcome, bot_id=dbmod.get_bot_id(conn, config))
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
            await _answer(message, conn, t("send_video_for_identification", "fa"),
                          reply_markup=_bank_button(_tok))
            # Channel stage was previously never run on links: run it
            # now on the page's advertised text, or say channels are
            # not configured — never skip silently.
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
    if _blocked(conn, message.from_user.id):
        await message.answer(t("blocked", "fa")); return
    current_state = None
    try:
        current_state = await state.get_state()
    except Exception:
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
        await _answer(message, conn, t("audio_not_found", "fa"),
                      reply_markup=_bank_button(_tok))
        # AI/channel-only fallback: the user's own file, labelled honestly
        # (never claimed identified), sent back for download with bot ID.
        try:
            cap = attach_bot_id(t("ai_channel_note", "fa"), dbmod.get_bot_id(conn, config))
            if message.video is not None:
                sent = await message.answer_video(media.file_id, caption=cap)
            elif message.audio is not None:
                sent = await message.answer_audio(media.file_id, caption=cap)
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
        await _answer(message, conn, t("audio_error", "fa")); return
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
    lines = [f"🎧 پیشنهاد برای: {mood}"]
    for r in results[:5]:
        lines.append(f"• {r.title} – {r.artist}" if r.artist else f"• {r.title}")
    await _answer(message, conn, "\n".join(lines))
    first = results[0]
    key = profeat.remember_result(first.title, first.artist, first.url, first.preview_url)
    is_fav = profeat.is_favorite(conn, message.from_user.id, first.title, first.artist)
    kb = InlineKeyboardMarkup(inline_keyboard=profeat.build_song_card_buttons(key, is_fav))
    if first.preview_url:
        send_audio = getattr(message, "answer_audio", None)
        if send_audio is not None:
            try:
                sent = await send_audio(first.preview_url,
                                        caption=professional_audio_caption(first, dbmod.get_bot_id(conn, config)),
                                        reply_markup=kb)
                _track(conn, message, sent)
                return
            except Exception:
                pass
    await _answer(message, conn, professional_audio_caption(first, dbmod.get_bot_id(conn, config)),
                  reply_markup=kb)


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
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 ترجمه", callback_data=f"tr:{key}"),
        InlineKeyboardButton(text="📋 کپی متن آهنگ", callback_data=f"cp:{key}"),
    ]])
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
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 کپی ترجمه", callback_data=f"ltc:{key}:{lang}"),
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
    await callback.answer("کپی شد")
    # Copy/share version: bot ID appended; the chat messages stay clean.
    shared = attach_bot_id(translated[:3000], dbmod.get_bot_id(conn, config))
    await callback.message.answer(f"📋 کپی ترجمه:\n```\n{shared}\n```", parse_mode="Markdown")


@router.callback_query(F.data.startswith("cp:"))
async def copy_callback(callback, conn: sqlite3.Connection, config: Config):
    key = callback.data[3:]
    got = await _lyrics_text_for(key, conn)
    if got is None:
        await callback.answer(brief_error("lyrics"), show_alert=True); return
    await callback.answer("کپی شد")
    title, artist, lyrics = got
    # Copy/share version: bot ID appended; the main lyrics message stays clean.
    shared = attach_bot_id(f"{title}" + (f" – {artist}" if artist else "") + f"\n\n{lyrics[:3000]}",
                           dbmod.get_bot_id(conn, config))
    await callback.message.answer(f"📋 کپی متن آهنگ:\n```\n{shared}\n```", parse_mode="Markdown")


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
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 ترجمه", callback_data=f"tr:{key}"),
        InlineKeyboardButton(text="📋 کپی متن آهنگ", callback_data=f"cp:{key}"),
    ]])
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
    dp = build_dispatcher(conn, config)
    log_event(log, "bot_start", **config.safe_summary())
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        if web:
            web.shutdown()
        conn.close()
