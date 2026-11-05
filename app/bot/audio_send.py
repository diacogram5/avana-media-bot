"""Send songs as proper Telegram audio messages — never as documents.

Root cause of the "song arrived as a file" bug: passing a remote URL
straight to ``answer_audio`` lets Telegram's own servers fetch it, and
for some CDN preview URLs (e.g. Apple's ``mzaf_*.plus.aac.p.m4a``)
Telegram classifies the fetched file as a *document* instead of audio.
Downloading the bytes ourselves and uploading them as an audio message
with an explicit clean filename plus ``title``/``performer``/``duration``
metadata always renders the Telegram music player.

Pure-ish helpers (no Bot instance needed); safe to import from handlers
and feature modules alike. ``app/bot/__init__.py`` stays import-light,
so there is no circular-import risk.
"""
from __future__ import annotations

import re
import httpx
from aiogram.types import Message

_DOWNLOAD_TIMEOUT = 25
_MAX_BYTES = 15 * 1024 * 1024
_UA = {"User-Agent": "Mozilla/5.0 (compatible; AvanoMediaBot/1.0)"}


def _safe_filename_part(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", text or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or "song"


def detect_audio_ext(data: bytes) -> str:
    """Detect the real container from magic bytes — never trust an extension.

    A mislabelled file (e.g. m4a bytes named .mp3) is a broken delivery:
    Telegram may refuse to play it. Returns one of mp3/m4a/ogg/wav.
    """
    if not data or len(data) < 16:
        return "m4a"
    head = data[:16]
    if head[:3] == b"ID3":
        return "mp3"
    if len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"  # MPEG frame sync
    if head[4:8] == b"ftyp":
        return "m4a"  # MP4/M4A container
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    return "m4a"


async def _download(url: str) -> tuple[bytes, str] | None:
    """(bytes, ext) or None on any failure. Size-capped.

    The extension always comes from the downloaded bytes (magic-byte
    detection), never from the URL or headers alone.
    """
    try:
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT,
                                     headers=_UA, follow_redirects=True) as client:
            resp = await client.get(url)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    data = resp.content
    if not data or len(data) > _MAX_BYTES:
        return None
    return data, detect_audio_ext(data)


async def send_song_audio(message: Message, audio_url: str, title: str,
                          artist: str = "", caption: str | None = None,
                          reply_markup=None, duration: int | None = None):
    """Send a song URL as a proper Telegram audio message.

    Downloads the file and delivers it through the unified media
    pipeline (real MP3, honest bitrate, music player). A Telegram
    file_id is re-sent as-is; a remote URL is never passed straight to
    Telegram (that is what produced the document bug).
    Returns the sent Message, or None when the download failed.
    """
    from .media_send import clean_caption, send_music
    downloaded = None
    if audio_url and "://" in audio_url:
        downloaded = await _download(audio_url)
    if downloaded:
        data, _ext = downloaded
        return await send_music(message, data, title, artist,
                                caption=caption, reply_markup=reply_markup,
                                duration=duration)
    if audio_url and "://" not in audio_url:
        # Looks like a Telegram file_id: safe to re-send as audio.
        try:
            return await message.answer_audio(
                audio_url,
                title=(title or "")[:64] or None,
                performer=(artist or "")[:64] or None,
                duration=duration,
                caption=clean_caption(caption), reply_markup=reply_markup)
        except Exception:
            pass
    return None


async def send_song_bytes(message: Message, data: bytes, ext: str, title: str,
                          artist: str = "", caption: str | None = None,
                          reply_markup=None, duration: int | None = None):
    """Send already-downloaded audio bytes via the unified media pipeline.

    ``ext`` is informational only: the real container is detected from
    the bytes, converted to a real MP3 when possible, and the honest
    probed bitrate is reported.
    """
    from .media_send import send_music
    return await send_music(message, data, title, artist,
                            caption=caption, reply_markup=reply_markup,
                            duration=duration)
