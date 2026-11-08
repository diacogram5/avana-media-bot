"""Sandbox tests for the exclusive-bank match speedup (GOAL 2, 2026-11-09).

The sliding-window match in app/bot/exclusive_bank.py was the bank
stage's bottleneck (~3.7 s on Render free for a 127-item query vs a
~1900-item fingerprint — pure-Python bin().count() per (offset, item)).
It now runs on a numpy fast path (lazy import, pure-Python fallback),
computing the EXACT same similarity with the same tier gate.

Correctness is proven by direct comparison against an inline copy of
the ORIGINAL pure-Python algorithm on the same inputs: every (song,
similarity) must be bit-identical, including ties, malformed/empty
fingerprints, all tier lengths, and the <3-items never-match rule.
Speed is measured on the same fixtures for both paths (sandbox numbers
are RELATIVE only — Render's 0.1 CPU is ~9x slower than this box, but
the ratio is what matters).

Run: python3 scripts/verify_bank_speed.py
Exit 0 = all checks pass.
"""
import os
import random
import subprocess
import sys
import tempfile
import time

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


# --- inline copy of the ORIGINAL pure-Python algorithm (reference) ---
def _bit_similarity_ref(a, b):
    if not a:
        return 0.0
    tot = 0
    for x, y in zip(a, b):
        tot += 32 - bin((x ^ y) & 0xFFFFFFFF).count("1")
    return tot / (32 * len(a))


def _best_match_ref(query, stored):
    n = len(query)
    if n == 0 or len(stored) < n:
        return 0.0, -1, 0
    best, boff = -1.0, -1
    for off in range(0, len(stored) - n + 1):
        sim = _bit_similarity_ref(query, stored[off:off + n])
        if sim > best:
            best, boff = sim, off
    return best, boff, n


def _match_against_ref(query_items, songs):
    if not query_items or len(query_items) < xb.MATCH_MIN_ITEMS:
        return None, 0.0
    min_sim = xb.MATCH_MIN_SIM
    for need, sim in xb.MATCH_TIERS:
        if len(query_items) >= need:
            min_sim = sim
            break
    best_song, best_sim = None, 0.0
    for song in songs:
        try:
            stored = [int(x) for x in (song.get("fingerprint") or "").split(",") if x]
        except ValueError:
            continue
        if len(stored) < xb.MATCH_MIN_ITEMS:
            continue
        sim, _o, _n = _best_match_ref(query_items, stored)
        if sim > best_sim:
            best_sim, best_song = sim, song
    if best_song is not None and best_sim >= min_sim:
        return best_song, best_sim
    return None, best_sim


def _song_id(result):
    return result[0]["id"] if result[0] else None


def _time_min(fn, reps=3):
    best = None
    out = None
    for _ in range(reps):
        t0 = time.perf_counter()
        out = fn()
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return best, out


def main():
    tmp = tempfile.mkdtemp(prefix="xb_speed_")
    print(f"fixtures in {tmp}")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", REF_CLIP, "-vn",
         "-ac", "2", "-ar", "44100", f"{tmp}/A_full.wav"])
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", SONG_B, "-vn",
         "-ac", "2", "-ar", "44100", f"{tmp}/B_full.wav"])
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "5", "-t", "5",
         "-i", f"{tmp}/A_full.wav", f"{tmp}/A_5s.wav"])
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "4", "-t", "6",
         "-i", f"{tmp}/B_full.wav", f"{tmp}/B_6s.wav"])
    for dur in (3, 4):
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "8", "-t", str(dur),
             "-i", f"{tmp}/A_full.wav", "-ac", "1", "-ar", "22050",
             f"{tmp}/A_mid_{dur}s.wav"])

    _d, fp_a = xb.fingerprint_file(f"{tmp}/A_full.wav")
    _d, fp_b = xb.fingerprint_file(f"{tmp}/B_full.wav")
    _d, q5 = xb.fingerprint_file(f"{tmp}/A_5s.wav")
    _d, qB = xb.fingerprint_file(f"{tmp}/B_6s.wav")
    _d, q3 = xb.fingerprint_file(f"{tmp}/A_mid_3s.wav")
    _d, q4 = xb.fingerprint_file(f"{tmp}/A_mid_4s.wav")
    song_a = {"id": 1, "title": "A", "artist": "X",
              "fingerprint": ",".join(map(str, fp_a))}
    song_b = {"id": 2, "title": "B", "artist": "Y",
              "fingerprint": ",".join(map(str, fp_b))}

    print("S1 real-music correctness (fpcalc fixtures, same as verify_exclusive)")
    m, sim = xb.match_against(q5, [song_a, song_b])
    check("5s excerpt matches song A", m is not None and m["id"] == 1, f"sim={sim:.3f}")
    m, sim = xb.match_against(qB, [song_a, song_b])
    check("B excerpt matches song B", m is not None and m["id"] == 2, f"sim={sim:.3f}")
    m, sim = xb.match_against(qB, [song_a])
    check("B excerpt does NOT match song A", m is None, f"sim={sim:.3f}")
    m, sim = xb.match_against(q5, [song_b])
    check("A excerpt does NOT match song B", m is None, f"sim={sim:.3f}")
    m, sim = xb.match_against(q3, [song_a, song_b])
    check("3s excerpt matches A (tier sim>=0.95)",
          m is not None and m["id"] == 1, f"sim={sim:.3f}")
    m, sim = xb.match_against(q4, [song_a, song_b])
    check("4s excerpt matches A (tier sim>=0.90)",
          m is not None and m["id"] == 1, f"sim={sim:.3f}")
    m, sim = xb.match_against(fp_a[:2], [song_a])
    check("too-short query (<3 items) never matches", m is None)
    m, sim = xb.match_against([], [song_a])
    check("empty query never matches", m is None)
    # Real fixtures must agree with the reference implementation too.
    for name, q, ss in (("5s vs A+B", q5, [song_a, song_b]),
                        ("B vs A+B", qB, [song_a, song_b]),
                        ("3s vs A+B", q3, [song_a, song_b]),
                        ("4s vs A+B", q4, [song_a, song_b])):
        a = xb.match_against(q, ss)
        b = _match_against_ref(q, ss)
        check(f"real fixture agrees with reference: {name}",
              _song_id(a) == _song_id(b) and a[1] == b[1],
              f"new=({_song_id(a)}, {a[1]:.6f}) ref=({_song_id(b)}, {b[1]:.6f})")

    print("S2 equivalence vs reference on production-scale synthetic data")
    rng = random.Random(20261109)
    stored_1900 = [rng.getrandbits(32) for _ in range(1900)]
    query_127 = [rng.getrandbits(32) for _ in range(127)]
    stored_true = [rng.getrandbits(32) for _ in range(1900)]
    query_true = list(stored_true[500:627])
    for i in range(0, len(query_true), 17):
        query_true[i] ^= rng.getrandbits(8)
    songs3 = [{"id": i, "title": f"S{i}", "artist": "X",
               "fingerprint": ",".join(map(str, stored_1900))} for i in range(3)]
    songs76 = list(songs3)
    for i in range(3, 76):
        arr = [rng.getrandbits(32) for _ in range(150)]
        songs76.append({"id": i, "title": f"S{i}", "artist": "X",
                        "fingerprint": ",".join(map(str, arr))})
    songs_true = [{"id": 1, "title": "T", "artist": "X",
                   "fingerprint": ",".join(map(str, stored_true))}]
    for name, q, s in (
            ("random 127 vs 1900", query_127, stored_1900),
            ("true match 127 vs 1900", query_true, stored_true),
            ("query == stored", stored_1900[:50], stored_1900[:50]),
            ("query longer than stored", stored_1900, stored_1900[:100]),
            ("empty query", [], stored_1900),
            ("empty stored", query_127, []),
            ("single item", [12345], [12345, 67890]),
            ("all zeros", [0] * 10, [0] * 100),
            ("all max uint32", [0xFFFFFFFF] * 10, [0xFFFFFFFF] * 100),
            ("tie: two identical windows", [7, 8, 9], [7, 8, 9, 7, 8, 9]),
            ("out-of-range ints", [0x1FFFFFFFF, -1, 5],
             [0xFFFFFFFF, 0xFFFFFFFF, 5, 6])):
        a = xb.best_match(q, s)
        b = _best_match_ref(q, s)
        check(f"best_match agrees: {name}", a == b, f"new={a} ref={b}")
    for name, q, ss in (
            ("random query, 3 songs", query_127, songs3),
            ("true query, 1 song", query_true, songs_true),
            ("random query, 76 mixed", query_127, songs76),
            ("3-item query (tier 0.95)", query_127[:3], songs3),
            ("8-item query (tier 0.90)", query_127[:8], songs3),
            ("15-item query (tier 0.75)", query_127[:15], songs3),
            ("2-item query (never)", query_127[:2], songs3),
            ("empty query", [], songs3),
            ("malformed fingerprint song", query_127,
             [{"id": 9, "title": "bad", "artist": "X",
               "fingerprint": "12,abc,34"}] + songs3),
            ("empty fingerprint song", query_127,
             [{"id": 9, "title": "empty", "artist": "X",
               "fingerprint": ""}] + songs3),
            ("tie: identical songs", query_true,
             [dict(songs_true[0], id=1), dict(songs_true[0], id=2)])):
        a = xb.match_against(q, ss)
        b = _match_against_ref(q, ss)
        check(f"match_against agrees: {name}",
              _song_id(a) == _song_id(b) and a[1] == b[1],
              f"new=({_song_id(a)}, {a[1]:.6f}) ref=({_song_id(b)}, {b[1]:.6f})")

    print("S3 pure-Python fallback (numpy disabled) returns identical results")
    orig_numpy = xb._numpy
    try:
        xb._numpy = lambda: None
        xb._FP_ARRAYS.clear()
        for name, q, ss in (("random query, 3 songs", query_127, songs3),
                            ("true query, 1 song", query_true, songs_true),
                            ("5s vs A+B", q5, [song_a, song_b])):
            a = xb.match_against(q, ss)
            b = _match_against_ref(q, ss)
            check(f"fallback agrees: {name}",
                  _song_id(a) == _song_id(b) and a[1] == b[1],
                  f"new=({_song_id(a)}, {a[1]:.6f}) ref=({_song_id(b)}, {b[1]:.6f})")
    finally:
        xb._numpy = orig_numpy

    print("S4 fingerprint-array cache behaviour")
    xb._FP_ARRAYS.clear()
    fp_str = songs3[0]["fingerprint"]
    a1 = xb._fp_array(fp_str)
    a2 = xb._fp_array(fp_str)
    check("same fingerprint string returns the cached array", a1 is a2)
    check("cached array has the stored length", len(a1) == 1900)
    check("malformed fingerprint caches as None (song skipped)",
          xb._fp_array("12,abc,34") is None)
    check("empty fingerprint parses to an empty array",
          xb._fp_array("") is not None and len(xb._fp_array("")) == 0)
    xb._invalidate_list_cache()
    check("list-cache invalidation clears the array cache",
          len(xb._FP_ARRAYS) == 0)
    # A changed fingerprint string must change the match, not reuse a stale array.
    alt = list(stored_1900)
    alt[600:727] = query_true
    song_alt = {"id": 1, "title": "T", "artist": "X",
                "fingerprint": ",".join(map(str, alt))}
    m, sim = xb.match_against(query_true, [song_alt])
    check("changed fingerprint matches at the new position",
          m is not None and sim >= 0.95, f"sim={sim:.3f}")
    m, sim = xb.match_against(query_true, songs_true)
    check("original fingerprint still matches as before",
          m is not None and sim >= 0.95, f"sim={sim:.3f}")

    print("S5 speed: new path vs pure-Python reference (same fixtures)")
    np = xb._numpy()
    print(f"  numpy available: {np is not None}"
          + (f" {np.__version__}" if np else ""))
    if np is None:
        print("  SKIP speed ratio: numpy unavailable, fallback active (correctness still proven above)")
    else:
        for name, q, ss in (("3 songs x 1900 items, query 127", query_127, songs3),
                            ("76 mixed songs, query 127", query_127, songs76)):
            t_new, r_new = _time_min(lambda: xb.match_against(q, ss))
            t_ref, r_ref = _time_min(lambda: _match_against_ref(q, ss))
            ratio = t_ref / t_new if t_new > 0 else float("inf")
            print(f"  {name}: new={t_new*1000:.2f} ms ref={t_ref*1000:.2f} ms "
                  f"speedup={ratio:.1f}x")
            check(f"speedup >= 10x: {name}", ratio >= 10, f"ratio={ratio:.1f}x")
            check(f"same result while timing: {name}",
                  _song_id(r_new) == _song_id(r_ref) and r_new[1] == r_ref[1])

    print(f"\n{PASS}/{PASS + FAIL} checks passed")
    if FAIL:
        print(f"{FAIL} FAILURES")
        sys.exit(1)


if __name__ == "__main__":
    main()
