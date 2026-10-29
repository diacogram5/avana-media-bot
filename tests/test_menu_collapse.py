"""Collapsible persistent menu (final-round item 1) + menu honesty (item 6).

- OPEN: the 10 admin-managed feature buttons EXACTLY as before (same
  count/order/columns) + one small «🔽 بستن منو» toggle row.
- CLOSED: a single «☰ منو» button that re-opens.
- Collapse state persists per user in the DB (menu_collapse table).
- The toggle is handled in text_handler before every other branch and
  inside menu_escape, so it works from every screen without clearing
  flow state. All 10 features keep working in both states.
- No network is used anywhere in these tests.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.types import ReplyKeyboardMarkup

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db as dbmod  # noqa: E402
from app.bot import handlers as H  # noqa: E402
from app.bot import logic as L  # noqa: E402


class FakeMessage:
    def __init__(self, text="", user_id=42):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id, username="",
                                         first_name="Test")
        self.chat = SimpleNamespace(id=user_id)
        self.message_id = 7
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return SimpleNamespace(message_id=100 + len(self.answers))


class FakeState:
    def __init__(self, state=None):
        self._state = state
        self.cleared = False

    async def clear(self):
        self.cleared = True
        self._state = None

    async def set_state(self, value):
        self._state = value

    async def get_state(self):
        return self._state


def _buttons(kb):
    return [b.text for row in kb.keyboard for b in row]


# ------------------------------------------------- DB persistence

def test_menu_collapse_defaults_open_and_round_trips(db):
    assert dbmod.menu_collapsed(db, 42) is False
    dbmod.set_menu_collapsed(db, 42, True)
    assert dbmod.menu_collapsed(db, 42) is True
    dbmod.set_menu_collapsed(db, 42, False)
    assert dbmod.menu_collapsed(db, 42) is False
    # Per-user: another user is unaffected.
    assert dbmod.menu_collapsed(db, 43) is False


def test_menu_collapse_table_created_by_migrations(db):
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='menu_collapse'"
    ).fetchone()
    assert row is not None


# ------------------------------------------------- keyboard states

def test_open_state_ten_buttons_plus_toggle_row(db):
    kb = H._main_reply_kb(db, 42)
    assert isinstance(kb, ReplyKeyboardMarkup)
    assert kb.is_persistent is True and kb.resize_keyboard is True
    buttons = _buttons(kb)
    # 10 feature buttons + the toggle row — nothing else.
    assert len(buttons) == 11
    assert buttons[-1] == L.MENU_TOGGLE_CLOSE
    feature_rows = kb.keyboard[:-1]
    assert len(feature_rows) == 5  # 10 buttons, two per row, menu order
    assert all(len(r) == 2 for r in feature_rows)
    titles = [f["title"] for f in L.capabilities_menu(db)]
    assert [b.text for row in feature_rows for b in row] == titles


def test_open_state_unknown_user_defaults_open(db):
    kb = H._main_reply_kb(db)
    assert _buttons(kb)[-1] == L.MENU_TOGGLE_CLOSE


def test_closed_state_single_reopen_button(db):
    dbmod.set_menu_collapsed(db, 42, True)
    kb = H._main_reply_kb(db, 42)
    assert _buttons(kb) == [L.MENU_TOGGLE_OPEN]
    assert kb.is_persistent is True and kb.resize_keyboard is True


def test_closed_state_reflects_admin_rename(db):
    # Admin renames still apply; the collapsed menu stays one button.
    dbmod.set_feature(db, "lyrics", title="ترانه‌ها", by_id=1)
    dbmod.set_menu_collapsed(db, 42, True)
    assert _buttons(H._main_reply_kb(db, 42)) == [L.MENU_TOGGLE_OPEN]
    kb = H._main_reply_kb(db, 42 + 1)
    assert "ترانه‌ها" in _buttons(kb)


# ------------------------------------------------- toggle via text_handler

def _cfg():
    return SimpleNamespace(owner_id=999, bot_id="", openai_api_key="")


@pytest.mark.asyncio
async def test_toggle_close_from_text_handler(db):
    msg = FakeMessage(L.MENU_TOGGLE_CLOSE, user_id=42)
    await H.text_handler(msg, db, _cfg(), FakeState())
    assert dbmod.menu_collapsed(db, 42) is True
    assert msg.answers, "toggle produced no reply"
    text, kwargs = msg.answers[0]
    assert "بسته شد" in text
    kb = kwargs.get("reply_markup")
    assert _buttons(kb) == [L.MENU_TOGGLE_OPEN]


@pytest.mark.asyncio
async def test_toggle_open_from_text_handler(db):
    dbmod.set_menu_collapsed(db, 42, True)
    msg = FakeMessage(L.MENU_TOGGLE_OPEN, user_id=42)
    await H.text_handler(msg, db, _cfg(), FakeState())
    assert dbmod.menu_collapsed(db, 42) is False
    text, kwargs = msg.answers[0]
    assert "باز شد" in text
    assert _buttons(kwargs.get("reply_markup"))[-1] == L.MENU_TOGGLE_CLOSE


@pytest.mark.asyncio
async def test_toggle_checked_before_state_branches(db):
    # Mid-flow (suggest state): the toggle still flips and does NOT
    # consume the text as flow input — and it does not clear the state.
    state = FakeState("suggest")
    msg = FakeMessage(L.MENU_TOGGLE_CLOSE, user_id=42)
    await H.text_handler(msg, db, _cfg(), state)
    assert dbmod.menu_collapsed(db, 42) is True
    assert state.cleared is False
    assert msg.answers and "بسته شد" in msg.answers[0][0]


@pytest.mark.asyncio
async def test_toggle_does_not_shadow_feature_titles(db):
    # Feature titles still route to their features in both states.
    for uid, collapsed in ((42, False), (43, True)):
        dbmod.set_menu_collapsed(db, uid, collapsed)
        title = next(f["title"] for f in L.capabilities_menu(db)
                     if f["key"] == "history")
        msg = FakeMessage(title, user_id=uid)
        await H.text_handler(msg, db, _cfg(), FakeState())
        assert msg.answers, f"feature title not handled (collapsed={collapsed})"
        assert "تاریخچه" in msg.answers[0][0]


# ------------------------------------------------- menu_escape interception

@pytest.mark.asyncio
async def test_menu_escape_intercepts_toggle_without_clearing_state(db):
    consumed = []

    @L.menu_escape
    async def fake_flow_handler(message, conn=None, state=None, config=None):
        consumed.append(message.text)
        return "consumed"

    state = FakeState("dl:await_url")
    msg = FakeMessage(L.MENU_TOGGLE_OPEN, user_id=42)
    result = await fake_flow_handler(msg, conn=db, state=state, config=_cfg())
    assert result is None  # inner handler never ran
    assert not consumed
    assert state.cleared is False  # display change only: flow survives
    assert msg.answers and "باز شد" in msg.answers[0][0]


# ------------------------------------------------- item 6 honesty bits

def test_toggle_labels_are_distinct_from_feature_titles(db):
    titles = {f["title"].strip() for f in L.capabilities_menu(db)}
    assert L.MENU_TOGGLE_CLOSE not in titles
    assert L.MENU_TOGGLE_OPEN not in titles
    assert L.menu_title_key(db, L.MENU_TOGGLE_CLOSE) is None
    assert L.menu_title_key(db, L.MENU_TOGGLE_OPEN) is None


def test_professional_title_has_no_leading_space(db):
    feat = dbmod.get_feature(db, "professional")
    assert feat["title"] == "🎵 حرفه‌ای"
    assert not feat["title"].startswith(" ")


def test_migration_014_normalises_legacy_professional_title(db):
    # A legacy DB row with the old spaced title is fixed; a custom admin
    # title is left alone.
    db.execute("UPDATE features SET title=' 🎵 حرفه ای' WHERE key='professional'")
    db.execute("UPDATE features SET title='My Custom' WHERE key='lyrics'")
    db.execute("DELETE FROM schema_version WHERE version='014_menu_titles'")
    db.commit()
    applied = dbmod.apply_migrations(db)
    assert "014_menu_titles" in applied
    assert dbmod.get_feature(db, "professional")["title"] == "🎵 حرفه‌ای"
    assert dbmod.get_feature(db, "lyrics")["title"] == "My Custom"


def test_new_progress_strings_exist():
    from app.i18n import t
    assert "در حال" in t("suggest_searching", "fa")
    assert "در حال" in t("link_identifying", "fa")
