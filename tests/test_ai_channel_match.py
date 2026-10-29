"""AI/channel-only identification: the Instagram-link gap.

Real cause (see handlers._identify_link_audio docstring): Instagram
pages do not advertise a public media file, so no audio reaches
Shazam/AudD; the channel engine was never run on links (no query
text); and channel matching was text-only, so a channel audio file
could never match by content. These tests cover the fix, offline,
with fakes — no live Instagram, channel, or Shazam call is claimed.
"""
import pytest

from app import db as dbmod
from app.bot import handlers
from app.search.audio_match import chunk_hashes, find_match, fingerprint
from app.search.channel import channel_post_matches
from app.services.link_media import extract_media_url, extract_page_text
from app.services.shazam_id import ShazamIdResult


def _blob(seed: bytes, blocks: int = 8) -> bytes:
    return seed * (65536 * blocks)


def test_chunk_fingerprint_identical_and_shortened_match():
    data = _blob(b"A", 8) + _blob(b"B", 8)
    stored = [{"sha256": fingerprint(data)["sha256"],
               "chunk_hashes": __import__("json").dumps(chunk_hashes(data)),
               "title": "AI Song", "artist": "", "channel": "ch1", "post_url": "https://t.me/ch1/1"}]
    assert find_match(stored, data) is not None  # identical
    shortened = data[: int(len(data) * 0.75)]
    assert find_match(stored, shortened) is not None  # prefix preserved


def test_chunk_fingerprint_unrelated_never_matches():
    data = _blob(b"A", 8)
    other = _blob(b"Z", 8)
    stored = [{"sha256": fingerprint(data)["sha256"],
               "chunk_hashes": __import__("json").dumps(chunk_hashes(data))}]
    assert find_match(stored, other) is None
    assert find_match(stored, b"") is None
    assert find_match([], data) is None


def test_audio_index_roundtrip(db):
    data = _blob(b"C", 4)
    dbmod.index_audio_file(db, fingerprint(data), title="AI Song",
                           channel="ch1", caption="AI Song")
    dbmod.index_audio_file(db, fingerprint(data), title="AI Song")  # idempotent
    assert len(dbmod.list_audio_index(db)) == 1
    assert find_match(dbmod.list_audio_index(db), data) is not None
    assert find_match(dbmod.list_audio_index(db), _blob(b"Q", 4)) is None


def test_page_text_is_the_link_channel_query():
    page = ('<meta property="og:title" content="AI Song - Artist">'
            '<meta property="og:video" content="https://cdn.example.com/x.mp4">')
    assert extract_page_text(page) == "AI Song - Artist"
    assert extract_media_url(page) == "https://cdn.example.com/x.mp4"
    # Instagram-style page: no media file, but a title -> channel query exists.
    page2 = '<meta property="og:title" content="Some Reel Song">'
    assert extract_media_url(page2) is None
    assert extract_page_text(page2) == "Some Reel Song"
    assert extract_page_text("<html></html>") == ""


def test_channel_text_gate_still_blocks_text_only_and_partial():
    post = {"text": "آهنگ هوش مصنوعی غمگین", "has_media": True}
    assert channel_post_matches(post, "آهنگ هوش مصنوعی غمگین") is True
    assert channel_post_matches(post, "آهنگ شاد") is False
    assert channel_post_matches({"text": "آهنگ هوش مصنوعی غمگین", "has_media": False},
                                "آهنگ هوش مصنوعی غمگین") is False


class _FakeMessage:
    def __init__(self):
        self.answers = []

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)


@pytest.mark.asyncio
async def test_link_channel_stage_silent_for_non_owner(db, monkeypatch):
    """No public nag about unset channels: non-owners hear nothing."""
    from app.config import Config
    msg = _FakeMessage()
    msg.from_user = type("U", (), {"id": 99})()
    found = await handlers._run_link_channel_stage(msg, db, Config(), "Some Reel Song")
    assert found is False
    assert msg.answers == []


@pytest.mark.asyncio
async def test_link_channel_stage_points_owner_to_admin_panel(db, monkeypatch):
    """The owner alone gets one pointer to the admin panel bank section —
    never a CHANNEL_USERNAMES env-var nag in public."""
    from app.config import Config
    cfg = Config()
    cfg.owner_id = 7
    msg = _FakeMessage()
    msg.from_user = type("U", (), {"id": 7})()
    found = await handlers._run_link_channel_stage(msg, db, cfg, "Some Reel Song")
    assert found is False
    assert any("پنل مدیریت" in a for a in msg.answers)
    assert not any("CHANNEL_USERNAMES" in a for a in msg.answers)


@pytest.mark.asyncio
async def test_link_channel_stage_uses_page_text(db, monkeypatch):
    from app.config import Config
    dbmod.set_setting(db, "channel_usernames", "ch1")
    cfg = Config()

    async def fake_search(channels, query):
        assert channels == ["ch1"] and query == "AI Song - Artist"
        return [{"post_url": "https://t.me/ch1/9", "text": "AI Song - Artist audio"}]
    monkeypatch.setattr(handlers, "search_channels", fake_search)
    msg = _FakeMessage()
    found = await handlers._run_link_channel_stage(msg, db, cfg, "AI Song - Artist")
    assert found is True
    assert any("t.me/ch1/9" in a for a in msg.answers)
    assert not any("شناسایی شد" in a for a in msg.answers)  # channel post, not identified


class _SentMessage:
    def __init__(self, text):
        self.text = text
        self.reply_markup = None

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.reply_markup = reply_markup


class _LinkFakeMessage:
    def __init__(self, text):
        self.text = text
        self.from_user = type("U", (), {"id": 42, "username": "t", "first_name": "T"})()
        self.answers = []
        self.last_sent = None

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)
        self.last_sent = _SentMessage(text)
        return self.last_sent

    async def answer_video(self, video, caption=None, reply_markup=None):
        pass


@pytest.mark.asyncio
async def test_link_handler_needs_file_single_compact_card(db, monkeypatch):
    """End-to-end link branch: Instagram-style needs_file folds into ONE
    compact card (video line + one honest line) — no channel-settings
    nag, no stacked messages."""
    from app.config import Config

    async def fake_identify(outcome, cfg):
        return ("needs_file", "", "", True, "Some Reel Song")
    monkeypatch.setattr(handlers, "_identify_link_audio", fake_identify)
    msg = _LinkFakeMessage("https://www.instagram.com/reel/De99zjKIQe5/")
    await handlers.text_handler(msg, db, Config())
    card = msg.last_sent.text
    assert "🎬 ویدیوی همان لینک:" in card
    assert "https://www.instagram.com/reel/De99zjKIQe5/" in card
    assert "مستقیم بفرستید" in card
    joined = "\n".join(msg.answers)
    assert "تنظیم نشده" not in joined
    assert "CHANNEL_USERNAMES" not in joined


@pytest.mark.asyncio
async def test_multi_window_shazam_only_accepts_identified(monkeypatch):
    calls = []

    async def fake_shazam(data):
        calls.append(len(data))
        if len(calls) == 1:
            return ShazamIdResult(status="not_found")
        return ShazamIdResult(status="identified", title="Dooset Daram",
                              artist="Mehdi Ahmadvand")
    # Directly test the multi-window helper via its own module.
    import app.services.shazam_id as sh
    monkeypatch.setattr(sh, "identify_bytes_shazam", fake_shazam)
    out = await sh.identify_bytes_shazam_multi(b"x" * 300_000)
    assert out.status == "identified" and out.title == "Dooset Daram"
    assert len(calls) >= 2  # second window was tried
