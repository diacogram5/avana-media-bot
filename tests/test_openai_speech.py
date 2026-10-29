"""OpenAI speech in both directions: Whisper (voice->text) and TTS (text->voice).

Primary engine is OpenAI under OPENAI_API_KEY; Azure is only a fallback
in the handlers. Without a key, both directions honestly report
"needs_key" and never fake audio or text. No real API calls are made:
httpx MockTransport stands in for OpenAI.
"""
import httpx
import pytest

from app.bot import handlers
from app.config import Config
from app.services.openai_speech import (
    OPENAI_VOICES, openai_voice_for, parse_whisper_payload,
    synthesize_openai, transcribe_whisper,
)


def test_openai_voice_mapping_female_male():
    assert openai_voice_for("female") == "nova"
    assert openai_voice_for("male") == "onyx"
    assert OPENAI_VOICES["female"] == "nova" and OPENAI_VOICES["male"] == "onyx"


def test_whisper_payload_parsing_never_invents_text():
    assert parse_whisper_payload({"text": "سلام خوب هستید"}) == ("ok", "سلام خوب هستید")
    assert parse_whisper_payload({"text": "   "}) == ("empty", "")
    assert parse_whisper_payload({}) == ("empty", "")
    assert parse_whisper_payload(None) == ("empty", "")


@pytest.mark.asyncio
async def test_whisper_and_tts_without_key_never_fake():
    assert await transcribe_whisper(b"voice-bytes", "") == ("needs_key", "")
    assert await transcribe_whisper(b"", "fake-key") == ("needs_key", "")
    assert await synthesize_openai("سلام", "female", "") == ("needs_key", b"")
    assert await synthesize_openai("   ", "female", "fake-key") == ("needs_key", b"")


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_whisper_mocked_ok_and_error():
    async def ok(request):
        assert "api.openai.com" in str(request.url)
        assert request.headers["Authorization"] == "Bearer fake-key"
        return httpx.Response(200, json={"text": "سلام"})

    assert await transcribe_whisper(b"voice", "fake-key", client=_mock_client(ok)) == ("ok", "سلام")

    async def denied(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    assert await transcribe_whisper(b"voice", "bad-key", client=_mock_client(denied)) == ("error", "")


@pytest.mark.asyncio
async def test_tts_mocked_ok_and_error():
    async def ok(request):
        body = request.read()
        assert b"nova" in body  # female voice
        return httpx.Response(200, content=b"ID3fake-mp3", headers={"Content-Type": "audio/mpeg"})

    status, audio = await synthesize_openai("سلام", "female", "fake-key", client=_mock_client(ok))
    assert status == "ok" and audio == b"ID3fake-mp3"

    async def denied(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    assert await synthesize_openai("سلام", "male", "bad-key", client=_mock_client(denied)) == ("error", b"")


@pytest.mark.asyncio
async def test_handler_speech_helpers_without_any_key_call_nothing(monkeypatch, db):
    """With neither OpenAI nor Azure key, helpers return needs_key untouched."""
    config = Config()
    config.openai_api_key = ""
    config.azure_speech_key = ""
    config.azure_speech_region = ""

    async def forbidden(*args, **kwargs):
        raise AssertionError("no service call without a key")

    monkeypatch.setattr(handlers, "synthesize_openai", forbidden)
    monkeypatch.setattr(handlers, "transcribe_whisper", forbidden)
    monkeypatch.setattr(handlers, "synthesize", forbidden)
    monkeypatch.setattr(handlers, "transcribe_bytes", forbidden)
    assert await handlers._synthesize_voice("سلام", "female", config, db) == ("needs_key", b"")
    assert await handlers._transcribe_voice(b"voice", "voice.ogg", config, db) == ("needs_key", "")


@pytest.mark.asyncio
async def test_handler_speech_helpers_prefer_openai(monkeypatch, db):
    """With an OpenAI key, Whisper/TTS are tried first and Azure is untouched."""
    config = Config()
    config.openai_api_key = "fake-openai-key"
    config.azure_speech_key = "fake-azure-key"
    config.azure_speech_region = "fake-region"

    async def fake_tts(text, gender, key):
        assert key == "fake-openai-key" and gender == "female"
        return "ok", b"openai-mp3"

    async def fake_whisper(data, key, filename):
        assert key == "fake-openai-key" and data == b"voice"
        return "ok", "سلام"

    async def forbidden_azure(*args, **kwargs):
        raise AssertionError("Azure fallback must not run when OpenAI succeeds")

    monkeypatch.setattr(handlers, "synthesize_openai", fake_tts)
    monkeypatch.setattr(handlers, "transcribe_whisper", fake_whisper)
    monkeypatch.setattr(handlers, "synthesize", forbidden_azure)
    monkeypatch.setattr(handlers, "transcribe_bytes", forbidden_azure)
    assert await handlers._synthesize_voice("سلام", "female", config, db) == ("ok", b"openai-mp3")
    assert await handlers._transcribe_voice(b"voice", "voice.ogg", config, db) == ("ok", "سلام")
