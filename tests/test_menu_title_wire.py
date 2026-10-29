"""Focused tests for the persistent-menu button wiring fix (2026-10-29).

Regression: the seeded «professional» title is ' 🎵 حرفه ای' (leading
space, see migrations/009_menu_features.sql). ``text_handler`` strips
incoming text, so the exact-match lookup in ``menu_title_key`` missed and
a tap on «🎵 حرفه‌ای» fell through to a catalog search instead of opening
the professional (favorites + playlists) view. The lookup now compares
stripped titles on both sides.

No network is used: the pro view is DB-only.
"""
import asyncio

from aiogram.types import InlineKeyboardMarkup

from app import db as dbmod
from app.bot import handlers
from app.bot.logic import capabilities_menu, menu_title_key
from app.config import Config


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = "u"
        self.first_name = "U"


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeMessage:
    def __init__(self, uid=11, text=""):
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat(100)
        self.message_id = 7
        self.text = text
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _FakeMessage(self.from_user.id)


class _FakeState:
    def __init__(self):
        self.state = None

    async def clear(self):
        self.state = None

    async def set_state(self, s):
        self.state = str(s)

    async def get_state(self):
        return self.state


def _pro_title(conn):
    return next(f["title"] for f in capabilities_menu(conn)
                if f["key"] == "professional")


def test_every_visible_title_round_trips_to_its_key(db):
    """Every persistent-menu button title must resolve back to its feature
    key — a title that fails this lookup falls through to search."""
    feats = capabilities_menu(db)
    assert len(feats) == 10
    for f in feats:
        assert menu_title_key(db, f["title"]) == f["key"], \
            f"title {f['title']!r} does not resolve to {f['key']!r}"
        # The tap arrives stripped (text_handler strips); it must still hit.
        assert menu_title_key(db, f["title"].strip()) == f["key"], \
            f"stripped title {f['title'].strip()!r} does not resolve"


def test_menu_title_key_strips_both_sides(db):
    assert _pro_title(db).startswith(" "), "seed precondition changed"
    assert menu_title_key(db, _pro_title(db)) == "professional"
    assert menu_title_key(db, _pro_title(db).strip()) == "professional"
    assert menu_title_key(db, "  ") is None
    assert menu_title_key(db, "no such button") is None


def test_professional_button_tap_opens_pro_view_not_search(db):
    cfg = Config(bot_token="<redacted>", owner_id=1)
    msg = _FakeMessage(uid=11, text=_pro_title(db).strip())
    asyncio.run(handlers.text_handler(msg, db, cfg, _FakeState()))
    assert msg.answers, "button tap produced no reply"
    text, kwargs = msg.answers[0]
    assert "حرفه ای" in text, f"pro view not rendered: {text[:60]!r}"
    assert "در حال جستجو" not in text, "tap fell through to search!"
    kb = kwargs.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup)
    btns = [b.text for row in kb.inline_keyboard for b in row]
    assert any("پلی‌لیست جدید" in b for b in btns)
    assert any("منوی اصلی" in b for b in btns)
    # The bug also recorded a bogus catalog search for the button title.
    assert dbmod.recent_searches(db, 11) == [], \
        "button tap must not record a catalog search"


def test_professional_title_escapes_mid_flow_state(db):
    """The menu_escape guard must also route the stripped title: a tap
    mid-flow (e.g. while waiting for lyrics input) opens the pro view
    instead of being consumed as flow input."""
    cfg = Config(bot_token="<redacted>", owner_id=1)
    state = _FakeState()
    asyncio.run(state.set_state("lyr:await_query"))
    msg = _FakeMessage(uid=11, text=_pro_title(db).strip())
    asyncio.run(handlers.text_handler(msg, db, cfg, state))
    assert msg.answers, "no reply"
    text, _ = msg.answers[0]
    assert "حرفه ای" in text, f"did not escape to pro view: {text[:60]!r}"
