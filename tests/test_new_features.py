"""New professional features: lyrics/translation, suggest, playlists,
channel engine, bot-ID rule, web fallback, brief errors. Offline fakes
except where a test explicitly uses a live public API (none here)."""
import pytest

from app import db as dbmod
from app.bot.logic import attach_bot_id, brief_error, professional_audio_caption
from app.config import Config
from app.search import SearchResult
from app.search.channel import channel_post_matches, parse_channel_posts
from app.search.deezer import parse_deezer_payload
from app.search.lyric_phrase import lyric_phrase_search
from app.search.lyrics import lyric_overlap, lyric_variants, normalize_fa
from app.search.suggest import detect_mood, is_mood_request, suggest_songs


def _song(title="Dooset Daram", artist="Mehdi Ahmadvand"):
    return SearchResult(source="itunes", id="7", title=title, artist=artist,
                        url="https://music.apple.com/track/7",
                        preview_url="https://audio.example.com/p.m4a")


# --- lyrics: exact, verified, never a nearest guess -------------------------
def test_normalize_and_overlap():
    assert normalize_fa("تو اون وَر دنیا باشی") == normalize_fa("تو اون ور دنیا باشی")
    lyrics = "تو اون ور دنیا باشی پشت ابرا باشی دوست دارم"
    assert lyric_overlap("پشت ابر ها باشی دوستدارم من", lyrics) >= 0.60
    assert lyric_overlap("کلاهمم بی افته توی این حوالی",
                         "دیگه کلاهمم بیفته توی این حوالی") >= 0.60
    assert lyric_overlap("یک عبارت کاملا نامربوط دیگر", lyrics) < 0.60
    assert "پشت ابرا باشی" in lyric_variants("پشت ابر ها باشی")


@pytest.mark.asyncio
async def test_lyric_phrase_verified_only():
    lyrics_map = {("Dooset Daram", "Mehdi Ahmadvand"):
                  "تو اون ور دنیا باشی پشت ابرا باشی دوست دارم"}

    async def catalog(q):
        return [_song(), _song("Sweet Dreams", "Eurythmics")]

    async def fetcher(t, a):
        return lyrics_map.get((t, a))

    out = await lyric_phrase_search("پشت ابر ها باشی دوستدارم من", catalog, fetcher,
                                    site_search=lambda p: [])
    assert [r.title for r in out] == ["Dooset Daram"]

    async def no_lyrics(t, a):
        return None
    assert await lyric_phrase_search("عبارتی که در هیچ ترانه‌ای نیست", catalog, no_lyrics,
                                     site_search=lambda p: []) == []


# --- translation parsing (MyMemory shape, fake client) ----------------------
@pytest.mark.asyncio
async def test_translate_text_fake_client():
    import httpx
    from app.search.lyrics import translate_text

    def handler(request):
        return httpx.Response(200, json={"responseData": {"translatedText": "دوستت دارم"}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await translate_text("Seni seviyorum", "fa", client=client) == "دوستت دارم"


# --- Deezer parse ------------------------------------------------------------
def test_deezer_parse():
    out = parse_deezer_payload({"data": [{"id": 1, "title": "Ghatel",
                                          "artist": {"name": "Saman Jalili"},
                                          "link": "https://deezer.com/track/1",
                                          "preview": "https://cdns-preview/p.mp3"}]})
    assert out[0].source == "deezer" and out[0].artist == "Saman Jalili"


# --- suggest: mood -> real catalog only --------------------------------------
def test_mood_detection():
    assert detect_mood("یه آهنگ غمگین برای شب می‌خوام") == "sad_night"
    assert detect_mood("آهنگ سیستم بیس‌دار") == "bass"
    assert is_mood_request("یه آهنگ غمگین برای شب می‌خوام") is True
    assert is_mood_request("Dooset Daram") is False


@pytest.mark.asyncio
async def test_suggest_never_invents():
    async def catalog(q):
        return [_song()] if "sad" in q else []
    mood, out = await suggest_songs("یه آهنگ غمگین برای شب می‌خوام", catalog)
    assert mood == "sad_night" and out and out[0].title == "Dooset Daram"

    async def empty(q):
        return []
    assert await suggest_songs("یه آهنگ غمگین برای شب می‌خوام", empty) == ("sad_night", [])
    assert await suggest_songs("Dooset Daram", empty) == (None, [])


# --- playlists + favorites (SQLite, survive clear-chat concept) --------------
def test_playlists_and_favorites(db):
    pl = dbmod.create_playlist(db, 42, "غمگین", description="شبانه")
    assert dbmod.add_to_playlist(db, pl["id"], "Dooset Daram", "Mehdi Ahmadvand", "u", "p") is True
    assert dbmod.add_to_playlist(db, pl["id"], "Dooset Daram", "Mehdi Ahmadvand", "u", "p") is False
    assert len(dbmod.playlist_items(db, pl["id"])) == 1
    dbmod.rename_playlist(db, pl["id"], "رانندگی")
    assert dbmod.get_playlist(db, pl["id"])["name"] == "رانندگی"
    assert dbmod.add_favorite(db, 42, "Ghatel", "Saman Jalili") is True
    assert len(dbmod.list_favorites(db, 42)) == 1
    dbmod.delete_playlist(db, pl["id"])
    assert dbmod.list_playlists(db, 42) == []
    assert len(dbmod.list_favorites(db, 42)) == 1  # favorites untouched by playlist delete


def test_search_cache(db):
    assert dbmod.get_cached_search(db, "k") is None
    dbmod.set_cached_search(db, "k", '[{"x": 1}]')
    assert dbmod.get_cached_search(db, "k") == '[{"x": 1}]'


# --- channel engine: accuracy gate -------------------------------------------
def test_channel_accuracy_gate():
    page = ('<div data-post="bbcpersian/123"><div class="tgme_widget_message_photo_wrap"></div>'
            '<div class="tgme_widget_message_text">آهنگ جدید غمگین شب</div></div>'
            '<div data-post="bbcpersian/124"><div class="tgme_widget_message_text">خبر روز بدون فایل</div></div>')
    posts = parse_channel_posts(page, "bbcpersian")
    assert len(posts) == 2 and posts[0]["has_media"] is True
    assert channel_post_matches(posts[0], "آهنگ غمگین شب") is True
    assert channel_post_matches(posts[1], "آهنگ غمگین شب") is False  # no media
    assert channel_post_matches(posts[0], "آهنگ شاد") is False  # not all words


# --- bot-ID rule + brief errors + web fallback --------------------------------
def test_bot_id_rule(db):
    # No-links policy: the bot ID never appears under media files. Its one
    # fixed, deliberate place is the welcome/help signature line.
    assert attach_bot_id("سلام", "") == "سلام"
    assert attach_bot_id("سلام", "@MyBot") == "سلام\n@MyBot"
    assert attach_bot_id("سلام\n@MyBot", "@MyBot") == "سلام\n@MyBot"
    caption = professional_audio_caption(_song(), bot_id="@MyBot")
    assert "@MyBot" not in caption
    assert "https://music.apple.com/track/7" not in caption
    from app.bot.handlers import _with_signature
    config = Config(bot_token="x", bot_id="@MyBot")
    out = _with_signature(db, config, "سلام")
    assert out.endswith("🤖 @MyBot")
    for kind in ("search", "identify", "file", "lyrics", "voice", "translate", "suggest"):
        msg = brief_error(kind)
        assert len(msg) < 120 and "\n" not in msg


def test_web_url_empty_by_default_and_configurable():
    cfg = Config()
    assert cfg.web_url == "" and cfg.bot_id == ""
    cfg.web_url = "https://example.com/bot"
    assert cfg.web_url.startswith("https://")


def test_db_bot_id_and_channels(db):
    cfg = Config()
    assert dbmod.get_bot_id(db, cfg) == ""
    assert dbmod.get_channels(db, cfg) == []
    dbmod.set_setting(db, "bot_id", "@MyBot")
    dbmod.set_setting(db, "channel_usernames", "ch1, @ch2")
    assert dbmod.get_bot_id(db, cfg) == "@MyBot"
    assert dbmod.get_channels(db, cfg) == ["ch1", "ch2"]
