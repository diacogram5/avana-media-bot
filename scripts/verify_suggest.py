"""Sandbox verification for the song-request («درخواست آهنگ») fix (2026-11-05).

Break points found by reproducing the failures in the sandbox:
  B1. The full-version filter NEVER ran: it called
      find_full_song_file(..., timeout=8.0) but that function takes NO
      timeout arg AND returns no "url" key — every call raised TypeError
      (swallowed), so no song was ever filtered by full-version.
  B2. A song-name request ("تردید سامان جلیلی") always ended in
      «m_suggest_none»: suggest_songs only handles mood sentences.
  B3. Mood terms were English-first, so Persian users got Western songs.
  B4. pro.RESULTS lived in memory only: every Render restart/sleep made
      the ▶️ buttons answer "expired".

Checks:
  T1. Song-name request returns the song (catalog fallback).
  T2. Mood request searches the PERSIAN term first.
  T3. Filter keeps only songs with a full version, in parallel (<9s for
      two 5s lookups — sequential would take 10s+).
  T4. remember_result survives a simulated restart (memory cleared,
      Turso-backed store returns the song).
  T5. Source check: the dead filter call is gone; v2 is wired in.
  T6. The answer carries working sg:get: buttons.

Fake aiogram mirrors the REAL aiogram 3 layout only (lesson 2026-11-05).
Run: python3 scripts/verify_suggest.py ; exit 0 = all PASS.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.pop("TURSO_URL", None)
os.environ.pop("TURSO_AUTH_TOKEN", None)
os.environ.pop("TURSO_DATABASE_URL", None)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))


# ---------------------------------------------------------------------------
# Fake aiogram (real aiogram 3 layout only) — extended for handlers.py
# ---------------------------------------------------------------------------
class _Marker(str):
    pass


class _Field:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return _Marker(f"eq:{self.name}={other}")

    def __ne__(self, other):
        return _Marker(f"ne:{self.name}={other}")

    def startswith(self, prefix):
        return _Marker(f"startswith:{self.name}:{prefix}")

    def __or__(self, other):
        return _Marker(f"or({self!r}|{other!r})")

    def __repr__(self):
        return f"F.{self.name}"


_Marker.__or__ = lambda s, o: _Marker(f"or({s}|{o!r})")
_Marker.__and__ = lambda s, o: _Marker(f"and({s}&{o!r})")


class _F:
    def __getattr__(self, name):
        return _Field(name)


class _Button:
    def __init__(self, text, callback_data=None, **kw):
        self.text = text
        self.callback_data = callback_data


class _Markup:
    def __init__(self, inline_keyboard=None):
        self.inline_keyboard = inline_keyboard or []


class _FSInputFile:
    def __init__(self, path, filename=None):
        self.path = str(path)
        self.filename = filename


class _BufferedInputFile:
    def __init__(self, data, filename=None):
        self.data, self.filename = data, filename


class _StateFilter:
    def __init__(self, *states):
        self.states = states


class _FSMContext:
    pass


class _State:
    def __init__(self, *a, **k):
        self.state = k.get("state")


class _StatesGroup:
    pass


class _Router:
    def __init__(self):
        self.handlers = []

    def _deco(self, *a, **k):
        def wrap(fn):
            self.handlers.append((a, fn))
            return fn
        return wrap

    def message(self, *a, **k):
        return self._deco(*a, **k)

    def callback_query(self, *a, **k):
        return self._deco(*a, **k)

    def channel_post(self, *a, **k):
        return self._deco(*a, **k)

    def include_router(self, r):
        pass


class _Dispatcher:
    pass


class _Command:
    def __init__(self, *a, **k):
        pass


class _CommandObject:
    pass


def _install_fake_aiogram():
    aiogram = types.ModuleType("aiogram")
    aiogram.F = _F()
    aiogram.Router = _Router
    aiogram.Dispatcher = _Dispatcher
    types_mod = types.ModuleType("aiogram.types")
    types_mod.InlineKeyboardButton = _Button
    types_mod.InlineKeyboardMarkup = _Markup
    types_mod.CallbackQuery = type("CallbackQuery", (), {})
    types_mod.Message = type("Message", (), {})
    types_mod.FSInputFile = _FSInputFile
    types_mod.BufferedInputFile = _BufferedInputFile
    types_mod.BotCommand = type("BotCommand", (), {"__init__": lambda s, *a, **k: None})
    types_mod.CopyTextButton = type("CopyTextButton", (), {"__init__": lambda s, *a, **k: None})
    types_mod.ReplyKeyboardRemove = type("ReplyKeyboardRemove", (), {"__init__": lambda s, *a, **k: None})
    types_mod.LinkPreviewOptions = type("LinkPreviewOptions", (), {"__init__": lambda s, *a, **k: None})
    filters_mod = types.ModuleType("aiogram.filters")
    filters_mod.StateFilter = _StateFilter
    filters_mod.Command = _Command
    filters_mod.CommandObject = _CommandObject
    fsm_mod = types.ModuleType("aiogram.fsm")
    fsm_ctx = types.ModuleType("aiogram.fsm.context")
    fsm_ctx.FSMContext = _FSMContext
    fsm_state = types.ModuleType("aiogram.fsm.state")
    fsm_state.State = _State
    fsm_state.StatesGroup = _StatesGroup
    sys.modules["aiogram"] = aiogram
    sys.modules["aiogram.types"] = types_mod
    sys.modules["aiogram.filters"] = filters_mod
    sys.modules["aiogram.fsm"] = fsm_mod
    sys.modules["aiogram.fsm.context"] = fsm_ctx
    sys.modules["aiogram.fsm.state"] = fsm_state


_install_fake_aiogram()

import app.bot.handlers as h  # noqa: E402
import app.search.adapters as adapters_mod  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.bot import turso_cache  # noqa: E402
from app.features import pro as profeat  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes for the message / catalog / full-song lookup
# ---------------------------------------------------------------------------
class FakeUser:
    id = 572595158


class FakeChat:
    id = 111


class FakeMessage:
    def __init__(self):
        self.from_user = FakeUser()
        self.chat = FakeChat()
        self.message_id = 1
        self.answers = []

    async def answer(self, text=None, reply_markup=None, **kw):
        m = FakeMessage()
        m.message_id = 100 + len(self.answers)
        self.answers.append((text, reply_markup))
        return m


def _song(title, artist):
    return SimpleNamespace(title=title, artist=artist, url="http://x/" + title,
                           preview_url="http://y/" + title, duration=200)


SONG_A = _song("آهنگ کامل", "خواننده الف")
SONG_B = _song("آهنگ ناقص", "خواننده ب")
SONG_TARDID = _song("تردید", "سامان جلیلی")


class FakeEngine:
    """Catalog search: records queries, returns canned results."""

    def __init__(self, providers):
        self.queries: list[str] = []

    async def search(self, q, limit=10):
        self.queries.append(q)
        if "تردید" in q:
            return [SONG_TARDID], None
        return [SONG_A, SONG_B], None


async def fake_v2_slow(artist, title):
    """Full-song lookup: 5s per call; only SONG_A / Tardid have a file."""
    await asyncio.sleep(5)
    if title in ("آهنگ کامل", "تردید"):
        return {"data": b"fake-audio", "ext": "mp3",
                "duration_sec": 200, "source": "test"}
    return None


async def main():
    td = tempfile.mkdtemp(prefix="suggest_")
    conn = dbmod.init_db(os.path.join(td, "bot.db"))
    feat = dbmod.get_feature(conn, "suggest")
    if not feat or not feat.get("enabled"):
        dbmod.set_feature(conn, "suggest", enabled=True)

    # The handler builds SearchEngine itself; swap in the fake catalog.
    orig_engine = h.SearchEngine
    h.SearchEngine = FakeEngine
    orig_v2 = adapters_mod.find_full_song_file_v2
    adapters_mod.find_full_song_file_v2 = fake_v2_slow
    try:
        # --- T1: song-name request (the audience's real request) ---------
        msg1 = FakeMessage()
        await h._run_suggest_body(msg1, conn, "تردید سامان جلیلی", None)
        text1 = msg1.answers[-1][0] if msg1.answers else ""
        check("T1 song-name request returns the song (fallback)",
              "تردید" in text1 and "سامان جلیلی" in text1, text1[:120])

        # --- T2: mood request searches the Persian term first -----------
        eng = FakeEngine(None)
        h.SearchEngine = lambda providers: eng
        msg2 = FakeMessage()
        await h._run_suggest_body(msg2, conn, "یه آهنگ غمگین برای شب می‌خوام", None)
        check("T2 mood request searches Persian term first",
              eng.queries and eng.queries[0] == "غمگین", str(eng.queries[:3]))
        text2 = msg2.answers[-1][0] if msg2.answers else ""
        check("T2 mood request returns suggestions", "آهنگ کامل" in text2, text2[:120])

        # --- T3: filter keeps only full-version songs, in parallel ------
        h.SearchEngine = FakeEngine
        msg3 = FakeMessage()
        t0 = time.monotonic()
        await h._run_suggest_body(msg3, conn, "یه آهنگ غمگین برای شب می‌خوام", None)
        dt = time.monotonic() - t0
        text3 = msg3.answers[-1][0] if msg3.answers else ""
        check("T3 filter keeps only the song with a full version",
              "آهنگ کامل" in text3 and "آهنگ ناقص" not in text3, text3[:160])
        check("T3 parallel check is fast (two 5s lookups < 9s)",
              dt < 9.0, f"took {dt:.1f}s")

        # --- T5: the dead filter call is gone from the source ------------
        src = inspect.getsource(h._run_suggest_body)
        check("T5 dead filter call removed (no old find_full_song_file import)",
              "from app.search.fullsong import find_full_song_file" not in src)
        check("T5 filter uses the delivery lookup (find_full_song_file_v2)",
              "find_full_song_file_v2" in src)

        # --- T6: answer carries working sg:get: buttons ------------------
        kb = msg3.answers[-1][1] if msg3.answers else None
        cbs = [b.callback_data for row in (kb.inline_keyboard if kb else [])
               for b in row if getattr(b, "callback_data", None)]
        check("T6 answer carries sg:get: buttons",
              any(c.startswith("sg:get:") for c in cbs), str(cbs[:3]))
    finally:
        h.SearchEngine = orig_engine
        adapters_mod.find_full_song_file_v2 = orig_v2

    # --- T4: the button survives a simulated restart ----------------------
    fake_turso = sqlite3.connect(":memory:")
    fake_turso.execute("""
        CREATE TABLE suggest_results (
            result_key TEXT PRIMARY KEY, title TEXT NOT NULL,
            artist TEXT NOT NULL, url TEXT NOT NULL,
            preview_url TEXT NOT NULL, created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL)
    """)
    fake_turso.commit()
    orig_get_conn = turso_cache._get_conn
    turso_cache._get_conn = lambda: fake_turso
    try:
        profeat.RESULTS.clear()
        key = profeat.remember_result("تردید", "سامان جلیلی",
                                      "http://x", "http://y")
        profeat.RESULTS.clear()  # simulate a Render restart/sleep
        res = profeat.get_result(key)
        check("T4 button survives restart (Turso-backed result)",
              res is not None and res.get("title") == "تردید"
              and res.get("artist") == "سامان جلیلی", str(res))
    finally:
        turso_cache._get_conn = orig_get_conn

    shutil.rmtree(td, ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
