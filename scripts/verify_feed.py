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
     active raw by design); first delivery burns the Waveframe badge
     once + the AVANO MEDIA outro (stubbed) and stores the file_id as
     processed_file_id; second delivery uses it instantly; badge
     failure -> raw-URL fallback + file_id capture, and the next
     delivery reuses the stored raw file_id.
  8. Queue analysis uses source_url (no get_file) for feed clips.
  9. Admin panel shows the feed status lines.
 10. Hourly loop survives a failing cycle and keeps running.
 11. Multi-channel Telegram: one cycle intakes from @SOBHbe,
     @yaldavamusic, @delll_nevveshteh and @dosti (not just @ehsaspaak)
     with distinct feed_keys; per-channel last_seen; legacy
     'tg:<id>' keys dedup against the new 'tg:ehsaspaak:<id>' form;
     tokenized video URLs kept whole. The friendship channel is wired
     as 'dosti' (t.me/s/refaghat serves the same page but its posts
     carry data-post='dosti/...').
 12. Aparat seed rotation: THREE seeds per run (since 2026-11-06 —
     one seed per run took ~15h to cover 15 seeds), round-robin
     across the 19 seeds / 18 creators (faezehradmanesh, Multimedia2020,
     Clip_film2023, Masafinia.m x2, Amirabbas.b, AparatVision,
     Tafrihi_Best, Aparat_Media_pars, Kartoonchi, nasi20, vidoma,
     Sargarmi_kids, Film_Tvs, toop_media, digikot, Star_2020, Mina_diba,
     Sjadkazmei); 7 runs cover all seeds, the 7th takes the last one;
     the per-run show-call bound (24) is TOTAL, not per seed; the
     Multimedia2020 fixture parses (50 related, 49 short);
     a 61s clip is rejected by the 60s cap.

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
SOBHBE_HTML = FIXTURES / "tg_SOBHbe_excerpt.html"
YALDAVA_HTML = FIXTURES / "tg_yaldavamusic_excerpt.html"
DELL_HTML = FIXTURES / "tg_delll_nevveshteh_excerpt.html"
DOSTI_HTML = FIXTURES / "tg_dosti_excerpt.html"
AP_JSON = FIXTURES / "aparat_show_excerpt.json"
TAF_JSON = FIXTURES / "aparat_show_tafrihi.json"
KAR_JSON = FIXTURES / "aparat_show_kartoonchi.json"
MM_JSON = FIXTURES / "aparat_show_multimedia2020.json"
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

    # --- T1b: the two new Telegram channels (real excerpts) ----------
    sob_posts = feed.parse_tg_page(
        SOBHBE_HTML.read_text(encoding="utf-8"), channel="SOBHbe")
    sob_ids = [p["post_id"] for p in sob_posts]
    check("tg parser SOBHbe: 4 video posts (text posts skipped)",
          sob_ids == [132203, 132209, 132214, 132219], f"got {sob_ids}")
    check("tg parser SOBHbe: memorial clip 132203 accepted (13s)",
          sob_posts[0]["duration"] == 13
          and "پنجشنبه" in sob_posts[0]["text"],
          f"got {sob_posts[0]}")
    check("tg parser: channel match is case-insensitive (data-post is lowercase)",
          all(p["channel"] == "SOBHbe" for p in sob_posts))
    yal_posts = feed.parse_tg_page(
        YALDAVA_HTML.read_text(encoding="utf-8"), channel="yaldavamusic")
    yal_ids = [p["post_id"] for p in yal_posts]
    check("tg parser yaldavamusic: 2 video posts (audio/link posts skipped)",
          yal_ids == [40, 43], f"got {yal_ids}")
    check("tg parser: tokenized video URL kept whole (no bare .mp4)",
          all("?token=TOKEN" in p["url"] for p in sob_posts + yal_posts)
          and all(p["url"].startswith("https://cdn")
                  for p in sob_posts + yal_posts))
    # --- T1c: the two REPORT2 Telegram channels (real excerpts) -------
    dell_posts = feed.parse_tg_page(
        DELL_HTML.read_text(encoding="utf-8"), channel="delll_nevveshteh")
    dell_ids = [p["post_id"] for p in dell_posts]
    check("tg parser delll_nevveshteh: 3 video posts",
          dell_ids == [326, 328, 339], f"got {dell_ids}")
    check("tg parser delll_nevveshteh: short post 328 parses as 3s",
          dell_posts[1]["duration"] == 3, f"got {dell_posts[1]}")
    dosti_posts = feed.parse_tg_page(
        DOSTI_HTML.read_text(encoding="utf-8"), channel="dosti")
    dosti_ids = [p["post_id"] for p in dosti_posts]
    check("tg parser dosti: 3 video posts (friendship channel)",
          dosti_ids == [13109, 13110, 13111], f"got {dosti_ids}")
    check("tg parser dosti: the refaghat alias parses ZERO posts",
          feed.parse_tg_page(
              DOSTI_HTML.read_text(encoding="utf-8"),
              channel="refaghat") == [],
          "data-post carries dosti/..., so wiring 'refaghat' is dead")
    check("tg parser: channel isolation (ehsaspaak page as SOBHbe -> [])",
          feed.parse_tg_page(tg_html, channel="SOBHbe") == [])
    check("tg parser: feed_key aliases (legacy <-> new, ehsaspaak only)",
          feed._tg_feed_key_aliases("tg:117099")
          == ["tg:117099", "tg:ehsaspaak:117099"]
          and feed._tg_feed_key_aliases("tg:ehsaspaak:117099")
          == ["tg:ehsaspaak:117099", "tg:117099"]
          and feed._tg_feed_key_aliases("tg:SOBHbe:132203")
          == ["tg:SOBHbe:132203"]
          and feed._parse_tg_feed_key("tg:117099") == ("ehsaspaak", 117099)
          and feed._parse_tg_feed_key("tg:SOBHbe:132203")
          == ("SOBHbe", 132203))

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
    # --- T2b: refresh_aparat_file_link (clip #193, 2026-11-07) --------
    # A stored Aparat file_link can expire; the show API returns a fresh
    # tokenized link for the same UID. The helper returns "" on failure.
    _real_show = feed._fetch_aparat_show
    feed._fetch_aparat_show = lambda uid: ap_payload
    try:
        check("aparat refresh: fresh file_link for the same UID",
              feed.refresh_aparat_file_link("jtk01l2")
              == (info["file_link"] or ""),
              f"got {feed.refresh_aparat_file_link('jtk01l2')!r}")
    finally:
        feed._fetch_aparat_show = _real_show
    feed._fetch_aparat_show = lambda uid: None
    try:
        check("aparat refresh: failed show fetch -> empty link",
              feed.refresh_aparat_file_link("jtk01l2") == "")
    finally:
        feed._fetch_aparat_show = _real_show
    check("aparat refresh: empty UID -> empty link",
          feed.refresh_aparat_file_link("") == "")
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

    # --- T2c: Aparat Tafrihi_Best (REPORT2 occasion source) ------------
    taf_payload = json.loads(TAF_JSON.read_text(encoding="utf-8"))
    taf_info = feed.parse_aparat_show(taf_payload)
    check("aparat show Tafrihi_Best: uid/duration/category/can_download",
          taf_info and taf_info["uid"] == "okm549l"
          and taf_info["duration"] == 23
          and taf_info["category"] == "موسیقی"
          and taf_info["owner"] == "Tafrihi_Best"
          and taf_info["can_download"] is True
          and (taf_info["file_link"] or "").startswith("https://"),
          f"got {taf_info}")
    check("aparat show Tafrihi_Best: passes the accept gate",
          bool(taf_info) and feed._aparat_accept(taf_info))
    taf_rel = feed.parse_aparat_related(taf_payload)
    check("aparat related Tafrihi_Best: 5 UIDs, first xcpt4f8 26s",
          len(taf_rel) == 5 and taf_rel[0]["uid"] == "xcpt4f8"
          and taf_rel[0]["duration"] == 26, f"got {len(taf_rel)}")

    # --- T2d: Aparat Kartoonchi (REPORT3 کنایه source) ------------------
    kar_payload = json.loads(KAR_JSON.read_text(encoding="utf-8"))
    kar_info = feed.parse_aparat_show(kar_payload)
    check("aparat show Kartoonchi: uid/duration/category/can_download",
          kar_info and kar_info["uid"] == "mtnx5t2"
          and kar_info["duration"] == 12
          and kar_info["category"] == "موسیقی"
          and kar_info["owner"] == "Kartoonchi"
          and kar_info["can_download"] is True
          and (kar_info["file_link"] or "").startswith("https://"),
          f"got {kar_info}")
    check("aparat show Kartoonchi: passes the accept gate",
          bool(kar_info) and feed._aparat_accept(kar_info))
    kar_rel = feed.parse_aparat_related(kar_payload)
    check("aparat related Kartoonchi: 5 UIDs, first kue01r8 6s",
          len(kar_rel) == 5 and kar_rel[0]["uid"] == "kue01r8"
          and kar_rel[0]["duration"] == 6, f"got {len(kar_rel)}")

    # --- T2b/T3b: Aparat Multimedia2020 (real show JSON) ---------------
    mm_payload = json.loads(MM_JSON.read_text(encoding="utf-8"))
    mm_info = feed.parse_aparat_show(mm_payload)
    check("aparat show Multimedia2020: uid/duration/category/can_download",
          mm_info and mm_info["uid"] == "t79ms8g"
          and mm_info["duration"] == 15
          and mm_info["category"] == "موسیقی"
          and mm_info["owner"] == "Multimedia2020"
          and mm_info["can_download"] is True
          and (mm_info["file_link"] or "").startswith("https://"),
          f"got {mm_info}")
    check("aparat show Multimedia2020: passes the accept gate",
          bool(mm_info) and feed._aparat_accept(mm_info))
    mm_rel = feed.parse_aparat_related(mm_payload)
    mm_short = sum(1 for r in mm_rel
                   if r.get("duration") is not None
                   and 10 <= r["duration"] <= 60)
    check("aparat related Multimedia2020: 50 UIDs, 49 short (10-60s)",
          len(mm_rel) == 50 and mm_short == 49,
          f"got {len(mm_rel)} related, {mm_short} short")
    check("aparat accept: 61s and 9s clips rejected by the duration cap",
          not feed._aparat_accept(
              {"uid": "x", "title": "کلیپ استوری فصل پاییز",
               "duration": 61, "category": "موسیقی", "category_en": "music",
               "can_download": True, "file_link": "https://x.mp4"})
          and not feed._aparat_accept(
              {"uid": "x", "title": "کلیپ عاشقانه",
               "duration": 9, "category": "موسیقی", "category_en": "music",
               "can_download": True, "file_link": "https://x.mp4"}))

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
        feed_key="tg:ehsaspaak:9001", source_url=url_ok, title="کلیپ عاشقانه",
        duration_hint=18, source_username="ehsaspaak")
    row = sarch.get_clip_by_unique("feed:tg:ehsaspaak:9001")
    check("intake: valid clip accepted as pending, categorized_by='feed'",
          ok and why == "ok" and row is not None
          and row["status"] == "pending"
          and row["categorized_by"] == "feed"
          and row["mode"] == "resend"
          and row["file_id"] == "" and row["source_url"] == url_ok
          and row["feed_key"] == "tg:ehsaspaak:9001",
          f"got ok={ok} why={why} row={bool(row)}")
    ok, why = await feed.intake_feed_item(
        feed_key="tg:ehsaspaak:9001", source_url=url_ok, title="کلیپ عاشقانه",
        duration_hint=18, source_username="ehsaspaak")
    check("intake: same feed_key twice -> duplicate",
          (not ok) and why == "duplicate", f"got {why}")
    ok, why = await feed.intake_feed_item(
        feed_key="tg:9001", source_url=url_ok, title="کلیپ عاشقانه",
        duration_hint=18, source_username="ehsaspaak")
    check("intake: legacy key tg:<id> dedups against tg:ehsaspaak:<id>",
          (not ok) and why == "duplicate", f"got {why}")
    check("intake: same post_id on another channel is a distinct key",
          not sarch.is_duplicate("feed:tg:SOBHbe:9001")
          and not feed.feed_key_seen("tg:SOBHbe:9001"))
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
    # The auto-approve pass runs INSIDE run_feed_cycle, so install
    # deterministic (unconfident) identify/lyrics fakes FIRST — otherwise
    # the pass would call the real Shazam/lyrics services in the sandbox.
    async def _no_identify(data):
        return ("", "")

    async def _no_lyrics(title, artist):
        return ""

    sarch._identify_audio_bytes = _no_identify
    sarch._fetch_lyrics_text = _no_lyrics

    html_cycle = re.sub(
        r"https://cdn4\.telesco\.pe/file/f2efeb7ace\.mp4\?token=TOKEN",
        "file://" + _media("ehsas_vid1.mp4"), tg_html)
    html_cycle = re.sub(
        r"https://cdn4\.telesco\.pe/file/aff5c09e37\.mp4\?token=TOKEN",
        "file://" + _media("ehsas_vid3.mp4"), html_cycle)

    def tg_fetcher(channel):
        # bootstrap test: only ehsaspaak has video posts this run; the
        # other channels' pages are empty (valid, no error).
        return {"ehsaspaak": html_cycle}.get(channel, "<html></html>")

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
    # incremental re-run: everything deduped, nothing new — except the
    # next THREE rotated seeds (3 seeds/run since 2026-11-06).
    stats2 = await feed.run_feed_cycle(bootstrap=False,
                                       tg_fetcher=tg_fetcher,
                                       aparat_fetcher=ap_fetcher)
    check("cycle incremental: tg_new=0 (dedup holds), ap_new=3 (3 rotated seeds)",
          stats2.get("tg_new") == 0 and stats2.get("ap_new") == 3,
          f"got {stats2}")
    check("cycle incremental: last_new=3 (the 3 rotated seeds)",
          feed.get_feed_status()["last_new"] == 3)
    check("cycle incremental: rotated seed t79ms8g intaken as pending",
          (sarch.get_clip_by_unique("feed:ap:t79ms8g") or {}).get("status")
          == "pending")

    # --- T6 (job errors): raising fetcher -> error, no crash, no intake
    before = len(sarch.pending_clips(50))

    def boom(channel):
        raise RuntimeError("network down")

    stats3 = await feed.run_feed_cycle(bootstrap=False, tg_fetcher=boom,
                                       aparat_fetcher=lambda uid: None)
    tg_err_all = ("ehsaspaak:tg_cycle_failed;SOBHbe:tg_cycle_failed;"
                  "yaldavamusic:tg_cycle_failed;delll_nevveshteh:tg_cycle_failed;"
                  "dosti:tg_cycle_failed")
    check("cycle error: raising fetcher -> per-channel tg_cycle_failed, no crash",
          stats3.get("tg_error") == tg_err_all
          and stats3.get("error") == tg_err_all, f"got {stats3}")
    check("cycle error: no partial intake",
          len(sarch.pending_clips(50)) == before)
    check("cycle error: last_error stored in feed_state",
          feed.get_feed_status()["last_error"] == tg_err_all,
          f"got {feed.get_feed_status()['last_error']!r}")

    def none_fetcher(channel):
        return None

    stats3b = await feed.run_feed_cycle(bootstrap=False,
                                        tg_fetcher=none_fetcher,
                                        aparat_fetcher=lambda uid: None)
    check("cycle error: empty fetch -> per-channel tg_fetch_failed",
          stats3b.get("tg_error")
          == ("ehsaspaak:tg_fetch_failed;SOBHbe:tg_fetch_failed;"
              "yaldavamusic:tg_fetch_failed;delll_nevveshteh:tg_fetch_failed;"
              "dosti:tg_fetch_failed"),
          f"got {stats3b}")

    # --- T5: single-flight ----------------------------------------------
    async def slow_tg(channel):
        await asyncio.sleep(0)
        return html_cycle

    def slow_tg_sync(channel):
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

    # --- T11: multi-channel Telegram cycle ------------------------------
    sob_html = SOBHBE_HTML.read_text(encoding="utf-8")
    sob_html = re.sub(
        r"https://cdn\d*\.telesco\.pe/file/[^\"']+\.mp4\?token=TOKEN",
        "file://" + _media("ehsas_vid1.mp4"), sob_html)
    yal_html = YALDAVA_HTML.read_text(encoding="utf-8")
    yal_html = re.sub(
        r"https://cdn\d*\.telesco\.pe/file/[^\"']+\.mp4\?token=TOKEN",
        "file://" + _media("ehsas_vid3.mp4"), yal_html)
    dell_html = DELL_HTML.read_text(encoding="utf-8")
    dell_html = re.sub(
        r"https://cdn\d*\.telesco\.pe/file/[^\"']+\.mp4\?token=TOKEN",
        "file://" + _media("ehsas_vid1.mp4"), dell_html)
    dosti_html = DOSTI_HTML.read_text(encoding="utf-8")
    dosti_html = re.sub(
        r"https://cdn\d*\.telesco\.pe/file/[^\"']+\.mp4\?token=TOKEN",
        "file://" + _media("ehsas_vid3.mp4"), dosti_html)

    def tg_multi(channel):
        return {"ehsaspaak": html_cycle, "SOBHbe": sob_html,
                "yaldavamusic": yal_html,
                "delll_nevveshteh": dell_html,
                "dosti": dosti_html}.get(channel)

    tg_stats = await feed.run_telegram_cycle(tg_fetcher=tg_multi)
    check("multi-channel TG: tg_new=12 (0 ehsaspaak deduped + 4 SOBHbe + 2 yaldavamusic + 3 delll + 3 dosti)",
          tg_stats.get("new") == 12
          and tg_stats.get("per_channel")
          == {"ehsaspaak": 0, "SOBHbe": 4, "yaldavamusic": 2,
              "delll_nevveshteh": 3, "dosti": 3}
          and tg_stats.get("error") is None, f"got {tg_stats}")
    multi_keys = ["tg:SOBHbe:132203", "tg:SOBHbe:132209",
                  "tg:SOBHbe:132214", "tg:SOBHbe:132219",
                  "tg:yaldavamusic:40", "tg:yaldavamusic:43",
                  "tg:delll_nevveshteh:326", "tg:delll_nevveshteh:328",
                  "tg:delll_nevveshteh:339",
                  "tg:dosti:13109", "tg:dosti:13110", "tg:dosti:13111"]
    multi_rows = [sarch.get_clip_by_unique("feed:" + k) for k in multi_keys]
    check("multi-channel TG: distinct feed_keys, all pending (no auto-accept)",
          all(r is not None and r["status"] == "pending" for r in multi_rows),
          f"got {[bool(r) for r in multi_rows]}")
    check("multi-channel TG: per-channel last_seen stored",
          feed.feed_get("tg_last_seen_sobhbe") == "132219"
          and feed.feed_get("tg_last_seen_yaldavamusic") == "43"
          and feed.feed_get("tg_last_seen_delll_nevveshteh") == "339"
          and feed.feed_get("tg_last_seen_dosti") == "13111"
          and feed.feed_get("tg_last_seen") == "117112",
          f"got sobhbe={feed.feed_get('tg_last_seen_sobhbe')!r} "
          f"yaldavamusic={feed.feed_get('tg_last_seen_yaldavamusic')!r} "
          f"delll={feed.feed_get('tg_last_seen_delll_nevveshteh')!r} "
          f"dosti={feed.feed_get('tg_last_seen_dosti')!r}")

    # --- T12: Aparat seed rotation + per-run call bound ------------------
    feed.feed_set("ap_seed_idx", "0")
    mm_payload = json.loads(MM_JSON.read_text(encoding="utf-8"))

    def ap_rot(uid):
        if uid == "t79ms8g":
            p = copy.deepcopy(mm_payload)
            p["data"]["attributes"]["file_link"] = (
                "file://" + _media("aparat_clip.mp4"))
            return p
        p = copy.deepcopy(ap_base)
        p["data"]["attributes"]["uid"] = uid
        p["data"]["attributes"]["file_link"] = (
            "file://" + _media("aparat_clip.mp4"))
        return p

    seeds = []
    news = []
    for _ in range(7):
        r = await feed.run_aparat_cycle(bootstrap=False,
                                        aparat_fetcher=ap_rot)
        seeds.append(r.get("seeds"))
        news.append(r.get("new"))
    check("rotation: THREE seeds per run, round-robin across 19 seeds",
          seeds == [["faezehradmanesh", "Multimedia2020", "Clip_film2023"],
                    ["Masafinia.m", "Amirabbas.b", "AparatVision"],
                    ["Tafrihi_Best", "Aparat_Media_pars", "Kartoonchi"],
                    ["nasi20", "vidoma", "Sargarmi_kids"],
                    ["Film_Tvs", "Masafinia.m", "toop_media"],
                    ["digikot", "Star_2020", "Mina_diba"],
                    ["Sjadkazmei", "faezehradmanesh", "Multimedia2020"]],
          f"got {seeds}")
    # Run 1 floods to the 15-new cap from the Multimedia2020 graph
    # (13 related; the 3 seeds are already intaken by the incremental
    # cycle test above). Run 2 takes only 2 fresh seeds — xse8if0 was
    # intaken by that same incremental cycle. Runs 3-6 take their 3
    # fresh seeds each (run 6 = the three REPORT4 seeds digikot /
    # Star_2020 / Mina_diba). Run 7 takes Sjadkazmei (the 19th seed)
    # plus 2 wrapped seeds whose clips are already intaken — and 1
    # more fresh clip from the Multimedia2020 graph's remaining
    # related (run 1 hit the 15-new cap before exhausting it).
    check("rotation: new counts per run [15, 2, 3, 3, 3, 3, 2]",
          news == [15, 2, 3, 3, 3, 3, 2], f"got {news}")
    check("rotation: seed index after 7 runs of 3 from 0 -> 2",
          feed.feed_get("ap_seed_idx") == "2",
          f"got {feed.feed_get('ap_seed_idx')!r}")
    check("rotation: each new seed's own clip intaken (REPORT2 + REPORT3 + REPORT4 seeds)",
          all((sarch.get_clip_by_unique("feed:ap:" + u) or {}).get("status")
              == "pending" for u in ("DCBas", "xse8if0", "lfe35b0",
                                     "m569u20", "okm549l", "o214420",
                                     "mtnx5t2", "JcnT6", "cxoia5d",
                                     "ryg56w3", "mwrma4b", "d27o7qv",
                                     "grkx541", "yuq794c", "necos8x",
                                     "n3570i1", "comv1g1")))

    def ap_bound(uid):
        # show rejected by the accept gate (61s) while 49 related
        # candidates keep flowing: the loop must stop on the CALL bound.
        p = copy.deepcopy(mm_payload)
        p["data"]["attributes"]["duration"] = 61
        p["data"]["attributes"]["uid"] = uid
        p["data"]["attributes"]["file_link"] = (
            "file://" + _media("aparat_clip.mp4"))
        return p

    bound = await feed.run_aparat_cycle(bootstrap=False,
                                        aparat_fetcher=ap_bound)
    check("rotation bound: 50 candidates, 0 new -> exactly 24 show calls",
          bound["calls"] == 24 and bound["new"] == 0, f"got {bound}")

    # --- T7: approval fallback without storage + first delivery --------
    # Order 2026-11-07: deliver_feed_first burns the static Waveframe
    # badge + the AVANO MEDIA end bumper (portrait only) in ONE encode
    # (apply_waveframe_badge_and_outro); the old two-step path
    # (apply_waveframe_badge then overlay_outro) is retired.
    # apply_user_badge must NOT be called here.
    import app.bot.motion_watermark as mw
    real_combined = mw.apply_waveframe_badge_and_outro
    real_user = mw.apply_user_badge
    combined_calls = []
    user_calls = []

    def stub_combined(src, out):
        combined_calls.append((src, out))
        shutil.copyfile(src, out)
        return {"style": "stub", "outro": True}

    def guard_user_badge(src, out):
        user_calls.append((src, out))
        raise AssertionError(
            "apply_user_badge must not be called from deliver_feed_first")

    mw.apply_waveframe_badge_and_outro = stub_combined
    mw.apply_user_badge = guard_user_badge
    try:
        r2router = FakeRouter()
        sarch.register_story_archive_handlers(r2router)
        play_h = find_handler(r2router, "startswith:data:sarch:play:")
        check("sarch:play handler registered", play_h is not None)

        clip = sarch.get_clip_by_unique("feed:tg:ehsaspaak:9001")
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
        check("first delivery: static badge+outro applied once via FSInputFile",
              len(combined_calls) == 1 and first_send is not None
              and first_send[2].startswith("FSInputFile:"),
              f"calls={bot.calls}")
        check("first delivery: user badge NOT called (Waveframe only)",
              user_calls == [], f"user_calls={user_calls}")
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
              and len(combined_calls) == 1,
              f"calls={bot.calls[-1:]} combined_calls={len(combined_calls)}")

        # badge failure -> raw URL fallback + file_id capture, then reuse
        def boom_badge(src, out):
            combined_calls.append((src, out))
            raise RuntimeError("ffmpeg down")

        mw.apply_waveframe_badge_and_outro = boom_badge
        clip2 = sarch.get_clip_by_unique("feed:tg:ehsaspaak:117099")
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
        n_badge = len(combined_calls)
        cb4 = FakeCallback(f"sarch:play:{clip2['id']}:love:express:0", bot2)
        await play_h(cb4, cfg, FakeState())
        check("badge failure: next delivery reuses stored raw file_id",
              bot2.calls[-1][2] == "raw-fid-1"
              and len(combined_calls) == n_badge,
              f"calls={bot2.calls[-1:]}")
        check("badge failure: user badge still NOT called",
              user_calls == [], f"user_calls={user_calls}")
    finally:
        mw.apply_waveframe_badge_and_outro = real_combined
        mw.apply_user_badge = real_user

    # --- T8: queue analysis uses source_url (no get_file) ---------------
    async def fake_identify(data):
        return ("تردید", "سامان جلیلی")

    async def fake_lyrics(title, artist):
        # Feed clips have no channel prior (category is '' until the
        # admin picks one), so the lyrics must carry the score alone.
        return TARDID_LYRICS + " عشق دوستت دارم عاشقانه"

    sarch._identify_audio_bytes = fake_identify
    sarch._fetch_lyrics_text = fake_lyrics
    feed_clip = sarch.get_clip_by_unique("feed:tg:ehsaspaak:117112")
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
        " WHERE feed_key='tg:ehsaspaak:117112'")
    conn.commit()
    fresh = feed.parse_tg_page(html_cycle)
    n_ref = feed.refresh_pending_tg_urls(fresh)
    row = sarch.get_clip_by_unique("feed:tg:ehsaspaak:117112")
    check("refresh: stale pending TG url replaced from the fresh page",
          n_ref >= 1 and row["source_url"].startswith("file://"),
          f"n={n_ref} url={row['source_url'][:40]!r}")

    # --- T11: auto-approve pass -------------------------------------------
    async def aa_identify(data):
        return ("تردید", "سامان جلیلی")

    async def aa_lyrics(title, artist):
        return TARDID_LYRICS + " عشق دوستت دارم عاشقانه"

    # 7 fresh pending feed clips (file:// media) for the cap test.
    aa_keys = []
    for i in range(7):
        k = f"tg:aa{i}"
        ok, why = await feed.intake_feed_item(
            feed_key=k, source_url=url_ok, title="کلیپ عاشقانه با متن",
            duration_hint=18, source_username="ehsaspaak")
        check(f"auto-approve setup: intake {k} ok", ok, f"got {why}")
        aa_keys.append(k)
    sarch._identify_audio_bytes = aa_identify
    sarch._fetch_lyrics_text = aa_lyrics
    stats_aa = await feed.run_auto_approve_pass(new_keys=aa_keys)
    check("auto-approve: cap — at most 20 approved in one pass",
          stats_aa.get("checked") == 20
          and 7 <= stats_aa.get("approved", 0) <= 20,
          f"got {stats_aa}")
    aa_rows = [sarch.get_clip_by_unique("feed:" + k) for k in aa_keys]
    ap_rows = [r for r in aa_rows if r and r["status"] == "active"]
    check("auto-approve: new intakes first (all 7 keys approved)",
          [r["feed_key"] for r in ap_rows] == aa_keys,
          f"got {[r['feed_key'] for r in ap_rows]}")
    check("auto-approve: approved rows active + feed-auto + love/express",
          len(ap_rows) == 7 and all(
              r["categorized_by"] == "feed-auto"
              and r["category"] == "love" and r["subcategory"] == "express"
              and (r["cat_score"] or 0) >= 3 for r in ap_rows))
    check("auto-approve: no aa clip left pending",
          sum(1 for r in aa_rows if r and r["status"] == "pending") == 0)
    # Drain the backlog: repeated passes (cap 20 each) until the 7 aa
    # clips and the pre-suggested 117112 are all active. The backlog
    # holds older cycle clips, so the aa clips are NOT necessarily next
    # — the pass works oldest-first. Already-analyzed clips without a
    # suggestion are re-scored from stored data (cheap, no re-download)
    # and may pass the current gate after a gate change.
    max_per_pass = 0
    for _ in range(10):
        done = all(
            (sarch.get_clip_by_unique("feed:" + k) or {}).get("status") == "active"
            for k in aa_keys)
        done = done and (sarch.get_clip_by_unique(
            "feed:tg:ehsaspaak:117112") or {}).get("status") == "active"
        if done:
            break
        st = await feed.run_auto_approve_pass()
        max_per_pass = max(max_per_pass, st.get("approved", 0))
    check("auto-approve: per-pass cap respected while draining",
          max_per_pass <= 20, f"max={max_per_pass}")
    check("auto-approve: all 7 aa clips eventually active",
          all((sarch.get_clip_by_unique("feed:" + k) or {}).get("status") == "active"
              for k in aa_keys))
    check("auto-approve: pre-suggested clip approved as feed-auto",
          (sarch.get_clip_by_unique("feed:tg:ehsaspaak:117112") or {}).get(
              "categorized_by") == "feed-auto")
    # Unconfident: not identified -> stays pending.
    sarch._identify_audio_bytes = _no_identify
    sarch._fetch_lyrics_text = _no_lyrics
    ok, why = await feed.intake_feed_item(
        feed_key="tg:aa-weak", source_url=url_ok, title="کلیپ",
        duration_hint=18, source_username="ehsaspaak")
    check("auto-approve setup: weak intake ok", ok, f"got {why}")
    stats_aa3 = await feed.run_auto_approve_pass(new_keys=["tg:aa-weak"])
    weak = sarch.get_clip_by_unique("feed:tg:aa-weak")
    check("auto-approve: unconfident clip stays pending",
          stats_aa3.get("approved") == 0 and weak
          and weak["status"] == "pending"
          and weak.get("last_error") == "not_identified",
          f"got {stats_aa3} row={weak}")
    # Toggle OFF: nothing auto-approved even with confident fakes.
    sarch._identify_audio_bytes = aa_identify
    sarch._fetch_lyrics_text = aa_lyrics
    ok, why = await feed.intake_feed_item(
        feed_key="tg:aa-off", source_url=url_ok, title="کلیپ عاشقانه با متن",
        duration_hint=18, source_username="ehsaspaak")
    check("auto-approve setup: toggle-off intake ok", ok, f"got {why}")
    feed.feed_set("auto_accept", "0")
    check("auto-approve: get_feed_status reports auto_accept off",
          feed.get_feed_status().get("auto_accept") is False)
    stats_aa4 = await feed.run_auto_approve_pass(new_keys=["tg:aa-off"])
    off_row = sarch.get_clip_by_unique("feed:tg:aa-off")
    check("auto-approve: toggle OFF -> nothing approved",
          stats_aa4.get("enabled") is False and stats_aa4.get("approved") == 0
          and off_row and off_row["status"] == "pending",
          f"got {stats_aa4}")
    feed.feed_set("auto_accept", "1")
    check("auto-approve: get_feed_status reports auto_accept on",
          feed.get_feed_status().get("auto_accept") is True)
    # No bot call for feed clips: StrictBot raises on ANY bot call, yet
    # analysis succeeds with the confident fakes.
    strict = StrictBot()
    await sarch.analyze_clip_for_queue(strict, off_row)
    off_row2 = sarch.get_clip(off_row["id"])
    check("auto-approve: feed analysis never calls the bot",
          off_row2.get("sugg_category") == "love"
          and off_row2.get("sugg_subcategory") == "express",
          f"got sugg={off_row2.get('sugg_category')}")
    # Source gone: a fresh unanalyzed clip stays pending, no crash.
    ok, why = await feed.intake_feed_item(
        feed_key="tg:aa-gone", source_url=url_ok, title="کلیپ عاشقانه با متن",
        duration_hint=18, source_username="ehsaspaak")
    check("auto-approve setup: gone intake ok", ok, f"got {why}")
    gone_row = sarch.get_clip_by_unique("feed:tg:aa-gone")
    conn.execute(
        "UPDATE story_archive SET source_url='file:///tmp/does-not-exist.mp4'"
        " WHERE id=?", (gone_row["id"],))
    conn.commit()
    stats_aa5 = await feed.run_auto_approve_pass(new_keys=["tg:aa-gone"])
    gone2 = sarch.get_clip(gone_row["id"])
    check("auto-approve: dead source stays pending, no crash",
          gone2 and gone2["status"] == "pending"
          and gone2.get("last_error") == "source_gone"
          and stats_aa5.get("approved", 0) <= 10,
          f"got {stats_aa5} err={gone2.get('last_error') if gone2 else None}")
    # Toggle handler: sarch:autoacc flips the flag + re-renders the panel.
    autoacc_h = find_handler(r2router, "eq:data=sarch:autoacc")
    check("auto-approve: sarch:autoacc handler registered", autoacc_h is not None)
    if autoacc_h is not None:
        cb = FakeCallback("sarch:autoacc", StrictBot())
        await autoacc_h(cb, cfg, FakeState())
        check("auto-approve: toggle handler flips auto_accept off",
              feed.feed_get("auto_accept", "1") == "0")
        btns = [b for _t, kb in (cb.message.edits + cb.message.answers)
                if kb is not None for b in kb_buttons(kb)]
        check("auto-approve: panel re-renders with خاموش",
              any("خاموش" in (t or "") for t, _c in btns), f"btns={btns}")
        cb2 = FakeCallback("sarch:autoacc", StrictBot())
        await autoacc_h(cb2, cfg, FakeState())
        check("auto-approve: toggle handler flips auto_accept back on",
              feed.feed_get("auto_accept", "1") == "1")
        btns2 = [b for _t, kb in (cb2.message.edits + cb2.message.answers)
                 if kb is not None for b in kb_buttons(kb)]
        check("auto-approve: panel re-renders with روشن",
              any("روشن" in (t or "") for t, _c in btns2), f"btns={btns2}")

    # --- T12: corporate_caption never shows a source line (2026-11-06) ---
    cap_with_src = sarch.corporate_caption(
        {"duration": 44, "source_username": "Clip_film2023"})
    cap_without_src = sarch.corporate_caption({"duration": 44})
    cap_expected = ("🎬 AVANO MEDIA\n"
                    "✨ منتخب استوری آوانو\n\n"
                    "⏱ مدت: 00:44\n\n"
                    "@AvanoMediaBot")
    check("caption: with source_username -> NO منبع line, template intact",
          cap_with_src == cap_expected and "منبع" not in cap_with_src
          and "Clip_film2023" not in cap_with_src,
          f"got {cap_with_src!r}")
    check("caption: without source -> identical output",
          cap_without_src == cap_expected, f"got {cap_without_src!r}")
    check("caption: duration formats MM:SS zero-padded",
          sarch.corporate_caption({"duration": 125}).split("مدت: ")[1][:5]
          == "02:05")

    # --- T13: final order 2026-11-06 — gate, title scoring, re-score,
    # chooser label, outro -------------------------------------------
    # Gate stays strict: a clear winner (margin >= 2) passes, a near-tie
    # (margin 1) and a tie (margin 0) stay queued — the margin was NOT
    # loosened, because the near-tie case (breakup 5 vs lonely 4) is
    # genuinely ambiguous. The drain blockers were the candidate filter,
    # the missing title signal, and the 5/cycle cap — all fixed below.
    c, s, sc, _r = sarch.suggest_subcategory(
        "عشق دوستت دارم عاشقانه", "", "", "")
    check("gate: clear winner (best>=3, margin>=2) -> suggestion",
          (c, s) == ("love", "express") and sc >= 3,
          f"got {(c, s, sc)}")
    c1, s1, sc1, _r1 = sarch.suggest_subcategory(
        "عشق دلتنگی", "عشق", "", "")
    check("gate: near-tie (best=3, margin=1) -> no suggestion",
          (c1, s1) == (None, None) and sc1 == 3, f"got {(c1, s1, sc1)}")
    c0, s0, sc0, _r0 = sarch.suggest_subcategory(
        "عشق دوستت دارم دلتنگی", "دلتنگی دلم تنگ", "", "")
    check("gate: best=4 margin=0 -> still no suggestion",
          (c0, s0) == (None, None) and sc0 == 4, f"got {(c0, s0, sc0)}")
    # Morning greetings (user report 2026-11-07): a "صبح بخیر" clip
    # with a romantic background song must NEVER auto-suggest love —
    # the greeting text is the theme, the song is incidental. With no
    # seed it stays queued; with a verified occasion/calendar seed the
    # unanimous title decides occasion/calendar.
    g, gs, gsc, _rg = sarch.suggest_subcategory(
        "عشق دوستت دارم عاشقانه", "صبح بخیر عزیزم", "love", "express")
    check("greeting: 'صبح بخیر' + romantic song, no seed -> no suggestion",
          (g, gs) == (None, None), f"got {(g, gs, gsc)}")
    g2, gs2, gsc2, _rg2 = sarch.suggest_subcategory(
        "عشق دوستت دارم عاشقانه", "صبح بخیر عزیزم", "love", "express",
        "occasion", "calendar")
    check("greeting: same + occasion/calendar seed -> occasion/calendar",
          (g2, gs2) == ("occasion", "calendar"), f"got {(g2, gs2, gsc2)}")
    # Title scoring: a thematic song title alone can carry the score.
    async def _title_identify(data):
        return ("تولدت مبارک", "خواننده")

    async def _empty_lyrics(title, artist):
        return ""

    sarch._identify_audio_bytes = _title_identify
    sarch._fetch_lyrics_text = _empty_lyrics
    ok, why = await feed.intake_feed_item(
        feed_key="tg:aa-title", source_url=url_ok, title="کلیپ",
        duration_hint=18, source_username="ehsaspaak")
    check("title scoring setup: intake ok", ok, f"got {why}")
    trow = sarch.get_clip_by_unique("feed:tg:aa-title")
    await sarch.analyze_clip_for_queue(None, trow)
    trow = sarch.get_clip(trow["id"])
    check("title scoring: song title 'تولدت مبارک' suggests occasion/birthday",
          (trow.get("sugg_category"), trow.get("sugg_subcategory"))
          == ("occasion", "birthday"),
          f"got {trow.get('sugg_category')}/{trow.get('sugg_subcategory')}")
    # Re-score from stored data (old-gate state, no re-download).
    ok, why = await feed.intake_feed_item(
        feed_key="tg:aa-rescore", source_url=url_ok, title="کلیپ",
        duration_hint=18, source_username="ehsaspaak")
    check("rescore setup: intake ok", ok, f"got {why}")
    rrow = sarch.get_clip_by_unique("feed:tg:aa-rescore")
    sarch.save_suggestion(rrow["id"], title="تولدت مبارک", artist="خواننده",
                          lyrics_excerpt="", sugg_category="",
                          sugg_subcategory="", score=2,
                          reasons="کپشن: تولد", error="not_identified")
    check("rescore: old-gate clip gains a suggestion from stored data",
          sarch.rescore_clip_from_stored(rrow["id"]) is True)
    rrow = sarch.get_clip(rrow["id"])
    check("rescore: suggestion is occasion/birthday, error cleared",
          (rrow.get("sugg_category"), rrow.get("sugg_subcategory"))
          == ("occasion", "birthday") and rrow.get("last_error") == "",
          f"got {rrow.get('sugg_category')}/{rrow.get('sugg_subcategory')} "
          f"err={rrow.get('last_error')!r}")
    # Dead source is never re-scored into an approval.
    ok, why = await feed.intake_feed_item(
        feed_key="tg:aa-rescore-gone", source_url=url_ok,
        title="کلیپ عاشقانه با متن", duration_hint=18,
        source_username="ehsaspaak")
    check("rescore-gone setup: intake ok", ok, f"got {why}")
    grow = sarch.get_clip_by_unique("feed:tg:aa-rescore-gone")
    sarch.save_suggestion(grow["id"], title="", artist="",
                          lyrics_excerpt="", sugg_category="",
                          sugg_subcategory="", score=0, reasons="",
                          error="source_gone")
    check("rescore: source_gone clip stays without suggestion",
          sarch.rescore_clip_from_stored(grow["id"]) is False)
    grow = sarch.get_clip(grow["id"])
    check("rescore: source_gone clip still has no suggestion",
          not grow.get("sugg_category") and not grow.get("sugg_subcategory"))
    # Chooser label: «استوک خارجی» is gone.
    kb = sarch.iranian_chooser_kb()
    btn_texts = [t for t, _c in kb_buttons(kb)]
    check("chooser: international button is «کلیپ‌های بین‌المللی»",
          "🌍 کلیپ‌های بین‌المللی" in btn_texts, f"got {btn_texts}")
    check("chooser: no «استوک خارجی» anywhere",
          not any("استوک" in t for t in btn_texts))
    # Outro: real overlay_outro on generated fixtures.
    def _frame_mean(path, t):
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", str(t), "-i", path,
             "-frames:v", "1", "-vf", "scale=8:8,format=gray",
             "-f", "rawvideo", "-"], capture_output=True)
        px = r.stdout
        return sum(px) / len(px) if px else -1

    def _has_audio(path):
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True)
        return bool(r.stdout.strip())

    p_src = os.path.join(tmpd, "portrait.mp4")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=360x640:rate=15:duration=12",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", p_src], check=True)
    p_out = os.path.join(tmpd, "portrait_out.mp4")
    res_o = mw.overlay_outro(p_src, p_out)
    check("outro: portrait clip gets the bumper applied",
          res_o.get("applied") is True, f"got {res_o}")
    check("outro: duration unchanged (±0.3s)",
          abs(res_o.get("duration", 0) - 12) < 0.3, f"got {res_o}")
    check("outro: audio preserved", _has_audio(p_out))
    m_in_last = _frame_mean(p_src, 11)
    m_out_last = _frame_mean(p_out, 11)
    m_in_mid = _frame_mean(p_src, 8)
    m_out_mid = _frame_mean(p_out, 8)
    check("outro: last 2.5s show the dark bumper, mid-clip unchanged",
          m_out_last < 50 and m_in_last > 80
          and abs(m_out_mid - m_in_mid) < 20,
          f"got last in={m_in_last:.0f} out={m_out_last:.0f} "
          f"mid in={m_in_mid:.0f} out={m_out_mid:.0f}")
    l_src = os.path.join(tmpd, "landscape.mp4")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=12",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", l_src], check=True)
    l_out = os.path.join(tmpd, "landscape_out.mp4")
    res_l = mw.overlay_outro(l_src, l_out)
    check("outro: landscape clip is copied unchanged",
          res_l.get("applied") is False
          and res_l.get("reason") == "not_portrait"
          and open(l_src, "rb").read() == open(l_out, "rb").read(),
          f"got {res_l}")

    shutil.rmtree(tmpd, ignore_errors=True)
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
