"""Surgical-fix tests: the one-click «🎵 شناسایی موسیقی» button on
link-fetched Instagram videos must identify the reel's REAL audio.

Root cause fixed here: resolve_instagram_media() returns a video-ONLY
DASH stream (no audio track) as video_url plus a separate audio_url.
The old code stored only the Telegram file_id of the video-only file,
so has_audio_stream() was correctly False for THAT file and the button
honestly-but-wrongly reported "no identifiable audio" — while the reel
itself has real music. The registry now carries audio_url and the
callback identifies THOSE bytes.
No network is used: downloads are monkeypatched with fixtures.
"""
import asyncio

from app.bot import handlers
from app.config import Config


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeCB:
    """Minimal stand-in for aiogram's CallbackQuery."""

    def __init__(self, data, uid, bot=None):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.bot = bot
        self.message = _FakeMsg()
        self.answered = None

    async def answer(self, text, show_alert=False):
        self.answered = text


class _FakeMsg:
    def __init__(self):
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)
        return None


def _cfg():
    return Config(bot_token="t", owner_id=1, max_file_mb=50)


def test_registry_stores_audio_url_for_link_video():
    handlers._VIDEO_IDENTIFY.clear()
    kb = handlers._video_identify_kb(
        7, "fileXYZ", "link", audio_url="https://cdn.example/a.mp4")
    token = kb.inline_keyboard[0][0].callback_data.split("vid:id:", 1)[1]
    entry = handlers._VIDEO_IDENTIFY[token]
    assert entry["audio_url"] == "https://cdn.example/a.mp4"
    assert entry["user_id"] == 7
    assert entry["file_id"] == "fileXYZ"
    handlers._VIDEO_IDENTIFY.clear()


def test_callback_identifies_audio_url_bytes_not_video_file():
    """The callback must download the AUDIO URL bytes and hand THEM to
    _identify_audio — never the video-only Telegram file."""
    handlers._VIDEO_IDENTIFY.clear()
    handlers._VIDEO_IDENTIFY_RUNNING.clear()
    handlers._VIDEO_IDENTIFY["tokA"] = {
        "user_id": 5, "file_id": "videoOnlyFileId", "kind": "link",
        "audio_url": "https://cdn.example/reel_audio.mp4",
    }
    audio_bytes = b"\x00\x00\x00\x18ftypmp41" + b"A" * 4096  # audio fixture
    seen = {}

    async def fake_download(url, config):
        seen["url"] = url
        assert url == "https://cdn.example/reel_audio.mp4"
        return audio_bytes

    async def fake_identify(data, filename, config, *args, **kwargs):
        seen["data"] = data
        seen["filename"] = filename
        from app.services.audio_id import AudioIdResult
        return AudioIdResult(status="identified", title="T", artist="A")

    async def fake_song_card(*a, **k):
        seen["card"] = True

    async def fake_arm(*a, **k):
        return None

    handlers._download_link_audio = fake_download
    orig_ident = handlers._identify_audio
    orig_card = handlers._send_professional_song_card
    orig_arm = handlers._arm_bank_sample
    orig_botid = handlers.dbmod.get_bot_id
    handlers._identify_audio = fake_identify
    handlers._send_professional_song_card = fake_song_card
    handlers._arm_bank_sample = fake_arm
    handlers.dbmod.get_bot_id = lambda conn, config: ""
    try:
        asyncio.run(handlers.video_identify(_FakeCB("vid:id:tokA", 5), None, _cfg()))
    finally:
        handlers._identify_audio = orig_ident
        handlers._send_professional_song_card = orig_card
        handlers._arm_bank_sample = orig_arm
        handlers.dbmod.get_bot_id = orig_botid
        handlers._VIDEO_IDENTIFY.clear()
        handlers._VIDEO_IDENTIFY_RUNNING.clear()

    assert seen["url"] == "https://cdn.example/reel_audio.mp4"
    assert seen["data"] is audio_bytes  # the AUDIO bytes, not video file
    assert seen.get("card") is True


def test_callback_without_audio_url_falls_back_to_telegram_file():
    """User-sent videos (no audio_url) keep the old Telegram-file path."""
    handlers._VIDEO_IDENTIFY.clear()
    handlers._VIDEO_IDENTIFY_RUNNING.clear()
    handlers._VIDEO_IDENTIFY["tokB"] = {
        "user_id": 5, "file_id": "userVideoFileId", "kind": "file",
        "audio_url": None,
    }
    video_bytes = b"\x00\x00\x00\x18ftypisom" + b"V" * 4096
    seen = {}

    class _FakeFile:
        file_path = "videos/x.mp4"

    class _FakeBot:
        got_file_id = None

        async def get_file(self, file_id):
            _FakeBot.got_file_id = file_id
            return _FakeFile()

        async def download_file(self, path, buf):
            buf.write(video_bytes)

    async def fake_identify(data, filename, config, *args, **kwargs):
        seen["data"] = data
        from app.services.audio_id import AudioIdResult
        return AudioIdResult(status="error", detail="lookup_error")

    async def no_download(url, config):
        raise AssertionError("_download_link_audio must not be called")

    handlers._download_link_audio = no_download
    orig_ident = handlers._identify_audio
    handlers._identify_audio = fake_identify
    try:
        asyncio.run(handlers.video_identify(
            _FakeCB("vid:id:tokB", 5, bot=_FakeBot()), None, _cfg()))
    finally:
        handlers._identify_audio = orig_ident
        handlers._VIDEO_IDENTIFY.clear()
        handlers._VIDEO_IDENTIFY_RUNNING.clear()

    assert _FakeBot.got_file_id == "userVideoFileId"
    assert seen["data"] == video_bytes


def test_looks_like_audio_magic_bytes():
    assert handlers._looks_like_audio(b"\x00\x00\x00\x18ftypmp41" + b"x" * 100)
    assert handlers._looks_like_audio(b"ID3\x04\x00" + b"x" * 100)
    assert handlers._looks_like_audio(b"RIFFxxxxWAVEfmt " + b"x" * 100)
    assert handlers._looks_like_audio(b"\xff\xf1" + b"x" * 100)  # AAC ADTS
    assert not handlers._looks_like_audio(b"")
    assert not handlers._looks_like_audio(b"garbage-bytes-here!!")
