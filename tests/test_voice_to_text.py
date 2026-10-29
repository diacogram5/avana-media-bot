"""Voice-to-text: speech recognition (not a speech engine), no stats button.

Without a recognition key the bot must honestly say so and never
fabricate a transcript. With a service transcript, only that text is sent.
"""
import io

import pytest

from app.bot import handlers
from app.bot.logic import main_menu_labels
from app.config import Config
from app.services.stt import STTResult, parse_stt_payload, transcribe_bytes


def test_stt_without_key_never_fabricates_text():
    import asyncio
    result = asyncio.run(transcribe_bytes(b"fake-voice", "", ""))
    assert result.status == "needs_key" and result.text == ""


def test_stt_payload_parsing_never_invents_text():
    assert parse_stt_payload({"DisplayText": "سلام خوب هستید"}).status == "ok"
    assert parse_stt_payload({"DisplayText": "سلام خوب هستید"}).text == "سلام خوب هستید"
    assert parse_stt_payload({}).status == "empty"
    assert parse_stt_payload({}).text == ""
    assert parse_stt_payload({"DisplayText": "  "}).status == "empty"


def test_user_menu_has_voice_to_text_and_no_stats(db):
    labels = main_menu_labels(db)
    assert "🎙 تبدیل ویس به متن" in labels
    assert not any("آمار" in label for label in labels), \
        "user menu must not contain a stats button"


class _FakeUser:
    def __init__(self):
        self.id = 42
        self.username = "tester"
        self.first_name = "Test"


class _FakeState:
    def __init__(self, value=None):
        self.value = value

    async def get_state(self):
        return self.value

    async def set_state(self, value):
        self.value = value

    async def clear(self):
        self.value = None


class _FakeFile:
    file_path = "voice.ogg"


class _FakeBot:
    async def get_file(self, file_id):
        assert file_id == "voice-1"
        return _FakeFile()

    async def download_file(self, file_path, buf):
        buf.write(b"fake-voice-bytes")


class _FakeMedia:
    file_id = "voice-1"


class _FakeMessage:
    def __init__(self):
        self.from_user = _FakeUser()
        self.caption = ""
        self.video = None
        self.audio = None
        self.voice = _FakeMedia()
        self.document = None
        self.bot = _FakeBot()
        self.answers = []

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)


@pytest.mark.asyncio
async def test_voice_to_text_mode_transcribes_and_skips_song_id(monkeypatch, db):
    config = Config()
    config.azure_speech_key = "fake-key"
    config.azure_speech_region = "fake-region"

    async def fake_transcribe(data, key, region):
        assert data == b"fake-voice-bytes"
        assert key == "fake-key" and region == "fake-region"
        return STTResult(status="ok", text="سلام خوب هستید")

    async def forbidden_identify(*args, **kwargs):
        raise AssertionError("song identification must not run in voice-to-text mode")

    monkeypatch.setattr(handlers, "transcribe_bytes", fake_transcribe)
    monkeypatch.setattr(handlers, "identify_bytes", forbidden_identify)

    msg = _FakeMessage()
    await handlers.media_handler(msg, db, config, _FakeState("voice_to_text"))
    joined = "\n".join(msg.answers)
    assert "📝 متن ویس:" in joined
    assert "سلام خوب هستید" in joined


@pytest.mark.asyncio
async def test_voice_to_text_without_key_says_so_and_fabricates_nothing(monkeypatch, db):
    config = Config()
    config.azure_speech_key = ""
    config.azure_speech_region = ""

    async def forbidden_transcribe(*args, **kwargs):
        raise AssertionError("no service call without a key")

    monkeypatch.setattr(handlers, "transcribe_bytes", forbidden_transcribe)
    msg = _FakeMessage()
    await handlers.media_handler(msg, db, config, _FakeState("voice_to_text"))
    joined = "\n".join(msg.answers)
    assert "نیازمند کلید" in joined or "تنظیم نشده" in joined
    # No fabricated transcript appears anywhere.
    assert "سلام" not in joined
