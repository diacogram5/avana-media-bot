"""Link rules: no catalog video search on a link, song download on a
found song, and the link's own video sent for download.

No network and no real Bot API are used here: the handler is exercised
with a fake message, and the search / audio-identification calls are
monkeypatched to record exactly which search flavour was invoked.
"""
import pytest

from aiogram.types import BufferedInputFile

from app.bot import audio_send, handlers
from app.bot.logic import (
    clean_title, is_video_file_url, link_video_delivery,
    professional_audio_caption,
)
from app.config import Config
from app.media import process_link
from app.search import SearchResult
from app.search.itunes import parse_itunes_payload
from app.services.audio_id import AudioIdResult


def test_itunes_payload_keeps_preview_url():
    parsed = parse_itunes_payload({"results": [{
        "trackId": 7, "trackName": "Dooset Daram", "artistName": "Mehdi Ahmadvand",
        "trackViewUrl": "https://music.apple.com/track/7",
        "previewUrl": "https://audio.example.com/preview.m4a",
    }]})
    assert parsed[0].preview_url == "https://audio.example.com/preview.m4a"
    assert parsed[0].url == "https://music.apple.com/track/7"


def test_professional_audio_caption_is_clean_and_official_only():
    """Professional card caption: clean title + official link, no clutter.

    No Google/Spotify/YouTube Music buttons, no stats, no lecture text.
    The audio message itself carries the legal preview; the caption's
    link is the official download/purchase link. A full copyrighted file
    is never claimed to be sent here.
    """
    result = SearchResult(
        source="itunes", id="7", title="Dooset Daram",
        artist="Mehdi Ahmadvand",
        url="https://music.apple.com/track/7",
        preview_url="https://audio.example.com/preview.m4a",
    )
    assert clean_title(result) == "Dooset Daram – Mehdi Ahmadvand"
    caption = professional_audio_caption(result)
    assert caption.startswith("Dooset Daram – Mehdi Ahmadvand")
    # No-links policy: no source URLs and no bot-ID links under media.
    assert "https://music.apple.com/track/7" not in caption
    assert "@" not in caption
    for banned in ("Google", "Spotify", "YouTube Music", "آمار", "جستجو بر اساس هنرمند"):
        assert banned not in caption


def test_link_video_delivery_direct_and_platform():
    direct = process_link("https://example.com/clip.mp4")
    kind, text = link_video_delivery(direct)
    assert kind == "direct_video_file"
    assert "https://example.com/clip.mp4" in text

    platform = process_link("https://www.youtube.com/watch?v=abc123")
    kind, text = link_video_delivery(platform)
    assert kind == "platform_official"
    assert text == "🎬 ویدیوی همان لینک:\nhttps://www.youtube.com/watch?v=abc123"
    # Professional minimal: no lecture paragraph, no extra sections.

    # A direct audio file is not a video file for this rule.
    assert not is_video_file_url("https://example.com/song.mp3")
    assert is_video_file_url("https://example.com/clip.mp4?x=1")
    audio = process_link("https://example.com/song.mp3")
    assert link_video_delivery(audio)[0] == "official_link"


class _FakeUser:
    def __init__(self):
        self.id = 42
        self.username = "tester"
        self.first_name = "Test"


class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.from_user = _FakeUser()
        self.answers = []
        self.videos = []
        self.audios = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append(text)

    async def answer_video(self, video, caption=None, reply_markup=None, **kwargs):
        self.videos.append((video, caption))

    async def answer_audio(self, audio, caption=None, reply_markup=None, **kwargs):
        self.audios.append((audio, caption, reply_markup))
        from types import SimpleNamespace
        return SimpleNamespace(message_id=1)


@pytest.mark.asyncio
async def test_link_handler_no_video_search_and_own_video(monkeypatch, db):
    """A link, professional flow: own video, then one clean song card.

    No video search, no button grid, no Google/Spotify/stats clutter,
    no empty sections. The song card itself is stubbed here; its own
    behaviour is tested in test_professional_after_search.py.
    """
    config = Config()

    seen = {}

    async def fake_identify_link(outcome, cfg, *args, **kwargs):
        seen["link_url"] = outcome.official_url
        return ("identified", "Dooset Daram", "Mehdi Ahmadvand", False)

    async def fake_card(message, conn, title, artist, **kw):
        seen["card"] = (title, artist)
        await message.answer(f"{title} – {artist}")

    monkeypatch.setattr(handlers, "_identify_link_audio", fake_identify_link)
    monkeypatch.setattr(handlers, "_send_professional_song_card", fake_card)

    # Direct video file link: own file must be sent, then the card.
    msg = _FakeMessage("https://example.com/clip.mp4")
    await handlers.text_handler(msg, db, config)
    assert msg.videos and msg.videos[0][0] == "https://example.com/clip.mp4"
    assert seen["card"] == ("Dooset Daram", "Mehdi Ahmadvand")
    joined = "\n".join(msg.answers)
    assert "Dooset Daram – Mehdi Ahmadvand" in joined
    for banned in ("Google", "Spotify", "YouTube Music", "آمار",
                   "جستجو بر اساس هنرمند", "آهنگ اشتباه"):
        assert banned not in joined
    assert not any("ویدیو" in a and "جستجو" in a for a in msg.answers), \
        "link path must not produce a video-search section"


@pytest.mark.asyncio
async def test_link_handler_platform_video_official_only(monkeypatch, db):
    config = Config()

    async def fake_identify_link(outcome, cfg, *args, **kwargs):
        return ("needs_file", "", "", True)

    monkeypatch.setattr(handlers, "_identify_link_audio", fake_identify_link)
    msg = _FakeMessage("https://www.youtube.com/watch?v=abc123")
    await handlers.text_handler(msg, db, config)
    assert not msg.videos  # no file fetched from the platform
    joined = "\n".join(msg.answers)
    assert "https://www.youtube.com/watch?v=abc123" in joined
    assert "🎬 ویدیوی همان لینک" in joined
    assert "ویدیو را مستقیم بفرستید" in joined


@pytest.mark.asyncio
async def test_do_search_sends_song_download(monkeypatch, db):
    """_do_search: clean list + one professional card, no separate download text.

    The list carries no source/duration/URL clutter. The song audio goes
    through the unified pipeline (downloaded + re-uploaded as a real
    audio message, never the raw preview URL), and the caption carries
    no source URLs (no-links policy).
    """
    async def fake_smart(engine, query, limit=10):
        # Prove the engine given to a text search still includes video
        # providers by default (unchanged text behaviour).
        assert len(engine.providers) >= 2 and any(getattr(pr, 'entity', None) == 'musicVideo' for pr in engine.providers)
        return [SearchResult(
            source="itunes", id="7", title="Dooset Daram",
            artist="Mehdi Ahmadvand", kind="music",
            url="https://music.apple.com/track/7",
            preview_url="https://audio.example.com/preview.m4a",
        )], "Dooset Daram"

    async def fake_download(url):
        assert url == "https://audio.example.com/preview.m4a"
        return b"ID3\x04\x00\x00" + b"\x00" * 4096, "mp3"

    monkeypatch.setattr(handlers, "smart_search", fake_smart)
    monkeypatch.setattr(audio_send, "_download", fake_download)
    msg = _FakeMessage("دوست دارم")
    await handlers._do_search(msg, db, "دوست دارم")
    joined = "\n".join(msg.answers)
    # Clean list: title – artist only, no source tag, duration or URL.
    assert "1. Dooset Daram – Mehdi Ahmadvand" in joined
    assert "(itunes)" not in joined
    assert "⬇️ دانلود آهنگ:" not in joined
    assert msg.audios
    audio, caption, _kb = msg.audios[0]
    # Pipeline: uploaded file (never the raw CDN URL), clean filename.
    assert isinstance(audio, BufferedInputFile)
    assert audio.filename == "Dooset Daram - Mehdi Ahmadvand.mp3"
    assert caption.startswith("Dooset Daram – Mehdi Ahmadvand")
    assert "https://music.apple.com/track/7" not in caption
    assert "🔊" in caption  # honest quality label
