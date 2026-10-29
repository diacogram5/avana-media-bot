import pytest

from app.search.smart import search_variants
from app.services.audio_id import parse_audd_payload
from app.services.tts import VOICES, build_ssml, voice_for


def test_audio_id_never_fakes_a_match():
    assert parse_audd_payload({"result": None}).status == "not_found"
    assert parse_audd_payload({"result": {}}).status == "not_found"
    hit = parse_audd_payload({"result": {"title": "Dooset Daram", "artist": "Mehdi Ahmadvand"}})
    assert hit.status == "identified" and hit.title == "Dooset Daram"


def test_tts_persian_voices_are_fa_ir():
    assert VOICES["female"] == "fa-IR-DilaraNeural"
    assert VOICES["male"] == "fa-IR-FaridNeural"
    assert voice_for("زن") == VOICES["female"]
    assert voice_for("مرد") == VOICES["male"]
    ssml = build_ssml("سلام", VOICES["female"])
    assert "fa-IR-DilaraNeural" in ssml and "سلام" in ssml


def test_smart_variants_partial_and_lyric_line():
    variants = search_variants("تو اون ور دنیا باشی دوست دارم")
    assert variants[0].startswith("تو اون")
    assert len(variants) >= 2  # full line + first-words variant
    assert search_variants("دوست دارم") == ["دوست دارم"]


@pytest.mark.asyncio
async def test_tts_without_key_never_returns_audio():
    from app.services.tts import synthesize
    status, audio = await synthesize("سلام", "female", "", "")
    assert status == "needs_key" and audio == b""


@pytest.mark.asyncio
async def test_audio_id_without_token_never_returns_match():
    from app.services.audio_id import identify_bytes
    result = await identify_bytes(b"fake", "clip.mp4", "")
    assert result.status == "needs_token" and result.title == ""
