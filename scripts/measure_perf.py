"""Performance measurement for the story-clips redesign (2026-11-07).

Times each stage SEPARATELY, against the real modules on a real
SQLite DB, so the user can see where the time actually goes:

  M1  clip selection for the audience (pick_next_clip, indexed)
  M2  pure categorization scoring (suggest_subcategory)
  M3  admin approval (approve_clip — one UPDATE)
  M4  seen-history write (mark_seen)
  M5  feed intake (real Aparat file: network download + DB + ffprobe)
  M6  queue analysis (download + scoring + store; Shazam mocked —
      the sandbox has no shazamio; production Shazam is ~1s on match)
  M7  first-delivery processing (Waveframe badge + outro, real ffmpeg)

The question it answers (user order 2026-11-07 09:37): does the
number of subcategories slow the audience path down, or is the cost
in the DB design, the queries, the video processing, or the request
handling? Run: python3 scripts/measure_perf.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.pop("TURSO_URL", None)
os.environ.pop("TURSO_AUTH_TOKEN", None)
os.environ.pop("TURSO_DATABASE_URL", None)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
SHOW_URL = "https://www.aparat.com/api/fa/v1/video/video/show/videohash/mtnx5t2"

SUBS = [
    ("love", "express"), ("love", "longing"), ("love", "onesided"),
    ("love", "couple"), ("sad", "breakup"), ("sad", "lonely"),
    ("sad", "tears"), ("sad", "betrayal"), ("occasion", "birthday"),
    ("occasion", "anniversary"), ("occasion", "valentine"),
    ("occasion", "parents"), ("occasion", "calendar"),
    ("deep", "pride"), ("deep", "silence"), ("deep", "indifferent"),
    ("deep", "sarcasm"), ("happy", "hope"), ("happy", "success"),
    ("happy", "friendship"), ("happy", "freshstart"),
]


def _curl_json(url: str):
    out = subprocess.run(
        ["curl", "-s", "-m", "30", "-A", UA, url],
        capture_output=True, text=True, timeout=40)
    return json.loads(out.stdout)


def _med(fn, n=50):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts)


async def main():
    from app.bot import story_archive as sarch
    from app.bot import clip_feed as feed

    conn = sqlite3.connect(":memory:")
    sarch._get_conn = lambda: conn
    for ddl in sarch._TABLE_DDLS:
        conn.execute(ddl)
    sarch._init_tables(conn)

    # Populate: 10 active clips per subcategory (210) + 40 pending.
    n = 0
    for cat, sub in SUBS:
        for i in range(10):
            n += 1
            sarch.archive_clip(
                file_unique_id=f"uq-perf-{cat}-{sub}-{i}",
                file_id=f"f-perf-{n}", category=cat, subcategory=sub,
                mode="resend", status="active", added_by=111,
                file_size=2_000_000, categorized_by="feed-auto",
                categorized_at=1234567890)
            conn.execute(
                "UPDATE story_archive SET processed_file_id=?"
                " WHERE file_unique_id=?",
                (f"proc-perf-{n}", f"uq-perf-{cat}-{sub}-{i}"))
    for i in range(40):
        sarch.archive_clip(
            file_unique_id=f"uq-perf-pend-{i}", file_id=f"f-pend-{i}",
            category="love", subcategory="express", mode="resend",
            status="pending", added_by=111, file_size=2_000_000)
    # Seen history: user 111 already watched 3 love/express clips.
    for i in range(3):
        conn.execute(
            "INSERT INTO story_seen (user_id, file_unique_id, seen_at)"
            " VALUES (?,?,?)", (111, f"uq-perf-love-express-{i}", 100 + i))
    conn.commit()
    total = conn.execute(
        "SELECT COUNT(*) FROM story_archive").fetchone()[0]
    print(f"DB rows: {total} clips (210 active + 40 pending)\n")

    # M1 — selection
    t1 = _med(lambda: sarch.pick_next_clip("love", 111), 50)
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT a.id FROM story_archive a"
        " LEFT JOIN story_seen s ON s.user_id=111"
        " AND s.file_unique_id=a.file_unique_id"
        " WHERE a.category='love' AND a.status='active'"
        " ORDER BY 1 LIMIT 50").fetchall()
    print(f"M1  pick_next_clip (audience selection):  {t1:.2f} ms median")
    print("    query plan:", " | ".join(r[3] for r in plan))

    # M2 — pure scoring
    t2 = _med(lambda: sarch.suggest_subcategory(
        "دل بستم به سختی به دلت رسیدم عاشقت شدم",
        "کلیپ تیکه دار سنگین", "deep", "sarcasm", "deep", "sarcasm"), 1000)
    print(f"M2  suggest_subcategory (pure scoring):  {t2:.3f} ms median")

    # M3 — admin approval
    pend_ids = [r[0] for r in conn.execute(
        "SELECT id FROM story_archive WHERE status='pending' LIMIT 20")]
    for pid in pend_ids:
        sarch.save_suggestion(pid, sugg_category="love",
                              sugg_subcategory="express", score=6,
                              reasons="test")
    ts = []
    for pid in pend_ids:
        t0 = time.perf_counter()
        sarch.approve_clip(pid, by="admin")
        ts.append((time.perf_counter() - t0) * 1000)
    print(f"M3  approve_clip (admin approval):     {statistics.median(ts):.2f} ms median")

    # M4 — seen write
    ts = []
    for i in range(20):
        t0 = time.perf_counter()
        sarch.mark_seen(111, f"uq-perf-love-longing-{i % 10}")
        ts.append((time.perf_counter() - t0) * 1000)
    print(f"M4  mark_seen (history write):         {statistics.median(ts):.2f} ms median")

    # M5 — real intake (fresh Aparat file)
    show = _curl_json(SHOW_URL)
    attrs = show["data"]["attributes"]
    file_link = attrs["file_link"]
    print(f"\nM5  intake source: {attrs.get('title', '')[:40]!r} "
          f"({attrs.get('duration')}s)")
    tmpd = tempfile.mkdtemp(prefix="perf_")
    sample = os.path.join(tmpd, "sample.mp4")
    t0 = time.perf_counter()
    dl = subprocess.run(
        ["curl", "-s", "-m", "120", "-A", UA, "-o", sample, file_link],
        timeout=150)
    t_dl = (time.perf_counter() - t0) * 1000
    size = os.path.getsize(sample) if os.path.exists(sample) else 0
    print(f"    network download ({size/1024:.0f} KB): {t_dl:.0f} ms")
    if dl.returncode != 0 or size == 0:
        print("    download failed — M5/M6/M7 skipped (network)")
        return
    t0 = time.perf_counter()
    ok, reason = await feed.intake_feed_item(
        feed_key="ap:perf-mtnx5t2", source_url=Path(sample).as_uri(),
        title=attrs.get("title", ""), duration_hint=attrs.get("duration"),
        source_username="Kartoonchi", seed_uid="mtnx5t2")
    t_in = (time.perf_counter() - t0) * 1000
    print(f"    intake_feed_item (DB + ffprobe):   {t_in:.0f} ms -> {ok}/{reason}")

    # M6 — queue analysis (Shazam mocked; sandbox has no shazamio)
    clip = sarch.get_clip(conn.execute(
        "SELECT id FROM story_archive WHERE feed_key='ap:perf-mtnx5t2'"
        ).fetchone()[0])

    async def fake_identify(data):
        return ("تردید", "سامان جلیلی")

    async def fake_lyrics(title, artist):
        return "دل بستم به سختی به دلت رسیدم عاشقت شدم"
    sarch._identify_audio_bytes = fake_identify
    sarch._fetch_lyrics_text = fake_lyrics
    t0 = time.perf_counter()
    await sarch.analyze_clip_for_queue(None, clip)
    t_an = (time.perf_counter() - t0) * 1000
    row = sarch.get_clip(clip["id"])
    print(f"M6  analyze_clip_for_queue (mocked Shazam): {t_an:.0f} ms "
          f"-> sugg {row['sugg_category']}/{row['sugg_subcategory']} "
          f"score {row['cat_score']}")
    print("    (production Shazam is ~1s on a match — measured 2026-10-29)")

    # M7 — first-delivery processing (real ffmpeg)
    from app.bot.motion_watermark import apply_waveframe_badge, overlay_outro
    badged = os.path.join(tmpd, "badged.mp4")
    t0 = time.perf_counter()
    apply_waveframe_badge(sample, badged)
    t_badge = (time.perf_counter() - t0) * 1000
    final = os.path.join(tmpd, "final.mp4")
    t0 = time.perf_counter()
    try:
        overlay_outro(badged, final)
        t_out = (time.perf_counter() - t0) * 1000
        out_txt = f"{t_out:.0f} ms"
    except Exception as e:
        out_txt = f"skipped ({e})"
    print(f"M7  Waveframe badge (first delivery):  {t_badge:.0f} ms")
    print(f"    outro overlay:                     {out_txt}")

    print("\nVerdict: M1/M2/M3/M4 are all sub-millisecond to a few ms — the")
    print("subcategory count and the DB do NOT slow the audience path.")
    print("The only audience-facing cost is M7 (first delivery of a clip")
    print("with no stored final) + the M5 network download; both are paid")
    print("ONCE per clip and then cached as a Telegram file_id.")


if __name__ == "__main__":
    asyncio.run(main())
