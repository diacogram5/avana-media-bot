"""Tests for the unified media pipeline (brief #3):

- Real MP3 with honest, probed bitrate (320 claimed only when verified).
- No URLs / link previews / stray bot-ID links under media.
- One shared send path: send_music / send_video / clean_caption.
"""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiogram.types import BufferedInputFile, LinkPreviewOptions

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bot import media_send as ms  # noqa: E402
from app.bot.audio_send import detect_audio_ext  # noqa: E402


def _sine_mp3(bitrate_kbps: int, seconds: int = 2) -> bytes:
    import subprocess
    out = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k", "-f", "mp3", "pipe:1"],
        capture_output=True, timeout=60)
    assert out.returncode == 0
    return out.stdout


def _sine_m4a(bitrate_kbps: int, seconds: int = 2) -> bytes:
    import subprocess
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as fh:
        path = fh.name
    try:
        out = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", f"sine=frequency=440:duration={seconds}",
             "-c:a", "aac", "-b:a", f"{bitrate_kbps}k", path],
            capture_output=True, timeout=60)
        assert out.returncode == 0
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class FakeMessage:
    def __init__(self):
        self.from_user = SimpleNamespace(id=7)
        self.chat = SimpleNamespace(id=7)
        self.calls = []

    async def answer(self, text, **kwargs):
        self.calls.append(("answer", text, kwargs))
        return SimpleNamespace(message_id=1)

    async def answer_audio(self, audio, **kwargs):
        self.calls.append(("answer_audio", audio, kwargs))
        return SimpleNamespace(message_id=2)

    async def answer_video(self, video, **kwargs):
        self.calls.append(("answer_video", video, kwargs))
        return SimpleNamespace(message_id=3)


# ------------------------------------------------------------------ probe

def test_probe_audio_measures_real_bitrate():
    data = _sine_mp3(320)
    br, dur = ms.probe_audio(data, "mp3")
    assert br is not None and 300 <= br <= 340, br
    assert dur is not None and 1.5 <= dur <= 2.5, dur


def test_detect_audio_ext_never_trusts_extension():
    assert detect_audio_ext(b"ID3\x04\x00" + b"\x00" * 20) == "mp3"
    assert detect_audio_ext(bytes([0xFF, 0xFB]) + b"\x00" * 20) == "mp3"
    assert detect_audio_ext(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 10) == "m4a"
    assert detect_audio_ext(b"garbage-no-magic-here-1234") == "m4a"


# ------------------------------------------------- prepare: honest MP3

def test_prepare_music_keeps_real_320_mp3():
    prep = ms.prepare_music(_sine_mp3(320), "Tardid", "Saman Jalili")
    assert prep.ext == "mp3" and not prep.converted
    assert prep.filename == "Tardid - Saman Jalili.mp3"
    assert prep.bitrate_kbps is not None and prep.bitrate_kbps >= 300
    assert prep.quality_label == "MP3 · 320kbps"


def test_prepare_music_never_fakes_320():
    prep = ms.prepare_music(_sine_mp3(128), "Song", "Artist")
    assert prep.ext == "mp3" and not prep.converted
    assert "320" not in prep.quality_label
    assert "128" in prep.quality_label


def test_prepare_music_converts_m4a_to_real_verified_mp3():
    prep = ms.prepare_music(_sine_m4a(256), "Song", "Artist")
    assert prep.ext == "mp3" and prep.converted
    # The output is genuinely MP3 bytes, not a renamed container.
    assert detect_audio_ext(prep.data) == "mp3"
    assert prep.filename.endswith(".mp3")


def test_prepare_music_low_source_is_not_upscaled():
    prep = ms.prepare_music(_sine_m4a(96), "Song", "Artist")
    assert prep.converted and prep.ext == "mp3"
    # No fake 320: the real (low) source bitrate is reported honestly.
    assert "320" not in prep.quality_label
    assert "کیفیت اصلی منبع" in prep.quality_label


def test_prepare_music_without_converter_is_honest():
    with patch.object(ms, "_ffmpeg_available", return_value=False):
        prep = ms.prepare_music(_sine_m4a(128), "Song", "Artist")
    assert not prep.converted
    assert prep.ext == "m4a"  # true container, never a renamed .mp3
    assert "MP3" not in prep.quality_label.replace("(بدون تبدیل)", "")
    assert prep.filename.endswith(".m4a")


# ------------------------------------------------------------- send path

@pytest.mark.asyncio
async def test_send_music_is_proper_audio_with_clean_filename():
    msg = FakeMessage()
    sent = await ms.send_music(msg, _sine_mp3(320), "Tardid", "Saman Jalili",
                               caption="🎵 یافته شد")
    assert sent is not None
    kind, audio, kwargs = msg.calls[0]
    assert kind == "answer_audio"
    assert isinstance(audio, BufferedInputFile)
    assert audio.filename == "Tardid - Saman Jalili.mp3"
    assert kwargs.get("title") == "Tardid"
    assert kwargs.get("performer") == "Saman Jalili"
    assert "🔊" in (kwargs.get("caption") or "")


@pytest.mark.asyncio
async def test_send_music_strips_urls_from_caption():
    msg = FakeMessage()
    await ms.send_music(msg, _sine_mp3(128), "T", "A",
                        caption="title\nhttps://music.apple.com/x\n@SomeBot")
    _k, _a, kwargs = msg.calls[0]
    caption = kwargs.get("caption") or ""
    assert "https://" not in caption
    # ...but an explicit opt-in keeps links.
    msg2 = FakeMessage()
    await ms.send_music(msg2, _sine_mp3(128), "T", "A",
                        caption="see https://example.com/x", allow_links=True)
    assert "https://example.com/x" in msg2.calls[0][2].get("caption", "")


@pytest.mark.asyncio
async def test_send_music_rejects_oversize():
    msg = FakeMessage()
    sent = await ms.send_music(msg, b"ID3" + b"\x00" * (51 * 1024 * 1024),
                               "T", "A")
    assert sent is None
    assert msg.calls == []


@pytest.mark.asyncio
async def test_send_video_caption_has_no_links():
    msg = FakeMessage()
    sent = await ms.send_video(msg, "https://example.com/v.mp4",
                               caption="🎬 ویدیو\nhttps://example.com/page")
    assert sent is not None
    kind, _v, kwargs = msg.calls[0]
    assert kind == "answer_video"
    assert "https://" not in (kwargs.get("caption") or "")


@pytest.mark.asyncio
async def test_answer_no_preview_disables_preview():
    msg = FakeMessage()
    await ms.answer_no_preview(msg, "لینک: https://example.com/x")
    _k, text, kwargs = msg.calls[0]
    assert text.startswith("لینک:")
    lp = kwargs.get("link_preview_options")
    assert isinstance(lp, LinkPreviewOptions) and lp.is_disabled is True


def test_clean_caption():
    assert ms.clean_caption(None) is None
    assert ms.clean_caption("  hello  ") == "hello"
    assert "http" not in (ms.clean_caption("a https://x.y/b c") or "")
    assert "https://x.y/b" in (ms.clean_caption("a https://x.y/b c",
                                                allow_links=True) or "")
