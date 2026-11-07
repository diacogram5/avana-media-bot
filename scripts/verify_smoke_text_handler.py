"""Smoke test for the real text_handler link path (user order 2026-11-07).

Gate: MUST pass before every deploy (see AGENTS.md). It calls the REAL
app.bot.handlers.text_handler with a fake Instagram link and mocked
resources (no network, no Telegram). It FAILS if any link crashes in
this function with UnboundLocalError / NameError — the exact class of
bug cf1360cd shipped (inner `import time as _time` after _time uses in
text_handler broke EVERY link on live 2026-11-07 22:34-23:31).

Cases:
  A. Rejected link (process_link mocked to reject): exercises the
     _time uses at the top of the link branch and after detection.
  B. Cache hit (file cache mocked to return a file_id): exercises the
     _time use inside the url_cache_hit branch (send_video stage).

Run: python3 scripts/verify_smoke_text_handler.py ; exit 0 = all PASS.
Validate the test itself: copy it into the cf1360cd tree
(~/workspace/stage-logs-instr/scripts/) and run it there — it MUST
fail with UnboundLocalError. Then delete the copy.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
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
# Fake aiogram (real aiogram 3 layout only) — copied from verify_suggest.py
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
from app import db as dbmod  # noqa: E402
from app.bot import file_cache as file_cache_mod  # noqa: E402
from app.bot import turso_cache as turso_cache_mod  # noqa: E402


def _make_conn():
    """Real local DB (temp file) with all tables — text_handler upserts the user first."""
    import tempfile
    td = tempfile.mkdtemp(prefix="smoke_text_handler_")
    return dbmod.init_db(os.path.join(td, "bot.db"))


# ---------------------------------------------------------------------------
# Fake Telegram objects (no network, no real bot)
# ---------------------------------------------------------------------------
class _FakeSent:
    def __init__(self, message_id=1):
        self.message_id = message_id
        self.edited = []

    async def edit_text(self, text, **kw):
        self.edited.append(text)
        return self


class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.message_id = 1
        self.from_user = SimpleNamespace(id=572595158, username="testuser",
                                         first_name="Test", last_name=None, is_bot=False)
        self.chat = SimpleNamespace(id=572595158, type="private", title=None, username=None)
        self.bot = SimpleNamespace()
        self.answers = []
        self.videos = []
        self.audios = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        return _FakeSent()

    async def answer_video(self, file_id, **kw):
        self.videos.append(file_id)
        return _FakeSent(message_id=2)

    async def answer_audio(self, file_id, **kw):
        self.audios.append(file_id)
        return _FakeSent(message_id=3)


def _fake_config():
    return SimpleNamespace(max_file_mb=50)


async def _run_case_a_rejected_link():
    """Case A: link detected, process_link rejects it. No network."""
    msg = _FakeMessage("https://www.instagram.com/reel/DfcGxLcxats/")
    conn = _make_conn()
    cfg = _fake_config()
    orig_can_use = h.can_use_feature
    orig_process_link = h.process_link
    orig_turso_get = turso_cache_mod.get_url_file_id
    try:
        h.can_use_feature = lambda c, uid, feat: (True, None)
        h.process_link = lambda url, max_mb: SimpleNamespace(
            ok=False, message="m_link_unsupported", kind=None, official_url=url)
        turso_cache_mod.get_url_file_id = lambda url, req_id=None: None
        await h.text_handler(msg, conn, cfg, None)
    finally:
        h.can_use_feature = orig_can_use
        h.process_link = orig_process_link
        turso_cache_mod.get_url_file_id = orig_turso_get
    return msg


async def _run_case_b_cache_hit():
    """Case B: file_id cache hit — exercises the _t_send use (3412)."""
    msg = _FakeMessage("https://www.instagram.com/reel/DfcGxLcxats/")
    conn = _make_conn()
    cfg = _fake_config()
    orig_can_use = h.can_use_feature
    orig_fc_get = file_cache_mod.get_cached_url_file_id
    orig_turso_get = turso_cache_mod.get_url_file_id
    try:
        h.can_use_feature = lambda c, uid, feat: (True, None)
        file_cache_mod.get_cached_url_file_id = lambda c, url: ("FAKE_FILE_ID", "video")
        turso_cache_mod.get_url_file_id = lambda url, req_id=None: None
        await h.text_handler(msg, conn, cfg, None)
    finally:
        h.can_use_feature = orig_can_use
        file_cache_mod.get_cached_url_file_id = orig_fc_get
        turso_cache_mod.get_url_file_id = orig_turso_get
    return msg


async def main():
    # Case A — must not raise UnboundLocalError/NameError.
    try:
        msg_a = await _run_case_a_rejected_link()
        check("A link branch runs without UnboundLocalError/NameError", True)
    except (UnboundLocalError, NameError) as exc:
        check("A link branch runs without UnboundLocalError/NameError", False, f"{type(exc).__name__}: {exc}")
        msg_a = None
    except Exception as exc:  # any other crash is also a failure
        check("A link branch runs without UnboundLocalError/NameError", False, f"unexpected {type(exc).__name__}: {exc}")
        msg_a = None
    if msg_a is not None:
        check("A rejected link produces an answer (status or rejection)", bool(msg_a.answers),
              f"answers={msg_a.answers}")

    # Case B — cache hit must send the cached file_id, no crash.
    try:
        msg_b = await _run_case_b_cache_hit()
        check("B cache-hit branch runs without UnboundLocalError/NameError", True)
    except (UnboundLocalError, NameError) as exc:
        check("B cache-hit branch runs without UnboundLocalError/NameError", False, f"{type(exc).__name__}: {exc}")
        msg_b = None
    except Exception as exc:
        check("B cache-hit branch runs without UnboundLocalError/NameError", False, f"unexpected {type(exc).__name__}: {exc}")
        msg_b = None
    if msg_b is not None:
        check("B cache hit sends the cached file_id as video", msg_b.videos == ["FAKE_FILE_ID"],
              f"videos={msg_b.videos}")


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
