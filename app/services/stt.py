"""Voice-to-text (speech recognition) — the reverse of text-to-speech.

This does NOT use a speech-synthesis engine. It uses a speech-recognition
service: Azure Speech-to-Text, language fa-IR (Persian, Iran), with the
same Azure Speech key/region as TTS (env only, AZURE_SPEECH_KEY /
AZURE_SPEECH_REGION). A Whisper-style API could be swapped in behind the
same function boundary later; the honesty contract is the same either way.

Honesty rule, enforced here and in the handler:
- No key/region -> status "needs_key", empty text. Never fabricate text.
- API returns no text -> status "empty", empty text. Never invent text.
- Only a service transcript (status "ok") may be sent as the user's text.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

LANGUAGE = "fa-IR"


@dataclass
class STTResult:
    status: str  # ok | needs_key | empty | error
    text: str = ""


def parse_stt_payload(payload: dict) -> STTResult:
    """Extract Azure STT DisplayText, or an empty result — never invented."""
    text = ""
    if isinstance(payload, dict):
        text = (payload.get("DisplayText") or payload.get("text") or "").strip()
    if not text:
        return STTResult(status="empty")
    return STTResult(status="ok", text=text)


async def transcribe_bytes(data: bytes, key: str, region: str,
                           client: httpx.AsyncClient | None = None) -> STTResult:
    """Transcribe voice/audio bytes to Persian text. Caller sends only text."""
    if not key or not region or not data:
        return STTResult(status="needs_key")
    url = (f"https://{region}.stt.speech.microsoft.com/speech/recognition/"
           f"conversation/cognitiveservices/v1?language={LANGUAGE}")
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "audio/wav",
        "Accept": "application/json",
    }
    try:
        if client is not None:
            resp = await client.post(url, headers=headers, content=data, timeout=60)
        else:
            async with httpx.AsyncClient(timeout=60) as c:
                resp = await c.post(url, headers=headers, content=data)
        resp.raise_for_status()
        return parse_stt_payload(resp.json())
    except Exception:
        return STTResult(status="error")
