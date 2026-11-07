"""Sandbox tests for the exclusive Avano song bank (exclusive_bank.py).

Pure-layer tests only (no aiogram, no Turso): fingerprint matching on
real music, real-bitrate probing, DB CRUD/dedup/stats, delivery card.
Fixtures are generated on the fly from the reference clip (song A,
«تردید») and a real Aparat clip (song B) — nothing is committed.

Run: python3 scripts/verify_exclusive.py
Exit 0 = all checks pass.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bot import exclusive_bank as xb

REF_CLIP = os.path.expanduser(
    "~/workspace/user/media_library/video/53/"
    "53e2091f582cf40588ff9687ac70321fff63ce8795884a99481f76b44799956a.mp4")
SONG_B = os.path.expanduser(
    "~/workspace/clip_source_hunt/vid4/aparat_baranklip_valentine_necos8x.mp4")

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {extra}")


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def main():
    tmp = tempfile.mkdtemp(prefix="xb_test_")
    print(f"fixtures in {tmp}")
    # --- fixtures: real music, real encodes ---
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", REF_CLIP, "-vn",
         "-ac", "2", "-ar", "44100", f"{tmp}/A_full.wav"])
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", SONG_B, "-vn",
         "-ac", "2", "-ar", "44100", f"{tmp}/B_full.wav"])
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", f"{tmp}/A_full.wav",
         "-b:a", "320k", f"{tmp}/A_320.mp3"])
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", f"{tmp}/A_full.wav",
         "-b:a", "128k", f"{tmp}/A_128.mp3"])
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "5", "-t", "5",
         "-i", f"{tmp}/A_full.wav", f"{tmp}/A_5s.wav"])
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "12", "-t", "6",
         "-i", f"{tmp}/A_full.wav", f"{tmp}/A_6s.wav"])
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "5", "-t", "5",
         "-i", f"{tmp}/A_full.wav", "-b:a", "128k", f"{tmp}/A_5s_128k.mp3"])
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "4", "-t", "6",
         "-i", f"{tmp}/B_full.wav", f"{tmp}/B_6s.wav"])
    # T6 fixtures: short middle excerpts, bot-style mono 22050 WAVs.
    for dur, off in ((2, 8), (3, 8), (4, 8)):
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(off),
             "-t", str(dur), "-i", f"{tmp}/A_full.wav", "-ac", "1",
             "-ar", "22050", f"{tmp}/A_mid_{dur}s.wav"])
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", f"{tmp}/A_mid_4s.wav",
         "-c:a", "libopus", "-b:a", "32k", f"{tmp}/A_mid_4s_opus.opus"])
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", f"{tmp}/A_mid_4s_opus.opus",
         "-ac", "1", "-ar", "22050", f"{tmp}/A_mid_4s_opus.wav"])
    for dur in (3, 4, 5, 8, 15):
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "2", "-t", str(dur),
             "-i", f"{tmp}/B_full.wav", "-ac", "1", "-ar", "22050",
             f"{tmp}/B_{dur}s.wav"])

    print("T1 fingerprint matching (real music, vendored fpcalc)")
    check("fpcalc binary present + executable",
          os.path.isfile(xb._fpcalc_path())
          and os.access(xb._fpcalc_path(), os.X_OK))
    dur_a, fp_a = xb.fingerprint_file(f"{tmp}/A_320.mp3")
    dur_b, fp_b = xb.fingerprint_file(f"{tmp}/B_full.wav")
    check("full song A fingerprints", fp_a is not None and len(fp_a) > 50,
          f"items={len(fp_a) if fp_a else 0}")
    check("full song B fingerprints", fp_b is not None and len(fp_b) > 50)
    _d, q5 = xb.fingerprint_file(f"{tmp}/A_5s.wav")
    _d, q6 = xb.fingerprint_file(f"{tmp}/A_6s.wav")
    _d, q128 = xb.fingerprint_file(f"{tmp}/A_5s_128k.mp3")
    _d, qB = xb.fingerprint_file(f"{tmp}/B_6s.wav")
    song_a = {"id": 1, "title": "تردید", "artist": "سامان جلیلی",
              "fingerprint": ",".join(map(str, fp_a))}
    song_b = {"id": 2, "title": "ولنتاین", "artist": "باران کلیپ",
              "fingerprint": ",".join(map(str, fp_b))}
    m, sim = xb.match_against(q5, [song_a, song_b])
    check("5s excerpt matches song A", m is not None and m["id"] == 1,
          f"sim={sim:.3f}")
    check("5s excerpt similarity >= 0.75", sim >= 0.75, f"sim={sim:.3f}")
    m, sim = xb.match_against(q6, [song_a, song_b])
    check("6s excerpt (other position) matches song A",
          m is not None and m["id"] == 1, f"sim={sim:.3f}")
    m, sim = xb.match_against(q128, [song_a, song_b])
    check("5s excerpt re-encoded at 128k still matches A",
          m is not None and m["id"] == 1, f"sim={sim:.3f}")
    m, sim = xb.match_against(qB, [song_a, song_b])
    check("B excerpt matches song B (not A)",
          m is not None and m["id"] == 2, f"sim={sim:.3f}")
    m, sim = xb.match_against(qB, [song_a])
    check("B excerpt does NOT match song A", m is None, f"sim={sim:.3f}")
    m, sim = xb.match_against(q5, [song_b])
    check("A excerpt does NOT match song B", m is None, f"sim={sim:.3f}")
    m, sim = xb.match_against(fp_a[:2], [song_a])
    check("too-short query (<3 items) never matches", m is None)
    m, sim = xb.match_against([], [song_a])
    check("empty query never matches", m is None)

    print("T2 real-bitrate probe (ffprobe)")
    check("320k file probes 320", xb.probe_bitrate(f"{tmp}/A_320.mp3") == 320)
    check("128k file probes 128", xb.probe_bitrate(f"{tmp}/A_128.mp3") == 128)
    with open(f"{tmp}/A_320.mp3", "rb") as f:
        head = f.read(100000)
    with open(f"{tmp}/A_320_head.mp3", "wb") as f:
        f.write(head)
    check("truncated head still probes the true 320",
          xb.probe_bitrate(f"{tmp}/A_320_head.mp3") == 320)
    info = xb.analyse_audio_bytes(open(f"{tmp}/A_320.mp3", "rb").read())
    check("analyse_audio_bytes: bitrate 320 + fingerprint",
          info["bitrate_kbps"] == 320 and len(info["fp_items"].split(",")) > 50)

    print("T3 DB: CRUD, dedup, toggle, stats (in-memory sqlite)")
    conn = sqlite3.connect(":memory:")
    sid_a = xb.add_song(conn, title="تردید", artist="سامان جلیلی",
                        file_id="fileA", file_unique_id="uqA",
                        duration_sec=dur_a or 18.0, bitrate_kbps=320,
                        file_size=735652, fingerprint=",".join(map(str, fp_a)),
                        fp_duration=dur_a or 0, fp_partial=False,
                        description="آهنگ آزمایشی", added_by=572595158)
    sid_b = xb.add_song(conn, title="ولنتاین", artist="باران کلیپ",
                        file_id="fileB", file_unique_id="uqB",
                        duration_sec=dur_b or 21.0, bitrate_kbps=192,
                        file_size=900000, fingerprint=",".join(map(str, fp_b)),
                        fp_duration=dur_b or 0, fp_partial=False)
    check("two songs added", sid_a and sid_b and sid_a != sid_b)
    check("list returns 2", len(xb.list_songs(conn)) == 2)
    got = xb.find_by_title_artist(conn, "تردید", "سامان جلیلی")
    check("find by exact title/artist", got is not None and got["id"] == sid_a)
    got = xb.find_by_title_artist(conn, "  تردید  ", "سامان جلیلی")
    check("find tolerates surrounding spaces", got is not None)
    dup, reason = xb.find_duplicate(conn, "تردید", "سامان جلیلی", fp_a)
    check("duplicate by title", dup is not None and reason == "title")
    dup, reason = xb.find_duplicate(conn, "نام دیگر", "خواننده دیگر", fp_a)
    check("duplicate by audio fingerprint", dup is not None and reason == "audio")
    dup, reason = xb.find_duplicate(conn, "نام دیگر", "خواننده دیگر", fp_b)
    check("song B fingerprint is not a dup of A... (it IS B itself)",
          dup is not None and dup["id"] == sid_b)
    _d, q128full = xb.fingerprint_file(f"{tmp}/A_128.mp3")
    dup, reason = xb.find_duplicate(conn, "تازه", "تازه‌تر", q128full)
    check("full re-encode (128k) of A IS flagged as A's audio",
          dup is not None and dup["id"] == sid_a, f"reason={reason}")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=5", f"{tmp}/tone.wav"])
    _d, q_tone = xb.fingerprint_file(f"{tmp}/tone.wav")
    dup, reason = xb.find_duplicate(conn, "تازه", "تازه‌تر", q_tone)
    check("unrelated audio does not false-dup", dup is None,
          f"reason={reason}")
    xb.set_active(conn, sid_a, False)
    check("deactivated song is not findable",
          xb.find_by_title_artist(conn, "تردید", "سامان جلیلی") is None)
    check("deactivated song stays listed (inactive)",
          len(xb.list_songs(conn)) == 2
          and len(xb.list_songs(conn, active_only=True)) == 1)
    xb.set_active(conn, sid_a, True)
    xb.inc_identify(conn, sid_a)
    xb.inc_identify(conn, sid_a)
    xb.inc_download(conn, sid_a)
    got = xb.get_song(conn, sid_a)
    check("stats counted (identify 2, download 1)",
          got["identify_count"] == 2 and got["download_count"] == 1)
    xb.update_song(conn, sid_a, title="تردید (نسخه آزمایشی)")
    check("edit title works",
          xb.get_song(conn, sid_a)["title"] == "تردید (نسخه آزمایشی)")
    xb.update_song(conn, sid_a, title="تردید")
    xb.delete_song(conn, sid_b)
    check("delete works", len(xb.list_songs(conn)) == 1
          and xb.get_song(conn, sid_b) is None)

    print("T4 match_sample end-to-end (DB + fingerprint)")
    m, sim = xb.match_sample(conn, q5)
    check("match_sample finds A from a 5s excerpt",
          m is not None and m["title"] == "تردید", f"sim={sim:.3f}")
    m, sim = xb.match_sample(conn, qB)
    check("match_sample: B excerpt finds nothing after B deleted",
          m is None, f"sim={sim:.3f}")
    xb.set_active(conn, sid_a, False)
    m, sim = xb.match_sample(conn, q5)
    check("match_sample skips inactive songs", m is None)
    xb.set_active(conn, sid_a, True)

    print("T5 delivery card (exact format, measured bitrate)")
    card = xb.exclusive_card_text(
        {"title": "تردید", "artist": "سامان جلیلی", "bitrate_kbps": 320})
    check("card is the specified format + bot id",
          card == ("🎵 آهنگ در بانک اختصاصی آوانو شناسایی شد!\n\n"
                   "🎤 سامان جلیلی — تردید\n"
                   "💎 نسخه کامل اختصاصی آوانو\n"
                   "🎧 کیفیت واقعی: 320kbps\n\n@AvanoMediaBot"),
          repr(card))
    card192 = xb.exclusive_card_text(
        {"title": "X", "artist": "Y", "bitrate_kbps": 192})
    check("non-320 file shows its REAL bitrate, never 320",
          "192kbps" in card192 and "320" not in card192, repr(card192))

    print("T6 tiered gate: short samples match, other songs never do")
    _d, q3 = xb.fingerprint_file(f"{tmp}/A_mid_3s.wav")
    _d, q4 = xb.fingerprint_file(f"{tmp}/A_mid_4s.wav")
    _d, q2 = xb.fingerprint_file(f"{tmp}/A_mid_2s.wav")
    q2 = q2 or []  # fingerprint_file returns None when nothing is emitted
    _d, q4opus = xb.fingerprint_file(f"{tmp}/A_mid_4s_opus.wav")
    check("3s middle excerpt has only a few items (fpcalc warm-up)",
          0 < len(q3) <= 7, f"items={len(q3)}")
    m, sim = xb.match_against(q3, [song_a, song_b])
    check("3s middle excerpt matches song A (tier sim>=0.95)",
          m is not None and m["id"] == 1, f"sim={sim:.3f}")
    m, sim = xb.match_against(q4, [song_a, song_b])
    check("4s middle excerpt matches song A (tier sim>=0.90)",
          m is not None and m["id"] == 1, f"sim={sim:.3f}")
    m, sim = xb.match_against(q4opus, [song_a, song_b])
    check("4s voice-like Opus excerpt matches song A",
          m is not None and m["id"] == 1, f"sim={sim:.3f}")
    check("2s excerpt yields no usable fingerprint",
          len(q2) < 3, f"items={len(q2)}")
    m, sim = xb.match_against(q2, [song_a, song_b])
    check("2s excerpt honestly never matches", m is None)
    for dur in (3, 4, 5, 8, 15):
        _d, qf = xb.fingerprint_file(f"{tmp}/B_{dur}s.wav")
        m, sim = xb.match_against(qf, [song_a])
        check(f"other song {dur}s excerpt does NOT match A",
              m is None, f"sim={sim:.3f}")

    print("T6b audit + fingerprint repair (in-memory sqlite)")
    conn2 = sqlite3.connect(":memory:")
    sid_ok = xb.add_song(conn2, title="تردید", artist="سامان جلیلی",
                         file_id="fileA", file_unique_id="uqA",
                         duration_sec=18.0, bitrate_kbps=320, file_size=735652,
                         fingerprint=",".join(map(str, fp_a)),
                         fp_duration=dur_a or 0, fp_partial=False)
    sid_nofp = xb.add_song(conn2, title="بی‌اثر", artist="نامشخص",
                           file_id="fileX", file_unique_id="uqX",
                           duration_sec=200.0, bitrate_kbps=320,
                           file_size=8000000,
                           fingerprint="", fp_duration=0, fp_partial=False)
    rows = xb.audit_bank(conn2)
    by_id = {r["id"]: r for r in rows}
    check("audit flags the fingerprint-less song",
          by_id[sid_nofp]["status"] == "no_fingerprint")
    check("audit passes the healthy song",
          by_id[sid_ok]["status"] == "ok")
    ok = xb.update_song_fingerprint(conn2, sid_nofp,
                                    ",".join(map(str, fp_b)), 21.0)
    check("repair stores a fingerprint", ok)
    rows = xb.audit_bank(conn2)
    by_id = {r["id"]: r for r in rows}
    check("audit passes the repaired song",
          by_id[sid_nofp]["status"] == "ok")
    m, sim = xb.match_sample(conn2, q5)
    check("match_sample still finds A after repair",
          m is not None and m["title"] == "تردید", f"sim={sim:.3f}")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
