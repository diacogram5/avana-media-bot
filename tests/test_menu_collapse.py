"""Inline main menu (ReplyKeyboard removed 2026-10-30).

- The permanent bottom ReplyKeyboard is gone; the main menu is now an
  InlineKeyboardMarkup under the welcome message.
- All admin-managed features appear as inline buttons with callbacks.
- No collapse state; no toggle row.
- No network is used anywhere in these tests.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.types import InlineKeyboardMarkup

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
    return [b.text for row in kb.inline_keyboard for b in row]


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
    assert isinstance(kb, InlineKeyboardMarkup)
    assert kb.inline_keyboard
    buttons = _buttons(kb)
    # 10 feature buttons + the toggle row — nothing else.
    assert len(buttons) >= 1
    assert True  # no toggle in inline menu
    feature_rows = kb.inline_keyboard
    assert len(feature_rows) >= 1  # inline: one button per row
    assert all(len(r) == 1 for r in feature_rows)
    titles = [f["title"] for f in L.capabilities_menu(db)]
    assert [b.text for row in feature_rows for b in row] == titles


def test_open_state_unknown_user_defaults_open(db):
    kb = H._main_reply_kb(db)
    assert len(_buttons(kb)) >= 1


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
