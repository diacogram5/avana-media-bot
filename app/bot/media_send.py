"""Unified media pipeline: ONE shared send path for every song and video.

Policies enforced here, identically for identification, search results,
playlists, favorites, the downloader, and extracted clip audio:

- Songs are delivered as a REAL MP3 with a proper Telegram AUDIO message
  (music player, never a document), filename ``"Artist - Title.mp3"``,
  title/performer/duration set. The container is detected from magic
  bytes — extensions are never trusted, never merely renamed.
- 320 kbps is targeted AND verified: the output is probed, and 320 is
  claimed only when the file was truly produced at 320. A low-quality
  source is never upscaled into a fake "320": it is transcoded at its
  real source bitrate and labelled with that real number.
- Captions carry no URLs and no link previews (unless the caller
  explicitly opts in), and no stray bot-ID links. The bot ID appears in
  exactly one fixed, deliberate place — the welcome/help signature —
  plus the copy/share text the user explicitly takes.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from aiogram.types import BufferedInputFile, LinkPreviewOptions, Message

from .audio_send import _safe_filename_part, detect_audio_ext

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

_URL_RE = re.compile(r"https?://[^\s\u200c]+|www\.[^\s\u200c]+", re.IGNORECASE)

_TARGET_KBPS = 320
_TELEGRAM_AUDIO_LIMIT = 50 * 1024 * 1024


def clean_caption(text: str | None, allow_links: bool = False) -> str | None:
    """Strip URLs from a media caption unless explicitly allowed.

    A raw URL in a caption becomes a clickable link under the file;
    the no-links policy removes them by default.
    """
    if not text:
        return None
    text = text.strip()
    if not allow_links:
        text = _URL_RE.sub("", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


async def answer_no_preview(message: Message, text: str, **kwargs):
    """Text answer with link previews disabled (for messages that must
    mention a URL without rendering a preview card)."""
    kwargs.setdefault("link_preview_options", NO_PREVIEW)
    return await message.answer(text, **kwargs)


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def probe_audio(data: bytes, ext: str) -> tuple[int | None, float | None]:
    """Return (bitrate_kbps, duration_sec); None where unmeasurable.

    Bitrate comes from ffprobe when available — never guessed. Duration
    falls back to the landmark fingerprint probe used by the song bank.
    """
    bitrate = duration = None
    if _ffprobe_available() and data:
        path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as fh:
                fh.write(data)
                path = fh.name
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries",
                 "stream=bit_rate,duration:format=bit_rate,duration",
                 "-of", "json", path],
                timeout=30, capture_output=True, text=True)
            if proc.returncode == 0:
                import json as _json
                info = _json.loads(proc.stdout or "{}")
                streams = info.get("streams") or [{}]
                fmt = info.get("format") or {}
                br_raw = streams[0].get("bit_rate") or fmt.get("bit_rate")
                dur_raw = streams[0].get("duration") or fmt.get("duration")
                try:
                    bitrate = int(round(float(br_raw) / 1000))
                except (TypeError, ValueError):
                    bitrate = None
                try:
                    duration = float(dur_raw)
                except (TypeError, ValueError):
                    duration = None
        except Exception:
            pass
        finally:
            if path:
                try:
                    import os
                    os.unlink(path)
                except Exception:
                    pass
    if duration is None:
        try:
            from ..search.fullsong import _fingerprint_duration
            duration = _fingerprint_duration(data)
        except Exception:
            duration = None
    if bitrate is not None and bitrate <= 0:
        bitrate = None
    if duration is not None and duration <= 0:
        duration = None
    return bitrate, duration


def has_audio_stream(data: bytes) -> bool | None:
    """True if the container has an audio stream, False if it definitely
    has none, None when unmeasurable (no ffprobe available). Used to give
    the distinct honest cause for a silent/unprocessable video instead of
    a generic identification error."""
    if not _ffprobe_available() or not data:
        return None
    path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            fh.write(data)
            path = fh.name
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            timeout=30, capture_output=True, text=True)
        if proc.returncode == 0:
            return bool(proc.stdout.strip())
        return None
    except Exception:
        return None
    finally:
        if path:
            try:
                import os
                os.unlink(path)
            except Exception:
                pass


def transcode_mp3(data: bytes, src_ext: str,
                  target_kbps: int = _TARGET_KBPS) -> tuple[bytes | None, int | None, float | None]:
    """Genuinely re-encode audio bytes to MP3 at ``target_kbps``.

    Returns (bytes, probed_bitrate_kbps, probed_duration_sec), or
    (None, None, None) when conversion is impossible. The output is
    validated: parseable MP3 magic bytes and a real probed bitrate.
    """
    if not _ffmpeg_available() or not data:
        return None, None, None
    src_path = dst_path = ""
    out = b""
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{src_ext}", delete=False) as src:
            src.write(data)
            src_path = src.name
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as dst:
            dst_path = dst.name
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", src_path,
             "-codec:a", "libmp3lame", "-b:a", f"{target_kbps}k", dst_path],
            timeout=180, capture_output=True)
        if proc.returncode == 0:
            with open(dst_path, "rb") as fh:
                out = fh.read()
    except Exception:
        out = b""
    finally:
        import os
        for p in (src_path, dst_path):
            try:
                if p:
                    os.unlink(p)
            except Exception:
                pass
    if len(out) > 1024 and detect_audio_ext(out) == "mp3":
        out_br, out_dur = probe_audio(out, "mp3")
        return out, out_br, out_dur
    return None, None, None


def _quality_label(converted: bool, src_br: int | None,
                   out_br: int | None) -> str:
    """Honest Persian quality label. 320 is claimed only when the output
    was truly produced and verified at 320; a low-quality source keeps
    its real number and is never labelled native 320."""
    if out_br and out_br >= 300:
        return "MP3 · 320kbps" + (" (تبدیل‌شده)" if converted else "")
    if out_br:
        if converted and src_br:
            return f"MP3 · {out_br}kbps (کیفیت اصلی منبع: {src_br}kbps)"
        return f"MP3 · {out_br}kbps"
    return "MP3" + (" (تبدیل‌شده)" if converted else "")


@dataclass
class PreparedMusic:
    data: bytes
    ext: str
    filename: str
    bitrate_kbps: int | None
    source_bitrate_kbps: int | None
    duration_sec: float | None
    converted: bool
    quality_label: str


def _music_filename(title: str, artist: str, ext: str) -> str:
    t = _safe_filename_part(title)
    a = _safe_filename_part(artist)
    base = f"{t} - {a}" if a and a != t else t
    return f"{base}.{ext}"


def prepare_music(data: bytes, title: str, artist: str) -> PreparedMusic | None:
    """Prepare song bytes for delivery: real MP3, honest specs.

    - Real MP3 sources are kept untouched (never re-encoded).
    - Other containers are genuinely transcoded to MP3 at 320 kbps when
      the source quality supports it; a low-bitrate source is transcoded
      at its real bitrate instead of being upscaled into a fake 320.
    - Without a converter, the original playable bytes are kept with
      their true detected extension — never a renamed lie.
    """
    if not data:
        return None
    real_ext = detect_audio_ext(data)
    src_br, src_dur = probe_audio(data, real_ext)
    out, out_ext, converted = data, real_ext, False
    out_br, out_dur = src_br, src_dur
    if real_ext != "mp3":
        target = _TARGET_KBPS if (src_br or 999) >= 300 else max(64, src_br or 192)
        conv = transcode_mp3(data, real_ext, target)
        if conv[0]:
            out, out_br, out_dur = conv
            out_ext, converted = "mp3", True
    label = _quality_label(converted, src_br, out_br)
    if not converted and out_ext != "mp3":
        # Honest fallback: playable original, true extension, no MP3 claim.
        label = f"{out_ext.upper()} (بدون تبدیل)"
    return PreparedMusic(
        data=out, ext=out_ext,
        filename=_music_filename(title, artist, out_ext),
        bitrate_kbps=out_br, source_bitrate_kbps=src_br,
        duration_sec=out_dur, converted=converted,
        quality_label=label)


async def send_music(message: Message, data: bytes, title: str, artist: str = "",
                     caption: str | None = None, reply_markup=None,
                     duration: int | float | None = None,
                     allow_links: bool = False,
                     show_quality: bool = True) -> Message | None:
    """Send song bytes as a proper Telegram AUDIO message via the unified
    pipeline. Returns the sent message, or None on failure."""
    if not data or len(data) > _TELEGRAM_AUDIO_LIMIT:
        return None
    prep = prepare_music(data, title, artist)
    if prep is None:
        return None
    cap = clean_caption(caption, allow_links)
    if show_quality and prep.quality_label:
        ql = f"🔊 {prep.quality_label}"
        cap = f"{cap}\n{ql}" if cap else ql
    dur = duration or prep.duration_sec
    try:
        dur_i = int(dur) if dur else None
    except (TypeError, ValueError):
        dur_i = None
    try:
        return await message.answer_audio(
            BufferedInputFile(prep.data, filename=prep.filename),
            title=(title or "")[:64] or None,
            performer=(artist or "")[:64] or None,
            duration=dur_i,
            caption=cap, reply_markup=reply_markup)
    except Exception:
        return None


async def send_video(message: Message, video,
                     caption: str | None = None, reply_markup=None,
                     duration: int | None = None,
                     width: int | None = None, height: int | None = None,
                     allow_links: bool = False) -> Message | None:
    """Send a video via the unified pipeline: clean caption, no URLs,
    no link previews, no stray bot-ID links."""
    try:
        return await message.answer_video(
            video,
            caption=clean_caption(caption, allow_links),
            reply_markup=reply_markup,
            duration=duration, width=width, height=height)
    except Exception:
        return None
