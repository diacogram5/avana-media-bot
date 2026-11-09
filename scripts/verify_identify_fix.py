"""Sandbox tests for the identify fix (2026-11-09) as reworked by the
confirm fix (2026-11-09) and the result-unification fix (2026-11-09):
NO «شناسایی شد» card is ever sent — a CONFIRMED identification (bank
match or Shazam+AudD agreement) spawns the background delivery at
once (bank-sample arming, exclusive-bank lookup, iTunes/Deezer
catalog search, full-song delivery), and the ONE final message is
the audio with the identified caption. The temporary status message
is threaded through the delivery and deleted once the final message
is sent. The bank list query runs once per process (in-memory
cache).

Cases:
  I1  Delivery order: the call returns fast with NO message sent —
      _arm_bank_sample, _exclusive_song_for, smart_search and the
      full-song delivery all run in the background. The status
      message is threaded to the full-song delivery and deleted once
      it finishes; no identified name is sent before the file.
  I2  Bank cache: the Turso list query runs once; the second identify
      uses the memory cache (zero Turso executes); an admin add /
      update / delete invalidates it and the next read re-queries; a
      failed fetch never populates the cache; the uncached default
      still queries every time.
  I3  Exclusive after delivery spawn: when _exclusive_song_for finds
      a bank song, the exclusive file is sent and the full-song lookup
      never runs; when it misses, the full-song delivery is spawned.
      The status message is threaded to _send_exclusive_song and
      deleted after the send.
  I4  Enrichment: the stored result gains the catalog match's
      url/preview_url after smart_search; no message is ever sent or
      edited before the file.
  I5  Bug-fix guard still holds: a mismatched full-song file is never
      sent; the clip's original audio goes out with the case-(b)
      caption (unify fix 2026-11-09 — the name IS confirmed, so the
      corporate "official version not found" caption no longer
      applies; F2b-style, one focused case — the full 95-check
      suite runs separately in the gate).

Reuses the fake aiogram / fake libsql / fake message harness from
scripts/verify_speedfix.py and the clip fixtures from
scripts/verify_fullsong_match.py. No network, no Telegram, no real Turso.

Run: python3 scripts/verify_identify_fix.py ; exit 0 = all PASS.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_speedfix as vs  # noqa: E402  (installs fake aiogram, imports handlers)
import verify_fullsong_match as vm  # noqa: E402  (clip fixtures, _Msg, _deliver)

import app.bot.exclusive_bank as xb  # noqa: E402
import app.bot.handlers as h  # noqa: E402
from app.features import pro as profeat  # noqa: E402
from app.search import SearchResult  # noqa: E402

check = vs.check
RESULTS = vs.RESULTS
LOG_LINES = vs.LOG_LINES

_TITLE = "Test Song"
_ARTIST = "Test Artist"
_FAKE_MATCH = SearchResult(
    source="itunes", id="1", title=_TITLE, artist=_ARTIST,
    url="https://example.invalid/song",
    preview_url="https://example.invalid/preview.mp3")


class _EventSent(vs._FakeSent):
    """Sent card message that records any edit/delete attempt."""

    def __init__(self, events):
        super().__init__()
        self._events = events

    async def edit_text(self, text, **kw):
        self._events.append(("card_edit_text", text))
        return self

    async def edit_reply_markup(self, **kw):
        self._events.append(("card_edit_markup", ""))
        return self

    async def delete(self):
        self._events.append(("card_delete", ""))
        return None


class _EventMsg(vs._FakeMessage):
    """Fake message that records the card send as an event."""

    def __init__(self, events, *a, **k):
        super().__init__(*a, **k)
        self.events = events

    async def answer(self, text, **kw):
        self.events.append(("card", text))
        return _EventSent(self.events)


class _CountingConn:
    """sqlite3 proxy that counts execute() calls (Turso-query proxy)."""

    def __init__(self, real):
        self._real = real
        self.n = 0

    def execute(self, *a, **k):
        self.n += 1
        return self._real.execute(*a, **k)

    def commit(self):
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


async def _drain(timeout=5.0):
    """Let background tasks finish (both task dicts empty) or time out."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if not h._AFTER_CARD_TASKS and not h._FULLSONG_TASKS:
            return True
        await asyncio.sleep(0.05)
    return False


def _card_key(title=_TITLE, artist=_ARTIST):
    # Most recent matching key: every card call mints a fresh key, and
    # earlier tests used the same title/artist.
    for key, res in reversed(list(profeat.RESULTS.items())):
        if res.get("title") == title and res.get("artist") == artist:
            return key
    return None


async def _run_identified(events, conn, arm, exclusive_song, search_results,
                        fullsong_sleep=0.4):
    """Run _send_professional_song_card with recording stubs.

    Returns (call_seconds, events_at_return, drained, spawned).
    Restores the patched handlers.
    """
    orig_arm = h._arm_bank_sample
    orig_excl = h._exclusive_song_for
    orig_send_excl = h._send_exclusive_song
    orig_search = h.smart_search
    orig_fullsong = h._deliver_fullsong_bg

    async def _fake_arm(c, config, kind, seed, data, url, clue):
        events.append(("arm", kind))
        await asyncio.sleep(0.15)
        return "tok123"

    def _fake_excl(title, artist):
        events.append(("exclusive", title))
        return exclusive_song

    async def _fake_send_excl(message, song, status_msg=None, kb=None,
                              title=None, artist=None, ulang="fa"):
        events.append(("exclusive_send", song.get("title", "")))
        events.append(("exclusive_status_msg", status_msg is not None))
        return True

    async def _fake_search(engine, query, limit=10):
        events.append(("smart_search", query))
        await asyncio.sleep(0.25)
        return (search_results, "")

    async def _fake_fullsong(message, c, title, artist, config, kb,
                             own_media, bank_token, query, ulang,
                             status_msg=None):
        events.append(("fullsong_start", title))
        events.append(("fullsong_status_msg", status_msg is not None))
        await asyncio.sleep(fullsong_sleep)
        # The real delivery owns the status and deletes it in its
        # finally; the fake mirrors that contract.
        await h._delete_card(status_msg)
        events.append(("fullsong_end", title))

    h._arm_bank_sample = _fake_arm
    h._exclusive_song_for = _fake_excl
    h._send_exclusive_song = _fake_send_excl
    h.smart_search = _fake_search
    h._deliver_fullsong_bg = _fake_fullsong
    try:
        msg = _EventMsg(events)
        status = _EventSent(events)
        t0 = time.perf_counter()
        spawned = await h._send_professional_song_card(
            msg, conn, _TITLE, _ARTIST, bot_id="@AvanoMediaBot",
            config=None, arm=arm, status_msg=status)
        dt = time.perf_counter() - t0
        events_at_return = list(events)
        drained = await _drain()
        return dt, events_at_return, drained, spawned
    finally:
        h._arm_bank_sample = orig_arm
        h._exclusive_song_for = orig_excl
        h._send_exclusive_song = orig_send_excl
        h.smart_search = orig_search
        h._deliver_fullsong_bg = orig_fullsong


def _idx(events, name):
    for i, (kind, _v) in enumerate(events):
        if kind == name:
            return i
    return None


async def main():
    conn = vs._make_conn()

    # ---- I1: no message sent; everything runs in the background --------
    try:
        events: list = []
        dt, at_return, drained, spawned = await _run_identified(
            events, conn, ("video_button", "fid123", b"data", "", ""),
            None, [])
        check("I1 NO message is sent when the delivery call returns",
              at_return == [], f"at_return={at_return}")
        check("I1 call returns fast (background work still pending)",
              dt < 0.15, f"dt={dt:.3f}s")
        check("I1 background tasks drained", drained)
        arm_i = _idx(events, "arm")
        excl_i = _idx(events, "exclusive")
        search_i = _idx(events, "smart_search")
        fs_i = _idx(events, "fullsong_start")
        check("I1 arm, exclusive lookup, catalog search run in the background",
              arm_i is not None and excl_i is not None
              and search_i is not None,
              f"events={events}")
        check("I1 full-song delivery starts in the background",
              fs_i is not None, f"events={events}")
        check("I1 no identified name is sent before the file",
              not any(k == "card" for k, _v in events),
              f"events={events}")
        check("I1 delivery call reports spawned",
              spawned is True, f"spawned={spawned!r}")
        check("I1 status is deleted once the background delivery finishes",
              _idx(events, "card_delete") is not None,
              f"events={events}")
        check("I1 full-song delivery receives the status message",
              ("fullsong_status_msg", True) in events,
              f"events={events}")
    except Exception as exc:
        check("I1 instant card order", False, f"{type(exc).__name__}: {exc}")

    # ---- I3: exclusive hit -> file sent, no full-song lookup ------------
    try:
        events = []
        song = {"id": 1, "title": _TITLE, "artist": _ARTIST,
                "ready_file_id": "", "file_id": "bankfid"}
        dt, at_return, drained, spawned = await _run_identified(
            events, conn, ("video_button", "fid123", b"data", "", ""),
            song, [])
        check("I3 exclusive file sent in the background (no card first)",
              _idx(events, "exclusive_send") is not None
              and not any(k == "card" for k, _v in events),
              f"events={events}")
        check("I3 exclusive hit never spawns the full-song lookup",
              _idx(events, "fullsong_start") is None
              and _idx(events, "smart_search") is None,
              f"events={events}")
        check("I3 status message is threaded to _send_exclusive_song",
              ("exclusive_status_msg", True) in events,
              f"events={events}")
        check("I3 status is deleted after the exclusive send",
              _idx(events, "card_delete") is not None,
              f"events={events}")
    except Exception as exc:
        check("I3 exclusive after card", False, f"{type(exc).__name__}: {exc}")

    # ---- I4: enrichment updates the stored result; nothing sent/edited --
    try:
        events = []
        dt, at_return, drained, spawned = await _run_identified(
            events, conn, ("video_button", "fid123", b"data", "", ""),
            None, [_FAKE_MATCH])
        key = _card_key()
        res = profeat.RESULTS.get(key) if key else None
        check("I4 stored result gains the catalog url after the call",
              res is not None and res.get("url") == "https://example.invalid/song",
              f"res={res}")
        check("I4 stored result gains the catalog preview_url",
              res is not None
              and res.get("preview_url") == "https://example.invalid/preview.mp3",
              f"res={res}")
        check("I4 no message is ever edited",
              _idx(events, "card_edit_text") is None
              and _idx(events, "card_edit_markup") is None,
              f"events={events}")
        check("I4 no card text is sent at any point",
              not any(k == "card" for k, _v in events),
              f"events={events}")
    except Exception as exc:
        check("I4 catalog enrichment", False, f"{type(exc).__name__}: {exc}")

    # ---- I2: bank list cache ---------------------------------------------
    try:
        vs._turso_on()
        xb._xb_conn = None
        xb._xb_ok = None
        xb._LIST_CACHE = None
        real = xb._get_conn()
        counting = _CountingConn(real)
        fp = ",".join(str(i) for i in range(50))
        sid1 = xb.add_song(counting, title="Cache Song", artist="Cache Artist",
                           file_id="fid1", file_unique_id="uniq1",
                           duration_sec=200, bitrate_kbps=320, file_size=1000,
                           fingerprint=fp, fp_duration=200, fp_partial=False)
        counting.n = 0
        rows1 = xb.list_songs(counting, active_only=True, use_cache=True)
        check("I2 first cached read queries Turso and returns the song",
              len(rows1) == 1 and counting.n > 0,
              f"rows={len(rows1)} queries={counting.n}")
        counting.n = 0
        rows2 = xb.list_songs(counting, active_only=True, use_cache=True)
        check("I2 second read uses the memory cache (zero Turso queries)",
              rows2 == rows1 and counting.n == 0,
              f"queries={counting.n}")
        sid2 = xb.add_song(counting, title="Cache Song 2", artist="Cache Artist",
                           file_id="fid2", file_unique_id="uniq2",
                           duration_sec=200, bitrate_kbps=320, file_size=1000,
                           fingerprint=fp, fp_duration=200, fp_partial=False)
        counting.n = 0
        rows3 = xb.list_songs(counting, active_only=True, use_cache=True)
        check("I2 admin add invalidates the cache (re-queries, sees 2)",
              len(rows3) == 2 and counting.n > 0,
              f"rows={len(rows3)} queries={counting.n}")
        xb.set_active(counting, sid2, False)
        rows4 = xb.list_songs(counting, active_only=True, use_cache=True)
        check("I2 admin deactivation invalidates (active read sees 1)",
              len(rows4) == 1 and rows4[0]["id"] == sid1,
              f"rows={rows4}")
        xb.delete_song(counting, sid1)
        rows5 = xb.list_songs(counting, use_cache=True)
        check("I2 admin delete invalidates (deleted row gone)",
              all(r["id"] != sid1 for r in rows5),
              f"rows={rows5}")
        counting.n = 0
        rows6 = xb.list_songs(counting, active_only=True, use_cache=True)
        check("I2 cached read after delete is a hit (zero queries, sees 0 active)",
              len(rows6) == 0 and counting.n == 0,
              f"rows={len(rows6)} queries={counting.n}")
        counting.n = 0
        xb.list_songs(counting, active_only=True, use_cache=False)
        n_first = counting.n
        counting.n = 0
        xb.list_songs(counting, active_only=True, use_cache=False)
        check("I2 uncached reads query Turso on every call",
              n_first > 0 and counting.n > 0,
              f"first={n_first} second={counting.n}")
        # Fail open: a broken connection raises and never populates cache.
        xb._LIST_CACHE = None
        bad = sqlite3.connect(":memory:")
        bad.close()
        raised = False
        try:
            xb.list_songs(bad, active_only=True, use_cache=True)
        except Exception:
            raised = True
        check("I2 failed fetch raises (callers fail open) and cache stays empty",
              raised and xb._LIST_CACHE is None,
              f"raised={raised} cache={xb._LIST_CACHE}")
        rows7 = xb.list_songs(counting, active_only=True, use_cache=True)
        check("I2 cache recovers after a failure",
              len(rows7) == 0, f"rows={rows7}")
    except Exception as exc:
        check("I2 bank list cache", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            vs._turso_off()
        except Exception:
            pass
        xb._xb_conn = None
        xb._xb_ok = None
        xb._LIST_CACHE = None

    # ---- I5: bug-fix guard still holds (F2b-style, one focused case) -----
    try:
        vm._MP4 = vm._make_mp4()
        LOG_LINES.clear()
        h._VIDEO_IDENTIFY.clear()
        _kb0 = h._video_identify_kb(572595158, "VID123", "file")
        _tok = _kb0.inline_keyboard[0][0].callback_data.split("vid:id:", 1)[1]
        mp3 = vm._make_mp3("Bu Üräyin Dərdi Sənsən", "Canım Xəstə", seconds=5)
        msg = vm._Msg()
        vm._attach_clip_video(msg)
        conn5 = vs._make_conn()
        await vm._deliver(msg, conn5,
                          vm._full_dict(mp3, "persian_music:musicdel",
                                        duration_sec=209.0),
                          "ATM", "Tiem & Salii",
                          own_media=("video", "VID123"))
        check("I5 mismatched file is never sent (exactly one audio send)",
              len(msg.audio_calls) == 1 and msg.audio_calls[0][0] == "bytes",
              f"calls={msg.audio_calls}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        # 2026-11-09 extracted-buttons fix: the clip audio carries the
        # CORPORATE caption + ONLY the retry button (the keyboard
        # follows the file's origin, not the known name).
        check("I5 clip audio carries the corporate caption byte-exact (name known, no full file)",
              cap == vm._NEW_EXACT, f"caption={cap!r}")
        _kb = msg.audio_kwargs[0].get("reply_markup") if msg.audio_kwargs else None
        check("I5 clip audio has ONLY the retry button in ONE row (never playlist/lyrics)",
              _kb is not None
              and h._keyboard_button_texts(_kb) == ["🔍 جستجوی مجدد آهنگ"]
              and len(_kb.inline_keyboard) == 1
              and _kb.inline_keyboard[0][0].callback_data == f"vid:id:{_tok}",
              f"buttons={h._keyboard_button_texts(_kb) if _kb is not None else None}")
        check("I5 mismatch is logged", vs._log_has("fullsong_mismatch"))
    except Exception as exc:
        check("I5 bug-fix guard still holds", False,
              f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
