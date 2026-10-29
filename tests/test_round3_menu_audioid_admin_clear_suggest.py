"""Targeted tests for the 2026-10-29 fix round 3:
1. Standard menu/back behavior: in-bot «❌ لغو» on every flow, flow:cancel
   returns to the persistent main menu, keyboard policy documented.
2. «شناسایی موسیقی از فایل»: the menu button arms a real await-file state;
   audio/voice files get a same-file «🔄 تلاش مجدد» retry; expired tokens
   fail honestly.
3. Admin panel hidden from regular users: /admin not in the user command
   menu; cmd_admin and the _owner_only decorator deny non-owners.
4. Clear-chat split: «پاک‌سازی وضعیت گفت‌وگو» vs «حذف کامل اطلاعات من».
5. Suggest buttons: expired keys and delivery errors produce honest
   messages, never a silent death.
No network is used.
"""
import asyncio

import pytest
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from app import db as dbmod
from app.bot import handlers
from app.config import Config
from app.features import admin as adminfeat
from app.features import pro as profeat


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = "u"
        self.first_name = "U"


class _FakeMessage:
    def __init__(self, uid=11, text="x"):
        self.from_user = _FakeUser(uid)
        self.text = text
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _FakeMessage(self.from_user.id)


class _FakeState:
    def __init__(self):
        self.cleared = False
        self.state = None

    async def clear(self):
        self.cleared = True
        self.state = None

    async def set_state(self, s):
        self.state = s

    async def get_state(self):
        return self.state


class _FakeCallback:
    def __init__(self, uid=11, data=""):
        self.from_user = _FakeUser(uid)
        self.data = data
        self.message = _FakeMessage(uid)
        self.answered = []

    async def answer(self, text="", **kwargs):
        self.answered.append((text, kwargs))

    @property
    def bot(self):
        raise AssertionError("must not be reached in these tests")


# --- item 1: menu / back behavior -------------------------------------------

def test_cancel_kb_offers_flow_cancel():
    kb = handlers._cancel_kb()
    assert isinstance(kb, InlineKeyboardMarkup)
    btns = [b for row in kb.inline_keyboard for b in row]
    assert any(b.text == "❌ لغو" and b.callback_data == "flow:cancel"
               for b in btns)


def test_flow_cancel_registered():
    names = {getattr(h.callback, "__name__", "")
             for h in handlers.router.callback_query.handlers}
    assert "flow_cancel" in names, "flow:cancel callback not registered"


def test_flow_cancel_clears_state_and_restores_main_menu(db):
    cfg = Config(bot_token="<redacted>", owner_id=1)
    cb = _FakeCallback(uid=11, data="flow:cancel")
    state = _FakeState()
    state.state = "suggest"
    asyncio.run(handlers.flow_cancel(cb, db, cfg, state))
    assert state.cleared, "flow:cancel must clear the conversation state"
    assert cb.message.answers, "no status message sent"
    text, kwargs = cb.message.answers[0]
    assert "منوی اصلی" in text
    kb = kwargs.get("reply_markup")
    assert isinstance(kb, ReplyKeyboardMarkup)
    assert kb.resize_keyboard and kb.is_persistent


# --- item 2: «شناسایی موسیقی از فایل» ----------------------------------------

def test_audio_id_menu_arms_await_file_state(db):
    cfg = Config(bot_token="<redacted>", owner_id=1)
    msg = _FakeMessage(uid=11)
    state = _FakeState()
    asyncio.run(handlers._run_menu_title(msg, db, cfg, state, "audio_id"))
    assert state.state == "audio_id:await_file", \
        f"audio_id must arm a pending-file state, got {state.state!r}"
    assert msg.answers, "no prompt sent"
    text, kwargs = msg.answers[0]
    assert "فایل صوتی" in text and "ویس" in text, text[:80]
    kb = kwargs.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup)
    btns = [b for row in kb.inline_keyboard for b in row]
    assert any(b.callback_data == "flow:cancel" for b in btns), \
        "the await-file prompt must offer in-bot cancel"


def test_audio_retry_registry_and_label():
    handlers._VIDEO_IDENTIFY.clear()
    kb = handlers._video_identify_kb(11, "file_abc", "audio",
                                     label="🔄 تلاش مجدد")
    btns = [b for row in kb.inline_keyboard for b in row]
    assert btns and btns[0].text == "🔄 تلاش مجدد"
    token = btns[0].callback_data.split("vid:id:", 1)[1]
    entry = handlers._VIDEO_IDENTIFY.get(token)
    assert entry and entry["kind"] == "audio"
    assert entry["user_id"] == 11 and entry["file_id"] == "file_abc"
    handlers._VIDEO_IDENTIFY.clear()


def test_video_identify_expired_token_is_honest(db):
    cfg = Config(bot_token="<redacted>", owner_id=1)
    handlers._VIDEO_IDENTIFY.clear()
    cb = _FakeCallback(uid=11, data="vid:id:deadbeefdeadbeef")
    asyncio.run(handlers.video_identify(cb, db, cfg))
    assert cb.answered, "expired token must not silently die"
    text, kwargs = cb.answered[0]
    assert "منقضی" in text, text
    assert kwargs.get("show_alert")


# --- item 3: admin hidden from regular users ---------------------------------

def test_user_commands_hide_admin_owner_commands_show_it():
    user_cmds = [c.command for c in handlers._user_commands()]
    owner_cmds = [c.command for c in handlers._owner_commands()]
    assert "admin" not in user_cmds, f"regular users must not see /admin: {user_cmds}"
    assert "admin" in owner_cmds
    assert "start" in user_cmds and "help" in user_cmds


def test_cmd_admin_denies_non_owner(db):
    cfg = Config(bot_token="<redacted>", owner_id=1)
    msg = _FakeMessage(uid=999, text="/admin")
    asyncio.run(handlers.cmd_admin(msg, db, cfg))
    assert msg.answers, "non-owner /admin must get a denial, not silence"
    text, _ = msg.answers[0]
    assert "دسترسی" in text, text


def test_cmd_admin_allows_owner(db):
    cfg = Config(bot_token="<redacted>", owner_id=1)
    msg = _FakeMessage(uid=1, text="/admin")
    asyncio.run(handlers.cmd_admin(msg, db, cfg))
    assert msg.answers
    text, kwargs = msg.answers[0]
    assert "پنل مدیریت" in text, text[:60]


def test_owner_only_decorator_denies_non_owner(db):
    cfg = Config(bot_token="<redacted>", owner_id=1)

    @adminfeat._owner_only
    async def _dummy(event, conn, config):
        return "SHOULD NOT RUN"

    class _Ev:
        from_user = _FakeUser(999)

    denied = []

    async def _deny(event, lang="fa"):
        denied.append(True)

    orig = adminfeat._deny_owner
    adminfeat._deny_owner = _deny
    try:
        res = asyncio.run(_dummy(_Ev(), db, cfg))
    finally:
        adminfeat._deny_owner = orig
    assert res is None and denied, "non-owner must be denied, handler not run"


# --- item 4: clear-chat split -------------------------------------------------

def test_clear_chat_feature_title_split(db):
    feat = dbmod.get_feature(db, "clear_chat")
    assert feat["title"] == "پاک‌سازی وضعیت گفت‌وگو", feat["title"]


def test_clear_and_wipe_titles_are_distinct():
    assert adminfeat.CLEAR_TITLE == "پاک‌سازی وضعیت گفت‌وگو"
    assert adminfeat.WIPE_TITLE == "حذف کامل اطلاعات من"
    assert adminfeat.CLEAR_TITLE != adminfeat.WIPE_TITLE
    text, kb = adminfeat.clear_view()
    btns = [b.text for row in kb.inline_keyboard for b in row]
    assert any("حذف کامل اطلاعات من" in b for b in btns), btns


# --- item 5: suggest -> real files --------------------------------------------

def test_suggest_get_expired_key_is_honest(db):
    cfg = Config(bot_token="<redacted>", owner_id=1)
    profeat.RESULTS.clear()
    cb = _FakeCallback(uid=11, data="sg:get:deadbeefdead")
    asyncio.run(handlers.suggest_get(cb, db, cfg))
    assert cb.answered, "expired suggestion must not silently die"
    text, kwargs = cb.answered[0]
    assert "منقضی" in text, text
    assert kwargs.get("show_alert")


def test_suggest_get_delivery_error_is_honest(db, monkeypatch):
    cfg = Config(bot_token="<redacted>", owner_id=1)
    key = profeat.remember_result("Dooset Daram", "Mehdi Ahmadvand", "", "")

    async def _boom(*a, **k):
        raise RuntimeError("send failed")

    monkeypatch.setattr(handlers, "_send_professional_song_card", _boom)
    cb = _FakeCallback(uid=11, data=f"sg:get:{key}")
    asyncio.run(handlers.suggest_get(cb, db, cfg))
    assert cb.message.answers, "delivery exception must produce an honest error"
    text, _ = cb.message.answers[0]
    assert "ممکن نشد" in text, text
    profeat.RESULTS.clear()
