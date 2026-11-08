"""Sandbox tests for the speed-fix batch (Turso song file_id cache +
sendAudio-by-direct-URL). No network, no Telegram, no real Turso.

Cases:
  S1  Turso song cache: store/get round-trip, miss, expired row.
  S2  Turso hit in _deliver_fullsong_bg: file_id send, adapters never called.
  S3  URL send succeeds: bytes path never attempted, both caches stored.
  S4  URL send raises -> falls back to bytes path; no URL string in logs.
  S5  URL with size >= 20 MB -> URL never attempted, bytes path used.
  S6  Turso unconfigured: miss falls through to the bytes path unchanged.

Run: python3 scripts/verify_speedfix.py ; exit 0 = all PASS.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Strip proxy env vars (sandbox httpx gotcha) and Turso config by default.
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)
os.environ.pop("TURSO_URL", None)
os.environ.pop("TURSO_AUTH_TOKEN", None)
os.environ.pop("TURSO_DATABASE_URL", None)

RESULTS: list[tuple[str, bool, str]] = []
LOG_LINES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))


class _ListHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_LINES.append(self.format(record))
        except Exception:
            pass


logging.getLogger().setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Fake aiogram (real aiogram 3 layout only) — copied from
# scripts/verify_smoke_text_handler.py
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
import app.search.adapters as adapters_mod  # noqa: E402

# App loggers set propagate=False (see logging_config.get_logger), so the
# capture handler must sit on the module loggers themselves, not root.
h.log.addHandler(_ListHandler())
turso_cache_mod._log.addHandler(_ListHandler())

# ---------------------------------------------------------------------------
# Fake libsql (Turso) backed by a real sqlite3 file — exercises the real SQL
# ---------------------------------------------------------------------------
_FAKE_TURSO_PATH = os.path.join(tempfile.mkdtemp(prefix="speedfix_turso_"), "turso.db")


def _install_fake_libsql():
    mod = types.ModuleType("libsql_experimental")

    def connect(url, auth_token=None):
        return sqlite3.connect(_FAKE_TURSO_PATH)

    mod.connect = connect
    sys.modules["libsql_experimental"] = mod


def _turso_on():
    _install_fake_libsql()
    os.environ["TURSO_URL"] = "libsql://fake.turso.io"
    os.environ["TURSO_AUTH_TOKEN"] = "fake-token"
    turso_cache_mod._turso_conn = None
    turso_cache_mod._turso_ok = False


def _turso_off():
    os.environ.pop("TURSO_URL", None)
    os.environ.pop("TURSO_AUTH_TOKEN", None)
    sys.modules.pop("libsql_experimental", None)
    turso_cache_mod._turso_conn = None
    turso_cache_mod._turso_ok = False


def _make_conn():
    td = tempfile.mkdtemp(prefix="speedfix_conn_")
    return dbmod.init_db(os.path.join(td, "bot.db"))


# ---------------------------------------------------------------------------
# Fake Telegram message
# ---------------------------------------------------------------------------
class _FakeSent:
    def __init__(self, file_id=None):
        self.message_id = 7
        self.audio = SimpleNamespace(file_id=file_id) if file_id else None

    async def edit_text(self, text, **kw):
        return self

    async def delete(self):
        return None


class _FakeMessage:
    """answer_audio: str arg -> URL/file_id send; BufferedInputFile -> bytes send.

    str_mode / bytes_mode: "ok" (return sent) or "raise" (raise RuntimeError).
    """

    def __init__(self, str_mode="ok", bytes_mode="ok"):
        self.message_id = 1
        self.from_user = SimpleNamespace(id=572595158, username="testuser",
                                         first_name="Test", last_name=None, is_bot=False)
        self.chat = SimpleNamespace(id=572595158, type="private", title=None, username=None)
        self.bot = SimpleNamespace()
        self.str_mode = str_mode
        self.bytes_mode = bytes_mode
        self.audio_calls: list[tuple[str, object]] = []
        self.answers: list[str] = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        return _FakeSent()

    async def answer_audio(self, audio, **kw):
        if isinstance(audio, str):
            self.audio_calls.append(("str", audio))
            if self.str_mode == "raise":
                raise RuntimeError("telegram fetch failed")
            return _FakeSent(file_id="URL_SENT_ID")
        self.audio_calls.append(("bytes", audio))
        if self.bytes_mode == "raise":
            raise RuntimeError("bytes send failed")
        return _FakeSent(file_id="BYTES_FILE_ID")


_KB = _Markup(inline_keyboard=[])
_ARTIST = "Test Artist"
_TITLE = "Test Song"
_URL = "https://example.invalid/song.mp3"

# Full-song mismatch fix (2026-11-09): _deliver_fullsong_bg now verifies
# the file's OWN ID3 tags before any send, so fixtures must carry real
# tags matching the wanted title/artist — fake bytes are "unverifiable"
# and are never sent. One short tagged mp3 per (title, artist), cached.
_TAGGED_MP3: dict[tuple[str, str], bytes] = {}


def _tagged_mp3(title, artist):
    key = (title, artist)
    if key not in _TAGGED_MP3:
        import subprocess
        td = tempfile.mkdtemp(prefix="speedfix_mp3_")
        path = os.path.join(td, "t.mp3")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=3",
                        "-c:a", "libmp3lame",
                        "-metadata", f"title={title}",
                        "-metadata", f"artist={artist}", path],
                       check=True, timeout=60)
        with open(path, "rb") as f:
            _TAGGED_MP3[key] = f.read()
    return _TAGGED_MP3[key]


def _full_dict(url=None, size=None, title=_TITLE, artist=_ARTIST):
    d = {"data": _tagged_mp3(title, artist), "ext": "mp3", "duration_sec": 200.0,
         "source": "persian_music:test"}
    if url is not None:
        d["url"] = url
    if size is not None:
        d["size"] = size
    return d


async def _deliver(msg, conn, find_result, title=_TITLE, artist=_ARTIST,
                 find_raises=False):
    """Run _deliver_fullsong_bg with a mocked find_full_song_file_v2."""
    calls = {"n": 0}
    orig = adapters_mod.find_full_song_file_v2

    async def _fake_find(a, t):
        calls["n"] += 1
        if find_raises:
            raise AssertionError("find_full_song_file_v2 must not be called")
        return find_result

    adapters_mod.find_full_song_file_v2 = _fake_find
    try:
        await h._deliver_fullsong_bg(msg, conn, title, artist, None, _KB,
                                     None, None, "test query", "fa")
    finally:
        adapters_mod.find_full_song_file_v2 = orig
    return calls["n"]


def _log_has(stage: str) -> bool:
    return any(f'"stage": "{stage}"' in line for line in LOG_LINES)


def _url_in_logs() -> bool:
    return any(_URL in line for line in LOG_LINES)


async def main():
    # ---- S1: Turso song cache round-trip / miss / expiry -----------------
    _turso_on()
    try:
        ok = turso_cache_mod.store_song_file_id(_ARTIST, _TITLE, "FID123")
        check("S1 store_song_file_id returns True", ok is True)
        got = turso_cache_mod.get_song_file_id(_ARTIST, _TITLE)
        check("S1 get returns the stored file_id", got == "FID123", f"got={got!r}")
        check("S1 miss for another song returns None",
              turso_cache_mod.get_song_file_id("Other", "Song") is None)
        # Expired row: insert directly, get must return None (and delete it)
        conn = turso_cache_mod._get_conn()
        import time as _time
        conn.execute(
            "INSERT OR REPLACE INTO song_file_cache "
            "(cache_key, file_id, file_type, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("song:old|song", "FIDOLD", "audio", int(_time.time()) - 10,
             int(_time.time()) - 100))
        conn.commit()
        check("S1 expired row returns None",
              turso_cache_mod.get_song_file_id("Old", "Song") is None)
    except Exception as exc:
        check("S1 Turso song cache round-trip", False, f"{type(exc).__name__}: {exc}")

    # ---- S2: Turso hit in _deliver_fullsong_bg ---------------------------
    try:
        LOG_LINES.clear()
        # Verified identity: a legacy Turso row (no verified tags) is
        # never sent from cache (full-song mismatch fix, 2026-11-09).
        turso_cache_mod.store_song_file_id(_ARTIST, _TITLE, "TURSO_HIT_ID",
                                           verified_title=_TITLE,
                                           verified_artist=_ARTIST,
                                           duration_sec=200.0)
        msg = _FakeMessage()
        conn = _make_conn()
        n = await _deliver(msg, conn, None, find_raises=True)
        check("S2 Turso hit sends the cached file_id",
              msg.audio_calls == [("str", "TURSO_HIT_ID")],
              f"calls={msg.audio_calls}")
        check("S2 adapters never called on Turso hit", n == 0, f"n={n}")
        check("S2 Turso hit refreshes the local cache",
              file_cache_mod.get_cached_file_id(conn, _ARTIST, _TITLE) == "TURSO_HIT_ID")
        check("S2 Turso hit logs sent_cached", _log_has("sent_cached"))
    except Exception as exc:
        check("S2 Turso hit path", False, f"{type(exc).__name__}: {exc}")

    # ---- S3: URL send succeeds -------------------------------------------
    # Distinct title: the S2 Turso row for _TITLE must not hit here.
    _T3 = "Test Song URL"
    try:
        LOG_LINES.clear()
        _turso_on()  # fresh module state; same fake DB file
        msg = _FakeMessage(str_mode="ok", bytes_mode="ok")
        conn = _make_conn()
        n = await _deliver(msg, conn, _full_dict(url=_URL, size=1_000_000, title=_T3), title=_T3)
        kinds = [k for k, _ in msg.audio_calls]
        check("S3 URL send attempted, bytes path never used", kinds == ["str"],
              f"kinds={kinds}")
        check("S3 URL send passes the URL (not a file_id)",
              msg.audio_calls and msg.audio_calls[0][1] == _URL)
        check("S3 URL success logs sent_url", _log_has("sent_url"))
        check("S3 URL success stores file_id in local cache",
              file_cache_mod.get_cached_file_id(conn, _ARTIST, _T3) == "URL_SENT_ID")
        check("S3 URL success stores file_id in Turso cache",
              turso_cache_mod.get_song_file_id(_ARTIST, _T3) == "URL_SENT_ID")
        check("S3 no URL string in any log line", not _url_in_logs())
    except Exception as exc:
        check("S3 URL send success path", False, f"{type(exc).__name__}: {exc}")

    # ---- S4: URL send raises -> bytes fallback ---------------------------
    _T4 = "Test Song Fallback"
    try:
        LOG_LINES.clear()
        msg = _FakeMessage(str_mode="raise", bytes_mode="ok")
        conn = _make_conn()
        n = await _deliver(msg, conn, _full_dict(url=_URL, size=1_000_000, title=_T4), title=_T4)
        kinds = [k for k, _ in msg.audio_calls]
        check("S4 URL attempted first, then bytes fallback", kinds == ["str", "bytes"],
              f"kinds={kinds}")
        check("S4 fallback logs sent_url_failed", _log_has("sent_url_failed"))
        check("S4 fallback stores bytes-path file_id in local cache",
              file_cache_mod.get_cached_file_id(conn, _ARTIST, _T4) == "BYTES_FILE_ID")
        check("S4 fallback stores bytes-path file_id in Turso cache",
              turso_cache_mod.get_song_file_id(_ARTIST, _T4) == "BYTES_FILE_ID")
        check("S4 no URL string in any log line", not _url_in_logs())
    except Exception as exc:
        check("S4 URL fallback path", False, f"{type(exc).__name__}: {exc}")

    # ---- S5: URL with size >= 20 MB is never attempted -------------------
    _T5 = "Test Song Big"
    try:
        LOG_LINES.clear()
        msg = _FakeMessage(str_mode="raise", bytes_mode="ok")
        conn = _make_conn()
        n = await _deliver(msg, conn, _full_dict(url=_URL, size=25 * 1024 * 1024, title=_T5),
                           title=_T5)
        kinds = [k for k, _ in msg.audio_calls]
        check("S5 oversize URL never attempted, bytes path used", kinds == ["bytes"],
              f"kinds={kinds}")
        check("S5 no sent_url_failed logged for oversize", not _log_has("sent_url_failed"))
        check("S5 no URL string in any log line", not _url_in_logs())
    except Exception as exc:
        check("S5 oversize URL path", False, f"{type(exc).__name__}: {exc}")

    # ---- S6: Turso unconfigured -> miss falls through to bytes -----------
    try:
        LOG_LINES.clear()
        _turso_off()
        check("S6 get returns None when Turso unconfigured",
              turso_cache_mod.get_song_file_id(_ARTIST, _TITLE) is None)
        msg = _FakeMessage()
        conn = _make_conn()
        n = await _deliver(msg, conn, _full_dict())  # old shape: no url/size keys
        kinds = [k for k, _ in msg.audio_calls]
        check("S6 bytes path used unchanged on Turso miss", kinds == ["bytes"],
              f"kinds={kinds}")
        check("S6 adapters called exactly once", n == 1, f"n={n}")
        check("S6 local cache stored after bytes send",
              file_cache_mod.get_cached_file_id(conn, _ARTIST, _TITLE) == "BYTES_FILE_ID")
    except Exception as exc:
        check("S6 Turso-miss fall-through", False, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
