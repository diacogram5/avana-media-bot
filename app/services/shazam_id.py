"""Song identification from audio via a Shazam signature — no token needed.

This is the method the browser test version proved on the user's exact
reel: the audio of the video is signed and the signature is looked up
against Shazam's catalogue. Three consecutive calls on that reel returned
"Dooset Daram – Mehdi Ahmadvand".

Honesty rules, same as the AudD module:
- No match -> status "not_found". A catalog search result is never
  labelled "identified" here.
- Library missing or lookup error -> status "error". Nothing is invented.
- This uses Shazam's unofficial (reverse-engineered) endpoint via the
  `shazamio` library, so it can be rate-limited or blocked under heavy
  use; AudD (with a token) remains the paid fallback in the handlers.

The `shazamio` import is lazy on purpose: the package still imports and
its tests still run on a machine without that optional dependency; in
that case identification via this path returns "error" instead of a match.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
from dataclasses import dataclass

from ..logging_config import get_logger, log_event

log = get_logger(__name__)


@dataclass
class ShazamIdResult:
    status: str  # identified | not_found | error
    title: str = ""
    artist: str = ""
    raw: dict | None = None


def parse_shazam_payload(payload: dict) -> ShazamIdResult:
    """Parse a Shazam v2 recognise response.

    A match looks like {"matches": [...], "track": {"title": ...,
    "subtitle": ...}} where subtitle is the artist. Anything without a
    track title is "not_found" — never a guessed nearest song.
    """
    if not isinstance(payload, dict):
        return ShazamIdResult(status="not_found")
    track = payload.get("track") or {}
    if not isinstance(track, dict):
        return ShazamIdResult(status="not_found")
    title = (track.get("title") or "").strip()
    artist = (track.get("subtitle") or "").strip()
    if not title:
        return ShazamIdResult(status="not_found")
    return ShazamIdResult(status="identified", title=title, artist=artist, raw=track)


async def _recognize_bytes(data: bytes) -> tuple[dict | None, bool]:
    """Run shazamio on raw bytes.

    Returns (payload, unsupported_codec). ``unsupported_codec`` is True
    only when the signature core rejects the container/codec itself
    (e.g. ``SignatureError: unsupported feature: aac``) — in which case
    the caller may transcode and retry. Any other exception propagates
    to the caller, which logs it as a lookup error.
    """
    from shazamio import Shazam
    shazam = Shazam()
    try:
        try:
            payload = await shazam.recognize(data)
        except Exception as exc:
            if "unsupported feature" in str(exc):
                return None, True
            raise
        return payload, False
    finally:
        try:
            await shazam.close()
        except Exception:
            pass


def transcode_to_wav(data: bytes, max_seconds: int = 90) -> bytes | None:
    """Transcode arbitrary audio bytes to Shazam-signable WAV.

    Root-cause fix (2026-10-29): shazamio's signature core rejects AAC
    outright (``unsupported feature: aac: aac too complex``), so every
    Instagram DASH audio — AAC in MP4 — failed *before any network
    lookup*. Transcoding to PCM WAV makes the same bytes signable.
    Returns None when ffmpeg is missing or the transcode fails; the
    caller then reports an honest error instead of crashing.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log_event(log, "transcode_no_ffmpeg")
        return None
    import tempfile
    # NOTE: output goes to a temp FILE, not pipe:1 — with a pipe ffmpeg
    # cannot know the total size upfront and writes RIFF size 0xFFFFFFFF,
    # which shazamio_core rejects ("unexpected end of file").
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-i", "pipe:0",
             "-ac", "1", "-ar", "22050", "-c:a", "pcm_s16le",
             "-t", str(max_seconds), "-f", "wav", tmp_path],
            input=data, capture_output=True, timeout=90)
        if proc.returncode != 0:
            log_event(log, "transcode_failed",
                      returncode=proc.returncode, stderr=str(proc.stderr[:120]))
            return None
        with open(tmp_path, "rb") as fh:
            out = fh.read()
        if not out or len(out) < 1000:
            log_event(log, "transcode_failed", returncode=proc.returncode,
                      stderr="empty output")
            return None
        return out
    except Exception as exc:
        log_event(log, "transcode_error",
                  error=f"{type(exc).__name__}: {str(exc)[:100]}")
        return None
    finally:
        try:
            import os
            os.unlink(tmp_path)
        except Exception:
            pass


def analyze_audio(data: bytes) -> dict:
    """Validate extracted audio before it reaches the lookup service.

    Returns {ok, reason, duration_s, sample_rate, channels, rms_db}.
    ``reason`` is one of: ok | undecodable | silent | too_short |
    unverified_no_ffmpeg. An unsuitable sample is reported with its own
    reason — never misreported downstream as "song not identified".
    """
    wav = transcode_to_wav(data, max_seconds=120)
    if wav is None:
        if shutil.which("ffmpeg") is None:
            return {"ok": True, "reason": "unverified_no_ffmpeg",
                    "duration_s": 0, "sample_rate": 0, "channels": 0,
                    "rms_db": 0}
        return {"ok": False, "reason": "undecodable",
                "duration_s": 0, "sample_rate": 0, "channels": 0, "rms_db": 0}
    try:
        # Proper WAV parse: locate the "data" chunk (ffmpeg emits extra
        # LIST/INFO chunks, so the audio rarely starts at byte 44).
        if len(wav) < 12 or wav[0:4] != b"RIFF" or wav[8:12] != b"WAVE":
            return {"ok": False, "reason": "undecodable",
                    "duration_s": 0, "sample_rate": 0, "channels": 0,
                    "rms_db": 0}
        pos, sample_rate, channels = 12, 0, 0
        pcm = b""
        while pos + 8 <= len(wav):
            chunk_id, chunk_size = wav[pos:pos + 4], \
                struct.unpack("<I", wav[pos + 4:pos + 8])[0]
            body = wav[pos + 8:pos + 8 + chunk_size]
            if chunk_id == b"fmt " and len(body) >= 16:
                _fmt, channels = struct.unpack("<HH", body[0:4])
                sample_rate = struct.unpack("<I", body[4:8])[0]
            elif chunk_id == b"data":
                pcm = body
                break
            pos += 8 + chunk_size + (chunk_size % 2)
        n = len(pcm) // 2
        if n == 0 or not sample_rate:
            return {"ok": False, "reason": "undecodable",
                    "duration_s": 0, "sample_rate": sample_rate,
                    "channels": channels, "rms_db": -99}
        samples = struct.unpack(f"<{n}h", pcm)
        mean_sq = sum(s * s for s in samples) / n
        import math
        rms_db = 20 * math.log10((mean_sq ** 0.5) / 32768) if mean_sq > 0 else -99
        duration_s = n / sample_rate if sample_rate else 0
        info = {"duration_s": round(duration_s, 2), "sample_rate": sample_rate,
                "channels": channels, "rms_db": round(rms_db, 1)}
        if duration_s < 1.0:
            return {"ok": False, "reason": "too_short", **info}
        if rms_db < -45:
            return {"ok": False, "reason": "silent", **info}
        return {"ok": True, "reason": "ok", **info}
    except Exception as exc:
        log_event(log, "analyze_error",
                  error=f"{type(exc).__name__}: {str(exc)[:100]}")
        return {"ok": False, "reason": "undecodable",
                "duration_s": 0, "sample_rate": 0, "channels": 0, "rms_db": 0}


async def identify_bytes_shazam(data: bytes) -> ShazamIdResult:
    """Identify raw audio/video bytes via a Shazam signature.

    `data` is the Telegram file bytes or a downloaded media file. When
    the container codec is unsupported by the signature core (AAC from
    Instagram DASH, Opus voice notes, …), the bytes are transcoded to
    WAV once and the lookup is retried — previously this whole class of
    inputs failed with a bare "error" before any lookup happened.
    """
    if not data:
        return ShazamIdResult(status="error")
    try:
        from shazamio import Shazam  # noqa: F401  (imported for the error path)
    except ImportError:
        log_event(log, "shazam_error", error="ImportError: shazamio not installed")
        return ShazamIdResult(status="error")
    try:
        payload, unsupported = await _recognize_bytes(data)
        if unsupported:
            log_event(log, "shazam_transcode", input_bytes=len(data))
            wav = transcode_to_wav(data)
            if not wav:
                return ShazamIdResult(status="error")
            log_event(log, "shazam_transcode_ok", wav_bytes=len(wav))
            payload, unsupported = await _recognize_bytes(wav)
            if unsupported:
                log_event(log, "shazam_error",
                          error="SignatureError: still unsupported after transcode")
                return ShazamIdResult(status="error")
        return parse_shazam_payload(payload)
    except Exception as exc:
        log_event(log, "shazam_error",
                  error=f"{type(exc).__name__}: {str(exc)[:150]}")
        return ShazamIdResult(status="error")


async def identify_bytes_shazam_multi(data: bytes) -> ShazamIdResult:
    """Try Shazam on several windows of the same file, stop on a match.

    Rationale: a clip with speech over the start, a remix intro, or a
    shortened copy may fail on the full file but match on a later
    window. Slicing container bytes can fail to decode — that only
    yields "error", never a false match, because only an identified
    result (track title from Shazam) is accepted. Bounded to 3 attempts
    so one file cannot burn unbounded lookup time.
    """
    if not data:
        return ShazamIdResult(status="error")
    windows = [data]
    if len(data) > 200_000:
        windows.append(data[len(data) // 3:])
        windows.append(data[2 * len(data) // 3:])
    last = ShazamIdResult(status="error")
    for window in windows:
        result = await identify_bytes_shazam(window)
        if result.status == "identified":
            return result
        last = result
    return last


async def identify_file_shazam(path: str) -> ShazamIdResult:
    """Identify an audio/video file on disk via a Shazam signature."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return ShazamIdResult(status="error")
    return await identify_bytes_shazam(data)
