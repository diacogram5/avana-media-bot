"""Spectral-landmark fingerprint: port of the web bot's Shazam-style
fingerprint (privileged.ts computeLandmarks/fftMagnitudes, actions.ts
encodeLandmarks/landmarkSimilarity).

All signals are synthetic (sine melodies written with math+wave+struct),
so the tests are fully offline. The melodies are music-like (note
onsets with decay, two voices of harmonics): the same melody matches
with thousands of aligned votes, a cut/noisy variant still matches
above the 280-vote bar, and a different melody/rhythm does not match —
mirroring the web's measured separation (same recording >= 342 votes,
unrelated <= 198).
"""
import base64
import io
import json
import math
import shutil
import struct
import subprocess
import sys
import time
import wave

import pytest

from app.search import landmark
from app.search.landmark import (
    compute_landmarks,
    decode_to_pcm,
    fingerprint_bytes,
    landmark_similarity,
    pack_landmarks,
    unpack_landmarks,
)

SR = 16000


def _lcg(seed):
    x = seed
    while True:
        x = (1103515245 * x + 12345) & 0x7FFFFFFFFFFFFFFF
        x &= 0x7FFFFFFF
        yield x / 2**31


def _melody(n_notes, f_lo, seed):
    """Deterministic pentatonic random-walk melody (Hz per note)."""
    g = _lcg(seed)
    scale = [0, 2, 4, 7, 9]
    deg = 12
    out = []
    for _ in range(n_notes):
        deg = max(0, min(24, deg + int(next(g) * 5) - 2))
        semis = scale[deg % 5] + 12 * (deg // 5)
        out.append(f_lo * 2 ** (semis / 12))
    return out


def _synth_notes(freqs, note_dur, gap, noise_amp=0.0, seed=99):
    """Notes with exponential decay + harmonics (+ optional noise)."""
    total = int((note_dur + gap) * len(freqs) * SR)
    out = [0.0] * total
    xr = _lcg(seed)
    for ni, f0 in enumerate(freqs):
        start = int(ni * (note_dur + gap) * SR)
        n = int(note_dur * SR)
        for i in range(n):
            t = i / SR
            env = math.exp(-t * 9.0)
            v = (
                math.sin(2 * math.pi * f0 * t)
                + 0.4 * math.sin(2 * math.pi * 2 * f0 * t)
                + 0.2 * math.sin(2 * math.pi * 3.01 * f0 * t)
            ) * 0.5 * env
            if noise_amp:
                v += (next(xr) - 0.5) * noise_amp
            out[start + i] += v
    return out


def _to_wav_bytes(pcm, sr=SR, channels=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for v in pcm:
            s = max(-32768, min(32767, int(v * 32767)))
            frames += struct.pack("<h", s) * channels
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _song_a(noise_amp=0.0):
    return _synth_notes(_melody(40, 220.0, 7), 0.30, 0.20, noise_amp=noise_amp)


def _song_b():
    return _synth_notes(_melody(30, 110.0, 21), 0.25, 0.40)


@pytest.fixture(scope="module")
def wav_a():
    return _to_wav_bytes(_song_a())


@pytest.fixture(scope="module")
def fp_a(wav_a):
    fp = fingerprint_bytes(wav_a)
    assert fp["status"] == "ok"
    return fp


def test_decode_wav_directly_no_ffmpeg_needed(wav_a):
    decoded = decode_to_pcm(wav_a)
    assert decoded is not None
    pcm, sr = decoded
    assert sr == 16000
    assert len(pcm) == 20 * 16000
    assert all(-1.0 <= v <= 1.0 for v in pcm[::997])


def test_decode_stereo_and_resample():
    frames = 5 * SR
    pcm = _song_a()[:frames]
    wav = _to_wav_bytes(pcm, sr=22050, channels=2)
    decoded = decode_to_pcm(wav)
    assert decoded is not None
    out, sr = decoded
    assert sr == 16000
    expected = round(frames / 22050 * 16000)  # naive linear resample
    assert abs(len(out) - expected) <= 2


def test_identical_signals_match_with_high_votes(fp_a):
    sim = landmark_similarity(fp_a, fp_a)
    assert sim["match"] is True
    assert sim["votes"] >= 1000
    assert sim["ratio"] >= 0.13


def test_cut_from_middle_still_matches(wav_a, fp_a):
    pcm = _song_a()
    cut = pcm[: 5 * SR] + pcm[8 * SR:]  # 3 s removed from the middle
    fp_cut = fingerprint_bytes(_to_wav_bytes(cut))
    assert fp_cut["status"] == "ok"
    sim = landmark_similarity(fp_cut, fp_a)
    assert sim["match"] is True
    assert sim["votes"] >= 280
    assert sim["ratio"] >= 0.13


def test_noisy_signal_still_matches(wav_a, fp_a):
    fp_noisy = fingerprint_bytes(_to_wav_bytes(_song_a(noise_amp=0.05)))
    assert fp_noisy["status"] == "ok"
    sim = landmark_similarity(fp_noisy, fp_a)
    assert sim["match"] is True
    assert sim["votes"] >= 280  # lower than clean, but above the bar
    assert sim["votes"] < landmark_similarity(fp_a, fp_a)["votes"]


def test_different_signals_do_not_match(fp_a):
    fp_b = fingerprint_bytes(_to_wav_bytes(_song_b()))
    assert fp_b["status"] == "ok"
    sim = landmark_similarity(fp_b, fp_a)
    assert sim["match"] is False
    assert sim["votes"] < 280 or sim["ratio"] < 0.13


def test_garbage_bytes_never_ok():
    fp = fingerprint_bytes(b"\x00\x01\x02\x03" * 4096 + b"not audio at all")
    assert fp["status"] in ("error", "no_audio")
    assert fp["status"] != "ok"
    assert fp["landmarks"] == []
    assert fp["message"]


def test_empty_bytes_error_not_ok():
    fp = fingerprint_bytes(b"")
    assert fp["status"] in ("error", "no_audio")
    assert fp["landmarks"] == []


def test_short_audio_is_no_audio():
    fp = fingerprint_bytes(_to_wav_bytes(_song_a()[: SR // 2]))  # 0.5 s
    assert fp["status"] == "no_audio"
    assert fp["landmarks"] == []


def test_silent_audio_is_no_audio():
    fp = fingerprint_bytes(_to_wav_bytes([0.0] * (2 * SR)))
    assert fp["status"] == "no_audio"
    assert fp["landmarks"] == []


def test_compute_landmarks_shape(wav_a):
    pcm, sr = decode_to_pcm(wav_a)
    r = compute_landmarks(pcm, sr)
    assert r["landmark_count"] == len(r["landmarks"]) // 2
    assert r["landmark_count"] > 0
    assert abs(r["duration_sec"] - 20.0) < 0.01
    assert all(isinstance(h, int) for h in r["landmarks"])


def test_pack_unpack_roundtrip(fp_a):
    packed = pack_landmarks(fp_a["landmarks"])
    p = json.loads(packed)
    assert p["v"] == 2 and isinstance(p["b64"], str)
    base64.b64decode(p["b64"], validate=True)  # valid base64
    assert unpack_landmarks(packed) == fp_a["landmarks"]


def test_pack_unpack_empty_roundtrip():
    assert unpack_landmarks(pack_landmarks([])) == []


def test_unpack_rejects_malformed():
    assert unpack_landmarks("not json") == []
    assert unpack_landmarks('{"v":1,"b64":"AAAA"}') == []
    assert unpack_landmarks('{"v":2}') == []
    assert unpack_landmarks('{"v":2,"b64":"!!!"}') == []
    assert unpack_landmarks('{"v":2,"b64":"AAA"}') == []  # bad length


def test_pack_matches_web_wire_format(fp_a):
    """hash in low 18 bits, anchor frame above it, little-endian u32."""
    packed = pack_landmarks(fp_a["landmarks"][:4])
    raw = base64.b64decode(json.loads(packed)["b64"])
    assert len(raw) == 8
    h0, f0, h1, f1 = fp_a["landmarks"][:4]
    (w0,) = struct.unpack_from("<I", raw, 0)
    (w1,) = struct.unpack_from("<I", raw, 4)
    assert (w0 & 0x3FFFF) == (h0 & 0x3FFFF) and (w0 >> 18) == f0
    assert (w1 & 0x3FFFF) == (h1 & 0x3FFFF) and (w1 >> 18) == f1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_ffmpeg_path_decodes_and_matches(wav_a, fp_a):
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-i", "pipe:0", "-f", "mp3", "-b:a", "128k", "pipe:1"],
        input=wav_a, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60, check=True,
    )
    mp3 = proc.stdout
    assert not mp3[:4] == b"RIFF"  # really went through ffmpeg, not the WAV path
    decoded = decode_to_pcm(mp3)
    assert decoded is not None
    fp_mp3 = fingerprint_bytes(mp3)
    assert fp_mp3["status"] == "ok"
    sim = landmark_similarity(fp_mp3, fp_a)
    assert sim["match"] is True
    assert sim["votes"] >= 280


def test_import_is_safe_without_ffmpeg(monkeypatch):
    """Importing the module must not invoke ffmpeg."""
    import importlib

    def _boom(*a, **k):
        raise AssertionError("ffmpeg must not run at import")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(shutil, "which", _boom)
    importlib.reload(landmark)
    assert landmark.TARGET_RATE == 16000


def test_performance_20s_under_15s(wav_a):
    t0 = time.time()
    fp = fingerprint_bytes(wav_a)
    dt = time.time() - t0
    assert fp["status"] == "ok"
    assert dt < 15, "20 s fingerprint took %.1f s" % dt
