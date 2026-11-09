"""Sandbox tests for the full-song mismatch fix (2026-11-09).

The 23:54 live case: identify said "ATM" / "Tiem & Salii" but the sent
full song was a DIFFERENT song — the site labelled the file correctly
while the audio inside was another song. The guard now checks the
FILE's own ID3 tags (ffprobe) before ANY full-song send, in the fresh
lookup and on both cache hits, and falls back to the clip's original
audio (stream copy) with the corporate text instead of a wrong song.

Cases:
  F1  Matching tags send (bytes path + URL path): real m:ss caption,
      never the static "over 90 seconds" label.
  F2  Mismatched tags (the literal 23:54 pair) are NEVER sent; no
      own_media -> the honest no-full message.
  F2b Mismatched tags + own_media video -> the clip's original audio
      (stream copy) with the case-(b) caption (unify fix 2026-11-09:
      the name IS confirmed, so the corporate caption no longer
      applies to this path).
  F2c/F2d The same guard + fallback in the download-full flow
      (_download_full_bg): matching tags send, mismatch -> clip audio.
      (Untouched by the unify fix — the download-full flow still
      carries the corporate caption.)
  F3  Untagged file is NEVER sent; own_media video -> the clip's
      original audio + the case-(b) caption.
  F4  Duration label: real m:ss; unit checks on _fullsong_duration_label.
  F5  Missing duration omits the label entirely (unit + cache-hit send).
  F6  Cache-hit guard: verified match sends; verified mismatch blocks
      and invalidates/deletes; legacy rows (no verified identity) never
      send — local cache and Turso alike.
  F6g Cache-hit mismatch + no fresh file -> the original audio too
      (honest own-media path; the identification itself stands).
  F7  Matcher cases (measured pairs incl. the 23:54 pair) via the
      verify module, plus direct verify_fullsong_file statuses.
  F8  A fresh verified send stores the FILE's own tags (not the query)
      in both caches, so later hits can be re-checked.
  F9  No fingerprinting: the guard is tag-only (ffprobe, no fpcalc);
      the corporate fa text is byte-identical to the user order.

Reuses the fake aiogram / fake libsql / fake message harness from
scripts/verify_speedfix.py (importing it runs its module setup, not
its main). Test mp3/mp4 files are generated at runtime with ffmpeg.
No network, no Telegram, no real Turso.

Run: python3 scripts/verify_fullsong_match.py ; exit 0 = all PASS.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_speedfix as vs  # noqa: E402  (installs fake aiogram, imports handlers)

from app.bot import file_cache as file_cache_mod  # noqa: E402
from app.bot import turso_cache as turso_cache_mod  # noqa: E402
from app.i18n import t  # noqa: E402
from app.search import fullsong_verify as verify_mod  # noqa: E402
import app.bot.handlers as h  # noqa: E402

check = vs.check
RESULTS = vs.RESULTS
LOG_LINES = vs.LOG_LINES
adapters_mod = vs.adapters_mod

_NEW_FA = t("m_extracted_audio_new", "fa")
# Case-(b) caption template (unify fix 2026-11-09): the name IS
# confirmed but no verified full file exists — the clip's own audio
# is the one final message.
_B_FA = t("m_identified_no_full_clip", "fa")
_STATIC_FA = t("m_full_duration_long", "fa")
_EXTRACTED_TITLE_FA = t("m_extracted_audio_title", "fa")


def _no_full_text(title, artist):
    return t("h_song_no_full", "fa").format(title=title, artist=artist)


class _Msg(vs._FakeMessage):
    """_FakeMessage that also records answer_audio kwargs (caption/title)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.audio_kwargs: list[dict] = []

    async def answer_audio(self, audio, **kw):
        self.audio_kwargs.append(kw)
        return await super().answer_audio(audio, **kw)


async def _deliver(msg, conn, find_result, title, artist, own_media=None,
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
        await h._deliver_fullsong_bg(msg, conn, title, artist, None, vs._KB,
                                     own_media, None, "test query", "fa")
    finally:
        adapters_mod.find_full_song_file_v2 = orig
    return calls["n"]


def _make_mp3(title=None, artist=None, seconds=5):
    """Short mp3 with the given ID3 tags (or none) — ffmpeg sine tone."""
    td = tempfile.mkdtemp(prefix="fullsong_mp3_")
    path = os.path.join(td, "t.mp3")
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
           "-i", f"sine=frequency=440:duration={seconds}",
           "-c:a", "libmp3lame"]
    if title is not None:
        cmd += ["-metadata", f"title={title}"]
    if artist is not None:
        cmd += ["-metadata", f"artist={artist}"]
    cmd += [path]
    subprocess.run(cmd, check=True, timeout=60)
    with open(path, "rb") as f:
        return f.read()


def _make_mp4(seconds=3):
    """Tiny mp4 with an audio track (the user's clip for the fallback)."""
    td = tempfile.mkdtemp(prefix="fullsong_mp4_")
    path = os.path.join(td, "c.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"color=c=black:s=320x240:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", path],
                   check=True, timeout=60)
    with open(path, "rb") as f:
        return f.read()


_MP4 = None  # generated lazily in main (ffmpeg)


def _attach_clip_video(msg):
    """Fake bot.get_file/download_file serving the generated mp4."""
    async def _get_file(file_id):
        return SimpleNamespace(file_path="clip.mp4")

    async def _download_file(file_path, buf):
        buf.write(_MP4)

    msg.bot.get_file = _get_file
    msg.bot.download_file = _download_file


def _full_dict(data, source, url=None, size=None, duration_sec=0.0):
    d = {"data": data, "ext": "mp3", "duration_sec": duration_sec,
         "source": source}
    if url is not None:
        d["url"] = url
    if size is not None:
        d["size"] = size
    return d


# The EXACT new extracted-audio caption from the user order
# (2026-11-09 01:17), typed here independently of i18n.py so the test
# proves byte equality. (The 00:12 corporate text is superseded.)
_NEW_EXACT = ("🎵 نسخه رسمی این آهنگ در منابع موسیقی پیدا نشد؛ اما آوانو "
              "صدای آن را مستقیماً از همین کلیپ استخراج کرده تا چیزی از "
              "دست ندهید. 💜\n\n🎧 صدای کلیپ، آماده شنیدن!\n\n"
              "AVANO AI | همیشه یک راه برای شنیدن موسیقی هست!")


class _FakeCallback:
    """Minimal CallbackQuery stand-in for _download_full_bg."""

    def __init__(self, msg):
        self.message = msg
        self.bot = msg.bot
        self.from_user = msg.from_user
        self.data = "dl:full:test"


async def _download_full(msg, conn, find_result, title, artist,
                         own_media=None, find_raises=False):
    """Run _download_full_bg with a mocked find_full_song_file_v2."""
    calls = {"n": 0}
    orig = adapters_mod.find_full_song_file_v2

    async def _fake_find(a, t):
        calls["n"] += 1
        if find_raises:
            raise AssertionError("find_full_song_file_v2 must not be called")
        return find_result

    adapters_mod.find_full_song_file_v2 = _fake_find
    cb = _FakeCallback(msg)
    result = {"own_media": own_media} if own_media else {}
    try:
        await h._download_full_bg(cb, conn, None, title, artist, result)
    finally:
        adapters_mod.find_full_song_file_v2 = orig
    return calls["n"]


async def main():
    global _MP4
    _MP4 = _make_mp4()
    vs._turso_on()

    # ---- F1a: matching tags send via the bytes path -------------------
    # Adapter reports duration 0 (persian_music always does); the probe
    # must supply the real duration for the caption.
    try:
        LOG_LINES.clear()
        mp3 = _make_mp3("Fereshteh", "Omid Oghabi موزیکدل", seconds=5)
        msg = _Msg()
        conn = vs._make_conn()
        n = await _deliver(msg, conn,
                           _full_dict(mp3, "persian_music:musicdel", duration_sec=0.0),
                           "Fereshteh", "Omid Oghabi")
        kinds = [k for k, _ in msg.audio_calls]
        check("F1a matching tags send via bytes path", kinds == ["bytes"],
              f"kinds={kinds}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("F1a caption shows the real probed duration m:ss",
              "0:05" in cap, f"caption={cap!r}")
        check("F1a caption never shows the static over-90s label",
              "بیش از ۹۰ ثانیه" not in cap, f"caption={cap!r}")
        check("F1a bytes send logs sent", vs._log_has("sent"))
        # F8a: the FILE's own tags (with the brand stamp) are stored.
        row = file_cache_mod.get_cached_song(conn, "Omid Oghabi", "Fereshteh")
        check("F8a local cache stores the file's verified title",
              row is not None and row["verified_title"] == "Fereshteh",
              f"row={row}")
        check("F8a local cache stores the file's verified artist",
              row is not None and row["verified_artist"] == "Omid Oghabi موزیکدل",
              f"row={row}")
        trow = turso_cache_mod.get_song_file("Omid Oghabi", "Fereshteh")
        check("F8a Turso cache stores the file's verified identity",
              trow is not None and trow["verified_title"] == "Fereshteh"
              and trow["verified_artist"] == "Omid Oghabi موزیکدل",
              f"trow={trow}")
        check("F8a Turso cache stores the probed duration",
              trow is not None and 4.0 < (trow["duration_sec"] or 0) < 6.0,
              f"trow={trow}")
    except Exception as exc:
        check("F1a matching tags bytes path", False, f"{type(exc).__name__}: {exc}")

    # ---- F1b: matching tags send via the URL path (the 23:54 path) ----
    try:
        LOG_LINES.clear()
        mp3 = _make_mp3("Yani Chi Nemishe [Msbmusic.IR]", "Hichkas", seconds=65)
        msg = _Msg()
        conn = vs._make_conn()
        n = await _deliver(msg, conn,
                           _full_dict(mp3, "persian_music:msbmusic",
                                      url="https://example.invalid/song.mp3",
                                      size=1_000_000, duration_sec=0.0),
                           "Yani Chi Nemishe", "Hichkas")
        kinds = [k for k, _ in msg.audio_calls]
        check("F1b matching tags send via URL path", kinds == ["str"],
              f"kinds={kinds}")
        check("F1b URL send passes the URL",
              msg.audio_calls and msg.audio_calls[0][1] == "https://example.invalid/song.mp3",
              f"calls={msg.audio_calls}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("F1b caption shows the real probed duration m:ss",
              "1:05" in cap, f"caption={cap!r}")
        check("F1b caption never shows the static over-90s label",
              "بیش از ۹۰ ثانیه" not in cap, f"caption={cap!r}")
        check("F1b URL send logs sent_url", vs._log_has("sent_url"))
        trow = turso_cache_mod.get_song_file("Hichkas", "Yani Chi Nemishe")
        check("F8b Turso cache stores the file's verified title",
              trow is not None
              and trow["verified_title"] == "Yani Chi Nemishe [Msbmusic.IR]",
              f"trow={trow}")
    except Exception as exc:
        check("F1b matching tags URL path", False, f"{type(exc).__name__}: {exc}")

    # ---- F2: the literal 23:54 mismatch is NEVER sent ------------------
    try:
        LOG_LINES.clear()
        mp3 = _make_mp3("Bu Üräyin Dərdi Sənsən", "Canım Xəstə", seconds=5)
        msg = _Msg()
        conn = vs._make_conn()
        n = await _deliver(msg, conn,
                           _full_dict(mp3, "persian_music:musicdel",
                                      duration_sec=209.0),
                           "ATM", "Tiem & Salii")
        check("F2 mismatched file is never sent", msg.audio_calls == [],
              f"calls={msg.audio_calls}")
        check("F2 honest no-full message is sent",
              any(_no_full_text("ATM", "Tiem & Salii") in a for a in msg.answers),
              f"answers={msg.answers}")
        check("F2 mismatch is logged", vs._log_has("fullsong_mismatch"))
        check("F2 adapters were still called once", n == 1, f"n={n}")
    except Exception as exc:
        check("F2 mismatched tags blocked", False, f"{type(exc).__name__}: {exc}")

    # ---- F2b: mismatched tags + own_media -> original audio, exact text --
    try:
        LOG_LINES.clear()
        mp3 = _make_mp3("Bu Üräyin Dərdi Sənsən", "Canım Xəstə", seconds=5)
        msg = _Msg()
        _attach_clip_video(msg)
        conn = vs._make_conn()
        n = await _deliver(msg, conn,
                           _full_dict(mp3, "persian_music:musicdel",
                                      duration_sec=209.0),
                           "ATM", "Tiem & Salii",
                           own_media=("video", "VID123"))
        check("F2b mismatched tags + clip -> exactly one audio send",
              len(msg.audio_calls) == 1 and msg.audio_calls[0][0] == "bytes",
              f"calls={msg.audio_calls}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("F2b clip audio carries the case-(b) caption",
              cap == _B_FA.format(title="ATM", artist="Tiem & Salii"),
              f"caption={cap!r}")
        check("F2b clip audio is titled as extracted audio",
              (msg.audio_kwargs[0].get("title", "") if msg.audio_kwargs else "")
              == _EXTRACTED_TITLE_FA)
        check("F2b mismatch is logged", vs._log_has("fullsong_mismatch"))
        check("F2b no full-song caption anywhere",
              not any("نسخه کامل آهنگ پیدا" in
                     (k.get("caption", "") or "") for k in msg.audio_kwargs))
    except Exception as exc:
        check("F2b mismatched tags fallback to clip audio", False,
              f"{type(exc).__name__}: {exc}")

    # ---- F2c: download-full flow, matching tags send ---------------------
    try:
        LOG_LINES.clear()
        mp3 = _make_mp3("Fereshteh", "Omid Oghabi", seconds=5)
        msg = _Msg()
        conn = vs._make_conn()
        n = await _download_full(msg, conn,
                                 _full_dict(mp3, "persian_music:musicdel",
                                            duration_sec=0.0),
                                 "Fereshteh", "Omid Oghabi")
        kinds = [k for k, _ in msg.audio_calls]
        check("F2c download-full matching tags send", kinds == ["bytes"],
              f"kinds={kinds}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("F2c download-full caption shows the real probed duration",
              "0:05" in cap and "بیش از ۹۰ ثانیه" not in cap, f"caption={cap!r}")
        check("F2c download-full logs no mismatch",
              not vs._log_has("fullsong_mismatch"))
    except Exception as exc:
        check("F2c download-full matching tags", False,
              f"{type(exc).__name__}: {exc}")

    # ---- F2d: download-full flow, mismatch -> original audio, exact text -
    try:
        LOG_LINES.clear()
        mp3 = _make_mp3("Bu Üräyin Dərdi Sənsən", "Canım Xəstə", seconds=5)
        msg = _Msg()
        _attach_clip_video(msg)
        conn = vs._make_conn()
        n = await _download_full(msg, conn,
                                 _full_dict(mp3, "persian_music:musicdel",
                                            duration_sec=209.0),
                                 "ATM", "Tiem & Salii",
                                 own_media=("video", "VID123"))
        check("F2d download-full mismatch -> exactly one audio send",
              len(msg.audio_calls) == 1 and msg.audio_calls[0][0] == "bytes",
              f"calls={msg.audio_calls}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("F2d clip audio carries the new caption",
              cap == _NEW_FA, f"caption={cap!r}")
        check("F2d mismatch is logged", vs._log_has("fullsong_mismatch"))
        check("F2d no full-song caption anywhere",
              not any("نسخه کامل آهنگ پیدا" in
                     (k.get("caption", "") or "") for k in msg.audio_kwargs))
    except Exception as exc:
        check("F2d download-full mismatch fallback", False,
              f"{type(exc).__name__}: {exc}")

    # ---- F3: untagged file -> original clip audio + corporate text ----
    try:
        LOG_LINES.clear()
        mp3 = _make_mp3(None, None, seconds=5)  # no ID3 tags at all
        msg = _Msg()
        _attach_clip_video(msg)
        conn = vs._make_conn()
        n = await _deliver(msg, conn,
                           _full_dict(mp3, "persian_music:musicdel",
                                      duration_sec=158.0),
                           "Terror", "Tiem & Salii",
                           own_media=("video", "VID123"))
        check("F3 untagged file is never sent as the full song",
              len(msg.audio_calls) == 1 and msg.audio_calls[0][0] == "bytes",
              f"calls={msg.audio_calls}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("F3 clip audio carries the case-(b) caption",
              cap == _B_FA.format(title="Terror", artist="Tiem & Salii"),
              f"caption={cap!r}")
        title_kw = msg.audio_kwargs[0].get("title", "") if msg.audio_kwargs else ""
        check("F3 clip audio is titled as extracted audio",
              title_kw == _EXTRACTED_TITLE_FA, f"title={title_kw!r}")
        check("F3 unverifiable is logged", vs._log_has("fullsong_unverifiable"))
        check("F3 no full-song caption anywhere",
              not any("نسخه کامل آهنگ پیدا" in a for a in msg.answers)
              and not any("نسخه کامل آهنگ پیدا" in
                         (k.get("caption", "") or "") for k in msg.audio_kwargs))
    except Exception as exc:
        check("F3 untagged file fallback", False, f"{type(exc).__name__}: {exc}")

    # ---- F4: duration label units --------------------------------------
    try:
        check("F4 label 209s is real m:ss",
              h._fullsong_duration_label(209.0, "fa") == " — مدت: 3:29",
              h._fullsong_duration_label(209.0, "fa"))
        check("F4 label 65.04s is real m:ss",
              h._fullsong_duration_label(65.04, "fa") == " — مدت: 1:05",
              h._fullsong_duration_label(65.04, "fa"))
        _en = h._fullsong_duration_label(5.0, "en")
        check("F4 en label is real m:ss, never the static label",
              "0:05" in _en and "over 90" not in _en, f"label={_en!r}")
    except Exception as exc:
        check("F4 duration label units", False, f"{type(exc).__name__}: {exc}")

    # ---- F5: missing duration omits the label --------------------------
    try:
        check("F5 label 0 omits the duration",
              h._fullsong_duration_label(0, "fa") == "")
        check("F5 label None omits the duration",
              h._fullsong_duration_label(None, "fa") == "")
        check("F5 label negative omits the duration",
              h._fullsong_duration_label(-3, "fa") == "")
        LOG_LINES.clear()
        msg = _Msg()
        conn = vs._make_conn()
        file_cache_mod.store_file_id(conn, "LOVIXX", "Benz", "FID_ZERO",
                                     "audio", 0, 0, "",
                                     verified_title="Benz",
                                     verified_artist="LOVIXX")
        n = await _deliver(msg, conn, None, "Benz", "LOVIXX", find_raises=True)
        check("F5 cache hit with stored duration 0 still sends",
              msg.audio_calls == [("str", "FID_ZERO")],
              f"calls={msg.audio_calls}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("F5 cache-hit caption omits the duration entirely",
              "مدت" not in cap and "بیش از" not in cap, f"caption={cap!r}")
        check("F5 adapters never called on cache hit", n == 0, f"n={n}")
    except Exception as exc:
        check("F5 missing duration omits label", False, f"{type(exc).__name__}: {exc}")

    # ---- F6a: local verified match sends -------------------------------
    try:
        LOG_LINES.clear()
        msg = _Msg()
        conn = vs._make_conn()
        file_cache_mod.store_file_id(conn, "Mehdi Ahmadvand", "Dooset Daram",
                                     "FID_MATCH", "audio", 0, 185.2, "",
                                     verified_title="Dooset Daram",
                                     verified_artist="Mehdi Ahmadvand")
        n = await _deliver(msg, conn, None, "Dooset Daram", "Mehdi Ahmadvand",
                           find_raises=True)
        check("F6a local verified match sends the cached file",
              msg.audio_calls == [("str", "FID_MATCH")],
              f"calls={msg.audio_calls}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("F6a cache-hit caption uses the stored real duration",
              "3:05" in cap, f"caption={cap!r}")
        check("F6a adapters never called on cache hit", n == 0, f"n={n}")
    except Exception as exc:
        check("F6a local verified match", False, f"{type(exc).__name__}: {exc}")

    # ---- F6b: local verified mismatch blocks + invalidates -------------
    try:
        LOG_LINES.clear()
        msg = _Msg()
        conn = vs._make_conn()
        file_cache_mod.store_file_id(conn, "Tiem & Salii", "Mahvare",
                                     "FID_WRONG", "audio", 0, 200.0, "",
                                     verified_title="ATM",
                                     verified_artist="Tiem & Salii")
        n = await _deliver(msg, conn, None, "Mahvare", "Tiem & Salii")
        check("F6b local verified mismatch is never sent",
              msg.audio_calls == [], f"calls={msg.audio_calls}")
        check("F6b honest no-full message is sent",
              any(_no_full_text("Mahvare", "Tiem & Salii") in a for a in msg.answers))
        check("F6b cache mismatch is logged",
              vs._log_has("fullsong_cache_mismatch"))
        check("F6b poisoned local row is invalidated",
              file_cache_mod.get_cached_song(conn, "Tiem & Salii", "Mahvare") is None)
        check("F6b falls through to a fresh lookup", n == 1, f"n={n}")
    except Exception as exc:
        check("F6b local verified mismatch", False, f"{type(exc).__name__}: {exc}")

    # ---- F6c: local legacy row (no verified identity) never sends ------
    try:
        LOG_LINES.clear()
        msg = _Msg()
        conn = vs._make_conn()
        file_cache_mod.store_file_id(conn, "Amir Tataloo", "Taziyane",
                                     "FID_LEGACY", "audio", 0, 200.0, "")
        n = await _deliver(msg, conn, None, "Taziyane", "Amir Tataloo")
        check("F6c local legacy row is never sent",
              msg.audio_calls == [], f"calls={msg.audio_calls}")
        check("F6c honest no-full message is sent",
              any(_no_full_text("Taziyane", "Amir Tataloo") in a for a in msg.answers))
        check("F6c legacy row is logged unverified",
              vs._log_has("fullsong_cache_unverified"))
        check("F6c falls through to a fresh lookup", n == 1, f"n={n}")
    except Exception as exc:
        check("F6c local legacy row", False, f"{type(exc).__name__}: {exc}")

    # ---- F6d: Turso verified match sends --------------------------------
    try:
        LOG_LINES.clear()
        msg = _Msg()
        conn = vs._make_conn()
        turso_cache_mod.store_song_file_id("Tiem & Salii", "Mahvare",
                                           "TURSO_MATCH", "audio",
                                           verified_title="Mahvare",
                                           verified_artist="Tiem & Salii",
                                           duration_sec=158.0)
        n = await _deliver(msg, conn, None, "Mahvare", "Tiem & Salii",
                           find_raises=True)
        check("F6d Turso verified match sends the cached file",
              msg.audio_calls == [("str", "TURSO_MATCH")],
              f"calls={msg.audio_calls}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("F6d Turso-hit caption uses the stored real duration",
              "2:38" in cap, f"caption={cap!r}")
        row = file_cache_mod.get_cached_song(conn, "Tiem & Salii", "Mahvare")
        check("F6d Turso hit refreshes the local cache with verified identity",
              row is not None and row["file_id"] == "TURSO_MATCH"
              and row["verified_title"] == "Mahvare",
              f"row={row}")
        check("F6d adapters never called on cache hit", n == 0, f"n={n}")
    except Exception as exc:
        check("F6d Turso verified match", False, f"{type(exc).__name__}: {exc}")

    # ---- F6e: Turso verified mismatch blocks + deletes ------------------
    try:
        LOG_LINES.clear()
        msg = _Msg()
        conn = vs._make_conn()
        turso_cache_mod.store_song_file_id("Tiem & Salii", "Terror",
                                           "TURSO_WRONG", "audio",
                                           verified_title="ATM",
                                           verified_artist="Tiem & Salii",
                                           duration_sec=200.0)
        n = await _deliver(msg, conn, None, "Terror", "Tiem & Salii")
        check("F6e Turso verified mismatch is never sent",
              msg.audio_calls == [], f"calls={msg.audio_calls}")
        check("F6e honest no-full message is sent",
              any(_no_full_text("Terror", "Tiem & Salii") in a for a in msg.answers))
        check("F6e cache mismatch is logged",
              vs._log_has("fullsong_cache_mismatch"))
        check("F6e poisoned Turso row is deleted",
              turso_cache_mod.get_song_file("Tiem & Salii", "Terror") is None)
        check("F6e falls through to a fresh lookup", n == 1, f"n={n}")
    except Exception as exc:
        check("F6e Turso verified mismatch", False, f"{type(exc).__name__}: {exc}")

    # ---- F6f: Turso legacy row never sends ------------------------------
    try:
        LOG_LINES.clear()
        msg = _Msg()
        conn = vs._make_conn()
        turso_cache_mod.store_song_file_id("Farhad", "Exam", "TURSO_LEGACY")
        n = await _deliver(msg, conn, None, "Exam", "Farhad")
        check("F6f Turso legacy row is never sent",
              msg.audio_calls == [], f"calls={msg.audio_calls}")
        check("F6f honest no-full message is sent",
              any(_no_full_text("Exam", "Farhad") in a for a in msg.answers))
        check("F6f legacy row is logged unverified",
              vs._log_has("fullsong_cache_unverified"))
        check("F6f falls through to a fresh lookup", n == 1, f"n={n}")
    except Exception as exc:
        check("F6f Turso legacy row", False, f"{type(exc).__name__}: {exc}")

    # ---- F6g: cache-hit mismatch still falls back to the original audio --
    # The cached file is poisoned, the fresh lookup finds nothing: the
    # user still gets the clip's own audio (the honest own-media path),
    # never the wrong cached file. The identification itself stands,
    # so the fallback now carries the case-(b) caption (unify fix
    # 2026-11-09 — the corporate caption no longer applies to a
    # confirmed name).
    try:
        LOG_LINES.clear()
        msg = _Msg()
        _attach_clip_video(msg)
        conn = vs._make_conn()
        file_cache_mod.store_file_id(conn, "Maviler", "Aramazsan Arama",
                                     "FID_WRONG", "audio", 0, 200.0, "",
                                     verified_title="ATM",
                                     verified_artist="Tiem & Salii")
        n = await _deliver(msg, conn, None, "Aramazsan Arama", "Maviler",
                           own_media=("video", "VID123"))
        check("F6g cache mismatch + no fresh file -> original audio sent",
              len(msg.audio_calls) == 1 and msg.audio_calls[0][0] == "bytes",
              f"calls={msg.audio_calls}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("F6g original audio carries the case-(b) caption",
              cap == _B_FA.format(title="Aramazsan Arama", artist="Maviler"),
              f"caption={cap!r}")
        check("F6g cache mismatch is logged",
              vs._log_has("fullsong_cache_mismatch"))
        check("F6g poisoned local row is invalidated",
              file_cache_mod.get_cached_song(conn, "Maviler", "Aramazsan Arama") is None)
        check("F6g no full-song caption anywhere",
              not any("نسخه کامل آهنگ پیدا" in
                     (k.get("caption", "") or "") for k in msg.audio_kwargs))
    except Exception as exc:
        check("F6g cache mismatch fallback to original audio", False,
              f"{type(exc).__name__}: {exc}")

    # ---- F7: matcher cases (measured pairs) ------------------------------
    _pairs = [
        # (wanted title, wanted artist, file title, file artist, expected)
        ("ATM", "Tiem & Salii",
         "Bu Üräyin Dərdi Sənsən", "Canım Xəstə", False),   # the 23:54 pair
        ("Fereshteh", "Omid Oghabi",
         "Tighe Tize Zaman [Msbmusic.IR]", "Poori", False),  # msbmusic wrong song
        ("Fereshteh", "Omid Oghabi",
         "Che Fatemiyei", "Mahmood Karimi [ Mifa-Music.ir]", False),  # mifa wrong
        ("Fereshteh", "Omid Oghabi",
         "Fereshteh موزیکدل", "Omid Oghabi موزیکدل", True),  # brand stamp
        ("Fereshteh", "Omid Oghabi",
         "Fereshteh", "Omid Oghabi - Download1Music.IR", True),  # brand stamp
        ("Yani Chi Nemishe", "Hichkas",
         "Yani Chi Nemishe [Msbmusic.IR]", "Hichkas", True),  # bracket suffix
        ("Bang O Hashish", "Big Shaggy",
         "Bang O Hashish", "Big Shaggy", True),              # exact
        ("Dooset Daram", "Mehdi Ahmadvand",
         "Kheili Dooset Daram Ye Rooz", "Mehdi Ahmadvand", False),  # extra words
        ("Taziyane (Remix)", "Amir Tataloo",
         "Taziyane", "Amir Tataloo", True),                  # remix suffix
        ("ATM", "Tiem & Salii", "ATM", "Somebody Else", False),  # artist differs
        ("ATM", "Tiem & Salii", "Terror", "Tiem & Salii", False),  # title differs
        ("Mahvare", "Tiem & Salii", "Mahvare", "Tiem & Salii", True),  # exact
    ]
    try:
        for _wt, _wa, _ft, _fa, _exp in _pairs:
            _got = verify_mod.file_identity_matches(_wt, _wa, _ft, _fa)
            check(f"F7 matcher {_wt!r}/{_wa!r} vs file {_ft!r}/{_fa!r}",
                  _got is _exp, f"got={_got} expected={_exp}")
    except Exception as exc:
        check("F7 matcher cases", False, f"{type(exc).__name__}: {exc}")

    # ---- F7b: verify_fullsong_file statuses on real bytes ----------------
    try:
        _ok = _make_mp3("ATM", "Tiem & Salii", seconds=5)
        _r = verify_mod.verify_fullsong_file(_ok, "mp3", "ATM", "Tiem & Salii")
        check("F7b matching bytes verify as match with real duration",
              _r["status"] == "match" and 4.0 < _r["duration_sec"] < 6.0,
              f"result={_r}")
        _bad = _make_mp3("Bu Üräyin Dərdi Sənsən", "Canım Xəstə", seconds=5)
        _r = verify_mod.verify_fullsong_file(_bad, "mp3", "ATM", "Tiem & Salii")
        check("F7b wrong-song bytes verify as mismatch",
              _r["status"] == "mismatch", f"result={_r}")
        _none = _make_mp3(None, None, seconds=5)
        _r = verify_mod.verify_fullsong_file(_none, "mp3", "ATM", "Tiem & Salii")
        check("F7b untagged bytes verify as unverifiable",
              _r["status"] == "unverifiable", f"result={_r}")
    except Exception as exc:
        check("F7b verify_fullsong_file statuses", False,
              f"{type(exc).__name__}: {exc}")

    # ---- F9: no fingerprinting anywhere in the guard ----------------------
    try:
        from pathlib import Path as _Path
        _vsrc = _Path(vs.ROOT, "app", "search", "fullsong_verify.py").read_text()
        check("F9 verify module uses no fpcalc", "fpcalc" not in _vsrc)
        check("F9 verify module uses no fingerprint analysis",
              "analyse_audio_bytes" not in _vsrc
              and "exclusive_bank" not in _vsrc)
        check("F9 verify module reads tags with ffprobe only",
              "ffprobe" in _vsrc and "ffmpeg" not in _vsrc)
        check("F9 new-caption fa text is byte-identical to the user order",
              t("m_extracted_audio_new", "fa") == _NEW_EXACT,
              f"i18n={t('m_extracted_audio_new', 'fa')!r}")
        _hsrc = _Path(vs.ROOT, "app", "bot", "handlers.py").read_text()
        check("F9 clip-copy helper uses stream copy, never re-encode",
              '"-c:a", "copy"' in _hsrc)
        check("F9 guard calls the tag verifier (not a fingerprint)",
              "verify_fullsong_file" in _hsrc)
    except Exception as exc:
        check("F9 no-fingerprinting checks", False, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
