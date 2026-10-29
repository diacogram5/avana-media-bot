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
    assert "https://music.apple.com/track/7" in caption
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

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)
        assert reply_markup is None, "text messages must have no button grid"

    async def answer_audio(self, audio, caption=None, reply_markup=None):
        self.audios.append((audio, caption, reply_markup))


@pytest.mark.asyncio
async def test_professional_card_exact_match_only(monkeypatch, db):
    """Exact catalog match -> one audio card; nearest-only -> clean title only."""
    async def fake_smart_exact(engine, query, limit=10):
        return [_song()], "Dooset Daram"

    monkeypatch.setattr(handlers, "smart_search", fake_smart_exact)
    msg = _FakeMessage()
    await handlers._send_professional_song_card(msg, db, "Dooset Daram", "Mehdi Ahmadvand")
    assert msg.audios and msg.audios[0][0] == "https://audio.example.com/preview.m4a"
    assert msg.audios[0][1].startswith("Dooset Daram – Mehdi Ahmadvand")
    # Song card: heart (favorite) + lyrics + add-to-playlist.
    labels = [b.text for row in msg.audios[0][2].inline_keyboard for b in row]
    assert labels == ["🤍", "📝 متن آهنگ", "🎶 افزودن به پلی‌لیست"]
    assert "https://music.apple.com/track/7" in msg.audios[0][1]
    assert not msg.answers  # no extra text messages, no buttons, no sections

    async def fake_smart_nearest(engine, query, limit=10):
        return [_song(title="Sweet Dreams", artist="Eurythmics")], "Dooset Daram"

    monkeypatch.setattr(handlers, "smart_search", fake_smart_nearest)
    msg2 = _FakeMessage()
    await handlers._send_professional_song_card(msg2, db, "Dooset Daram", "Mehdi Ahmadvand")
    assert not msg2.audios  # a nearest song is never sent as the identified song
    joined = "\n".join(msg2.answers)
    assert "Dooset Daram – Mehdi Ahmadvand" in joined
    assert "Sweet Dreams" not in joined
    assert "پیدا نشد" in joined


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
    # One identifying message, then the professional audio card — no list,
    # no nearest song, no button grid (answer() asserts reply_markup None).
    assert msg.audios and msg.audios[0][0] == "https://audio.example.com/preview.m4a"
    assert "Dooset Daram – Mehdi Ahmadvand" in msg.audios[0][1]
