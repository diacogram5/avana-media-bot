"""ITEM 3 — clear-chat re-verification (final round).

Simulates the user's exact expectation: start a flow -> clear -> start a
new flow, asserting ZERO leakage from the previous operation (stale FSM
state/data, stale in-memory identify tokens, stale tracked messages),
while favorites / playlists / search history / settings stay untouched.

Also covers the three visually distinct options:
  (a) «پاک‌سازی وضعیت گفتگو»  -> adm:clear:state
  (b) «حذف پیام‌های چت»        -> adm:clear:msgs
  (c) «حذف کامل اطلاعات من»    -> adm:wipe
and the honest Telegram delete limits (Bot API: a message can only be
deleted if it was sent less than 48 hours ago; in a private chat the bot
may delete its own and the user's incoming messages within that window).

No network is used.
"""
import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from app import db as dbmod
from app.bot import handlers
from app.config import Config
from app.features import admin as adminfeat


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeChatMessage:
    """Stands in for the callback's message (supports delete + answer)."""

    def __init__(self, uid):
        self.from_user = _FakeUser(uid)
        self.chat = type("C", (), {"id": uid})()
        self.answers = []
        self.deleted = False

    async def delete(self):
        self.deleted = True

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _FakeChatMessage(self.from_user.id)


class _FakeBot:
    """delete_message outcomes keyed by message_id."""

    def __init__(self, fail_ids=(), gone_ids=()):
        self.fail_ids = set(fail_ids)
        self.gone_ids = set(gone_ids)
        self.attempted = []

    async def delete_message(self, chat_id, message_id):
        self.attempted.append(message_id)
        if message_id in self.gone_ids:
            raise TelegramBadRequest(
                method="deleteMessage",
                message="Bad Request: message to delete not found")
        if message_id in self.fail_ids:
            raise TelegramBadRequest(
                method="deleteMessage",
                message="Bad Request: message can't be deleted")
        return True


class _FakeCallback:
    def __init__(self, uid, bot):
        self.from_user = _FakeUser(uid)
        self.message = _FakeChatMessage(uid)
        self._bot = bot
        self.answered = []

    @property
    def bot(self):
        return self._bot

    async def answer(self, text="", **kwargs):
        self.answered.append((text, kwargs))


class _FakeState:
    def __init__(self):
        self.state = None
        self.data = {}
        self.cleared = False

    async def clear(self):
        self.cleared = True
        self.state = None
        self.data = {}

    async def set_state(self, s):
        self.state = s

    async def get_state(self):
        return self.state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)


def _cfg():
    return Config(bot_token="<redacted>", owner_id=1)


# --- (a)/(b)/(c) are visually distinct ----------------------------------------

def test_clear_view_has_three_distinct_options():
    text, kb = adminfeat.clear_view()
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert datas.count("adm:clear:state") == 1
    assert datas.count("adm:clear:msgs") == 1
    assert datas.count("adm:wipe") == 1
    # The option labels name the three operations explicitly.
    assert any("وضعیت گفتگو" in l for l in labels)
    assert any("پیام‌های چت" in l for l in labels)
    assert any("اطلاعات من" in l for l in labels)
    # Honest Telegram limits are stated on the card itself.
    assert "۴۸ ساعت" in text


def test_clear_msgs_confirm_states_48h_limit():
    text, kb = adminfeat.clear_msgs_view(3)
    assert "۴۸ ساعت" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "adm:clear:msgs:yes" in datas and "adm:clear:msgs:no" in datas


# --- stale in-memory tokens are purged -----------------------------------------

def test_purge_user_registries_drops_only_that_user():
    handlers._VIDEO_IDENTIFY.clear()
    handlers._VIDEO_IDENTIFY_RUNNING.clear()
    handlers._VIDEO_IDENTIFY["t1"] = {"user_id": 11, "file_id": "f1"}
    handlers._VIDEO_IDENTIFY["t2"] = {"user_id": 11, "file_id": "f2"}
    handlers._VIDEO_IDENTIFY["t3"] = {"user_id": 22, "file_id": "f3"}
    handlers._VIDEO_IDENTIFY_RUNNING.add("t1")
    try:
        dropped = handlers.purge_user_registries(11)
        assert dropped == 2
        assert "t1" not in handlers._VIDEO_IDENTIFY
        assert "t2" not in handlers._VIDEO_IDENTIFY
        assert "t3" in handlers._VIDEO_IDENTIFY, "other user's token leaked into purge"
        assert "t1" not in handlers._VIDEO_IDENTIFY_RUNNING
    finally:
        handlers._VIDEO_IDENTIFY.clear()
        handlers._VIDEO_IDENTIFY_RUNNING.clear()


# --- (b) honest delete counts ----------------------------------------------------

def test_clear_msgs_yes_honest_counts(db):
    bot = _FakeBot(fail_ids={102}, gone_ids={103})
    cb = _FakeCallback(11, bot)
    adminfeat.track(db, 11, 11, 101)   # will delete
    adminfeat.track(db, 11, 11, 102)   # 48h-style failure
    adminfeat.track(db, 11, 11, 103)   # already gone -> counts as deleted
    adminfeat.track(db, 22, 22, 201)   # other user untouched
    asyncio.run(adminfeat.clear_msgs_yes(cb, db, _cfg()))
    assert bot.attempted == [101, 102, 103]
    text, _ = cb.message.answers[0]
    assert "2 پیام پاک شد" in text, text
    assert "1 پیام پاک نشد" in text, text
    assert "۴۸ ساعت" in text, "the 48h limit must be stated plainly"
    assert adminfeat.list_outbox(db, 11) == []
    assert len(adminfeat.list_outbox(db, 22)) == 1


def test_clear_msgs_yes_empty_outbox_is_honest(db):
    cb = _FakeCallback(11, _FakeBot())
    asyncio.run(adminfeat.clear_msgs_yes(cb, db, _cfg()))
    text, _ = cb.message.answers[0]
    assert "پیامی برای پاک‌سازی پیدا نشد" in text


# --- full leakage simulation: flow -> clear -> new flow --------------------------

def test_clear_state_zero_leakage_then_new_flow(db):
    """The user's exact expectation: after clear, back to post-/start
    state with nothing leaking from the previous operation."""
    uid = 11
    cfg = _cfg()

    # 1) A previous operation is mid-flight: FSM state+data, an
    #    identify-button token, tracked messages, and stored user data.
    state = _FakeState()
    asyncio.run(state.set_state("lyr:await_query"))
    asyncio.run(state.update_data(query="old query", page=2))
    handlers._VIDEO_IDENTIFY.clear()
    handlers._VIDEO_IDENTIFY["oldtok"] = {"user_id": uid, "file_id": "oldfile",
                                         "kind": "file", "audio_url": None}
    adminfeat.track(db, uid, uid, 301)
    adminfeat.track(db, uid, uid, 302)
    dbmod.add_favorite(db, uid, "fav song", "fav artist")
    pl = dbmod.create_playlist(db, uid, "old playlist")
    dbmod.add_to_playlist(db, pl["id"], "s", "a")
    dbmod.record_search(db, uid, "old search")
    dbmod.set_setting(db, "theme", "dark", by_id=uid)

    try:
        # 2) The user taps (a) «پاک‌سازی وضعیت گفتگو».
        cb = _FakeCallback(uid, _FakeBot())
        asyncio.run(adminfeat.clear_state(cb, db, cfg, state))

        # 3) Zero leakage assertions.
        assert state.cleared
        assert asyncio.run(state.get_state()) is None
        assert asyncio.run(state.get_data()) == {}, "stale FSM data leaked"
        assert "oldtok" not in handlers._VIDEO_IDENTIFY, "stale token leaked"
        assert adminfeat.list_outbox(db, uid) == [], "stale tracked messages leaked"
        # Stored user data untouched.
        assert len(dbmod.list_favorites(db, uid)) == 1
        assert len(dbmod.list_playlists(db, uid)) == 1
        assert len(dbmod.recent_searches(db, uid)) == 1
        assert dbmod.get_setting(db, "theme", "") == "dark"
        # Landed back on the persistent main menu.
        assert cb.message.answers
        _text, kwargs = cb.message.answers[0]
        assert isinstance(kwargs.get("reply_markup"), InlineKeyboardMarkup)

        # 4) Start a NEW flow: nothing from the old one may surface.
        state2 = _FakeState()
        asyncio.run(state2.set_state("suggest"))
        asyncio.run(state2.update_data(mood="new mood"))
        assert asyncio.run(state2.get_state()) == "suggest"
        data2 = asyncio.run(state2.get_data())
        assert data2 == {"mood": "new mood"}, data2
        assert "query" not in data2 and "old query" not in str(data2)
        assert "oldtok" not in handlers._VIDEO_IDENTIFY
    finally:
        handlers._VIDEO_IDENTIFY.clear()
        handlers._VIDEO_IDENTIFY_RUNNING.clear()


def test_clear_state_does_not_delete_chat_messages(db):
    """(a) resets state; it must not delete chat messages — that is (b)."""
    bot = _FakeBot()
    cb = _FakeCallback(11, bot)
    adminfeat.track(db, 11, 11, 401)
    state = _FakeState()
    asyncio.run(state.set_state("audio_id:await_file"))
    asyncio.run(adminfeat.clear_state(cb, db, _cfg(), state))
    assert bot.attempted == [], "option (a) must not delete chat messages"
    assert state.cleared
