"""Superior-intelligence layer (OpenAI brain): intent + cleaned query.

Without OPENAI_API_KEY the brain honestly reports "needs_key" and the
handlers fall back to rule-based/smart-variant search — never a faked
AI answer. With a key, a mocked Chat Completions response is parsed
into a strict intent + query; anything unparseable is "unknown" and the
original text is used unchanged. No real API calls are made.
"""
import httpx
import pytest

from app.bot import handlers
from app.config import Config
from app.services.brain import (
    brain_clean_query, parse_intent_payload, understand_intent,
)


def _chat_json(content: str):
    return {"choices": [{"message": {"content": content}}]}


def test_parse_intent_payload_strict():
    ok = parse_intent_payload(_chat_json('{"intent": "song_search", "query": "دوست دارم"}'))
    assert ok.status == "ok" and ok.intent == "song_search" and ok.query == "دوست دارم"
    assert parse_intent_payload(_chat_json("not json at all")).status == "unknown"
    assert parse_intent_payload({}).status == "unknown"
    assert parse_intent_payload(_chat_json('{"intent": "teleport", "query": "x"}')).status == "unknown"
    assert parse_intent_payload(_chat_json('{"intent": "song_search", "query": ""}')).status == "unknown"


@pytest.mark.asyncio
async def test_brain_without_key_never_fakes_intent():
    result = await understand_intent("دوست دارم", "")
    assert result.status == "needs_key" and result.query == ""
    assert await brain_clean_query("دوست دارم", "") == "دوست دارم"


def _mock_client(payload, status=200):
    async def handler(request):
        assert "api.openai.com" in str(request.url)
        return httpx.Response(status, json=payload)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_brain_mocked_ok_and_error():
    client = _mock_client(_chat_json('{"intent": "song_search", "query": "Dooset Daram"}'))
    result = await understand_intent("دوست دارم مهدی احمدوند", "fake-key", client=client)
    assert result.status == "ok" and result.query == "Dooset Daram"
    assert await brain_clean_query("دوست دارم مهدی احمدوند", "fake-key", client=client) == "Dooset Daram"

    bad = _mock_client({"error": {"message": "bad key"}}, status=401)
    assert (await understand_intent("دوست دارم", "bad-key", client=bad)).status == "error"
    assert await brain_clean_query("دوست دارم", "bad-key", client=bad) == "دوست دارم"


@pytest.mark.asyncio
async def test_text_handler_uses_brain_query_when_connected(monkeypatch, db):
    """With an OpenAI key, the brain-cleaned query reaches the search."""
    config = Config()
    config.openai_api_key = "fake-key"
    seen = {}

    async def fake_brain(text, key):
        assert key == "fake-key"
        return "Dooset Daram"

    async def fake_search(message, conn, query, **kw):
        seen["query"] = query
        await message.answer("searched")

    monkeypatch.setattr(handlers, "brain_clean_query", fake_brain)
    monkeypatch.setattr(handlers, "_do_search", fake_search)

    class FakeUser:
        id = 42
        username = "tester"
        first_name = "Test"

    class FakeMessage:
        def __init__(self):
            self.text = "دوست دارم مهدی احمدوند"
            self.from_user = FakeUser()
            self.answers = []

        async def answer(self, text, reply_markup=None):
            self.answers.append(text)

    await handlers.text_handler(FakeMessage(), db, config)
    assert seen["query"] == "Dooset Daram"


@pytest.mark.asyncio
async def test_text_handler_without_key_never_calls_brain(monkeypatch, db):
    """Without a key the brain is not called at all (rule-based fallback)."""
    config = Config()
    config.openai_api_key = ""
    seen = {}

    async def forbidden_brain(*args, **kwargs):
        raise AssertionError("brain must not be called without a key")

    async def fake_search(message, conn, query, **kw):
        seen["query"] = query

    monkeypatch.setattr(handlers, "brain_clean_query", forbidden_brain)
    monkeypatch.setattr(handlers, "_do_search", fake_search)

    class FakeUser:
        id = 42
        username = "tester"
        first_name = "Test"

    class FakeMessage:
        def __init__(self):
            self.text = "دوست دارم"
            self.from_user = FakeUser()

        async def answer(self, text, reply_markup=None):
            pass

    await handlers.text_handler(FakeMessage(), db, config)
    assert seen["query"] == "دوست دارم"
