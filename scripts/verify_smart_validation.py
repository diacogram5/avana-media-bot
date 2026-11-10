"""Sandbox tests for the smart-path validation pipeline
(app/search/smart_validate.py) — the PREREQUISITE for the free-only
smart search. The live verify_fullsong_file gives INVERTED verdicts
on the Taziyane fixtures (accepts the 7:23 original, rejects the
5:41 remix); this suite pins the new pipeline's verdicts on the same
four validated mp3 fixtures from ~/workspace/free-only-study/.

No network, no Telegram, no real Turso, no account / API key /
payment anywhere. ffprobe/ffmpeg/fpcalc run locally on the fixtures.

Cases:
  V1  Canon: taziyane == tazianeh == tazianh; remix == rimiks.
  V2  Brand suffix stripped: the javanmelody file title matches.
  V3  Transliteration: the melonmusic file title matches.
  V4  Extra tokens reject.
  V5  Artist rules: exact, missing-tag OK, different artist rejects.
  V6  Duration gate: 341 vs 341 ok; 443 vs 341 rejected; unknown
      expected duration skips the gate.
  V7  Fixture javanmelody -> "match" (tags + duration + version).
  V8  Fixture melonmusic -> "conditional" (version not in tags);
      acceptable via the source URL, NOT without it.
  V9  Fixture msbmusic -> "mismatch" / "duration_mismatch".
  V10 Fixture musicviral -> "mismatch" / "duration_mismatch".
  V11 No-title-tag mp3 -> "unverifiable".
  V12 Fingerprint: a 15 s clip cut from the javanmelody file scores
      >= 0.90 against it, and < 0.90 against the msbmusic file.
  V13 probe_file_identity_from_path: missing path / non-audio file
      -> ("", "", 0.0).
  V14 No expected duration: javanmelody still "match".
  V15 The OLD verify_fullsong_file is UNCHANGED and still inverted
      on the fixtures (javanmelody "mismatch", msbmusic "match") —
      this pins the prerequisite: the normal path was not rewritten.
  V16 Version tokens are compared BEFORE bracket-stripping: a file
      titled "Taziyane" (no Remix) with the right duration is
      "conditional", never "match".
  V17 conditional_acceptable requires a passing duration: a
      conditional whose duration is 102 s off is NOT acceptable even
      with the version in the URL.
  V18 Joft 6 number-word fix (2026-11-11, spec
      joft6-number-fix-spec-2026-11-10.md): the 12 prototype cases —
      Joft 6 vs shish/six/۶/shesh match; vs 7/5/no-number no match;
      Taziyane and Sob Zohr Shab unchanged; Song 2 vs Song Two and
      Song 10 vs Song Dah match; the documented miss (جفت ۶ vs Joft
      shish, pre-existing skeleton gap jft vs joft) stays a
      no-match and is OUT OF SCOPE. The spec lists 11 cases under
      "11/12 pass"; the 12th passing case is Joft 6 vs Joft shesh,
      the documented "shesh" alias for 6, which restores the count
      (11 pass + 1 miss = 12).

Run: python3 scripts/verify_smart_validation.py ; exit 0 = all PASS.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Strip proxy env vars (sandbox httpx gotcha).
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)

from app.search import smart_validate as sv  # noqa: E402
from app.search import fullsong_verify as old_verify  # noqa: E402

FIXTURES = Path.home() / "workspace" / "free-only-study"
JAVAN = FIXTURES / "javanmelody-taziyane-remix-320.mp3"
MELON = FIXTURES / "melonmusic-taziyane-remix-320.mp3"
MSB = FIXTURES / "msbmusic-taziyane.mp3"
VIRAL = FIXTURES / "musicviral-taziyane-320.mp3"

WANT_TITLE = "Taziyane (Remix)"
WANT_ARTIST = "Amir Tataloo"
EXPECTED = 341.0
MELON_URL = ("https://dl.melonmusic.ir/Music/Amir%20Tataloo%20-%20"
             "Tazianeh%20%28Remix-320%29.mp3")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name
          + (f" — {detail}" if detail and not ok else ""))


def _make_mp3_no_tags(seconds=3) -> bytes:
    td = tempfile.mkdtemp(prefix="smartval_mp3_")
    path = os.path.join(td, "t.mp3")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:a", "libmp3lame", path],
                   check=True, timeout=60)
    with open(path, "rb") as f:
        return f.read()


def _cut_clip(src: Path, seconds: int = 15) -> str:
    td = tempfile.mkdtemp(prefix="smartval_clip_")
    path = os.path.join(td, "clip.mp3")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-t", str(seconds), "-c:a", "copy", path],
                   check=True, timeout=60)
    return path


def _validate_fixture(path: Path, source_url: str = "") -> dict:
    ft, fa, dur = sv.probe_file_identity_from_path(str(path))
    return sv.validate_smart_file(WANT_TITLE, WANT_ARTIST, ft, fa, dur,
                                  EXPECTED, source_url=source_url)


def main() -> int:
    for p in (JAVAN, MELON, MSB, VIRAL):
        if not p.is_file():
            print(f"missing fixture: {p}")
            return 1

    # V1: canon.
    check("V1 canon taziyane==tazianeh==tazianh",
          sv._canon_token("taziyane") == sv._canon_token("tazianeh")
          == sv._canon_token("tazianh") == "tazian",
          f"{sv._canon_token('taziyane')} {sv._canon_token('tazianeh')}")
    check("V1 canon remix==rimiks",
          sv._canon_token("remix") == sv._canon_token("rimiks") == "rimiks")

    # V2: brand suffix stripped.
    check("V2 javanmelody title matches (brand suffix stripped)",
          sv.smart_title_matches(
              WANT_TITLE,
              "Taziyane (Remix) [Amir Tataloo] From abanmusics.com"))

    # V3: transliteration.
    check("V3 melonmusic title matches (transliteration)",
          sv.smart_title_matches(WANT_TITLE, "Tazianeh~MelonMusic.ir"))

    # V4: extra tokens reject.
    check("V4 extra tokens reject",
          not sv.smart_title_matches(WANT_TITLE, "Taziyane (Remix) Extra"))
    check("V4 different song rejects",
          not sv.smart_title_matches(WANT_TITLE, "Az Man Bekesh Biroon"))

    # V5: artist rules.
    check("V5 artist exact (case-insensitive)",
          sv.smart_artist_matches(WANT_ARTIST, "Amir tataloo"))
    check("V5 artist missing tag is OK",
          sv.smart_artist_matches(WANT_ARTIST, ""))
    check("V5 different artist rejects",
          not sv.smart_artist_matches(WANT_ARTIST, "Sami Lone"))

    # V6: duration gate.
    check("V6 duration gate: 340.8 vs 341 passes",
          sv.duration_gate_ok(340.82, 341.0))
    check("V6 duration gate: 443 vs 341 rejects",
          not sv.duration_gate_ok(442.97, 341.0))
    check("V6 duration gate: unknown expected skips",
          sv.duration_gate_ok(442.97, None))

    # V7: javanmelody fixture -> match.
    ft, fa, dur = sv.probe_file_identity_from_path(str(JAVAN))
    check("V7 javanmelody tags probed (title/artist/duration)",
          "Taziyane" in ft and "Remix" in ft and fa == "Amir Tataloo"
          and abs(dur - 340.82) < 1.0,
          f"title={ft!r} artist={fa!r} dur={dur}")
    v7 = _validate_fixture(JAVAN)
    check("V7 javanmelody fixture -> match",
          v7["status"] == "match" and v7["detail"] == "match",
          f"v7={v7}")

    # V8: melonmusic fixture -> conditional, URL-accepted only.
    v8 = _validate_fixture(MELON, source_url=MELON_URL)
    check("V8 melonmusic fixture -> conditional (version not in tags)",
          v8["status"] == "conditional"
          and v8["detail"] == "version_not_in_tags"
          and v8["version_in_source"] is True,
          f"v8={v8}")
    check("V8 conditional acceptable via source URL",
          sv.conditional_acceptable(v8, MELON_URL) is True)
    check("V8 conditional NOT acceptable without the URL",
          sv.conditional_acceptable(v8, "") is False)

    # V9/V10: wrong-version fixtures -> duration mismatch.
    v9 = _validate_fixture(MSB)
    check("V9 msbmusic fixture -> mismatch / duration_mismatch",
          v9["status"] == "mismatch" and v9["detail"] == "duration_mismatch",
          f"v9={v9}")
    v10 = _validate_fixture(VIRAL)
    check("V10 musicviral fixture -> mismatch / duration_mismatch",
          v10["status"] == "mismatch"
          and v10["detail"] == "duration_mismatch",
          f"v10={v10}")

    # V11: no title tag -> unverifiable.
    no_tag = _make_mp3_no_tags()
    r11 = old_verify.verify_fullsong_file(no_tag, "mp3", WANT_TITLE, WANT_ARTIST)
    check("V11 no-title-tag mp3 is unverifiable (old prober agrees)",
          r11["status"] == "unverifiable", f"r11={r11}")
    v11 = sv.validate_smart_file(WANT_TITLE, WANT_ARTIST, "", "", 5.0,
                                 EXPECTED)
    check("V11 smart validate: no title -> unverifiable",
          v11["status"] == "unverifiable"
          and v11["detail"] == "no_title_tag", f"v11={v11}")

    # V12: fingerprint — same audio high, different audio low.
    clip_path = _cut_clip(JAVAN, 15)
    clip_items = sv.fingerprint_file_items(clip_path)
    full_items = sv.fingerprint_file_items(str(JAVAN))
    msb_items = sv.fingerprint_file_items(str(MSB))
    if clip_items and full_items and msb_items:
        sim_same = sv.fpclip_similarity(clip_items, full_items)
        sim_diff = sv.fpclip_similarity(clip_items, msb_items)
        check("V12 fingerprint: clip vs same file >= 0.90",
              sim_same >= 0.90, f"sim={sim_same}")
        check("V12 fingerprint: clip vs wrong file < 0.90",
              sim_diff < 0.90, f"sim={sim_diff}")
    else:
        check("V12 fingerprint items available (fpcalc)",
              False, "fpcalc returned nothing for the fixtures")

    # V13: prober on missing / non-audio paths.
    check("V13 probe missing path -> empty",
          sv.probe_file_identity_from_path("/nonexistent/x.mp3") == ("", "", 0.0))
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False,
                                     mode="w") as fh:
        fh.write("not audio at all")
        txt_path = fh.name
    check("V13 probe non-audio file -> empty",
          sv.probe_file_identity_from_path(txt_path) == ("", "", 0.0))

    # V14: no expected duration -> match still (gate skipped).
    v14 = sv.validate_smart_file(WANT_TITLE, WANT_ARTIST, ft, fa, dur, None)
    check("V14 no expected duration: javanmelody still match",
          v14["status"] == "match", f"v14={v14}")

    # V15: the OLD verifier is UNCHANGED and still inverted.
    with open(JAVAN, "rb") as f:
        j_bytes = f.read()
    with open(MSB, "rb") as f:
        m_bytes = f.read()
    r_old_j = old_verify.verify_fullsong_file(
        j_bytes, "mp3", WANT_TITLE, WANT_ARTIST)
    r_old_m = old_verify.verify_fullsong_file(
        m_bytes, "mp3", WANT_TITLE, WANT_ARTIST)
    check("V15 old verifier UNCHANGED: javanmelody still mismatch",
          r_old_j["status"] == "mismatch", f"old_j={r_old_j}")
    check("V15 old verifier UNCHANGED: msbmusic still match",
          r_old_m["status"] == "match", f"old_m={r_old_m}")

    # V16: version compared before bracket-stripping.
    v16a = sv.validate_smart_file(WANT_TITLE, WANT_ARTIST, "Taziyane",
                                  "Amir Tataloo", 341.0, EXPECTED)
    check("V16 title without Remix -> conditional, never match",
          v16a["status"] == "conditional", f"v16a={v16a}")
    v16b = sv.validate_smart_file(WANT_TITLE, WANT_ARTIST,
                                  "Taziyane (Remix)", "Amir Tataloo",
                                  341.0, EXPECTED)
    check("V16 title with Remix -> match",
          v16b["status"] == "match", f"v16b={v16b}")

    # V17: conditional needs a passing duration too.
    v17 = {"status": "conditional", "duration_diff": 102.0,
           "version_in_source": True}
    check("V17 conditional with bad duration is NOT acceptable",
          sv.conditional_acceptable(v17, MELON_URL) is False)

    # V18: Joft 6 number-word fix — the 12 prototype cases.
    # Token level: the digit and the word must collapse to one token.
    check("V18 tokens: Joft 6 == Joft shish == ['joft', 'n6']",
          sv.smart_tokens("Joft 6") == sv.smart_tokens("Joft shish")
          == ["joft", "n6"],
          f"{sv.smart_tokens('Joft 6')} {sv.smart_tokens('Joft shish')}")
    check("V18 J1 Joft 6 vs Joft shish -> match",
          sv.smart_title_matches("Joft 6", "Joft shish"))
    check("V18 J2 Joft 6 vs Joft six -> match",
          sv.smart_title_matches("Joft 6", "Joft six"))
    check("V18 J3 Joft 6 vs Joft ۶ -> match",
          sv.smart_title_matches("Joft 6", "Joft ۶"))
    check("V18 J4 Joft 6 vs Joft 7 -> no match",
          not sv.smart_title_matches("Joft 6", "Joft 7"))
    check("V18 J5 Joft 6 vs Joft 5 -> no match",
          not sv.smart_title_matches("Joft 6", "Joft 5"))
    check("V18 J6 Joft 6 vs Joft (no number) -> no match",
          not sv.smart_title_matches("Joft 6", "Joft"))
    check("V18 J7 Taziyane (Remix) vs itself -> match",
          sv.smart_title_matches("Taziyane (Remix)", "Taziyane (Remix)"))
    check("V18 J8 Sob Zohr Shab vs itself -> match",
          sv.smart_title_matches("Sob Zohr Shab", "Sob Zohr Shab"))
    check("V18 J9 Song 2 vs Song Two -> match",
          sv.smart_title_matches("Song 2", "Song Two"))
    check("V18 J10 Song 10 vs Song Dah -> match",
          sv.smart_title_matches("Song 10", "Song Dah"))
    check("V18 J11 Joft 6 vs Joft shesh -> match",
          sv.smart_title_matches("Joft 6", "Joft shesh"))
    check("V18 J12 DOCUMENTED MISS (out of scope): جفت ۶ vs Joft "
          "shish stays no match (skeleton jft vs joft)",
          not sv.smart_title_matches("جفت ۶", "Joft shish"))

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
