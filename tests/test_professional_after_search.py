"""Professional minimal after-search output + Shazam identification.

Rules tested here:
- Shazam payload parsing never invents a match.
- Link-page media extraction only returns a validated direct media file.
- An identified song is shown as ONE clean card (video, clean title,
  legal preview audio + official link); a merely similar catalog result
  is never shown as the identified song.
- No Faravahr-style beginner layout is copied: no Google / YouTube
  Music / Spotify buttons, no artist-search or wrong-song buttons, no
  stats, no empty sections, no video search for a link.
No network and no real Bot API are used; handlers are exercised with
fake messages and monkeypatched identification/search calls.
"""
import pytest
from types import SimpleNamespace

from app.bot import handlers
from app.bot.logic import (
    clean_title, match_identified_song, professional_audio_caption,
)
from app.config import Config
from app.search import SearchResult
from app.services.link_media import extract_media_url
from app.services.shazam_id import parse_shazam_payload


def _song(title="Dooset Daram", artist="Mehdi Ahmadvand"):
    return SearchResult(
        source="itunes", id="7", title=title, artist=artist, kind="music",
        url="https://music.apple.com/track/7",
        preview_url="https://audio.example.com/preview.m4a",
    )


# --- Shazam payload parsing: never invent a match -------------------------

def test_shazam_payload_identified_and_not_found():
    hit = parse_shazam_payload({
        "matches": [{"id": "1"}],
        "track": {"title": "Dooset Daram", "subtitle": "Mehdi Ahmadvand"},
    })
    assert hit.status == "identified"
    assert hit.title == "Dooset Daram" and hit.artist == "Mehdi Ahmadvand"
    assert parse_shazam_payload({}).status == "not_found"
    assert parse_shazam_payload({"matches": []}).status == "not_found"
    assert parse_shazam_payload({"track": {"title": "  ", "subtitle": "X"}}).status == "not_found"
    assert parse_shazam_payload({"track": {"subtitle": "Mehdi Ahmadvand"}}).status == "not_found"


# --- Link-page media extraction --------------------------------------------

def test_extract_media_url_og_video_and_json():
    page = '<html><head><meta property="og:video" content="https://cdn.example.com/clip.mp4"></head></html>'
    assert extract_media_url(page) == "https://cdn.example.com/clip.mp4"
    page2 = '<script>{"video_url": "https:\\/\\/cdn.example.com\\/reel.mp4"}</script>'
    assert extract_media_url(page2) == "https://cdn.example.com/reel.mp4"


def test_extract_media_url_rejects_player_and_private():
    assert extract_media_url("<html><body>no media here</body></html>") is None
    assert extract_media_url(
        '<meta property="og:video" content="https://127.0.0.1/clip.mp4">') is None
    assert extract_media_url(
        '<meta property="og:video" content="https://example.com/watch">') is None


# --- Identified-song matching: exact only, never a nearest song ------------

def test_match_identified_song_exact_only():
    results = [_song(), _song(title="Dooset Daram", artist="Other Singer"),
               _song(title="Sweet Dreams", artist="Eurythmics")]
    assert match_identified_song(results, "Dooset Daram", "Mehdi Ahmadvand") is results[0]
    assert match_identified_song(results, "Dooset Daram", "Unknown Artist") is None
    assert match_identified_song(results, "Some Other Song", "Mehdi Ahmadvand") is None


def test_professional_caption_has_no_beginner_layout():
    caption = professional_audio_caption(_song())
    assert clean_title(_song()) == "Dooset Daram – Mehdi Ahmadvand"
    assert caption.startswith("Dooset Daram – Mehdi Ahmadvand")
    # No-links policy: no source URLs under the song.
    assert "https://music.apple.com/track/7" not in caption
    for banned in ("Google", "Spotify", "YouTube Music", "آمار",
                   "جستجو بر اساس هنرمند", "آهنگ اشتباه"):
        assert banned not in caption


class _FakeUser:
    def __init__(self):
        self.id = 42
        self.username = "tester"
        self.first_name = "Test"


class _FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.from_user = _FakeUser()
        self.answers = []
        self.audios = []
        self.last_markup = None

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)
        self.last_markup = reply_markup

    async def answer_audio(self, audio, caption=None, reply_markup=None,
                           title=None, performer=None, duration=None):
        self.audios.append((audio, caption, reply_markup, title, performer, duration))
        return SimpleNamespace(message_id=1)


@pytest.mark.asyncio
async def test_professional_card_full_version_first(monkeypatch, db):
    """Identified song -> FULL version first as real audio (never a
    preview-as-song); nearest-only with no full file -> honest message."""
    from aiogram.types import BufferedInputFile

    async def fake_smart_exact(engine, query, limit=10):
        return [_song()], "Dooset Daram"

    async def fake_full(artist, title):
        assert (artist, title) == ("Mehdi Ahmadvand", "Dooset Daram")
        return {"data": b"F" * 200000, "ext": "mp3",
                "duration_sec": 210.0, "source": "s"}

    monkeypatch.setattr(handlers, "smart_search", fake_smart_exact)
    monkeypatch.setattr(handlers, "find_full_song_file", fake_full)
    msg = _FakeMessage()
    await handlers._send_professional_song_card(msg, db, "Dooset Daram", "Mehdi Ahmadvand")
    assert msg.audios, "the full version must be sent"
    audio, caption, kb, title, performer, duration = msg.audios[0]
    assert isinstance(audio, BufferedInputFile), "uploaded bytes, not a CDN URL"
    assert title == "Dooset Daram" and performer == "Mehdi Ahmadvand"
    assert duration == 210
    assert "نسخه کامل" in caption
    # Song card: heart (favorite) + lyrics + add-to-playlist.
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels == ["🤍", "📝 متن آهنگ", "🎶 افزودن به پلی‌لیست"]
    assert not msg.answers  # no extra text messages

    async def fake_smart_nearest(engine, query, limit=10):
        return [_song(title="Sweet Dreams", artist="Eurythmics")], "Dooset Daram"

    async def fake_full_none(artist, title):
        return None

    monkeypatch.setattr(handlers, "smart_search", fake_smart_nearest)
    monkeypatch.setattr(handlers, "find_full_song_file", fake_full_none)
    msg2 = _FakeMessage()
    await handlers._send_professional_song_card(msg2, db, "Dooset Daram", "Mehdi Ahmadvand")
    assert not msg2.audios  # a preview is never presented as the song
    joined = "\n".join(msg2.answers)
    assert "Dooset Daram" in joined and "Mehdi Ahmadvand" in joined
    assert "Sweet Dreams" not in joined
    assert "نسخه کامل" in joined and "پیدا نشد" in joined
    flat = [b.text for row in msg2.last_markup.inline_keyboard for b in row]
    assert any("مشابه" in t_ for t_ in flat)


@pytest.mark.asyncio
async def test_media_handler_uses_shazam_before_audd(monkeypatch, db):
    """A sent film is identified by Shazam first, even with no AudD token."""
    config = Config()
    config.audd_token = ""

    async def fake_shazam(data):
        class R:
            status = "identified"
            title = "Dooset Daram"
            artist = "Mehdi Ahmadvand"
        return R()

    async def forbidden_audd(*args, **kwargs):
        raise AssertionError("AudD must not be called when Shazam already identified")

    monkeypatch.setattr(handlers, "identify_bytes_shazam", fake_shazam)
    monkeypatch.setattr(handlers, "identify_bytes", forbidden_audd)

    async def fake_smart(engine, query, limit=10):
        return [_song()], "Dooset Daram"

    monkeypatch.setattr(handlers, "smart_search", fake_smart)

    async def fake_full(artist, title):
        return {"data": b"F" * 200000, "ext": "mp3",
                "duration_sec": 210.0, "source": "s"}

    monkeypatch.setattr(handlers, "find_full_song_file", fake_full)

    class FakeFile:
        file_path = "clip.mp4"

    class FakeBot:
        async def get_file(self, file_id):
            return FakeFile()

        async def download_file(self, file_path, buf):
            buf.write(b"fake-video-bytes")

    class FakeMedia:
        file_id = "video-1"

    class FakeState:
        async def get_state(self):
            return None

        async def clear(self):
            pass

    class Msg(_FakeMessage):
        def __init__(self):
            super().__init__()
            self.caption = ""
            self.video = FakeMedia()
            self.audio = None
            self.voice = None
            self.document = None
            self.bot = FakeBot()

    msg = Msg()
    await handlers.media_handler(msg, db, config, FakeState())
    # One identifying message, then the full-version audio card — Shazam
    # was consulted before AudD, and the preview is never the song.
    from aiogram.types import BufferedInputFile
    assert msg.audios and isinstance(msg.audios[0][0], BufferedInputFile)
    assert "Dooset Daram" in (msg.audios[0][3] or "")
    assert "نسخه کامل" in (msg.audios[0][1] or "")
