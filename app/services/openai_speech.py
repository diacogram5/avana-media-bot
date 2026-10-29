"""Speech in both directions via OpenAI — the primary speech engine.

Why OpenAI, in plain language (also written in .env.example and the
README): "connect it to yourself" (Muse) is not available as an API the
bot's code can call — Muse has no public API for this bot to call. The
practical equivalent is OpenAI (the ChatGPT maker): its Whisper model
for speech recognition and its TTS model for speech synthesis, both
under one env key, OPENAI_API_KEY. Azure Speech is kept only as a
fallback (TTS: Dilara/Farid fa-IR; STT: fa-IR).

- Voice-to-text: Whisper (model "whisper-1"), language auto-detected,
  with strong Persian support. Input is the user's voice/audio bytes.
- Text-to-voice: TTS (model "tts-1"), output MP3. Voices are OpenAI's
  own names, not gendered by OpenAI: "nova" is conventionally
  female-sounding and "onyx" male-sounding, so female -> nova and
  male -> onyx. The model reads the input text's language itself, so
  the same voices serve Persian and English text.

Honesty rules, enforced here and in the handlers:
- No key -> "needs_key", no audio, no text. Never fake either.
- Service returns empty text / non-audio -> "empty"/"error", never invented.
- Keys are env-only and never logged.
"""
from __future__ import annotations

import httpx

WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
TTS_URL = "https://api.openai.com/v1/audio/speech"
WHISPER_MODEL = "whisper-1"
TTS_MODEL = "tts-1"
# OpenAI voice names (not gendered by OpenAI; conventional mapping).
OPENAI_VOICES = {"female": "nova", "male": "onyx"}


def openai_voice_for(gender: str) -> str:
    return OPENAI_VOICES.get("male" if gender == "male" else "female", "nova")


def parse_whisper_payload(payload: dict) -> tuple[str, str]:
    """Return (status, text) from a Whisper response — never invented text."""
    text = ""
    if isinstance(payload, dict):
        text = (payload.get("text") or "").strip()
    if not text:
        return "empty", ""
    return "ok", text


async def transcribe_whisper(data: bytes, api_key: str, filename: str = "voice.ogg",
                             client: httpx.AsyncClient | None = None) -> tuple[str, str]:
    """Transcribe audio bytes with Whisper. Returns (status, text).

    Status is ok | needs_key | empty | error. The filename extension
    tells Whisper the container format (ogg for Telegram voice, mp4 for
    video, mp3 for audio files).
    """
    if not api_key or not data:
        return "needs_key", ""
    headers = {"Authorization": f"Bearer {api_key}"}
    files = {"file": (filename, data)}
    form = {"model": WHISPER_MODEL}
    try:
        if client is not None:
            resp = await client.post(WHISPER_URL, headers=headers, data=form, files=files, timeout=120)
        else:
            async with httpx.AsyncClient(timeout=120) as c:
                resp = await c.post(WHISPER_URL, headers=headers, data=form, files=files)
        if resp.status_code != 200:
            return "error", ""
        return parse_whisper_payload(resp.json())
    except Exception:
        return "error", ""


async def synthesize_openai(text: str, gender: str, api_key: str,
                            client: httpx.AsyncClient | None = None) -> tuple[str, bytes]:
    """Synthesise text with OpenAI TTS. Returns (status, mp3 bytes).

    Status is ok | needs_key | error. Persian and English text are both
    read by the same voices; the model detects the input language.
    """
    if not api_key or not text.strip():
        return "needs_key", b""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": TTS_MODEL, "input": text.strip(),
               "voice": openai_voice_for(gender), "response_format": "mp3"}
    try:
        if client is not None:
            resp = await client.post(TTS_URL, headers=headers, json=payload, timeout=120)
        else:
            async with httpx.AsyncClient(timeout=120) as c:
                resp = await c.post(TTS_URL, headers=headers, json=payload)
        if resp.status_code != 200 or not resp.content:
            return "error", b""
        return "ok", resp.content
    except Exception:
        return "error", b""
