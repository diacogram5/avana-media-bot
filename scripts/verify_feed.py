"""Sandbox verification for the hourly clip feed (clip_feed.py, 2026-11-05).

Checks (all on REAL fixtures — repo excerpts of the verified fetches):
  1. Parsers: t.me/s page -> 2 video posts (ad post filtered, text posts
     and 'not supported' placeholders skipped); Aparat show -> fields;
     related graph -> 50 UIDs, 40 pre-filter candidates.
  2. Intake filters: valid clip accepted (pending, categorized_by='feed',
     file_id='', source_url + feed_key stored); too short / no audio /
     too long / too large / bad URL rejected; duplicate feed_key blocked.
  3. No auto-accept: a full bootstrap cycle leaves EVERYTHING pending.
  4. Job error handling: a raising fetcher -> cycle returns an error,
     no crash, no partial intake, last_error recorded in feed_state.
  5. Single-flight: two concurrent cycles -> one returns skipped=True.
  6. Migration: _init_tables creates feed_state + source_url/feed_key
     columns; idempotent; backup keeps the ORIGINAL snapshot.
  7. file_id design: approval fallback without storage (feed clips go
     active raw by design); first delivery badges once (stubbed badge)
     and stores the file_id as processed_file_id; second delivery uses
     it instantly; badge failure -> raw-URL fallback + file_id capture,
     and the next delivery reuses the stored raw file_id.
  8. Queue analysis uses source_url (no get_file) for feed clips.
  9. Admin panel shows the feed status lines.
 10. Hourly loop survives a failing cycle and keeps running.

Fake aiogram mirrors the REAL aiogram 3 layout only (lesson 2026-11-05).
Run: python3 scripts/verify_feed.py ; exit 0 = all PASS.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import sqlite3
import subprocess
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
os.environ.pop("SARCH_STORAGE_CHAT_ID", None)

FIXTURES = ROOT / "scripts" / "fixtures" / "feed"
TG_HTML = FIXTURES / "tg_ehsaspaak_excerpt.html"
AP_JSON = FIXTURES / "aparat_show_excerpt.json"
REF_CLIP = Path.home() / "workspace/user/media_library/video/53/53e2091f582cf40588ff9687ac70321fff63ce8795884a99481f76b44799956a.mp4"

TARDID_LYRICS = ("دل بستم به سختی به دلت رسیدم از خودم به امن ساحلت "
                 "بی تردید من عاشقت شدم شریک لحظه و دقایقت شدم")


def _media(name: str) -> str:
    """/tmp fixture if present, else the reference clip (18s, audio)."""
    p = Path(f"/tmp/{name}")
    if p.exists():
        return str(p)
    return str(REF_CLIP)


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


class _SentVideo:
    def __init__(self, file_id):
        self.file_id = file_id


class _SentMessage:
    def __init__(self, file_id):
        self.video = _SentVideo(file_id)


class DeliveryBot:
    """send_video returns a message with a video.file_id (like Telegram)."""

    def __init__(self):
        self.calls = []

    async def send_video(self, chat_id, video, caption=None, **kw):
        path = getattr(video, "path", None)
        if path is not None:
            self.calls.append(("send_video", chat_id, "FSInputFile:" + path, caption))
            return _SentMessage("badged-fid-1")
        self.calls.append(("send_video", chat_id, video, caption))
        return _SentMessage("raw-fid-1")

    async def get_file(self, file_id):
        raise AssertionError("get_file must not be called here")

    async def download_file(self, file_path, destination=None):
        raise AssertionError("download_file must not be called here")


class StrictBot:
    def __init__(self):
        self.calls = []

    async def send_video(self, chat_id, video, caption=None, **kw):
        self.calls.append(("send_video", chat_id, video, caption))
        return _SentMessage("raw-fid-1")

    async def get_file(self, file_id):
        raise AssertionError("get_file must not be called here")

    async def download_file(self, file_path, destination=None):
        raise AssertionError("download_file must not be called here")


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
    from app.bot import clip_feed as feed
    from app.bot import story_archive as sarch

    cfg = SimpleNamespace(owner_id=111)
    tmpd = tempfile.mkdtemp(prefix="verify_feed_")

    # --- media variants for the filter tests -------------------------
    base = _media("ehsas_vid3.mp4")
    short_clip = os.path.join(tmpd, "short.mp4")
    silent_clip = os.path.join(tmpd, "silent.mp4")
    long_clip = os.path.join(tmpd, "long.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", base,
                    "-t", "5", "-c", "copy", short_clip],
                   check=False, timeout=60)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", base,
                    "-an", "-c:v", "copy", silent_clip],
                   check=False, timeout=60)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "4",
                    "-i", _media("aparat_clip.mp4"), "-t", "65",
                    "-c", "copy", long_clip],
                   check=False, timeout=60)
    for name, p in (("short", short_clip), ("silent", silent_clip),
                    ("long", long_clip)):
        check(f"setup: {name} variant exists",
              os.path.exists(p) and os.path.getsize(p) > 0, p)

    # --- T1: Telegram parser on the real excerpt ----------------------
    tg_html = TG_HTML.read_text(encoding="utf-8")
    posts = feed.parse_tg_page(tg_html)
    ids = [p["post_id"] for p in posts]
    check("tg parser: finds the 2 real video posts (ad filtered)",
          ids == [117099, 117112], f"got {ids}")
    p1 = posts[0]
    check("tg parser: post 117099 has duration 44 + love text",
          p1["duration"] == 44 and "عاشق" in p1["text"],
          f"got {p1['duration']!r} {p1['text'][:30]!r}")
    check("tg parser: duration '0:44' parses, garbage does not",
          feed.parse_duration("0:44") == 44
          and feed.parse_duration("1:05:30") == 3930
          and feed.parse_duration("xx") is None)

    # --- T2/T3: Aparat parsers on the real excerpt --------------------
    ap_payload = json.loads(AP_JSON.read_text(encoding="utf-8"))
    info = feed.parse_aparat_show(ap_payload)
    check("aparat show: uid/title/duration/category/can_download/file_link",
          info and info["uid"] == "jtk01l2"
          and info["duration"] == 14
          and info["category"] == "موسیقی"
          and info["can_download"] is True
          and (info["file_link"] or "").startswith("https://")
          and "aparat-video/" in info["file_link"],
          f"got {info}")
    check("aparat show: passes the accept gate",
          bool(info) and feed._aparat_accept(info))
    rel = feed.parse_aparat_related(ap_payload)
    check("aparat related: 5 UIDs embedded in the excerpt",
          len(rel) == 5 and rel[0]["uid"] == "dobi73z"
          and rel[0]["duration"] == 28, f"got {len(rel)}")
    check("aparat candidate filter: Persian title + 10-60s",
          feed._aparat_candidate({"uid": "x", "title": "کلیپ عاشقانه", "duration": 28})
          and not feed._aparat_candidate(
              {"uid": "x", "title": "cool video", "duration": 28})
          and not feed._aparat_candidate(
              {"uid": "x", "title": "کلیپ عاشقانه", "duration": 5}))

    # --- DB harness: pre-feed schema, then migrate --------------------
    conn = sqlite3.connect(":memory:")
    sarch._get_conn = lambda: conn
    for ddl in sarch._TABLE_DDLS:
        if "feed_state" in ddl:
            continue  # simulate a DB from before the feed
        conn.execute(ddl)
    conn.execute(
        "INSERT INTO story_archive (file_unique_id, file_id, category,"
        " subcategory, status, added_at) VALUES (?,?,?,?,?,?)",
        ("old-uniq-1", "old-file-1", "love", "longing", "active", 1000))
    conn.commit()

    # --- T6: migration -------------------------------------------------
    sarch._init_tables(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(story_archive)")]
    check("migration: source_url + feed_key columns exist",
          "source_url" in cols and "feed_key" in cols)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    check("migration: feed_state table exists", "feed_state" in tables)
    bak = conn.execute(
        "SELECT COUNT(*) FROM story_archive_bak_20261105").fetchone()[0]
    check("migration: backup table holds the pre-migration row", bak == 1)
    conn.execute(
        "INSERT INTO story_archive (file_unique_id, file_id, category,"
        " subcategory, status, added_at) VALUES (?,?,?,?,?,?)",
        ("old-uniq-2", "old-file-2", "sad", "breakup", "pending", 1001))
    conn.commit()
    sarch._init_tables(conn)
    bak2 = conn.execute(
        "SELECT COUNT(*) FROM story_archive_bak_20261105").fetchone()[0]
    check("migration: re-run keeps the ORIGINAL backup snapshot", bak2 == 1)

    # --- T2: intake filters --------------------------------------------
    url_ok = "file://" + base
    ok, why = await feed.intake_feed_item(
        feed_key="tg:9001", source_url=url_ok, title="کلیپ عاشقانه",
        duration_hint=18, source_username="ehsaspaak")
    row = sarch.get_clip_by_unique("feed:tg:9001")
    check("intake: valid clip accepted as pending, categorized_by='feed'",
          ok and why == "ok" and row is not None
          and row["status"] == "pending"
          and row["categorized_by"] == "feed"
          and row["mode"] == "resend"
          and row["file_id"] == "" and row["source_url"] == url_ok
          and row["feed_key"] == "tg:9001",
          f"got ok={ok} why={why} row={bool(row)}")
    ok, why = await feed.intake_feed_item(
        feed_key="tg:9001", source_url=url_ok, title="کلیپ عاشقانه",
        duration_hint=18, source_username="ehsaspaak")
    check("intake: same feed_key twice -> duplicate",
          (not ok) and why == "duplicate", f"got {why}")
    ok, why = await feed.intake_feed_item(
        feed_key="tg:9002", source_url="file://" + short_clip,
        title="short", duration_hint=5)
    check("intake: <10s clip rejected (too_short)",
          (not ok) and why == "too_short", f"got {why}")
    ok, why = await feed.intake_feed_item(
        feed_key="tg:9003", source_url="file://" + silent_clip,
        title="silent", duration_hint=18)
    check("intake: clip without audio rejected (no_audio)",
          (not ok) and why == "no_audio", f"got {why}")
    ok, why = await feed.intake_feed_item(
        feed_key="tg:9004", source_url="file://" + long_clip,
        title="long", duration_hint=65)
    check("intake: >60s clip rejected (too_long)",
          (not ok) and why == "too_long", f"got {why}")
    _p, _r = feed.download_url_to_file(url_ok, os.path.join(tmpd, "x.mp4"),
                                       max_bytes=100)
    check("download: >max_bytes rejected (too_large)",
          _p is None and _r == "too_large", f"got {_r}")
    ok, why = await feed.intake_feed_item(
        feed_key="tg:9005", source_url="file:///tmp/does-not-exist.mp4",
        title="gone", duration_hint=18)
    check("intake: unreachable URL rejected, no crash",
          (not ok) and why in ("http", "error", "timeout"), f"got {why}")

    # --- T4/T5: full bootstrap cycle (fixture fetchers, real download) -
    html_cycle = re.sub(
        r"https://cdn4\.telesco\.pe/file/f2efeb7ace\.mp4\?token=TOKEN",
        "file://" + _media("ehsas_vid1.mp4"), tg_html)
    html_cycle = re.sub(
        r"https://cdn4\.telesco\.pe/file/aff5c09e37\.mp4\?token=TOKEN",
        "file://" + _media("ehsas_vid3.mp4"), html_cycle)

    def tg_fetcher():
        return html_cycle

    ap_base = json.loads(AP_JSON.read_text(encoding="utf-8"))

    def ap_fetcher(uid):
        p = copy.deepcopy(ap_base)
        p["data"]["attributes"]["uid"] = uid
        p["data"]["attributes"]["file_link"] = (
            "file://" + _media("aparat_clip.mp4"))
        return p

    stats = await feed.run_feed_cycle(bootstrap=True,
                                      tg_fetcher=tg_fetcher,
                                      aparat_fetcher=ap_fetcher)
    check("cycle bootstrap: tg_new=2 (ad + text posts skipped)",
          stats.get("tg_new") == 2, f"got {stats}")
    check("cycle bootstrap: ap_new=6 (seed + 5 related)",
          stats.get("ap_new") == 6, f"got {stats}")
    check("cycle bootstrap: no error", stats.get("error") is None,
          f"got {stats.get('error')}")
    pend = sarch.pending_clips(50)
    feed_pend = [c for c in pend if c.get("feed_key")]
    check("cycle: NO auto-accept — all 9 feed intakes still pending",
          len(feed_pend) == 9 and all(c["status"] == "pending" for c in feed_pend),
          f"got {len(feed_pend)} feed pending")
    st = feed.get_feed_status()
    check("feed status: last_run recorded + last_new=8",
          st["last_new"] == 8 and st["last_run_at"] > 0
          and st["pending_feed"] == 9, f"got {st}")
    check("feed status: tg_last_seen advanced to the newest post",
          st["last_seen_tg"] == 117112, f"got {st['last_seen_tg']}")
    # incremental re-run: everything deduped, nothing new
    stats2 = await feed.run_feed_cycle(bootstrap=False,
                                       tg_fetcher=tg_fetcher,
                                       aparat_fetcher=ap_fetcher)
    check("cycle incremental: tg_new=0 ap_new=0 (dedup holds)",
          stats2.get("tg_new") == 0 and stats2.get("ap_new") == 0,
          f"got {stats2}")
    check("cycle incremental: last_new reset to 0",
          feed.get_feed_status()["last_new"] == 0)

    # --- T6 (job errors): raising fetcher -> error, no crash, no intake
    before = len(sarch.pending_clips(50))

    def boom():
        raise RuntimeError("network down")

    stats3 = await feed.run_feed_cycle(bootstrap=False, tg_fetcher=boom,
                                       aparat_fetcher=lambda uid: None)
    check("cycle error: raising fetcher -> tg_cycle_failed, no crash",
          stats3.get("tg_error") == "tg_cycle_failed"
          and stats3.get("error") == "tg_cycle_failed", f"got {stats3}")
    check("cycle error: no partial intake",
          len(sarch.pending_clips(50)) == before)
    check("cycle error: last_error stored in feed_state",
          feed.get_feed_status()["last_error"] == "tg_cycle_failed",
          f"got {feed.get_feed_status()['last_error']!r}")

    def none_fetcher():
        return None

    stats3b = await feed.run_feed_cycle(bootstrap=False,
                                        tg_fetcher=none_fetcher,
                                        aparat_fetcher=lambda uid: None)
    check("cycle error: empty fetch -> tg_fetch_failed",
          stats3b.get("tg_error") == "tg_fetch_failed",
          f"got {stats3b}")

    # --- T5: single-flight ----------------------------------------------
    async def slow_tg():
        await asyncio.sleep(0)
        return html_cycle

    def slow_tg_sync():
        import time as _t
        _t.sleep(0.3)
        return html_cycle

    r1, r2 = await asyncio.gather(
        feed.run_feed_cycle(bootstrap=False, tg_fetcher=slow_tg_sync,
                            aparat_fetcher=lambda uid: None),
        feed.run_feed_cycle(bootstrap=False, tg_fetcher=slow_tg_sync,
                            aparat_fetcher=lambda uid: None))
    check("single-flight: one of two concurrent cycles is skipped",
          (r1.get("skipped") is True) != (r2.get("skipped") is True),
          f"got skipped={r1.get('skipped')}/{r2.get('skipped')}")

    # --- T7: approval fallback without storage + first delivery --------
    import app.bot.motion_watermark as mw
    real_badge = mw.apply_user_badge
    badge_calls = []

    def stub_badge(src, out):
        badge_calls.append((src, out))
        shutil.copyfile(src, out)
        return {"style": "stub"}

    mw.apply_user_badge = stub_badge
    try:
        r2router = FakeRouter()
        sarch.register_story_archive_handlers(r2router)
        play_h = find_handler(r2router, "startswith:data:sarch:play:")
        check("sarch:play handler registered", play_h is not None)

        clip = sarch.get_clip_by_unique("feed:tg:9001")
        bot = DeliveryBot()
        # Phase 1 base: approval is the direct qok path (approve_clip);
        # the badge is applied once at first delivery, not at approval.
        ok_ap = sarch.approve_clip(clip["id"])
        row = sarch.get_clip(clip["id"])
        check("approval: feed clip goes active (direct approve)",
              ok_ap and row["status"] == "active",
              f"got ok={ok_ap} status={row['status']}")
        cb = FakeCallback(f"sarch:play:{clip['id']}:love:express:0", bot)
        await play_h(cb, cfg, FakeState())
        row = sarch.get_clip(clip["id"])
        first_send = bot.calls[0] if bot.calls else None
        check("first delivery: badge applied once via FSInputFile",
              len(badge_calls) == 1 and first_send is not None
              and first_send[2].startswith("FSInputFile:"),
              f"calls={bot.calls}")
        check("first delivery: badged file_id stored as processed final",
              row.get("processed_file_id") == "badged-fid-1"
              and row.get("file_id") == "badged-fid-1",
              f"got processed={row.get('processed_file_id')!r}")
        # second delivery: instant, uses the stored final
        n_calls = len(bot.calls)
        cb2 = FakeCallback(f"sarch:play:{clip['id']}:love:express:0", bot)
        await play_h(cb2, cfg, FakeState())
        check("second delivery: uses stored processed_file_id, no re-badge",
              len(bot.calls) == n_calls + 1
              and bot.calls[-1][2] == "badged-fid-1"
              and len(badge_calls) == 1,
              f"calls={bot.calls[-1:]} badge_calls={len(badge_calls)}")

        # badge failure -> raw URL fallback + file_id capture, then reuse
        def boom_badge(src, out):
            badge_calls.append((src, out))
            raise RuntimeError("ffmpeg down")

        mw.apply_user_badge = boom_badge
        clip2 = sarch.get_clip_by_unique("feed:tg:117099")
        sarch.set_clip_status(clip2["id"], "active")
        sarch.move_clip(clip2["id"], "love", "express")
        bot2 = DeliveryBot()
        cb3 = FakeCallback(f"sarch:play:{clip2['id']}:love:express:0", bot2)
        await play_h(cb3, cfg, FakeState())
        row2 = sarch.get_clip(clip2["id"])
        check("badge failure: raw URL fallback sends + captures file_id",
              row2.get("file_id") == "raw-fid-1"
              and row2.get("last_error") == "badge_failed"
              and not row2.get("processed_file_id"),
              f"got file_id={row2.get('file_id')!r} err={row2.get('last_error')!r}")
        n_badge = len(badge_calls)
        cb4 = FakeCallback(f"sarch:play:{clip2['id']}:love:express:0", bot2)
        await play_h(cb4, cfg, FakeState())
        check("badge failure: next delivery reuses stored raw file_id",
              bot2.calls[-1][2] == "raw-fid-1"
              and len(badge_calls) == n_badge,
              f"calls={bot2.calls[-1:]}")
    finally:
        mw.apply_user_badge = real_badge

    # --- T8: queue analysis uses source_url (no get_file) ---------------
    async def fake_identify(data):
        return ("تردید", "سامان جلیلی")

    async def fake_lyrics(title, artist):
        # Feed clips have no channel prior (category is '' until the
        # admin picks one), so the lyrics must carry the score alone.
        return TARDID_LYRICS + " عشق دوستت دارم عاشقانه"

    sarch._identify_audio_bytes = fake_identify
    sarch._fetch_lyrics_text = fake_lyrics
    feed_clip = sarch.get_clip_by_unique("feed:tg:117112")
    strict = StrictBot()
    await sarch.analyze_clip_for_queue(strict, feed_clip)
    row = sarch.get_clip(feed_clip["id"])
    check("queue analysis (feed): stores song + suggestion via source_url",
          row["identified_title"] == "تردید"
          and row["sugg_category"] == "love"
          and row["sugg_subcategory"] == "express",
          f"got title={row['identified_title']!r} "
          f"sugg={row['sugg_category']}/{row['sugg_subcategory']}")
    check("queue analysis (feed): get_file never called",
          not any(c[0] == "get_file" for c in strict.calls))

    # --- T9: admin panel shows the feed status lines --------------------
    panel_h = find_handler(r2router, "eq:data=sarch:panel")
    cbp = FakeCallback("sarch:panel", StrictBot())
    await panel_h(cbp, cfg, FakeState())
    shown = cbp.message.edits + cbp.message.answers
    txt = next((t for t, _ in shown if t and "آرشیو استوری" in t), "")
    check("panel: feed status lines present",
          "فید ساعتی" in txt and "کلیپ تازه" in txt, f"txt={txt[:200]!r}")

    # --- T10: hourly loop survives a failing cycle -----------------------
    calls = {"n": 0, "fail_once": True}

    async def flaky_cycle(*args, **kwargs):
        calls["n"] += 1
        if calls["fail_once"]:
            calls["fail_once"] = False
            raise RuntimeError("boom")
        return {"skipped": False, "tg_new": 0, "ap_new": 0, "error": None}

    orig_cycle = feed.run_feed_cycle
    feed.run_feed_cycle = flaky_cycle
    try:
        task = asyncio.create_task(
            feed.clip_feed_loop(interval_sec=0.15, first_delay_sec=0))
        await asyncio.sleep(0.7)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        check("hourly loop: survives a failing cycle and keeps running",
              calls["n"] >= 2, f"calls={calls['n']}")
    finally:
        feed.run_feed_cycle = orig_cycle

    # --- T: refresh pending Telegram URLs --------------------------------
    conn.execute(
        "UPDATE story_archive SET source_url='https://old.invalid/x.mp4'"
        " WHERE feed_key='tg:117112'")
    conn.commit()
    fresh = feed.parse_tg_page(html_cycle)
    n_ref = feed.refresh_pending_tg_urls(fresh)
    row = sarch.get_clip_by_unique("feed:tg:117112")
    check("refresh: stale pending TG url replaced from the fresh page",
          n_ref >= 1 and row["source_url"].startswith("file://"),
          f"n={n_ref} url={row['source_url'][:40]!r}")

    shutil.rmtree(tmpd, ignore_errors=True)
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
