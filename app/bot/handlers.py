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
from ..i18n import t
from ..logging_config import get_logger, log_event
from ..media import process_link
from ..search import SearchEngine, SearchResult
from ..search.itunes import ITunesProvider
from ..search.deezer import DeezerProvider
from ..search.smart import smart_search
from ..search.lyric_phrase import lyric_phrase_search
from ..search.lyrics import LANG_NAMES, fetch_lyrics, translate_text
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
    attach_bot_id, brief_error, can_use_feature, format_results,
    link_video_delivery, match_identified_song,
    professional_audio_caption, welcome_text,
)

log = get_logger(__name__)
router = Router()
rate_limiter = RateLimiter()
LAST_RESULTS: dict[str, SearchResult] = {}


def _remember(result: SearchResult) -> str:
    key = f"{result.source}:{result.id}" if result.id else f"{result.source}:{result.title}:{result.artist}"
    LAST_RESULTS[key] = result
    return key


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
    # Minimal: text only, no keyboard. Buttons are added only by a feature.
    await message.answer(welcome_text(conn))


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
    await message.answer(t("help", "fa") + "\n\n" + brain_line + "\n\n" + t("voice_usage", "fa"),
                         reply_markup=kb)


@router.message(Command("voicetotext", "stt"))
async def cmd_voice_to_text(message: Message, conn: sqlite3.Connection,
                            config: Config, state: FSMContext):
    """Button/command entry for voice-to-text: the next voice is transcribed."""
    _register(message, conn)
    if _blocked(conn, message.from_user.id):
        await message.answer(t("blocked", "fa")); return
    allowed, reason = can_use_feature(conn, message.from_user.id, "voice_to_text")
    if not allowed:
        await message.answer(t(reason, "fa")); return
    await state.set_state("voice_to_text")
    await message.answer(t("voice_to_text_prompt", "fa"))


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
    await message.answer(t("suggest_prompt", "fa"))


@router.callback_query(F.data == "suggest:start")
async def suggest_button(callback, conn: sqlite3.Connection, config: Config, state: FSMContext):
    await state.set_state("suggest")
    await callback.answer()
    await callback.message.answer(t("suggest_prompt", "fa"))


@router.message(Command("playlists"))
async def cmd_playlists(message: Message, conn: sqlite3.Connection, config: Config):
    _register(message, conn)
    pls = dbmod.list_playlists(conn, message.from_user.id)
    if not pls:
        await message.answer(t("playlists_empty", "fa")); return
    lines = ["🎶 پلی‌لیست‌های شما:"]
    for pl in pls:
        items = dbmod.playlist_items(conn, pl["id"])
        lines.append(f"• {pl['name']} ({len(items)} آهنگ)" + (f" — {pl['description']}" if pl.get("description") else ""))
    await message.answer("\n".join(lines))


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
        await message.answer("نام پلی‌لیست را بنویسید؛ مثلاً: /newplaylist غمگین"); return
    dbmod.create_playlist(conn, message.from_user.id, name)
    await message.answer(t("playlist_created", "fa"))


@router.message(Command("favorites"))
async def cmd_favorites(message: Message, conn: sqlite3.Connection, config: Config):
    _register(message, conn)
    favs = dbmod.list_favorites(conn, message.from_user.id)
    if not favs:
        await message.answer("😕 هنوز آهنگی به علاقه‌مندی‌ها اضافه نشده است."); return
    lines = ["❤️ علاقه‌مندی‌های شما:"]
    for f in favs:
        lines.append(f"• {f['title']} – {f['artist']}" if f["artist"] else f"• {f['title']}")
    await message.answer("\n".join(lines))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(t("cancelled", "fa"))


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
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


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
        await message.answer(t("blocked", "fa")); return
    allowed, reason = can_use_feature(conn, message.from_user.id, "tts")
    if not allowed:
        await message.answer(t(reason, "fa")); return
    args = (command.args or "").strip()
    if not args:
        await message.answer(t("voice_usage", "fa")); return
    first, _, rest = args.partition(" ")
    gender = "female" if first in ("زن", "خانم", "female") else "male" if first in ("مرد", "آقا", "male") else "female"
    text = rest.strip() if rest.strip() else args
    # Text-to-voice: OpenAI TTS first, Azure as fallback (see _synthesize_voice).
    # With neither key, it honestly says the speech service is not connected.
    status, audio = await _synthesize_voice(text, gender, config)
    if status == "needs_key":
        await message.answer(t("tts_needs_key", "fa"), reply_markup=_web_button(conn, config)); return
    if status != "ok" or not audio:
        await message.answer(t("tts_error", "fa")); return
    await message.answer_voice(BufferedInputFile(audio, filename="voice.mp3"),
                               caption=f"{t('tts_ready', 'fa')}\n{text[:200]}")


async def _send_link_video(message: Message, outcome, bot_id: str = "") -> None:
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
                await send_video(outcome.official_url,
                                 caption=attach_bot_id("🎬 ویدیوی همان لینک برای دانلود", bot_id))
                return
            except Exception:
                pass
        await message.answer(text)
    elif delivery == "platform_official":
        await message.answer(text)
    else:  # official_link only (e.g. a direct audio file): show the link, clean.
        await message.answer(outcome.official_url)


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

    Returns (status, title, artist, needs_file, page_text). The 5th
    value is new: title/caption text advertised by the page, or "".
    Callers must unpack tolerantly (older fakes return 4 values).

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
            return ("error", "", "", False, "")
        if len(data) > config.max_file_mb * 1024 * 1024:
            return ("error", "", "", False, "")
        result = await _identify_audio(data, "clip.mp4", config)
        return (result.status, result.title, result.artist, False, "")
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
                    return ("identified", result.title, result.artist, False, page_text)
        except Exception:
            pass
    if config.audd_token:
        try:
            result = await identify_url(url, config.audd_token)
        except Exception:
            result = None
        if result is not None and result.status == "identified":
            return ("identified", result.title, result.artist, False, page_text)
    return ("needs_file", "", "", True, page_text)


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
        await message.answer(t("channel_not_configured", "fa"))
        return False
    if not (page_text or "").strip():
        await message.answer(t("channel_no_result", "fa"))
        return False
    try:
        posts = await search_channels(channels, page_text)
    except Exception:
        posts = []
    if not posts:
        await message.answer(t("channel_no_result", "fa"))
        return False
    post = posts[0]
    await message.answer(
        f"{t('channel_post', 'fa')}\n{post['post_url']}\n{post['text'][:300]}")
    return True


async def _send_professional_song_card(message, conn, title: str, artist: str, bot_id: str = ""):
    """One clean card for an identified song: audio + official link, no buttons
    except lyrics and playlist (the two features the user asked for).

    Catalog search is song-only (iTunes + Deezer, current engine preserved).
    Only an exact-ish catalog match is sent as the card; otherwise only the
    clean identified title plus one honest line. Never a nearest song.
    The file caption carries the bot ID (shared content); chat text does not.
    """
    query = f"{title} {artist}".strip()
    engine = SearchEngine([ITunesProvider(entity="song"), DeezerProvider()])
    try:
        results, _ = await smart_search(engine, query, limit=10)
    except Exception:
        results = []
    dbmod.record_search(conn, message.from_user.id, query, ok=bool(results))
    match = match_identified_song(results, title, artist)
    if match is None:
        await message.answer(f"{title} – {artist}\n{t('catalog_preview_unavailable', 'fa')}")
        return
    key = _remember(match)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📝 متن آهنگ", callback_data=f"ly:{key}"),
        InlineKeyboardButton(text="🎶 افزودن به پلی‌لیست", callback_data=f"pl:{key}"),
    ]])
    if match.preview_url:
        send_audio = getattr(message, "answer_audio", None)
        if send_audio is not None:
            try:
                await send_audio(match.preview_url,
                                 caption=professional_audio_caption(match, bot_id),
                                 reply_markup=kb)
                return
            except Exception:
                pass
    await message.answer(professional_audio_caption(match, bot_id), reply_markup=kb)


async def _synthesize_voice(text: str, gender: str, config: Config):
    """Text-to-voice: OpenAI TTS first, Azure Speech as fallback.

    Returns (status, audio_bytes). With neither key configured it returns
    "needs_key" without calling any service; with a key it never returns
    a wrong-language substitute voice.
    """
    if config.openai_api_key:
        try:
            status, audio = await synthesize_openai(text, gender, config.openai_api_key)
        except Exception:
            status, audio = "error", b""
        if status == "ok" and audio:
            return status, audio
        if not (config.azure_speech_key and config.azure_speech_region):
            return status, audio
    if config.azure_speech_key and config.azure_speech_region:
        return await synthesize(text, gender, config.azure_speech_key, config.azure_speech_region)
    return "needs_key", b""


async def _transcribe_voice(data: bytes, filename: str, config: Config):
    """Voice-to-text: OpenAI Whisper first, Azure STT as fallback.

    Returns (status, text). With neither key configured it returns
    "needs_key" without calling any service, and text is never fabricated.
    """
    if config.openai_api_key:
        try:
            status, text = await transcribe_whisper(data, config.openai_api_key, filename)
        except Exception:
            status, text = "error", ""
        if status == "ok":
            return status, text
        if not (config.azure_speech_key and config.azure_speech_region):
            return status, text
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
        await message.answer("⏳ لطفاً کمی صبر کنید و دوباره تلاش کنید."); return
    await message.answer(t("searching", "fa"))
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
    await message.answer(text, reply_markup=kb)
    song = next((r for r in results if r.kind == "music"), None)
    if song is not None:
        _remember(song)
        bot_id = dbmod.get_bot_id(conn, config) if config is not None else ""
        if song.preview_url:
            send_audio = getattr(message, "answer_audio", None)
            if send_audio is not None:
                try:
                    await send_audio(song.preview_url,
                                     caption=professional_audio_caption(song, bot_id))
                    return
                except Exception:
                    pass
        await message.answer(professional_audio_caption(song, bot_id))


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
        await _send_link_video(message, outcome, bot_id=dbmod.get_bot_id(conn, config))
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
        if status == "identified":
            dbmod.set_cached_search(
                conn, f"link:{outcome.official_url}",
                _json.dumps({"title": title, "artist": artist}, ensure_ascii=False))
            await _send_professional_song_card(message, conn, title, artist, bot_id=dbmod.get_bot_id(conn, config))
        elif needs_file:
            await message.answer(t("send_video_for_identification", "fa"))
            # Channel stage was previously never run on links: run it
            # now on the page's advertised text, or say channels are
            # not configured — never skip silently.
            await _run_link_channel_stage(message, conn, config, page_text)
        else:
            await message.answer(t("link_stage_not_found", "fa"))
            await _run_link_channel_stage(message, conn, config, page_text)
        return
    allowed, reason = can_use_feature(conn, message.from_user.id, "song_search")
    if not allowed:
        await message.answer(t(reason, "fa")); return
    if is_mood_request(text):
        await _run_suggest(message, conn, text, config)
        return
    # Free text: if the superior-intelligence layer is connected, let it
    # understand the intent (song name / artist / lyric line) and clean
    # the query; otherwise the original text goes to the smart search
    # unchanged (rule-based fallback, never claimed to be an AI answer).
    query = sanitize_text(text, 300)
    if config.openai_api_key:
        try:
            query = await brain_clean_query(query, config.openai_api_key)
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
        status, text = await _transcribe_voice(data, filename, config)
        if status == "needs_key":
            await message.answer(t("voice_to_text_needs_key", "fa")); return
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
            await message.answer(
                f"{t('audio_index_match', 'fa')}\n{label}"
                + (f" (چنل: {ch})" if ch else "")
                + (f"\n{post}" if post else ""))
        else:
            dbmod.index_audio_file(
                conn, audio_fingerprint(data), title=caption, artist="",
                channel=_forward_channel(message), caption=caption)
        await message.answer(t("audio_not_found", "fa"))
        # AI/channel-only fallback: the user's own file, labelled honestly
        # (never claimed identified), sent back for download with bot ID.
        try:
            media = message.video or message.audio or message.voice or message.document
            cap = attach_bot_id(t("ai_channel_note", "fa"), dbmod.get_bot_id(conn, config))
            if message.video is not None:
                await message.answer_video(media.file_id, caption=cap)
            elif message.audio is not None:
                await message.answer_audio(media.file_id, caption=cap)
        except Exception:
            pass
        if caption:
            await _do_search(message, conn, sanitize_text(caption, 300), config=config)
        return
    if result.status != "identified":
        await message.answer(t("audio_error", "fa")); return
    await _send_professional_song_card(message, conn, result.title, result.artist, bot_id=dbmod.get_bot_id(conn, config))



async def _run_suggest(message, conn, text: str, config):
    """Mood suggestion: real catalog results only, never invented."""
    engine = SearchEngine([ITunesProvider(entity="song"), DeezerProvider()])

    async def _cat(q):
        r, _f = await engine.search(q, limit=10)
        return r
    try:
        mood, results = await suggest_songs(text, _cat)
    except Exception:
        await message.answer(brief_error("suggest")); return
    if not results:
        await message.answer(t("suggest_none", "fa")); return
    lines = [t("suggest_result", "fa")]
    for r in results[:5]:
        _remember(r)
        lines.append(f"• {r.title} – {r.artist}" if r.artist else f"• {r.title}")
    await message.answer("\n".join(lines))
    first = results[0]
    if first.preview_url:
        send_audio = getattr(message, "answer_audio", None)
        if send_audio is not None:
            try:
                await send_audio(first.preview_url,
                                 caption=professional_audio_caption(first, dbmod.get_bot_id(conn, config)))
                return
            except Exception:
                pass
    await message.answer(professional_audio_caption(first, dbmod.get_bot_id(conn, config)))


@router.callback_query(F.data.startswith("ly:"))
async def lyrics_callback(callback, conn: sqlite3.Connection, config: Config):
    key = callback.data[3:]
    result = LAST_RESULTS.get(key)
    if result is None:
        await callback.answer(brief_error("lyrics"), show_alert=True); return
    await callback.answer()
    try:
        lyrics = await fetch_lyrics(result.title, result.artist)
    except Exception:
        lyrics = None
    if not lyrics:
        await callback.message.answer(t("lyrics_not_found", "fa")); return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 ترجمه", callback_data=f"tr:{key}"),
        InlineKeyboardButton(text="📋 کپی", callback_data=f"cp:{key}"),
    ]])
    await callback.message.answer(f"📝 متن آهنگ: {result.title} – {result.artist}\n\n{lyrics[:3500]}",
                                  reply_markup=kb)


@router.callback_query(F.data.startswith("tr:"))
async def translate_prompt_callback(callback, conn: sqlite3.Connection, config: Config):
    key = callback.data[3:]
    if LAST_RESULTS.get(key) is None:
        await callback.answer(brief_error("translate"), show_alert=True); return
    await callback.answer()
    rows = [[InlineKeyboardButton(text=LANG_NAMES[c], callback_data=f"tl:{c}:{key}")]
            for c in ("fa", "en", "tr", "ar", "de", "fr", "es", "ru")]
    await callback.message.answer(t("lyrics_translate_prompt", "fa"),
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("tl:"))
async def translate_callback(callback, conn: sqlite3.Connection, config: Config):
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer(brief_error("translate"), show_alert=True); return
    _tl, lang, key = parts
    result = LAST_RESULTS.get(key)
    if result is None:
        await callback.answer(brief_error("translate"), show_alert=True); return
    await callback.answer()
    try:
        lyrics = await fetch_lyrics(result.title, result.artist)
    except Exception:
        lyrics = None
    if not lyrics:
        await callback.message.answer(t("lyrics_not_found", "fa")); return
    try:
        translated = await translate_text(lyrics, lang)
    except Exception:
        translated = None
    if not translated:
        await callback.message.answer(brief_error("translate")); return
    await callback.message.answer(
        f"🌐 ترجمه ({LANG_NAMES.get(lang, lang)}): {result.title} – {result.artist}\n\n{translated[:3500]}")


@router.callback_query(F.data.startswith("cp:"))
async def copy_callback(callback, conn: sqlite3.Connection, config: Config):
    key = callback.data[3:]
    result = LAST_RESULTS.get(key)
    if result is None:
        await callback.answer(brief_error("lyrics"), show_alert=True); return
    await callback.answer()
    try:
        lyrics = await fetch_lyrics(result.title, result.artist)
    except Exception:
        lyrics = None
    if not lyrics:
        await callback.message.answer(t("lyrics_not_found", "fa")); return
    # Copy/share version: bot ID appended; the main lyrics message stays clean.
    shared = attach_bot_id(f"{result.title} – {result.artist}\n\n{lyrics[:3000]}",
                           dbmod.get_bot_id(conn, config))
    await callback.message.answer(f"{t('lyrics_copy_label', 'fa')}\n```\n{shared}\n```", parse_mode="Markdown")


@router.callback_query(F.data.startswith("pl:"))
async def playlist_pick_callback(callback, conn: sqlite3.Connection, config: Config):
    key = callback.data[3:]
    if LAST_RESULTS.get(key) is None:
        await callback.answer(brief_error("search"), show_alert=True); return
    await callback.answer()
    pls = dbmod.list_playlists(conn, callback.from_user.id)
    if not pls:
        pl = dbmod.create_playlist(conn, callback.from_user.id, "علاقه‌مندی‌ها")
        pls = [pl]
    rows = [[InlineKeyboardButton(text=pl["name"], callback_data=f"pa:{pl['id']}:{key}")]
            for pl in pls]
    await callback.message.answer("🎶 پلی‌لیست را انتخاب کنید:",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("pa:"))
async def playlist_add_callback(callback, conn: sqlite3.Connection, config: Config):
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer(brief_error("search"), show_alert=True); return
    _pa, pl_id, key = parts
    result = LAST_RESULTS.get(key)
    if result is None:
        await callback.answer(brief_error("search"), show_alert=True); return
    added = dbmod.add_to_playlist(conn, int(pl_id), result.title, result.artist, result.url, result.preview_url)
    await callback.answer(t("playlist_added", "fa") if added else t("playlist_exists", "fa"), show_alert=True)


@router.callback_query(F.data.startswith("similar:"))
async def similar_callback(callback, conn: sqlite3.Connection, config: Config):
    query = callback.data.split(":", 1)[1]
    await callback.answer()
    short = " ".join(query.split()[:3])
    await _do_search(callback.message, conn, short, include_video=False, config=config)


def build_dispatcher(conn: sqlite3.Connection, config: Config) -> Dispatcher:
    dp = Dispatcher()
    dp.workflow_data.update(conn=conn, config=config)
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
