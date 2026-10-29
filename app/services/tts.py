"""Persian text-to-speech via Azure Speech (REST, env key/region only).

Voices (Azure Speech fa-IR, documented neural voices):
- female: fa-IR-DilaraNeural
- male:   fa-IR-FaridNeural
No device voice is ever substituted: if no key/region is configured the
function returns status "needs_key" and the bot must say so honestly,
never play a wrong-language voice and call it Persian.
Returns (status, audio_bytes): status is ok | needs_key | error.
"""
from __future__ import annotations

import html

import httpx

VOICES = {"female": "fa-IR-DilaraNeural", "male": "fa-IR-FaridNeural"}
# Persian user words mapped to the same voices.
VOICE_ALIASES = {"زن": "female", "خانم": "female", "female": "female",
                 "مرد": "male", "آقا": "male", "male": "male"}


def voice_for(choice: str) -> str:
    return VOICES[VOICE_ALIASES.get((choice or "").strip(), "female")]


def build_ssml(text: str, voice: str) -> str:
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xml:lang="fa-IR"><voice name="%s">%s</voice></speak>'
        % (voice, html.escape(text))
    )


async def synthesize(text: str, gender: str, key: str, region: str,
                     client: httpx.AsyncClient | None = None) -> tuple[str, bytes]:
    """Return (status, wav/mp3 bytes). Caller sends bytes as a Telegram voice/audio."""
    if not key or not region or not text.strip():
        return "needs_key", b""
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-16khz-128kbitrate-mono-mp3",
        "User-Agent": "telegram-song-bot",
    }
    try:
        if client is not None:
            resp = await client.post(url, headers=headers,
                                     content=build_ssml(text, voice_for(gender)).encode("utf-8"),
                                     timeout=60)
        else:
            async with httpx.AsyncClient(timeout=60) as c:
                resp = await c.post(url, headers=headers,
                                    content=build_ssml(text, voice_for(gender)).encode("utf-8"))
        resp.raise_for_status()
        if not resp.content:
            return "error", b""
        return "ok", resp.content
    except Exception:
        return "error", b""
