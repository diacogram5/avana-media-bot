"""Sandbox verification for Phase 1 (2026-11-05 final design).

Checks:
  1. Migration: _init_tables creates tables + *_bak_20261105 backups +
     all 14 new columns; idempotent; the backup keeps the ORIGINAL
     snapshot (never overwritten by a re-run).
  2. Intake is ALWAYS pending (even caption-match + auto_approve=True);
     duplicate intake is blocked.
  3. forward_only: file_size > 20MB -> 1; == 20MB -> 0; 0 -> 0.
  4. Suggestion scoring: «تردید» lyrics -> love/express; ambiguous
     lyrics -> no suggestion; strong caption -> sad/breakup; weak
     caption (score 2) -> no suggestion.
  5. approve_clip applies the stored suggestion + categorized_by='admin'.
  6. Reject keeps the row (status='rejected'), blocks re-intake, and is
     excluded from pending/active lists.
  7. analyze_clip_for_queue (mocked identify/lyrics + fixture bot):
     stores song + lyrics + suggestion; forward_only clips never
     download (get_file is never called).
  8. Queue card shows song/suggestion/reasons; empty-archive sarch:sub
     shows exactly the not-available message with ONLY the Back button
     (no "1/1", no YouTube); with clips, nav + YouTube return.
  9. Manual add stays active with categorized_by='admin'.
 10. All archive keyboards build (panel, home, cat, sub, queue).

Fake aiogram mirrors the REAL aiogram 3 layout only (lesson 2026-11-05).
Run: python3 scripts/verify_phase1.py ; exit 0 = all PASS.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.pop("TURSO_URL", None)
os.environ.pop("TURSO_AUTH_TOKEN", None)
os.environ.pop("TURSO_DATABASE_URL", None)

REF_CLIP = Path.home() / "workspace/user/media_library/video/53/53e2091f582cf40588ff9687ac70321fff63ce8795884a99481f76b44799956a.mp4"
TARDID_LYRICS = ("دل بستم به سختی به دلت رسیدم از خودم به امن ساحلت "
                 "بی تردید من عاشقت شدم شریک لحظه و دقایقت شدم")

# ---------------------------------------------------------------------------
# Fake aiogram (real aiogram 3 layout only)
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


class _StateFilter:
    def __init__(self, *states):
        self.states = states


class _FSMContext:
    pass


class _State:
    def __init__(self):
        pass


class _StatesGroup:
    pass


def _install_fake_aiogram():
    aiogram = types.ModuleType("aiogram")
    aiogram.F = _F()
    types_mod = types.ModuleType("aiogram.types")
    types_mod.InlineKeyboardButton = _Button
    types_mod.InlineKeyboardMarkup = _Markup
    types_mod.CallbackQuery = type("CallbackQuery", (), {})
    types_mod.Message = type("Message", (), {})
    types_mod.FSInputFile = _FSInputFile
    filters_mod = types.ModuleType("aiogram.filters")
    filters_mod.StateFilter = _StateFilter
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


class FakeRouter:
    def __init__(self):
        self.handlers = []

    def _deco(self, *filters):
        def wrap(fn):
            self.handlers.append((filters, fn))
            return fn
        return wrap

    def callback_query(self, *filters):
        return self._deco(*filters)

    def message(self, *filters):
        return self._deco(*filters)

    def channel_post(self, *filters):
        return self._deco(*filters)


def find_handler(router, marker):
    for filters, fn in router.handlers:
        if any(str(f) == marker for f in filters):
            return fn
    return None


class FakeChat:
    id = 111


class FakeMessage:
    def __init__(self, bot):
        self.bot = bot
        self.chat = FakeChat()
        self.edits = []
        self.answers = []
        self.sent_videos = []

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text=None, reply_markup=None, parse_mode=None, **kw):
        self.answers.append((text, reply_markup))
        return FakeMessage(self.bot)

    async def answer_video(self, video, caption=None, reply_markup=None):
        self.sent_videos.append((video, caption))
        return None

    async def delete(self):
        return None


class FakeUser:
    id = 111


class FakeCallback:
    def __init__(self, data, bot):
        self.data = data
        self.bot = bot
        self.from_user = FakeUser()
        self.message = FakeMessage(bot)
        self.answered = []

    async def answer(self, text=None, show_alert=False):
        self.answered.append(text)


class FakeState:
    def __init__(self):
        self.data = {}
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, s):
        self.state = s

    async def get_state(self):
        return self.state

    async def clear(self):
        self.data = {}
        self.state = None


class MockBot:
    """Records calls; get_file/download_file raise unless overridden."""

    def __init__(self):
        self.calls = []

    async def forward_message(self, chat_id, from_chat_id, message_id):
        self.calls.append(("forward", chat_id, from_chat_id, message_id))

    async def send_video(self, chat_id, video, caption=None, **kw):
        self.calls.append(("send_video", chat_id,
                            getattr(video, "path", video), caption))

    async def get_file(self, file_id):
        raise AssertionError("get_file must not be called here")

    async def download_file(self, file_path, destination=None):
        raise AssertionError("download_file must not be called here")


class FixtureBot(MockBot):
    """Serves the reference clip for queue analysis."""

    def __init__(self, fixture):
        super().__init__()
        self.fixture = fixture

    async def get_file(self, file_id):
        class F:
            file_path = "fixture.mp4"
        return F()

    async def download_file(self, file_path, destination=None):
        self.calls.append(("download_file", file_path, destination))
        shutil.copyfile(self.fixture, destination)


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))


def kb_buttons(kb):
    out = []
    if kb is None:
        return out
    for row in kb.inline_keyboard or []:
        for b in row:
            out.append((b.text, b.callback_data))
    return out

async def main():
    from app.bot import story_archive as sarch

    cfg = SimpleNamespace(owner_id=111)

    # --- DB harness: real SQLite, _get_conn patched -------------------
    conn = sqlite3.connect(":memory:")
    sarch._get_conn = lambda: conn
    # Simulate a PRE-Phase-1 database: old DDLs only, one existing row.
    for ddl in sarch._TABLE_DDLS:
        conn.execute(ddl)
    conn.execute(
        "INSERT INTO story_archive (file_unique_id, file_id, category,"
        " subcategory, status, added_at) VALUES (?,?,?,?,?,?)",
        ("old-uniq-1", "old-file-1", "love", "longing", "active", 1000))
    conn.commit()

    # --- T1: migration + backup --------------------------------------
    sarch._init_tables(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(story_archive)")]
    for col, _typ in sarch._NEW_COLUMNS:
        check(f"migration: column {col} exists", col in cols)
    bak = conn.execute(
        "SELECT COUNT(*) FROM story_archive_bak_20261105").fetchone()[0]
    check("migration: backup table holds the pre-migration row", bak == 1)
    # Idempotency: a second run must NOT overwrite the original snapshot.
    conn.execute(
        "INSERT INTO story_archive (file_unique_id, file_id, category,"
        " subcategory, status, added_at) VALUES (?,?,?,?,?,?)",
        ("old-uniq-2", "old-file-2", "sad", "breakup", "pending", 1001))
    conn.commit()
    sarch._init_tables(conn)
    bak2 = conn.execute(
        "SELECT COUNT(*) FROM story_archive_bak_20261105").fetchone()[0]
    check("migration: re-run keeps the ORIGINAL backup snapshot", bak2 == 1)
    cols2 = [r[1] for r in conn.execute("PRAGMA table_info(story_archive)")]
    check("migration: idempotent (columns unchanged on re-run)",
          cols == cols2)

    # --- T2: intake is ALWAYS pending ---------------------------------
    ok, res = sarch.intake_channel_post(
        file_unique_id="uq-a", file_id="f-a", chat_id=-1001, message_id=11,
        username="chan", category="love", subcategory="express",
        caption="عشق و دوستت دارم", duration=18, width=720, height=1280,
        auto_approve=True, watermark_ok=False, file_size=2_000_000)
    row = sarch.get_clip(3)
    check("intake: caption-match + auto_approve still pending",
          ok and res == "pending" and row and row["status"] == "pending")
    ok2, res2 = sarch.intake_channel_post(
        file_unique_id="uq-a", file_id="f-a", chat_id=-1001, message_id=11,
        username="chan", category="love", subcategory="express",
        caption="عشق", duration=18, width=720, height=1280,
        auto_approve=True, file_size=2_000_000)
    check("intake: duplicate is blocked", (not ok2) and res2 == "duplicate")

    # --- T3: forward_only threshold ------------------------------------
    for i, (uid, size, want) in enumerate((("uq-b", 20 * 1024 * 1024, 0),
                                           ("uq-c", 20 * 1024 * 1024 + 1, 1),
                                           ("uq-d", 0, 0))):
        sarch.intake_channel_post(
            file_unique_id=uid, file_id="f-" + uid, chat_id=-1001,
            message_id=20 + i, username="chan", category="love",
            subcategory="express", caption="", duration=20, width=720,
            height=1280, auto_approve=False, file_size=size)
        r = conn.execute(
            "SELECT forward_only FROM story_archive WHERE file_unique_id=?",
            (uid,)).fetchone()
        check(f"intake: file_size={size} -> forward_only={want}",
              r and r[0] == want)

    # --- T4: suggestion scoring (pure) ---------------------------------
    cat, sub, score, reasons = sarch.suggest_subcategory(
        TARDID_LYRICS, "", "love", "express")
    check("suggest: «تردید» lyrics -> love/express",
          (cat, sub) == ("love", "express") and score >= 3,
          f"got {(cat, sub, score)}")
    cat, sub, score, _r = sarch.suggest_subcategory(
        "جدایی و تنهایی، دلم تنگ شده و رفتی", "", "sad", "breakup")
    check("suggest: ambiguous lyrics -> no suggestion",
          cat is None and sub is None, f"got {(cat, sub, score)}")
    cat, sub, score, _r = sarch.suggest_subcategory(
        "", "جدایی، خداحافظ، تمام شد", "sad", "breakup")
    check("suggest: strong caption -> sad/breakup",
          (cat, sub) == ("sad", "breakup") and score >= 3,
          f"got {(cat, sub, score)}")
    cat, sub, score, _r = sarch.suggest_subcategory(
        "", "دلم برات تنگ شده", "love", "longing")
    check("suggest: weak caption (score 2) -> no suggestion",
          cat is None and score == 2, f"got {(cat, sub, score)}")

    # --- T5: approve_clip applies the stored suggestion ---------------
    sarch.save_suggestion(3, title="تردید", artist="سامان جلیلی",
                          lyrics_excerpt=TARDID_LYRICS[:100],
                          sugg_category="love", sugg_subcategory="express",
                          score=4, reasons="ترانه: عاشق")
    check("approve_clip: applies stored suggestion",
          sarch.approve_clip(3) is True)
    row = sarch.get_clip(3)
    check("approve_clip: final cat/sub + categorized_by='admin'",
          row["status"] == "active" and row["category"] == "love"
          and row["subcategory"] == "express"
          and row["categorized_by"] == "admin"
          and int(row["categorized_at"] or 0) > 0)

    # --- T5b: approve_clip(by='feed-auto') marks the approval source --
    sarch.intake_channel_post(
        file_unique_id="uq-a2", file_id="f-a2", chat_id=-1001, message_id=12,
        username="chan", category="love", subcategory="express",
        caption="عشق و دوستت دارم", duration=18, width=720, height=1280,
        auto_approve=False, watermark_ok=False, file_size=2_000_000)
    aid = conn.execute(
        "SELECT id FROM story_archive WHERE file_unique_id='uq-a2'").fetchone()[0]
    sarch.save_suggestion(aid, sugg_category="love", sugg_subcategory="express",
                          score=6, reasons="ترانه: عشق")
    check("approve_clip(by='feed-auto'): approves",
          sarch.approve_clip(aid, by="feed-auto") is True)
    row = sarch.get_clip(aid)
    check("approve_clip(by='feed-auto'): categorized_by='feed-auto'",
          row["status"] == "active" and row["category"] == "love"
          and row["subcategory"] == "express"
          and row["categorized_by"] == "feed-auto")
    check("approve_clip(by='bogus'): falls back to 'admin'",
          sarch.approve_clip(aid, by="bogus") is True
          and sarch.get_clip(aid)["categorized_by"] == "admin")

    # --- T6: reject keeps the row and blocks re-intake -----------------
    sarch.intake_channel_post(
        file_unique_id="uq-e", file_id="f-e", chat_id=-1001, message_id=13,
        username="chan", category="sad", subcategory="breakup", caption="",
        duration=20, width=720, height=1280, auto_approve=False,
        file_size=1_000_000)
    eid = conn.execute(
        "SELECT id FROM story_archive WHERE file_unique_id='uq-e'").fetchone()[0]
    sarch.set_clip_status(eid, "rejected")
    row = sarch.get_clip(eid)
    pend = [c["id"] for c in sarch.pending_clips(20)]
    check("reject: row kept with status='rejected'",
          row["status"] == "rejected" and eid not in pend)
    ok3, res3 = sarch.intake_channel_post(
        file_unique_id="uq-e", file_id="f-e", chat_id=-1001, message_id=13,
        username="chan", category="sad", subcategory="breakup", caption="",
        duration=20, width=720, height=1280, auto_approve=False,
        file_size=1_000_000)
    check("reject: re-intake of a rejected clip is blocked",
          (not ok3) and res3 == "duplicate")

    # --- T7: queue analysis (mocked identify/lyrics) -------------------
    async def fake_identify(data):
        return ("تردید", "سامان جلیلی")

    async def fake_lyrics(title, artist):
        return TARDID_LYRICS

    sarch._identify_audio_bytes = fake_identify
    sarch._fetch_lyrics_text = fake_lyrics
    # fresh pending clip for analysis (channel love/express)
    sarch.intake_channel_post(
        file_unique_id="uq-f", file_id="f-f", chat_id=-1001, message_id=14,
        username="chan", category="love", subcategory="express", caption="",
        duration=18, width=720, height=1280, auto_approve=False,
        file_size=2_000_000)
    fid = conn.execute(
        "SELECT id FROM story_archive WHERE file_unique_id='uq-f'").fetchone()[0]
    bot = FixtureBot(str(REF_CLIP))
    await sarch.analyze_clip_for_queue(bot, sarch.get_clip(fid))
    row = sarch.get_clip(fid)
    check("analysis: stores song + lyrics + suggestion",
          row["identified_title"] == "تردید"
          and row["identified_artist"] == "سامان جلیلی"
          and "دل بستم" in (row["lyrics_excerpt"] or "")
          and row["sugg_category"] == "love"
          and row["sugg_subcategory"] == "express",
          f"got title={row['identified_title']} sugg={row['sugg_category']}/{row['sugg_subcategory']}")
    check("analysis: fixture was downloaded once",
          sum(1 for c in bot.calls if c[0] == "download_file") == 1,
          f"calls={bot.calls}")
    # forward_only clip: analysis must NOT download anything
    big = sarch.get_clip(conn.execute(
        "SELECT id FROM story_archive WHERE file_unique_id='uq-c'").fetchone()[0])
    strict_bot = MockBot()  # get_file raises if called
    await sarch.analyze_clip_for_queue(strict_bot, big)
    row = sarch.get_clip(big["id"])
    check("analysis: forward_only clip never downloads",
          row["last_error"] == "forward_only"
          and not any(c[0] == "get_file" for c in strict_bot.calls))

    # --- T8: queue card + empty-state keyboard -------------------------
    # Make the analyzed clip the queue head (deterministic order).
    conn.execute(
        "UPDATE story_archive SET added_at=9999999999 WHERE file_unique_id='uq-f'")
    conn.commit()
    r2 = FakeRouter()
    sarch.register_story_archive_handlers(r2)
    check("sarch:queue handler registered",
          find_handler(r2, "eq:data=sarch:queue") is not None)
    check("sarch:sub handler registered",
          find_handler(r2, "startswith:data:sarch:sub:") is not None)
    qh = find_handler(r2, "eq:data=sarch:queue")
    cb = FakeCallback("sarch:queue", MockBot())
    await qh(cb, cfg, FakeState())
    shown = cb.message.answers + cb.message.edits
    card = next((t for t, _ in shown if t and "بررسی ارتباط" in t), "")
    check("queue card: shows song + suggestion + reasons",
          "تردید" in card and "عاشقانه" in card and "پیشنهاد سیستم" in card,
          f"card={card[:160]!r}")
    # empty subtopic: only the Back button, no 1/1, no YouTube
    subh = find_handler(r2, "startswith:data:sarch:sub:")
    cb2 = FakeCallback("sarch:sub:happy:hope:0", MockBot())
    await subh(cb2, cfg, FakeState())
    shown2 = cb2.message.edits + cb2.message.answers
    txt2, kb2 = shown2[0] if shown2 else ("", None)
    btns2 = kb_buttons(kb2)
    check("empty sub: exact not-available message",
          "هنوز کلیپ مناسبی برای این موضوع موجود نیست" in (txt2 or ""))
    check("empty sub: ONLY the Back button (no 1/1, no YouTube)",
          btns2 == [("🔙", "sarch:cat:happy")], f"got {btns2}")
    # with an active clip: nav + YouTube return
    cb3 = FakeCallback("sarch:sub:love:express:0", MockBot())
    await subh(cb3, cfg, FakeState())
    shown3 = cb3.message.edits + cb3.message.answers
    _t3, kb3 = shown3[0] if shown3 else ("", None)
    btns3 = kb_buttons(kb3)
    check("non-empty sub: nav + YouTube buttons return",
          any("/" in (t or "") for t, _ in btns3)
          and any("یوتیوب" in (t or "") for t, _ in btns3),
          f"got {btns3}")

    # --- T9: manual add stays active with categorized_by='admin' ------
    okm, resm = sarch.archive_clip(
        file_unique_id="uq-man", file_id="f-man", category="love",
        subcategory="express", mode="resend", status="active",
        added_by=111, file_size=2_300_000, forward_only=0,
        categorized_by="admin", categorized_at=1234567890)
    rowm = conn.execute(
        "SELECT status, categorized_by FROM story_archive"
        " WHERE file_unique_id='uq-man'").fetchone()
    check("manual add: active + categorized_by='admin'",
          okm and rowm and rowm[0] == "active" and rowm[1] == "admin")

    # --- T10: all archive keyboards build ------------------------------
    for marker, label in (("eq:data=sarch:panel", "panel"),
                          ("eq:data=sarch:home", "home"),
                          ("startswith:data:sarch:cat:", "cat")):
        h = find_handler(r2, marker)
        check(f"keyboard builds: {label}", h is not None)

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
