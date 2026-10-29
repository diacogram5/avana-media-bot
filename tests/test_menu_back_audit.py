"""Menu/back-button audit tests (2026-10-29 fix round).

Every multi-step flow must offer an in-bot way back without /start:
- reply-keyboard / inline entries that arm an FSM state must show
  «❌ لغو» (flow:cancel);
- admin (adm:*) and bank (bank:*) awaiting prompts must show «لغو»
  (adm:cancel / bank:cancel) which clears the FSM state;
- a persistent-menu tap must never be swallowed as flow input
  (menu_escape on bank add/edit handlers).

No network is used (bank panel probing is monkeypatched).
"""
import asyncio

import pytest
from aiogram import Router
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from app import db as dbmod
from app.bot import handlers
from app.bot.logic import capabilities_menu
from app.config import Config
from app.features import admin as adminfeat
from app.features import bank as bankfeat


OWNER = 1
STRANGER = 9


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = "u"
        self.first_name = "U"


class _FakeMessage:
    def __init__(self, uid=11, text="x"):
        self.from_user = _FakeUser(uid)
        self.text = text
        self.answers = []   # (text, kwargs)
        self.edits = []     # (text, kwargs)

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _FakeMessage(self.from_user.id)

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class _FakeState:
    def __init__(self):
        self.cleared = False
        self.state = None
        self.data = {}

    async def clear(self):
        self.cleared = True
        self.state = None

    async def set_state(self, s):
        self.state = s

    async def get_state(self):
        return self.state

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


class _FakeCallback:
    def __init__(self, uid=11, data=""):
        self.from_user = _FakeUser(uid)
        self.data = data
        self.message = _FakeMessage(uid)
        self.answered = []

    async def answer(self, text="", **kwargs):
        self.answered.append((text, kwargs))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _inline_buttons(kwargs):
    kb = kwargs.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup), "prompt must carry an inline keyboard"
    return [b for row in kb.inline_keyboard for b in row]


def _find_handler(router, kind, name):
    """Locate a nested feature handler by function __name__ (walks
    sub-routers; works through functools.wraps-preserving decorators)."""
    def _walk(r):
        for h in getattr(getattr(r, kind), "handlers"):
            if getattr(h.callback, "__name__", "") == name:
                return h.callback
        for sub in r.sub_routers:
            found = _walk(sub)
            if found is not None:
                return found
        return None
    found = _walk(router)
    assert found is not None, f"{kind} handler {name!r} not registered"
    return found


def _cfg(owner_id=OWNER):
    return Config(bot_token="<redacted>", owner_id=owner_id)


# --- primary flows: handlers.py --------------------------------------------

def test_lyrics_inline_entry_offers_cancel(db):
    """menu:lyrics (inline «قابلیت‌ها» menu) arms lyr:await_query: the prompt
    must offer in-bot ❌ لغو."""
    cb = _FakeCallback(uid=11, data="menu:lyrics")
    state = _FakeState()
    _run(handlers.menu_lyrics(cb, db, _cfg(), state))
    assert state.state == "lyr:await_query"
    assert cb.message.answers, "lyrics prompt not sent"
    btns = _inline_buttons(cb.message.answers[0][1])
    assert any(b.text == "❌ لغو" and b.callback_data == "flow:cancel" for b in btns), \
        "lyrics prompt must offer ❌ لغو"


def test_suggest_retry_offers_cancel(db):
    """sg:retry re-arms the suggest state: the prompt must offer ❌ لغو."""
    cb = _FakeCallback(uid=11, data="sg:retry")
    state = _FakeState()
    _run(handlers.suggest_retry(cb, db, _cfg(), state))
    assert state.state == "suggest"
    assert cb.message.answers, "suggest prompt not sent"
    btns = _inline_buttons(cb.message.answers[0][1])
    assert any(b.text == "❌ لغو" and b.callback_data == "flow:cancel" for b in btns), \
        "suggest-retry prompt must offer ❌ لغو"


def test_flow_cancel_works_from_admin_state(db):
    """flow:cancel clears ANY state (incl. admin/bank FSM states) and lands
    on the persistent main menu."""
    cb = _FakeCallback(uid=11, data="flow:cancel")
    state = _FakeState()
    state.state = adminfeat.AdmStates.ad_title.state
    _run(handlers.flow_cancel(cb, db, _cfg(), state))
    assert state.cleared
    text, kwargs = cb.message.answers[0]
    assert "منوی اصلی" in text
    assert isinstance(kwargs.get("reply_markup"), ReplyKeyboardMarkup)


# --- admin FSM prompts: لغو -> adm:cancel -----------------------------------

def _admin_router():
    r = Router()
    adminfeat.register(r)
    return r


def test_adm_cancel_registered_clears_state_returns_to_panel(db):
    fn = _find_handler(_admin_router(), "callback_query", "adm_cancel")
    cb = _FakeCallback(uid=OWNER, data="adm:cancel")
    state = _FakeState()
    state.state = adminfeat.AdmStates.ad_title.state
    _run(fn(cb, db, _cfg(), state=state))
    assert state.cleared, "adm:cancel must clear the FSM state"
    assert cb.message.edits, "panel not re-rendered"
    assert "پنل مدیریت" in cb.message.edits[0][0]


def test_adm_cancel_denies_non_owner(db):
    fn = _find_handler(_admin_router(), "callback_query", "adm_cancel")
    cb = _FakeCallback(uid=STRANGER, data="adm:cancel")
    state = _FakeState()
    state.state = adminfeat.AdmStates.ad_title.state
    _run(fn(cb, db, _cfg(), state=state))
    assert not state.cleared, "non-owner must not clear the owner's state"
    assert not cb.message.edits


@pytest.mark.parametrize("handler_name,data", [
    ("feat_rename_start", "adm:feat:rename:lyrics"),
    ("feat_add_start", "adm:feat:add"),
    ("ad_new_start", "adm:ad:new"),
    ("ad_every_start", "adm:ad:every"),
    ("key_save_start", "adm:key:save"),
    ("set_start", "adm:set:welcome_message"),
])
def test_admin_start_prompts_carry_cancel(db, handler_name, data):
    """Every admin entry that arms an FSM state must show لغو."""
    fn = _find_handler(_admin_router(), "callback_query", handler_name)
    cb = _FakeCallback(uid=OWNER, data=data)
    state = _FakeState()
    _run(fn(cb, db, _cfg(), state=state))
    assert state.state is not None and not state.cleared, \
        f"{handler_name} must arm its awaiting state"
    assert cb.message.answers, f"{handler_name} sent no prompt"
    btns = _inline_buttons(cb.message.answers[0][1])
    assert any(b.text == "لغو" and b.callback_data == "adm:cancel" for b in btns), \
        f"{handler_name} prompt must offer لغو"


def test_admin_feat_add_text_prompt_carries_cancel(db):
    fn = _find_handler(_admin_router(), "message", "feat_add_name")
    msg = _FakeMessage(uid=OWNER, text="دکمه تستی")
    state = _FakeState()
    state.state = adminfeat.AdmStates.feat_add_name.state
    _run(fn(msg, conn=db, config=_cfg(), state=state))
    assert state.state == adminfeat.AdmStates.feat_add_text.state
    btns = _inline_buttons(msg.answers[0][1])
    assert any(b.text == "لغو" and b.callback_data == "adm:cancel" for b in btns)


def test_admin_ad_photo_prompt_carries_skip_and_cancel(db):
    fn = _find_handler(_admin_router(), "message", "ad_title_got")
    msg = _FakeMessage(uid=OWNER, text="تبلیغ تستی")
    state = _FakeState()
    state.state = adminfeat.AdmStates.ad_title.state
    _run(fn(msg, conn=db, config=_cfg(), state=state))
    assert state.state == adminfeat.AdmStates.ad_photo.state
    btns = _inline_buttons(msg.answers[0][1])
    assert any(b.text == "رد شدن" for b in btns), "optional step keeps رد شدن"
    assert any(b.text == "لغو" and b.callback_data == "adm:cancel" for b in btns)


def test_admin_ad_edit_prompt_carries_cancel(db):
    ad_id = adminfeat.create_ad(db, "t", "d", by_id=OWNER)
    fn = _find_handler(_admin_router(), "callback_query", "ad_edit_start")
    cb = _FakeCallback(uid=OWNER, data=f"adm:ad:editfield:{ad_id}:title")
    state = _FakeState()
    _run(fn(cb, db, _cfg(), state=state))
    assert state.state == adminfeat.AdmStates.ad_edit.state
    btns = _inline_buttons(cb.message.answers[0][1])
    assert any(b.text == "لغو" and b.callback_data == "adm:cancel" for b in btns)


# --- bank FSM prompts: لغو -> bank:cancel -----------------------------------

def _bank_router():
    r = Router()
    bankfeat.register(r)
    return r


def test_bank_addkind_prompt_offers_cancel(db):
    fn = _find_handler(_bank_router(), "callback_query", "_bank_addkind")
    cb = _FakeCallback(uid=OWNER, data="bank:addkind:channel")
    state = _FakeState()
    _run(fn(cb, db, _cfg(), state=state))
    assert state.state == bankfeat.BankStates.add_identifier.state
    btns = _inline_buttons(cb.message.answers[0][1])
    assert any(b.text == "لغو" and b.callback_data == "bank:cancel" for b in btns), \
        "bank add prompt must offer لغو"


def test_bank_edit_prompt_offers_cancel(db):
    res = bankfeat.add_bank_source(db, "channel", "AiMusical")
    assert res["ok"] and res["id"]
    fn = _find_handler(_bank_router(), "callback_query", "_bank_edit")
    cb = _FakeCallback(uid=OWNER, data=f"bank:edit:{res['id']}")
    state = _FakeState()
    _run(fn(cb, db, _cfg(), state=state))
    assert state.state == bankfeat.BankStates.edit_identifier.state
    btns = _inline_buttons(cb.message.answers[0][1])
    assert any(b.text == "لغو" and b.callback_data == "bank:cancel" for b in btns), \
        "bank edit prompt must offer لغو"


def test_bank_cancel_clears_state_returns_to_panel(db, monkeypatch, tmp_path):
    from app.db import init_db
    # _send_panel re-reads the DB through config.db_path: point it at a real
    # migrated tmp DB so no network and no stray file are involved.
    tdb = init_db(str(tmp_path / "bank.db"))
    cfg = Config(bot_token="<redacted>", owner_id=OWNER,
                 db_path=str(tmp_path / "bank.db"))
    monkeypatch.setattr(bankfeat, "probe_suggested", lambda: {})
    fn = _find_handler(_bank_router(), "callback_query", "_bank_cancel")
    cb = _FakeCallback(uid=OWNER, data="bank:cancel")
    state = _FakeState()
    state.state = bankfeat.BankStates.add_identifier.state
    try:
        _run(fn(cb, tdb, cfg, state=state))
    finally:
        tdb.close()
    assert state.cleared, "bank:cancel must clear the FSM state"
    assert cb.message.edits, "bank panel not re-rendered"
    assert bankfeat.PANEL_TITLE in cb.message.edits[0][0]


def test_bank_cancel_denies_non_owner(db, monkeypatch):
    monkeypatch.setattr(bankfeat, "probe_suggested", lambda: {})
    fn = _find_handler(_bank_router(), "callback_query", "_bank_cancel")
    cb = _FakeCallback(uid=STRANGER, data="bank:cancel")
    state = _FakeState()
    state.state = bankfeat.BankStates.add_identifier.state
    _run(fn(cb, db, _cfg(), state=state))
    assert not state.cleared
    assert not cb.message.edits


def test_bank_add_identifier_menu_title_escapes(db):
    """A persistent-menu tap mid-bank-add must escape (menu_escape), not be
    swallowed as the channel identifier."""
    fn = _find_handler(_bank_router(), "message", "_bank_add_identifier")
    title = capabilities_menu(db)[0]["title"]
    msg = _FakeMessage(uid=OWNER, text=title)
    state = _FakeState()
    state.state = bankfeat.BankStates.add_identifier.state
    before = len(bankfeat.list_bank_sources(db))
    _run(fn(msg, conn=db, config=_cfg(), state=state))
    assert state.cleared, "menu tap must escape the bank-add state"
    assert len(bankfeat.list_bank_sources(db)) == before, \
        "menu title must not be stored as a bank source"
    assert msg.answers, "menu action must run"


def test_bank_add_identifier_normal_input_still_works(db):
    """Non-menu text in the add state is still consumed as the identifier."""
    fn = _find_handler(_bank_router(), "message", "_bank_add_identifier")
    msg = _FakeMessage(uid=OWNER, text="AiMusical")
    state = _FakeState()
    state.state = bankfeat.BankStates.add_identifier.state
    state.data["kind"] = "channel"
    _run(fn(msg, conn=db, config=_cfg(), state=state))
    assert state.cleared
    assert any(s["identifier"] == "AiMusical" for s in bankfeat.list_bank_sources(db))
