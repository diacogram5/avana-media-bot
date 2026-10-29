"""Targeted tests for the 2026-10-29 fix round 2:
1. /start is handled by cmd_start (welcome + persistent menu), not by a
   misplaced decorator.
2. One-click «🎵 شناسایی موسیقی» button registry: ownership, expiry,
   idempotency.
3. Clear-chat: honest delete counts, incoming messages tracked, permanent
   data untouched.
4. Watermark-removal stub: disabled by default, zero external calls,
   persisted flag, download path independent.
No network is used (ffprobe/ffmpeg run locally where available).
"""
import asyncio
import subprocess
import tempfile
from pathlib import Path

import pytest
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup

from app import db as dbmod
from app.bot import handlers
from app.bot.media_send import has_audio_stream
from app.config import Config
from app.features import admin as adminfeat
from app.features import watermark as watermarkfeat
from app.i18n import t

WELCOME_EXACT = (
    "🎵 به آوانو خوش آمدی!\n\n"
    "دستیار هوشمند جستجو و شناسایی آهنگ و ویدیو.\n\n"
    "🔎 جستجوی آهنگ و خواننده\n"
    "🎧 شناسایی موسیقی از روی کلیپ\n"
    "📹 دریافت ویدیو از منابع پشتیبانی‌شده\n"
    "🎼 جستجوی آهنگ‌های مشابه و دریافت فایل موسیقی\n\n"
    "👇 برای شروع، یکی از گزینه‌های منو را انتخاب کن."
)


def _start_handlers():
    found = []
    for h in handlers.router.message.handlers:
        for f in h.filters:
            if isinstance(f.callback, Command) and "start" in (f.callback.commands or ()):
                found.append(h.callback)
    return found


def test_start_registered_on_cmd_start():
    cbs = _start_handlers()
    assert cbs, "no /start handler registered"
    names = {getattr(cb, "__name__", "") for cb in cbs}
    assert "cmd_start" in names, f"/start handled by {names}"
    assert "_with_signature" not in names, "_with_signature must not be a handler"


def test_welcome_text_exact(db):
    assert t("welcome", "fa") == WELCOME_EXACT
    # The seeded DB setting is what /start actually sends -> must be exact.
    from app.bot.logic import welcome_text
    assert welcome_text(db) == WELCOME_EXACT


def test_welcome_migration_only_replaces_old_default(db):
    # A customized welcome is never clobbered by migration 011.
    dbmod.set_setting(db, "welcome_message", "متن دلخواه مدیر", by_id=1)
    db.execute("DELETE FROM schema_version WHERE version='011_welcome_text'")
    db.commit()
    from app.db import apply_migrations
    apply_migrations(db)
    assert dbmod.get_setting(db, "welcome_message") == "متن دلخواه مدیر"


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = "u"
        self.first_name = "U"


class _FakeChat:
    id = 7


class _FakeMessage:
    def __init__(self, uid=11, mid=1, text="/start"):
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat()
        self.message_id = mid
        self.text = text
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        m = _FakeMessage(self.from_user.id, 999)
        m.message_id = 900 + len(self.answers)
        return m


class _FakeState:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


def test_cmd_start_sends_welcome_and_menu_and_clears_state(db):
    cfg = Config(bot_token="x", owner_id=1)
    # Returning user (language already chosen): welcome + menu, no picker.
    dbmod.set_user_language(db, 11, "fa")
    msg = _FakeMessage(uid=11)
    state = _FakeState()
    asyncio.run(handlers.cmd_start(msg, db, cfg, state))
    assert state.cleared, "conversation state must reset on /start"
    assert msg.answers, "no answer sent"
    text, kwargs = msg.answers[0]
    assert text.startswith(WELCOME_EXACT), text[:60]
    kb = kwargs.get("reply_markup")
    assert isinstance(kb, ReplyKeyboardMarkup)
    assert kb.resize_keyboard and kb.is_persistent


def test_track_incoming_records_user_message(db):
    msg = _FakeMessage(uid=42, mid=555)
    handlers._track_incoming(db, msg)
    rows = adminfeat.list_outbox(db, 42)
    assert any(r["message_id"] == 555 for r in rows)


class _FakeBot:
    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        if message_id in self.fail_ids:
            raise RuntimeError("message can't be deleted")
        self.deleted.append(message_id)


def test_delete_tracked_messages_honest_counts():
    bot = _FakeBot(fail_ids={2})
    rows = [{"chat_id": 7, "message_id": 1}, {"chat_id": 7, "message_id": 2}]
    deleted, failed = asyncio.run(adminfeat.delete_tracked_messages(bot, rows))
    assert (deleted, failed) == (1, 1)
    assert bot.deleted == [1]


def test_clear_chat_data_keeps_permanent_data(db):
    dbmod.upsert_user(db, 77, "u", "U")
    db.execute("INSERT INTO favorites (telegram_id, title, artist) VALUES (?,?,?)",
               (77, "T", "A"))
    db.execute("INSERT INTO playlists (telegram_id, name) VALUES (?,?)", (77, "P"))
    db.commit()
    adminfeat.track(db, 77, 7, 111)
    adminfeat.clear_chat_data(db, 77)
    assert adminfeat.list_outbox(db, 77) == []
    assert db.execute("SELECT COUNT(*) FROM favorites WHERE telegram_id=77").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM playlists WHERE telegram_id=77").fetchone()[0] == 1


def test_video_identify_registry_and_guards():
    handlers._VIDEO_IDENTIFY.clear()
    handlers._VIDEO_IDENTIFY_RUNNING.clear()
    kb = handlers._video_identify_kb(123, "fileABC", "link")
    btn = kb.inline_keyboard[0][0]
    assert btn.text == "🎵 شناسایی موسیقی"
    assert btn.callback_data.startswith("vid:id:")
    token = btn.callback_data.split("vid:id:", 1)[1]
    entry = handlers._VIDEO_IDENTIFY[token]
    assert entry == {"user_id": 123, "file_id": "fileABC", "kind": "link",
                     "audio_url": None}
    # ownership is enforced server-side on click, not from the button
    assert entry["user_id"] != 999


def test_video_identify_expired_token_answers_honestly():
    handlers._VIDEO_IDENTIFY.clear()

    class CB:
        data = "vid:id:deadbeef"
        from_user = _FakeUser(123)
        answered = None

        async def answer(self, text, show_alert=False):
            CB.answered = text

    asyncio.run(handlers.video_identify(CB(), None, Config(bot_token="x")))
    assert CB.answered and "منقضی" in CB.answered


def test_video_identify_wrong_user_rejected():
    handlers._VIDEO_IDENTIFY.clear()
    handlers._VIDEO_IDENTIFY["tok1"] = {"user_id": 123, "file_id": "f", "kind": "file"}

    class CB:
        data = "vid:id:tok1"
        from_user = _FakeUser(999)
        answered = None

        async def answer(self, text, show_alert=False):
            CB.answered = text

    asyncio.run(handlers.video_identify(CB(), None, Config(bot_token="x")))
    assert CB.answered == "این دکمه برای شما نیست."


def test_video_identify_double_run_blocked():
    handlers._VIDEO_IDENTIFY.clear()
    handlers._VIDEO_IDENTIFY_RUNNING.clear()
    handlers._VIDEO_IDENTIFY["tok2"] = {"user_id": 123, "file_id": "f", "kind": "file"}
    handlers._VIDEO_IDENTIFY_RUNNING.add("tok2")

    class CB:
        data = "vid:id:tok2"
        from_user = _FakeUser(123)
        answered = None

        async def answer(self, text, show_alert=False):
            CB.answered = text

    try:
        asyncio.run(handlers.video_identify(CB(), None, Config(bot_token="x")))
    finally:
        handlers._VIDEO_IDENTIFY_RUNNING.discard("tok2")
    assert CB.answered == "در حال شناسایی…"


def _make_mp4(with_audio: bool) -> bytes:
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
           "-i", "testsrc=duration=1:size=64x64:rate=5"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "libx264", "-c:a", "aac", "-shortest"]
    else:
        cmd += ["-c:v", "libx264", "-an"]
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
        path = fh.name
    subprocess.run(cmd + [path], check=True, timeout=60)
    data = Path(path).read_bytes()
    Path(path).unlink()
    return data


def test_has_audio_stream_real_files():
    assert has_audio_stream(_make_mp4(True)) is True
    assert has_audio_stream(_make_mp4(False)) is False


def test_watermark_disabled_by_default_and_no_external_calls(db, monkeypatch):
    assert watermarkfeat.is_enabled(db) is False

    def _boom(*a, **k):
        raise AssertionError("external call attempted while disabled")

    monkeypatch.setattr("socket.socket.connect", _boom)
    msg, data = asyncio.run(watermarkfeat.request_removal(db, b"fake-bytes"))
    assert msg == watermarkfeat.DISABLED_MSG
    assert data is None


def test_watermark_toggle_persists(db):
    watermarkfeat.set_enabled(db, True, by_id=1)
    assert watermarkfeat.is_enabled(db) is True
    watermarkfeat.set_enabled(db, False, by_id=1)
    assert watermarkfeat.is_enabled(db) is False


def test_download_path_independent_of_watermark():
    root = Path(__file__).resolve().parent.parent
    for rel in ("app/features/downloader.py", "app/bot/handlers.py",
                "app/services/link_media.py"):
        src = (root / rel).read_text()
        assert "watermark" not in src.lower(), rel


def test_admin_panel_shows_watermark_stub(db):
    cfg = Config(bot_token="x", owner_id=1)
    text, kb = adminfeat._panel_view(db, cfg)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("حذف نوشته روی ویدیو" in l and "غیرفعال — به‌زودی" in l for l in labels), labels
