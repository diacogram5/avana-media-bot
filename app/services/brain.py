"""The bot's brain: a superior-intelligence layer via OpenAI (ChatGPT).

What it does, and what it does not do:
- It understands free text: whether the user typed a song name, an
  artist name, or a lyric line, and it cleans that into one search
  query (fixing filler words and Finglish/Persian spelling variants).
- Intent from a link or a film needs no brain call: it is deterministic
  from the message type (a link -> link flow, a film/audio -> audio
  identification), and the handlers already route those without any
  button. The brain is used for the ambiguous case — free text.
- Voice-to-text (Whisper) and text-to-voice (TTS) use the same OpenAI
  engine and key; see app/services/openai_speech.py.

Why OpenAI: "connect it to yourself" (Muse) is not available as an API
this bot's code can call — Muse has no public API for this bot. The
practical equivalent is OpenAI (the ChatGPT maker), under the same env
key, OPENAI_API_KEY. This is written in .env.example and the README too.

Honesty rules:
- No key -> status "needs_key", no intent, no cleaned query. The
  handlers then fall back to the existing rule-based/smart-variant
  search (which never claims to be an AI answer), and /help honestly
  says the superior intelligence is not connected.
- Unparseable or empty model output -> status "unknown", original text
  is used unchanged. An intent or query is never fabricated.
- The model is asked for strict JSON only, temperature 0, so its output
  is a classification, not free prose.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

CHAT_URL = "https://api.openai.com/v1/chat/completions"
CHAT_MODEL = "gpt-4o-mini"

_SYSTEM_PROMPT = (
    "You classify a user's short message for a Persian music bot and "
    "extract one clean search query. Reply with strict JSON only, no other "
    "text, in this exact shape: "
    '{"intent": "song_search", "query": "..."}. '
    'intent is one of: "song_search" (song name, artist name, or lyric '
    'line), "help" (a help question), "other" (anything else). '
    "query is the cleaned song/artist/lyric text, with filler words "
    "removed; for a lyric line keep the line itself; for help/other use "
    'an empty string. Never invent a song, artist or lyric that the user '
    "did not write."
)


@dataclass
class BrainIntent:
    status: str  # ok | needs_key | error | unknown
    intent: str = "other"
    query: str = ""


def parse_intent_payload(payload: dict) -> BrainIntent:
    """Parse a Chat Completions response into a BrainIntent, or unknown.

    The model reply must be strict JSON with an intent and a query;
    anything else is "unknown" and the caller's original text is used.
    """
    try:
        choices = payload.get("choices") or []
        content = choices[0]["message"]["content"]
        data = json.loads(content)
    except Exception:
        return BrainIntent(status="unknown")
    if not isinstance(data, dict):
        return BrainIntent(status="unknown")
    intent = str(data.get("intent") or "").strip()
    query = str(data.get("query") or "").strip()
    if intent not in ("song_search", "help", "other"):
        return BrainIntent(status="unknown")
    if intent == "song_search" and not query:
        return BrainIntent(status="unknown")
    return BrainIntent(status="ok", intent=intent, query=query)


async def understand_intent(text: str, api_key: str,
                            client: httpx.AsyncClient | None = None) -> BrainIntent:
    """Understand a user's free text via OpenAI. Returns a BrainIntent.

    Status is ok | needs_key | error | unknown. No key -> needs_key.
    A link or a film is not passed here by the handlers: their intent is
    deterministic from the message type (link flow / audio identification).
    """
    if not api_key or not (text or "").strip():
        return BrainIntent(status="needs_key")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": CHAT_MODEL, "temperature": 0,
               "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": text.strip()}]}
    try:
        if client is not None:
            resp = await client.post(CHAT_URL, headers=headers, json=payload, timeout=60)
        else:
            async with httpx.AsyncClient(timeout=60) as c:
                resp = await c.post(CHAT_URL, headers=headers, json=payload)
        if resp.status_code != 200:
            return BrainIntent(status="error")
        return parse_intent_payload(resp.json())
    except Exception:
        return BrainIntent(status="error")


async def brain_clean_query(text: str, api_key: str,
                            client: httpx.AsyncClient | None = None) -> str:
    """Return the brain-cleaned search query, or the original text.

    Used by the text handler when the brain is connected. If the brain
    is not connected or its answer is unusable, the original text is
    returned unchanged — the caller then runs the normal smart search.
    """
    result = await understand_intent(text, api_key, client=client)
    if result.status == "ok" and result.intent == "song_search" and result.query:
        return result.query
    return text
