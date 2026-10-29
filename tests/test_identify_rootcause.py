"""Root-cause regression tests for the 2026-10-29 identification outage.

Proven live failures (Render logs, commit 188756f5 window):
1. ``TypeError: _with_signature() got multiple values for argument 'conn'``
   — the /start decorator sat on a sync helper; every /start crashed.
2. ``panel() got an unexpected keyword argument 'dispatcher'`` — the
   admin _owner_only wrapper forwarded aiogram kwargs blindly.

Proven locally on the exact failing reel (DfDa4Epo8nr):
3. ``shazamio_core.SignatureError: unsupported feature: aac`` — the
   signature core rejects AAC outright, so every Instagram DASH audio
   failed *before any network lookup*. Fixed by transcoding to WAV.

No network is used (ffmpeg runs locally where available).
"""
import asyncio
import struct
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.bot import handlers
from app.config import Config
from app.features import admin as adminfeat
from app.services import shazam_id
from app.services.audio_id import AudioIdResult


# ---------------------------------------------------------------------------
# 1. _with_signature must never be a message handler (live TypeError)
# ---------------------------------------------------------------------------

def _start_callbacks():
    from aiogram.filters import Command
    found = []
    for h in handlers.router.message.handlers:
        for f in h.filters:
            cb = getattr(f, "callback", None)
            if isinstance(cb, Command) and "start" in (cb.commands or ()):
                found.append(h.callback)
    return found


def test_with_signature_not_registered_as_handler():
    cbs = _start_callbacks()
    assert cbs, "no /start handler registered"
    names = {getattr(cb, "__name__", "") for cb in cbs}
    assert "_with_signature" not in names, \
        "regression: _with_signature must not be a handler (live TypeError)"
    assert "cmd_start" in names


def test_with_signature_would_crash_under_aiogram_convention():
    """Replicates the exact live crash shape: aiogram calls a message
    handler as handler(event_positional, conn=..., config=...)."""
    with pytest.raises(TypeError):
        # _with_signature(conn, config, text): positional event binds to
        # conn, then conn= kwarg -> "got multiple values for 'conn'".
        handlers._with_signature(object(), conn=object(), config=object())


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = "u"
        self.first_name = "U"


class _FakeChat:
    id = 7


class _FakeMsg:
    def __init__(self, uid=11):
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat()
        self.message_id = 1
        self.text = "/start"
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class _FakeState:
    async def clear(self):
        pass


def test_cmd_start_survives_aiogram_call_convention(db):
    """cmd_start(message, conn=..., config=..., state=...) must not raise
    TypeError — the convention aiogram uses via workflow_data kwargs."""
    cfg = Config(bot_token="x", owner_id=1)
    msg = _FakeMsg(uid=11)
    asyncio.run(handlers.cmd_start(msg, conn=db, config=cfg, state=_FakeState()))
    assert msg.answers, "cmd_start sent nothing"


# ---------------------------------------------------------------------------
# 2. _owner_only must filter unexpected aiogram kwargs (live dispatcher crash)
# ---------------------------------------------------------------------------

def test_owner_only_filters_unexpected_kwargs():
    calls = []

    async def strict_handler(event, conn, config):
        calls.append((event, conn, config))
        return "ok"

    wrapped = adminfeat._owner_only(strict_handler)

    class Ev:
        from_user = _FakeUser(1)

    cfg = Config(bot_token="x", owner_id=1)
    # aiogram passed dispatcher=... -> used to crash with TypeError.
    result = asyncio.run(
        wrapped(Ev(), "CONN", cfg, dispatcher=object(), extra_kwarg=1))
    assert result == "ok"
    assert calls == [(Ev(), "CONN", cfg)] or len(calls) == 1


def test_owner_only_still_denies_non_owner():
    async def strict_handler(event, conn, config):
        raise AssertionError("must not run for non-owner")

    wrapped = adminfeat._owner_only(strict_handler)

    class Ev:
        from_user = _FakeUser(999)

        async def answer(self, text, **kwargs):
            self.denied = text

    cfg = Config(bot_token="x", owner_id=1)
    ev = Ev()
    asyncio.run(wrapped(ev, "CONN", cfg, dispatcher=object()))
    assert getattr(ev, "denied", ""), "non-owner must get the denial line"


# ---------------------------------------------------------------------------
# 3. AAC transcode + audio validation gate
# ---------------------------------------------------------------------------

def _make_aac(path: Path, seconds: int = 2):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "aac", str(path)],
        check=True, timeout=60)
    return path.read_bytes()


def _make_silent_wav(seconds: int = 2, sample_rate: int = 22050) -> bytes:
    n = seconds * sample_rate
    header = (b"RIFF" + struct.pack("<I", 36 + n * 2) + b"WAVE" + b"fmt " +
              struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                          sample_rate * 2, 2, 16) + b"data" +
              struct.pack("<I", n * 2))
    return header + b"\x00\x00" * n


def test_transcode_aac_to_signable_wav(tmp_path):
    pytest.importorskip("shutil")  # always present; documents intent
    aac = _make_aac(tmp_path / "s.m4a")
    wav = shazam_id.transcode_to_wav(aac)
    assert wav is not None, "ffmpeg must transcode the AAC fixture"
    assert wav[0:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert len(wav) > 1000


def test_analyze_audio_accepts_real_aac(tmp_path):
    aac = _make_aac(tmp_path / "s.m4a")
    info = shazam_id.analyze_audio(aac)
    assert info["ok"] is True, info
    assert info["reason"] == "ok"
    assert info["duration_s"] >= 1.5
    assert info["rms_db"] > -45


def test_analyze_audio_rejects_silence():
    info = shazam_id.analyze_audio(_make_silent_wav())
    assert info["ok"] is False
    assert info["reason"] == "silent", info


def test_analyze_audio_rejects_garbage():
    info = shazam_id.analyze_audio(b"\x00\x01\x02" * 5000)
    assert info["ok"] is False
    assert info["reason"] in ("undecodable", "silent"), info


def test_identify_bytes_shazam_retries_after_unsupported_codec(monkeypatch):
    """The AAC SignatureError path must transcode and retry, not die."""
    calls = []

    async def fake_recognize(data):
        calls.append(data)
        if len(calls) == 1:
            # Real _recognize_bytes catches the SignatureError internally
            # and reports (None, unsupported=True).
            return None, True
        return ({"track": {"title": "Tardid", "subtitle": "Saman Jalili"}},
                False)

    monkeypatch.setattr(shazam_id, "_recognize_bytes", fake_recognize)
    monkeypatch.setattr(shazam_id, "transcode_to_wav", lambda d: b"FAKEWAV")
    result = asyncio.run(shazam_id.identify_bytes_shazam(b"\x01\x02" * 70000))
    assert result.status == "identified", result
    assert result.title == "Tardid" and result.artist == "Saman Jalili"
    assert len(calls) == 2 and calls[1] == b"FAKEWAV"


def test_identify_audio_gate_reports_silent_not_not_found():
    """An unsuitable sample is an extraction-stage error with its own
    detail — it must never surface as 'song not identified'."""
    cfg = Config(bot_token="x", owner_id=1)
    result = asyncio.run(
        handlers._identify_audio(_make_silent_wav(), "clip.mp4", cfg))
    assert result.status == "error", result
    assert result.detail == "silent", result
    assert result.status != "not_found"


def test_identify_audio_gate_rejects_garbage():
    cfg = Config(bot_token="x", owner_id=1)
    result = asyncio.run(
        handlers._identify_audio(b"\x00\x01\x02" * 5000, "clip.mp4", cfg))
    assert result.status == "error", result
    assert result.detail in ("undecodable", "silent", "lookup_error"), result
