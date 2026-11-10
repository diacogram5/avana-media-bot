"""Sandbox tests for Step 1b — the shortcode-keyed identify-result cache
(user order 2026-11-09 17:15).

The problem: a repeated video link is fast, but click-to-song-name was
slow every time (15-27 s) — the Stage-0 hash cache covered only the
identify step, not the file resolution, and the tap re-downloaded the
video before checking any cache. Step 1b stores the FINAL result
(title/artist/source, file KIND, a SENDABLE file reference) keyed by
SHORTCODE — memory LRU (cap 200) first, then Turso — and the bg
identify (spawned at resolve) writes it even before any tap.

Cases (each driven end-to-end through the real handlers unless noted):
  C1  Key function: IG shortcode (reel/p/tv/reels, query, slash),
      Aparat videohash, Telegram file_id, content sha, None, precedence.
  C2  Memory LRU: cap 200, oldest evicted first, a get refreshes
      the LRU position.
  C3  TTLs: confirmed 7d, not_found/unconfirmed 10 min (ordered);
      an expired entry reads as a miss.
  C4  Tap cache-hit FULL song (bank file_id): NO _identify_audio call,
      NO Shazam/AudD stages; exactly one audio send of the stored
      file_id; caption with name + real duration + bank marker +
      measured kbps; exactly [playlist, lyrics] one row; video kb
      [Google, YouTube] + [report]; name_sent cache_hit=true; total
      click_to_identify_ms cache_hit=true; status deleted.
  C5  Tap cache-hit CLIP audio (staged disk file): one audio send from
      disk; corporate caption byte-exact; ONLY the retry button; the
      disk file is deleted and the returned file_id replaces the disk
      path in the cache; NO _identify_audio call.
  C6  Tap cache-MISS: the tap spawns the ONE on-demand bg job (the
      tap itself never downloads or identifies) and awaits it; the
      bg identifies once + resolves the full song; the tap sends it
      from the staged disk file; the store hook leaves the FINAL
      result (sent file_id) in the memory cache; name_sent
      cache_hit=false; the bg downloads the Telegram file once.
  C7  Tap unconfirmed: the bg identifies (single-source) + stages
      the clip; the tap sends it from disk with the corporate
      caption; the clip send is stored with the 10-min TTL (status
      unconfirmed, kind clip, sent file_id) for the next tap.
  C8  BG write (full song): _identify_bg_link, fed the merged video
      file on disk, identifies + resolves the file and writes the
      FINAL result (kind full, staged disk file) before any tap; a
      later tap then sends from disk with NO _identify_audio call.
  C9  BG write (clip): an unconfirmed bg result stages the clip's own
      audio to disk (kind clip, real duration) for the next tap.
  C10 Wrong-song: the disputed result is cleared from memory before
      the re-check and from Turso after the answer (only when the row
      still holds the disputed name); the NEW result is stored under
      the same key; the old name is never re-sent.
  C11 Turso round-trip: store/get/delete, conditional delete
      (atomic — a replaced row survives), expiry deletes the row,
      resolve_ms round-trips.
  C12 BG wait: _await_identify_bg returns True when the bg finishes
      in time and False on timeout WITHOUT cancelling the bg task;
      _spawn_identify_bg reuses the in-flight task per key; the
      await ceiling is 120 s; a tap spawn with tg_file_id+bot
      creates the bg task (direct-file path).
  C13 Wiring: the bg identify is spawned at resolve (not after the
      video is sent) and _send_link_video signals the video-ready
      event (source checks, same precedent as F6); the tap body
      contains no video-download / inline-identify call (the miss
      goes through the bg job only).
  C14 Timing stages: wait_bg / search_file / send appear in the
      identify_stage lines AND in log_total, on hit and miss;
      wait_bg=0 on a pure hit; on a hit search_file_ms is 0 (the tap
      does no file search — the stored entry's resolve_ms is logged
      separately as stored_resolve_ms, fix round 3 L2); on a miss
      search_file_ms equals the bg entry's resolve_ms.
  C15 File_id-only hit: the stored file_id is sent with NO download,
      NO _identify_audio call and NO file-search call (find /
      smart_search / exclusive).
  C16 Miss awaits the SAME bg job: a pre-seeded in-flight bg task
      is awaited (wait_bg proves the wait); no second job is
      spawned, no second download/identify starts.
  C17 Stale file_id: the stored file_id is attempted exactly once,
      invalidated, the bg re-resolves and the fresh file is sent.
  C18 Store guard: a file_id-less (disk-staged) store never
      overwrites an entry that holds a SENT file_id — memory, and
      the Turso row after a restart (memory empty); a store WITH a
      file_id always goes through.
  C19 BG early return on a Turso file_id row: after a restart the
      bg does not re-identify when the Turso row already holds the
      sent file_id.
  C20 Wrong-song unconfirmed: a re-check that yields an unconfirmed
      name stages the clip + stores it (kind clip, 10-min TTL) for
      the next tap — the entry cleared at the start is not left
      empty.
  C21 (fix round 3) Stage-0 not_found is a SHORT negative: a
      not_found row older than 10 min reads as a miss (re-identified),
      while an identified row of the same age still hits (7d); a
      caller-supplied smaller max_age is still respected.
  C22 (fix round 3) Turso song_file_cache writes: a smart-tap found
      file and a disk-staged FULL song sent from cache both mirror
      the file_id to Turso (verified tags); a staged CLIP sent from
      cache does NOT (it is not a full song).
  C23 (fix round 3) TTL-aware LRU put: when the 200-entry cap is
      hit, expired entries are evicted before the oldest LIVE entry.
  C24 (fix round 3) Smart negative cache: capped at 1000 entries,
      and forget_negative drops a stored negative (Q4's confirmed
      retry must not wait out the 10-minute window).
  C25 (fix round 3) URL normalization: www vs bare host and
      /reel/ vs /p/ spellings of the same reel share one url-cache
      key (local + Turso).

Reuses the fake aiogram / fake libsql / fake message harness from
scripts/verify_speedfix.py, the clip fixtures from
scripts/verify_fullsong_match.py, the video-token helpers from
scripts/verify_wrong_song.py and the video-tap driver shape from
scripts/verify_identify_ui_final.py. No network, no Telegram, no real
Turso.

Run: python3 scripts/verify_identify_cache.py ; exit 0 = all PASS.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_speedfix as vs  # noqa: E402  (installs fake aiogram, imports handlers)
import verify_fullsong_match as vm  # noqa: E402  (clip fixtures, _Msg)
import verify_wrong_song as vw  # noqa: E402  (video-token helpers)

import app.bot.handlers as h  # noqa: E402
import app.bot.turso_cache as tc  # noqa: E402
import app.bot.file_cache as fc  # noqa: E402
import app.db as dbmod  # noqa: E402
import app.search.smart_fullsong as sf  # noqa: E402
from app.services.audio_id import AudioIdResult  # noqa: E402

check = vs.check
RESULTS = vs.RESULTS
LOG_LINES = vs.LOG_LINES
adapters_mod = vs.adapters_mod

_TITLE = "Flick Shot!"
_ARTIST = "Ali Geramy"
_NEW_TITLE = "Mahvare"
_NEW_ARTIST = "Tiem"
_PL_FA = "🎵 افزودن به پلی‌لیست"
_LYRICS_FA = "📝 متن آهنگ و ترجمه"
_REPORT_FA = "⚠️ گزارش شناسایی اشتباه ⚠️"
_VGOOGLE_FA = "🌐 جستجو در Google"
_VYT_FA = "▶️ جستجو در YouTube"
_RETRY_FA = "🔍 جستجوی مجدد آهنگ"
_NEW_EXACT = vm._NEW_EXACT
_IG_LINK = "https://www.instagram.com/reel/TEST123/"
_IG_KEY = "ig:TEST123"


def _kb_texts(kb) -> list:
    return vw._kb_texts(kb)


def _kb_callbacks(kb) -> list:
    return vw._kb_callbacks(kb)


def _kb_rows(kb) -> list:
    return [[getattr(b, "text", "") or "" for b in row]
            for row in getattr(kb, "inline_keyboard", []) or []]


def _clear_cache():
    h._IDENTIFY_RESULT_CACHE.clear()
    h._IDENTIFY_BG_TASKS.clear()
    h._IDENTIFY_BG_VIDEO_READY.clear()


async def _run_tap(scripted, find_result, link=_IG_LINK,
                   file_id="VID123", calls=None, dl_calls=None,
                   search_calls=None, fail_fid=None, fid_attempts=None):
    """Drive one video-button tap with a RECORDING _identify_audio
    fake and a mocked full-song find. Returns (msg, cb, token).
    dl_calls records every Telegram-file download (the bg's ONE
    download on a miss; the tap itself never downloads);
    search_calls records every file-search call (find / smart_search
    / exclusive); fail_fid makes the FIRST answer_audio of that
    file_id raise (stale file_id simulation), recorded in
    fid_attempts."""
    kb = h._video_identify_kb(572595158, file_id, "file", link_url=link)
    token = kb.inline_keyboard[0][0].callback_data.split("vid:id:", 1)[1]
    msg = vw._VideoMsg()
    msg.reply_markup = kb
    vm._attach_clip_video(msg)

    async def _rec_get_file(_fid):
        return SimpleNamespace(file_path="clip.mp4")

    async def _rec_download(_path, buf):
        if dl_calls is not None:
            dl_calls.append(1)
        buf.write(vm._MP4)

    msg.bot.get_file = _rec_get_file
    msg.bot.download_file = _rec_download
    if fail_fid is not None:
        _orig_answer_audio = msg.answer_audio

        async def _flaky_answer_audio(audio, **kw):
            if (audio == fail_fid and fid_attempts is not None
                    and len(fid_attempts) == 0):
                fid_attempts.append(audio)
                raise RuntimeError("wrong file identifier (400)")
            return await _orig_answer_audio(audio, **kw)

        msg.answer_audio = _flaky_answer_audio
    orig_ident = h._identify_audio
    orig_find = adapters_mod.find_full_song_file_v2
    orig_search = h.smart_search
    orig_excl = h._exclusive_song_for

    async def _fake_ident(data, filename, config, conn=None,
                          cache_url_key=None, req_id=None, t_req=None,
                          exclude=None, skip_bank=False, **_kw):
        if calls is not None:
            calls.append(1)
        return scripted

    async def _fake_find(a, t):
        if search_calls is not None:
            search_calls.append(("find", a, t))
        return find_result

    async def _fake_search(engine, query, limit=10):
        if search_calls is not None:
            search_calls.append(("search", engine, query))
        return ([], "")

    def _fake_excl(t, a):
        if search_calls is not None:
            search_calls.append(("exclusive", t, a))
        return None

    h._identify_audio = _fake_ident
    adapters_mod.find_full_song_file_v2 = _fake_find
    h.smart_search = _fake_search
    h._exclusive_song_for = _fake_excl
    try:
        conn = vs._make_conn()
        cb = vw._FakeCallback(msg, f"vid:id:{token}")
        await h.video_identify(cb, conn, vw._config())
        await vw._drain()
        return msg, cb, token
    finally:
        h._identify_audio = orig_ident
        adapters_mod.find_full_song_file_v2 = orig_find
        h.smart_search = orig_search
        h._exclusive_song_for = orig_excl


def _seed(key, **kw):
    ent = h._identify_result_entry(
        kw.pop("status", "identified"),
        kw.pop("title", _TITLE), kw.pop("artist", _ARTIST),
        kw.pop("source", "shazam+audd"),
        kind=kw.pop("kind", "full"),
        file_id=kw.pop("file_id", ""),
        disk_path=kw.pop("disk_path", ""),
        duration_sec=kw.pop("duration_sec", 0),
        quality=kw.pop("quality", ""),
        extra=kw.pop("extra", ""),
        cover_file_id=kw.pop("cover_file_id", ""),
        resolve_ms=kw.pop("resolve_ms", 0))
    h._identify_result_put(key, ent)
    return ent


def _log_line_with(stage: str, frag: str) -> str:
    for line in LOG_LINES:
        if f'"stage": "{stage}"' in line and frag in line:
            return line
    return ""


async def main():
    vm._MP4 = vm._make_mp4()
    _clear_cache()

    # ---- C1: key function -------------------------------------------
    try:
        check("C1a IG reel URL -> ig:shortcode",
              h._identify_result_key(link="https://www.instagram.com/reel/AbC123_-x/") == "ig:AbC123_-x")
        check("C1b IG post URL with query -> ig:shortcode",
              h._identify_result_key(link="https://www.instagram.com/p/DfaZapXx5c4/?igsh=abc") == "ig:DfaZapXx5c4")
        check("C1c IG tv/reels URLs -> ig:shortcode",
              h._identify_result_key(link="https://www.instagram.com/tv/ZZ9/") == "ig:ZZ9"
              and h._identify_result_key(link="https://www.instagram.com/reels/ZZ9/") == "ig:ZZ9")
        check("C1d Aparat URL -> ap:videohash",
              h._identify_result_key(link="https://www.aparat.com/v/abc12") == "ap:abc12")
        check("C1e Telegram file_id -> tg:file_id",
              h._identify_result_key(file_id="VID123") == "tg:VID123")
        check("C1f content bytes -> sha:hex",
              h._identify_result_key(data=b"abc") ==
              "sha:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        check("C1g nothing derivable -> None",
              h._identify_result_key() is None)
        check("C1h link wins over file_id (shortcode is stable across re-sends)",
              h._identify_result_key(link=_IG_LINK, file_id="VID123") == _IG_KEY)
    except Exception as exc:
        check("C1 key function", False, f"{type(exc).__name__}: {exc}")

    # ---- C2: memory LRU ----------------------------------------------
    try:
        _clear_cache()
        for i in range(201):
            h._identify_result_put(f"ig:K{i}", h._identify_result_entry(
                "identified", f"T{i}", "A", "shazam+audd", kind="full",
                file_id=f"F{i}"))
        check("C2a memory LRU caps at 200 entries",
              len(h._IDENTIFY_RESULT_CACHE) == 200,
              f"len={len(h._IDENTIFY_RESULT_CACHE)}")
        check("C2b oldest entry evicted first",
              h._identify_result_get("ig:K0") is None
              and h._identify_result_get("ig:K1") is not None)
        h._identify_result_get("ig:K1")
        h._identify_result_put("ig:K201", h._identify_result_entry(
            "identified", "T201", "A", "shazam+audd", kind="full",
            file_id="F201"))
        check("C2c a get refreshes the LRU position (touched entry survives)",
              h._identify_result_get("ig:K1") is not None
              and h._identify_result_get("ig:K2") is None)
    except Exception as exc:
        check("C2 memory LRU", False, f"{type(exc).__name__}: {exc}")

    # ---- C3: TTLs ------------------------------------------------------
    try:
        _clear_cache()
        _c = h._identify_result_entry("identified", _TITLE, _ARTIST)
        _n = h._identify_result_entry("not_found")
        _u = h._identify_result_entry("unconfirmed", _TITLE, _ARTIST)
        check("C3a confirmed TTL is 7d (matches song_file_cache)",
              _c["ttl"] == 7 * 86400, f"ttl={_c['ttl']}")
        check("C3b not_found TTL is 10 min (ordered)",
              _n["ttl"] == 600, f"ttl={_n['ttl']}")
        check("C3c unconfirmed TTL is 10 min (single-source is not long-lived)",
              _u["ttl"] == 600, f"ttl={_u['ttl']}")
        _old = h._identify_result_entry("not_found")
        _old["created_at"] = time.time() - 601
        h._identify_result_put("ig:OLD", _old)
        check("C3d an expired entry reads as a miss",
              h._identify_result_get("ig:OLD") is None)
    except Exception as exc:
        check("C3 TTLs", False, f"{type(exc).__name__}: {exc}")

    # ---- C4: tap cache-hit, FULL song (bank file_id) -------------------
    try:
        _clear_cache()
        LOG_LINES.clear()
        _seed(_IG_KEY, source="bank", file_id="BANKFID1",
              duration_sec=280, quality="کیفیت واقعی: 320kbps",
              extra="💎 نسخه کامل اختصاصی آوانو")
        calls: list = []
        msg, cb, token = await _run_tap(
            AudioIdResult(status="identified", title="SHOULD-NOT-USE",
                          artist="SHOULD-NOT-USE"),
            None, calls=calls)
        check("C4a cache hit: _identify_audio is NEVER called (no bank/Shazam/AudD)",
              calls == [] and not vs._log_has("api_shazam")
              and not vs._log_has("api_audd"),
              f"calls={len(calls)}")
        check("C4b exactly ONE audio send, of the STORED file_id",
              len(msg.audio_calls) == 1 and msg.audio_calls[0] == ("str", "BANKFID1"),
              f"audio={msg.audio_calls}")
        _akw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("C4c full-song caption: name + real duration + bank marker + kbps",
              _TITLE in _akw.get("caption", "")
              and _ARTIST in _akw.get("caption", "")
              and "4:40" in _akw.get("caption", "")
              and "💎" in _akw.get("caption", "")
              and "320kbps" in _akw.get("caption", ""),
              f"caption={_akw.get('caption')!r}")
        check("C4d full-song buttons: exactly [playlist, lyrics] in ONE row",
              _kb_texts(_akw.get("reply_markup")) == [_PL_FA, _LYRICS_FA]
              and len(_akw.get("reply_markup").inline_keyboard) == 1,
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        _vkb = msg.markup_edits[-1] if msg.markup_edits else None
        check("C4e video keyboard: [Google, YouTube] + [report] (unchanged lock)",
              _vkb is not None
              and _kb_rows(_vkb) == [[_VGOOGLE_FA, _VYT_FA], [_REPORT_FA]],
              f"rows={_kb_rows(_vkb) if _vkb is not None else None}")
        check("C4f name_sent logged with cache_hit=true",
              _log_line_with("name_sent", '"cache_hit": true') != "",
              f"lines={[l for l in LOG_LINES if 'name_sent' in l]}")
        check("C4g total click_to_identify_ms logged with cache_hit=true",
              _log_line_with("total", '"cache_hit": true') != ""
              and '"click_to_identify_ms"' in _log_line_with("total", '"cache_hit": true'),
              f"lines={[l for l in LOG_LINES if 'click_to_identify' in l]}")
        check("C4h temp status is deleted",
              msg.sent_messages and msg.sent_messages[0].deleted,
              f"deleted={[s.deleted for s in msg.sent_messages]}")
    except Exception as exc:
        check("C4 tap cache-hit full song", False, f"{type(exc).__name__}: {exc}")

    # ---- C5: tap cache-hit, CLIP audio (staged disk file) --------------
    try:
        _clear_cache()
        LOG_LINES.clear()
        _mp3 = vm._make_mp3(_TITLE, _ARTIST, seconds=5)
        _disk = h._ident_disk_path(_IG_KEY, "mp3")
        with open(_disk, "wb") as _f:
            _f.write(_mp3)
        _seed(_IG_KEY, status="unconfirmed", source="shazam",
              kind="clip", disk_path=_disk, duration_sec=5.0)
        calls = []
        msg, cb, token = await _run_tap(
            AudioIdResult(status="identified", title="SHOULD-NOT-USE",
                          artist="SHOULD-NOT-USE"),
            None, calls=calls)
        check("C5a cache hit: _identify_audio is NEVER called",
              calls == [], f"calls={len(calls)}")
        check("C5b exactly ONE audio send, from the staged disk file",
              len(msg.audio_calls) == 1 and msg.audio_calls[0][0] == "bytes",
              f"audio={msg.audio_calls}")
        _akw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("C5c clip caption is the corporate caption byte-exact",
              _akw.get("caption", "") == _NEW_EXACT,
              f"caption={_akw.get('caption')!r}")
        check("C5d clip audio has ONLY the retry button",
              _kb_texts(_akw.get("reply_markup")) == [_RETRY_FA]
              and f"vid:id:{token}" in _kb_callbacks(_akw.get("reply_markup")),
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        check("C5e disk file deleted after first send; file_id replaces the path",
              not os.path.exists(_disk)
              and (h._identify_result_get(_IG_KEY) or {}).get("file_id") == "BYTES_FILE_ID"
              and (h._identify_result_get(_IG_KEY) or {}).get("disk_path") == "",
              f"disk_exists={os.path.exists(_disk)} "
              f"entry={h._identify_result_get(_IG_KEY)}")
        check("C5f name_sent logged with cache_hit=true",
              _log_line_with("name_sent", '"cache_hit": true') != "")
    except Exception as exc:
        check("C5 tap cache-hit clip audio", False, f"{type(exc).__name__}: {exc}")

    # ---- C6: tap cache-MISS goes through the ON-DEMAND bg job --------
    try:
        _clear_cache()
        LOG_LINES.clear()
        mp3 = vm._make_mp3(_TITLE, _ARTIST, seconds=5)
        calls = []
        dl = []
        msg, cb, token = await _run_tap(
            AudioIdResult(status="identified", title=_TITLE,
                          artist=_ARTIST, confirmed=True,
                          source="shazam+audd"),
            vm._full_dict(mp3, "persian_music:test", duration_sec=5.0),
            calls=calls, dl_calls=dl)
        check("C6a cache miss: _identify_audio IS called once (by the bg job, never by the tap)",
              len(calls) == 1, f"calls={len(calls)}")
        check("C6b full song is sent (from the bg's staged disk file)",
              len(msg.audio_calls) == 1 and msg.audio_calls[0][0] == "bytes",
              f"audio={msg.audio_calls}")
        _ent = h._identify_result_get(_IG_KEY)
        check("C6c the store hook leaves the FINAL result in the memory cache",
              _ent is not None and _ent.get("kind") == "full"
              and _ent.get("file_id") == "BYTES_FILE_ID"
              and _ent.get("title") == _TITLE,
              f"entry={_ent}")
        check("C6d name_sent logged with cache_hit=false",
              _log_line_with("name_sent", '"cache_hit": false') != "",
              f"lines={[l for l in LOG_LINES if 'name_sent' in l]}")
        check("C6e the Telegram file is downloaded exactly ONCE (by the bg job; the tap downloads nothing)",
              len(dl) == 1, f"downloads={len(dl)}")
    except Exception as exc:
        check("C6 tap cache-miss", False, f"{type(exc).__name__}: {exc}")

    # ---- C7: tap unconfirmed stores the clip (10-min TTL) --------------
    try:
        _clear_cache()
        LOG_LINES.clear()
        calls = []
        dl = []
        msg, cb, token = await _run_tap(
            AudioIdResult(status="unconfirmed", title=_TITLE,
                          artist=_ARTIST, confirmed=False,
                          source="shazam"),
            None, calls=calls, dl_calls=dl)
        check("C7a unconfirmed tap sends exactly ONE extracted audio (bg identified once)",
              len(msg.audio_calls) == 1 and len(calls) == 1,
              f"audio={msg.audio_calls} calls={len(calls)}")
        _akw7 = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("C7b the clip caption is the corporate caption byte-exact",
              _akw7.get("caption", "") == _NEW_EXACT,
              f"caption={_akw7.get('caption')!r}")
        _ent = h._identify_result_get(_IG_KEY)
        check("C7c the clip result is stored (status unconfirmed, kind clip, 10-min TTL, sent file_id)",
              _ent is not None and _ent.get("status") == "unconfirmed"
              and _ent.get("kind") == "clip" and _ent.get("ttl") == 600
              and bool(_ent.get("file_id")),
              f"entry={_ent}")
        check("C7d the Telegram file is downloaded exactly ONCE (by the bg job)",
              len(dl) == 1, f"downloads={len(dl)}")
    except Exception as exc:
        check("C7 tap unconfirmed stores clip", False, f"{type(exc).__name__}: {exc}")

    # ---- C8: bg write (full song) + later tap hits disk -----------------
    try:
        _clear_cache()
        LOG_LINES.clear()
        _key = "ig:TESTBG"
        _vpath = os.path.join(tempfile.mkdtemp(prefix="ident_bg_"), "video.mp4")
        with open(_vpath, "wb") as _f:
            _f.write(vm._MP4)
        _ev = asyncio.Event()
        _ev.set()
        h._IDENTIFY_BG_VIDEO_READY[_key] = (_ev, _vpath)
        mp3 = vm._make_mp3(_TITLE, _ARTIST, seconds=5)
        orig_ident = h._identify_audio
        orig_find = adapters_mod.find_full_song_file_v2
        orig_excl = h._exclusive_song_for

        async def _fake_ident8(data, filename, config, conn=None,
                               cache_url_key=None, req_id=None, t_req=None,
                               **_kw):
            return AudioIdResult(status="identified", title=_TITLE,
                                 artist=_ARTIST, confirmed=True,
                                 source="shazam+audd")

        async def _fake_find8(a, t):
            return vm._full_dict(mp3, "persian_music:test", duration_sec=5.0)

        h._identify_audio = _fake_ident8
        adapters_mod.find_full_song_file_v2 = _fake_find8
        h._exclusive_song_for = lambda t, a: None
        try:
            conn = vs._make_conn()
            await h._identify_bg_link(
                _key, "https://example.invalid/audio.mp3", 572595158,
                conn, vw._config())
        finally:
            h._identify_audio = orig_ident
            adapters_mod.find_full_song_file_v2 = orig_find
            h._exclusive_song_for = orig_excl
        _ent = h._identify_result_get(_key)
        check("C8a bg writes the FINAL result before any tap (kind full)",
              _ent is not None and _ent.get("kind") == "full"
              and _ent.get("title") == _TITLE,
              f"entry={_ent}")
        check("C8b bg stages the full file on DISK (bytes freed, file exists)",
              _ent is not None and bool(_ent.get("disk_path"))
              and os.path.exists(_ent.get("disk_path") or "")
              and os.path.getsize(_ent.get("disk_path") or "") == len(mp3),
              f"disk={(_ent or {}).get('disk_path')}")
        calls = []
        msg, cb, token = await _run_tap(
            AudioIdResult(status="identified", title="SHOULD-NOT-USE",
                          artist="SHOULD-NOT-USE"),
            None, link="https://www.instagram.com/reel/TESTBG/",
            calls=calls)
        check("C8c the later tap sends from the staged disk file, NO re-identify",
              calls == [] and len(msg.audio_calls) == 1
              and msg.audio_calls[0][0] == "bytes",
              f"calls={len(calls)} audio={msg.audio_calls}")
    except Exception as exc:
        check("C8 bg write full song", False, f"{type(exc).__name__}: {exc}")

    # ---- C9: bg write (clip) for an unconfirmed result ------------------
    try:
        _clear_cache()
        _key = "ig:TESTBG2"
        _vpath = os.path.join(tempfile.mkdtemp(prefix="ident_bg_"), "video.mp4")
        with open(_vpath, "wb") as _f:
            _f.write(vm._MP4)
        _ev = asyncio.Event()
        _ev.set()
        h._IDENTIFY_BG_VIDEO_READY[_key] = (_ev, _vpath)
        orig_ident = h._identify_audio
        orig_find = adapters_mod.find_full_song_file_v2
        orig_excl = h._exclusive_song_for

        async def _fake_ident9(data, filename, config, conn=None,
                               cache_url_key=None, req_id=None, t_req=None,
                               **_kw):
            return AudioIdResult(status="unconfirmed", title=_TITLE,
                                 artist=_ARTIST, confirmed=False,
                                 source="shazam")

        async def _fake_find9(a, t):
            return None

        h._identify_audio = _fake_ident9
        adapters_mod.find_full_song_file_v2 = _fake_find9
        h._exclusive_song_for = lambda t, a: None
        try:
            conn = vs._make_conn()
            await h._identify_bg_link(
                _key, "https://example.invalid/audio.mp3", 572595158,
                conn, vw._config())
        finally:
            h._identify_audio = orig_ident
            adapters_mod.find_full_song_file_v2 = orig_find
            h._exclusive_song_for = orig_excl
        _ent = h._identify_result_get(_key)
        check("C9a bg stages the clip's own audio (kind clip, 10-min TTL)",
              _ent is not None and _ent.get("kind") == "clip"
              and _ent.get("status") == "unconfirmed"
              and _ent.get("ttl") == 600,
              f"entry={_ent}")
        check("C9b the staged clip file exists with a real duration",
              _ent is not None and bool(_ent.get("disk_path"))
              and os.path.exists(_ent.get("disk_path") or "")
              and os.path.getsize(_ent.get("disk_path") or "") > 0
              and (_ent.get("duration_sec") or 0) > 0,
              f"entry={_ent}")
    except Exception as exc:
        check("C9 bg write clip audio", False, f"{type(exc).__name__}: {exc}")

    # ---- C10: wrong-song clears the cache, new result stored ------------
    try:
        _clear_cache()
        vs._turso_on()
        LOG_LINES.clear()
        token, entry, _kb = vw._mint_video_token(
            link=_IG_LINK)
        # The disputed result, in memory AND Turso.
        _seed(_IG_KEY, title=vw._WRONG_TITLE, artist=vw._WRONG_ARTIST,
              source="shazam+audd", kind="full", file_id="WRONGFID")
        tc.store_identify_result(_IG_KEY, h._identify_result_get(_IG_KEY))
        msg = vw._VideoMsg()
        msg.reply_markup = None
        vm._attach_clip_video(msg)
        mp3 = vm._make_mp3(_NEW_TITLE, _NEW_ARTIST, seconds=5)
        orig_ident = h._identify_audio
        orig_find = adapters_mod.find_full_song_file_v2
        orig_search = h.smart_search
        orig_excl = h._exclusive_song_for
        calls: list = []

        async def _fake_ident10(data, filename, config, conn=None,
                                cache_url_key=None, req_id=None, t_req=None,
                                exclude=None, skip_bank=False, **_kw):
            calls.append({"exclude": list(exclude or []),
                          "skip_bank": skip_bank})
            return AudioIdResult(status="identified", title=_NEW_TITLE,
                                 artist=_NEW_ARTIST, confirmed=True,
                                 source="shazam+audd")

        async def _fake_find10(a, t):
            return vm._full_dict(mp3, "persian_music:test", duration_sec=5.0)

        async def _fake_search10(engine, query, limit=10):
            return ([], "")

        h._identify_audio = _fake_ident10
        adapters_mod.find_full_song_file_v2 = _fake_find10
        h.smart_search = _fake_search10
        h._exclusive_song_for = lambda t, a: None
        try:
            conn = vs._make_conn()
            cb = vw._FakeCallback(msg, f"wrong:{token}")
            await h.wrong_song(cb, conn, vw._config())
            await vw._drain()
        finally:
            h._identify_audio = orig_ident
            adapters_mod.find_full_song_file_v2 = orig_find
            h.smart_search = orig_search
            h._exclusive_song_for = orig_excl
        check("C10a the re-check excludes the disputed name",
              calls and (vw._WRONG_TITLE, vw._WRONG_ARTIST) in calls[0]["exclude"],
              f"calls={calls}")
        _ent = h._identify_result_get(_IG_KEY)
        check("C10b memory holds the NEW result, never the disputed one",
              _ent is not None and _ent.get("title") == _NEW_TITLE
              and _ent.get("kind") == "full",
              f"entry={_ent}")
        _row = tc.get_identify_result(_IG_KEY)
        check("C10c Turso holds the NEW result (conditional delete kept it)",
              _row is not None and _row.get("title") == _NEW_TITLE,
              f"row={_row}")
        _akw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("C10d the sent audio is the NEW song, not the disputed one",
              _NEW_TITLE in _akw.get("caption", "")
              and vw._WRONG_TITLE not in _akw.get("caption", ""),
              f"caption={_akw.get('caption')!r}")
    except Exception as exc:
        check("C10 wrong-song clears cache", False, f"{type(exc).__name__}: {exc}")
    finally:
        vs._turso_off()

    # ---- C11: Turso round-trip ------------------------------------------
    try:
        vs._turso_on()
        _k = "ig:TURSO1"
        _e = h._identify_result_entry(
            "identified", _TITLE, _ARTIST, "shazam+audd", kind="full",
            file_id="TFID", duration_sec=200,
            quality="کیفیت واقعی: 320kbps",
            extra="💎 نسخه کامل اختصاصی آوانو",
            cover_file_id="COVER1")
        check("C11a Turso store returns True",
              tc.store_identify_result(_k, _e) is True)
        _g = tc.get_identify_result(_k)
        check("C11b Turso round-trip keeps title/artist/kind/file/quality/extra/cover",
              _g is not None and _g.get("title") == _TITLE
              and _g.get("kind") == "full" and _g.get("file_id") == "TFID"
              and _g.get("duration_sec") == 200
              and _g.get("quality") == "کیفیت واقعی: 320kbps"
              and _g.get("extra") == "💎 نسخه کامل اختصاصی آوانو"
              and _g.get("cover_file_id") == "COVER1",
              f"row={_g}")
        check("C11c Turso delete removes the row",
              tc.delete_identify_result(_k) is True
              and tc.get_identify_result(_k) is None)
        tc.store_identify_result(_k, _e)
        check("C11d conditional delete: a REPLACED row survives (atomic)",
              tc.delete_identify_result_if(
                  _k, "Some Other Song", "Someone Else") is False
              and tc.get_identify_result(_k) is not None)
        check("C11e conditional delete: the matching row is removed",
              tc.delete_identify_result_if(_k, _TITLE, _ARTIST) is True
              and tc.get_identify_result(_k) is None)
        _exp = h._identify_result_entry("not_found")
        _exp["created_at"] = time.time() - 3600
        tc.store_identify_result("ig:EXP", _exp)
        check("C11f an expired Turso row reads as a miss (and is deleted)",
              tc.get_identify_result("ig:EXP") is None
              and tc.get_identify_result("ig:EXP") is None)
        _e2 = h._identify_result_entry(
            "identified", _TITLE, _ARTIST, "shazam+audd", kind="full",
            file_id="TFID2", duration_sec=200, resolve_ms=1234)
        _stored2 = tc.store_identify_result("ig:TURSO2", _e2)
        _g2 = tc.get_identify_result("ig:TURSO2")
        check("C11g Turso round-trip keeps resolve_ms (the search_file stage)",
              _stored2 is True and _g2 is not None
              and _g2.get("resolve_ms") == 1234,
              f"row={_g2}")
    except Exception as exc:
        check("C11 Turso round-trip", False, f"{type(exc).__name__}: {exc}")
    finally:
        vs._turso_off()

    # ---- C12: bg wait + spawn reuse --------------------------------------
    try:
        _clear_cache()
        _key = "ig:WAIT1"

        async def _slow_bg():
            await asyncio.sleep(0.3)
            h._identify_result_put(
                _key, h._identify_result_entry(
                    "identified", _TITLE, _ARTIST, "shazam+audd",
                    kind="full", file_id="WAITFID"))

        _t1 = asyncio.create_task(_slow_bg())
        h._IDENTIFY_BG_TASKS[_key] = _t1
        _ok = await h._await_identify_bg(_key, timeout=5.0)
        check("C12a _await_identify_bg returns True when the bg finishes in time",
              _ok is True
              and (h._identify_result_get(_key) or {}).get("file_id") == "WAITFID")

        async def _slower_bg():
            await asyncio.sleep(5.0)

        _t2 = asyncio.create_task(_slower_bg())
        h._IDENTIFY_BG_TASKS[_key] = _t2
        _t0 = time.perf_counter()
        _ok2 = await h._await_identify_bg(_key, timeout=0.2)
        _dt = time.perf_counter() - _t0
        check("C12b timeout returns False fast WITHOUT cancelling the bg task",
              _ok2 is False and _dt < 2.0 and not _t2.done(),
              f"ok={_ok2} dt={_dt:.2f} done={_t2.done()}")
        _t2.cancel()
        try:
            await _t2
        except asyncio.CancelledError:
            pass
        _spawned = h._spawn_identify_bg(
            "ig:SPAWN1", "https://example.invalid/a.mp3", 572595158,
            None, vw._config())
        _again = h._spawn_identify_bg(
            "ig:SPAWN1", "https://example.invalid/a.mp3", 572595158,
            None, vw._config())
        check("C12c a repeat spawn reuses the in-flight task (max 1 per key)",
              _spawned is not None and _again is _spawned)
        if _spawned is not None:
            _spawned.cancel()
            try:
                await _spawned
            except asyncio.CancelledError:
                pass
        check("C12d the bg await ceiling is 120 s (the tap waits for the bg's natural completion)",
              h._IDENTIFY_BG_AWAIT_CEILING == 120.0,
              f"ceiling={h._IDENTIFY_BG_AWAIT_CEILING}")

        async def _tg_get_file(_fid):
            return SimpleNamespace(file_path="clip.mp4")

        async def _tg_dl(_path, buf):
            buf.write(vm._MP4)

        _tgbot = SimpleNamespace(get_file=_tg_get_file,
                                 download_file=_tg_dl)
        _tg_task = h._spawn_identify_bg(
            "ig:TGSPAWN", "", 572595158, None, vw._config(),
            tg_file_id="VID123", bot=_tgbot)
        check("C12e a tap spawn with tg_file_id+bot creates the bg task (direct-file path)",
              _tg_task is not None
              and h._IDENTIFY_BG_TASKS.get("ig:TGSPAWN") is _tg_task)
        if _tg_task is not None:
            _tg_task.cancel()
            try:
                await _tg_task
            except asyncio.CancelledError:
                pass
    except Exception as exc:
        check("C12 bg wait + spawn", False, f"{type(exc).__name__}: {exc}")

    # ---- C13: wiring (source checks) --------------------------------------
    try:
        _src = Path(__file__).resolve().parent.parent.joinpath(
            "app", "bot", "handlers.py").read_text(encoding="utf-8")
        _i_res = _src.find("_resolved = await resolve_instagram_media(")
        _i_spawn = _src.find("_spawn_identify_bg(_bg_key, _resolved[0], _cid, conn,",
                             _i_res)
        _i_send = _src.find("video_msg = await _send_link_video(")
        check("C13a the bg identify is spawned at RESOLVE (before the video send)",
              _i_res > 0 and _i_spawn > _i_res and _i_send > _i_spawn,
              f"resolve={_i_res} spawn={_i_spawn} send={_i_send}")
        check("C13b the old after-send precompute spawn is retired",
              "_spawn_bg_precompute(_resolved[0], _cid, conn, _req_id)" not in _src)
        check("C13c _send_link_video signals the video-ready event",
              "_signal_identify_bg_video_ready(" in _src
              and _src.count("_signal_identify_bg_video_ready(") >= 3)
        _i_tap = _src.find("async def video_identify")
        _i_next = _src.find("\nasync def ", _i_tap + 1)
        _tap_src = _src[_i_tap:_i_next] if _i_next > 0 else _src[_i_tap:]
        check("C13d the tap body has no video-download / inline-identify call (the miss goes through the bg job only)",
              "_download_link_audio(" not in _tap_src
              and "download_file(" not in _tap_src
              and "_identify_audio(" not in _tap_src
              and "_bg_precompute_get(" not in _tap_src,
              f"tap_has_download={'download_file(' in _tap_src} "
              f"tap_has_identify={'_identify_audio(' in _tap_src}")
    except Exception as exc:
        check("C13 wiring", False, f"{type(exc).__name__}: {exc}")

    # ---- C14: the three timing stages, hit + miss ----------------------
    try:
        # Hit: wait_bg=0; search_file=0 on a pure hit (fix round 3,
        # L2: the tap does no file search — the stored resolve_ms is
        # the bg job's HISTORICAL time, logged separately as
        # stored_resolve_ms on the total).
        _clear_cache()
        LOG_LINES.clear()
        _seed(_IG_KEY, source="bank", file_id="BANKFID1",
              duration_sec=280, resolve_ms=1234)
        msg, cb, token = await _run_tap(
            AudioIdResult(status="identified", title="SHOULD-NOT-USE",
                          artist="SHOULD-NOT-USE"),
            None)
        _tot = _log_line_with("total", '"wait_bg_ms"')
        check("C14a hit: the total logs wait_bg_ms=0 (no bg wait on a pure hit)",
              '"wait_bg_ms": 0' in _tot, f"total={_tot}")
        check("C14b hit: the total's search_file_ms is 0 on a pure hit",
              '"search_file_ms": 0' in _tot, f"total={_tot}")
        check("C14b2 hit: the stored resolve_ms is logged separately",
              '"stored_resolve_ms": 1234' in _tot, f"total={_tot}")
        check("C14c hit: the total logs send_ms",
              '"send_ms":' in _tot, f"total={_tot}")
        check("C14d hit: the three identify_stage lines exist (wait_bg / search_file / send)",
              _log_line_with("wait_bg", '"ms":') != ""
              and _log_line_with("search_file", '"ms":') != ""
              and _log_line_with("send", '"ms":') != "",
              f"lines={[l for l in LOG_LINES if 'wait_bg' in l or 'search_file' in l]}")
        # Miss: all three stages present; search_file == the bg entry's resolve_ms.
        _clear_cache()
        LOG_LINES.clear()
        mp3 = vm._make_mp3(_TITLE, _ARTIST, seconds=5)
        msg, cb, token = await _run_tap(
            AudioIdResult(status="identified", title=_TITLE,
                          artist=_ARTIST, confirmed=True,
                          source="shazam+audd"),
            vm._full_dict(mp3, "persian_music:test", duration_sec=5.0))
        _tot2 = _log_line_with("total", '"wait_bg_ms"')
        _ent2 = h._identify_result_get(_IG_KEY)
        check("C14e miss: the total logs all three stages",
              '"wait_bg_ms":' in _tot2 and '"search_file_ms":' in _tot2
              and '"send_ms":' in _tot2, f"total={_tot2}")
        check("C14f miss: the total's search_file_ms equals the bg entry's resolve_ms",
              _ent2 is not None
              and f'"search_file_ms": {_ent2.get("resolve_ms") or 0}' in _tot2,
              f"total={_tot2} entry_resolve_ms={(_ent2 or {}).get('resolve_ms')}")
        check("C14g miss: the three identify_stage lines exist",
              _log_line_with("wait_bg", '"ms":') != ""
              and _log_line_with("search_file", '"ms":') != ""
              and _log_line_with("send", '"ms":') != "")
    except Exception as exc:
        check("C14 timing stages", False, f"{type(exc).__name__}: {exc}")

    # ---- C15: file_id-only hit (no download, no identify, no file search) --
    try:
        _clear_cache()
        LOG_LINES.clear()
        _seed(_IG_KEY, source="bank", file_id="BANKFID1",
              duration_sec=280, quality="کیفیت واقعی: 320kbps",
              extra="💎 نسخه کامل اختصاصی آوانو")
        calls = []
        dl = []
        searches = []
        msg, cb, token = await _run_tap(
            AudioIdResult(status="identified", title="SHOULD-NOT-USE",
                          artist="SHOULD-NOT-USE"),
            None, calls=calls, dl_calls=dl, search_calls=searches)
        check("C15a file_id hit: _identify_audio is NEVER called",
              calls == [], f"calls={len(calls)}")
        check("C15b file_id hit: NO Telegram download (the tap downloads nothing)",
              dl == [], f"downloads={len(dl)}")
        check("C15c file_id hit: NO file search (no find / smart_search / exclusive call)",
              searches == [], f"searches={searches}")
        check("C15d file_id hit: exactly ONE audio send, of the STORED file_id",
              len(msg.audio_calls) == 1 and msg.audio_calls[0] == ("str", "BANKFID1"),
              f"audio={msg.audio_calls}")
    except Exception as exc:
        check("C15 file_id-only hit", False, f"{type(exc).__name__}: {exc}")

    # ---- C16: the miss AWAITS the same bg job (no second spawn) --------
    try:
        _clear_cache()
        LOG_LINES.clear()
        _key16 = _IG_KEY

        async def _slow_bg16():
            await asyncio.sleep(0.3)
            h._identify_result_put(
                _key16, h._identify_result_entry(
                    "identified", _TITLE, _ARTIST, "shazam+audd",
                    kind="full", file_id="WAITFID", duration_sec=280,
                    resolve_ms=42))

        _t16 = asyncio.create_task(_slow_bg16())
        h._IDENTIFY_BG_TASKS[_key16] = _t16
        calls = []
        dl = []
        searches = []
        msg, cb, token = await _run_tap(
            AudioIdResult(status="identified", title="SHOULD-NOT-USE",
                          artist="SHOULD-NOT-USE"),
            None, calls=calls, dl_calls=dl, search_calls=searches)
        check("C16a the tap reuses the in-flight bg task (same object, not replaced)",
              h._IDENTIFY_BG_TASKS.get(_key16) is _t16,
              f"task={h._IDENTIFY_BG_TASKS.get(_key16)}")
        check("C16b the tap sends the bg job's file (WAITFID) — the SAME job's result",
              len(msg.audio_calls) == 1 and msg.audio_calls[0] == ("str", "WAITFID"),
              f"audio={msg.audio_calls}")
        check("C16c NO second identify starts (_identify_audio never called)",
              calls == [], f"calls={len(calls)}")
        check("C16d NO second download starts (the tap downloads nothing)",
              dl == [], f"downloads={len(dl)}")
        check("C16e NO file search starts",
              searches == [], f"searches={searches}")
        _tot16 = _log_line_with("total", '"wait_bg_ms"')
        _wb16 = 0
        try:
            import re as _re
            _m16 = _re.search(r'"wait_bg_ms": (\d+)', _tot16)
            _wb16 = int(_m16.group(1)) if _m16 else 0
        except Exception:
            _wb16 = 0
        check("C16f wait_bg proves the wait (>=200 ms for the 0.3 s bg job)",
              _wb16 >= 200, f"wait_bg_ms={_wb16} total={_tot16}")
    except Exception as exc:
        check("C16 miss awaits same bg job", False, f"{type(exc).__name__}: {exc}")

    # ---- C17: stale file_id attempted once, invalidated, bg re-resolves --
    try:
        _clear_cache()
        LOG_LINES.clear()
        _seed(_IG_KEY, source="shazam+audd", file_id="STALEFID",
              duration_sec=280)
        calls = []
        dl = []
        attempts = []
        mp3 = vm._make_mp3(_TITLE, _ARTIST, seconds=5)
        msg, cb, token = await _run_tap(
            AudioIdResult(status="identified", title=_TITLE,
                          artist=_ARTIST, confirmed=True,
                          source="shazam+audd"),
            vm._full_dict(mp3, "persian_music:test", duration_sec=5.0),
            calls=calls, dl_calls=dl, fail_fid="STALEFID",
            fid_attempts=attempts)
        check("C17a the stale file_id is attempted exactly ONCE",
              attempts == ["STALEFID"], f"attempts={attempts}")
        check("C17b exactly ONE successful audio send — the fresh file (bytes)",
              len(msg.audio_calls) == 1 and msg.audio_calls[0][0] == "bytes",
              f"audio={msg.audio_calls}")
        _ent17 = h._identify_result_get(_IG_KEY)
        check("C17c the stale entry is replaced by the fresh result (new file_id)",
              _ent17 is not None and _ent17.get("file_id") == "BYTES_FILE_ID"
              and _ent17.get("title") == _TITLE,
              f"entry={_ent17}")
        check("C17d the bg re-resolves once (_identify_audio called once, by the bg)",
              len(calls) == 1, f"calls={len(calls)}")
        check("C17e the bg downloads the Telegram file exactly once (the tap downloads nothing)",
              len(dl) == 1, f"downloads={len(dl)}")
    except Exception as exc:
        check("C17 stale file_id", False, f"{type(exc).__name__}: {exc}")

    # ---- C18: a staged entry never clobbers a sent file_id ------------
    try:
        _clear_cache()
        vs._turso_on()
        _seed(_IG_KEY, source="bank", file_id="SENTFID1", duration_sec=280)
        tc.store_identify_result(_IG_KEY, h._identify_result_get(_IG_KEY))
        _diskless = h._identify_result_entry(
            "identified", _TITLE, _ARTIST, "shazam+audd", kind="full",
            file_id="", disk_path="/tmp/does-not-exist.mp3")
        h._identify_result_store(_IG_KEY, _diskless)
        _ent18 = h._identify_result_get(_IG_KEY)
        check("C18a a file_id-less store does not overwrite a sent file_id (memory)",
              _ent18 is not None and _ent18.get("file_id") == "SENTFID1",
              f"entry={_ent18}")
        _row18 = tc.get_identify_result(_IG_KEY)
        check("C18b the Turso row still holds the sent file_id",
              _row18 is not None and _row18.get("file_id") == "SENTFID1",
              f"row={_row18}")
        # Post-restart case: memory empty, Turso holds the sent file_id.
        h._IDENTIFY_RESULT_CACHE.clear()
        h._identify_result_store(_IG_KEY, _diskless)
        check("C18c a file_id-less store is refused when the Turso row holds a sent file_id",
              h._identify_result_get(_IG_KEY) is None
              and (tc.get_identify_result(_IG_KEY) or {}).get("file_id") == "SENTFID1",
              f"row={tc.get_identify_result(_IG_KEY)}")
        _with_fid = h._identify_result_entry(
            "identified", _TITLE, _ARTIST, "shazam+audd", kind="full",
            file_id="SENTFID2")
        h._identify_result_store(_IG_KEY, _with_fid)
        check("C18d a store WITH a file_id always goes through",
              (h._identify_result_get(_IG_KEY) or {}).get("file_id") == "SENTFID2",
              f"entry={h._identify_result_get(_IG_KEY)}")
    except Exception as exc:
        check("C18 store guard", False, f"{type(exc).__name__}: {exc}")
    finally:
        vs._turso_off()

    # ---- C19: bg early return on a Turso file_id row -------------------
    try:
        _clear_cache()
        vs._turso_on()
        _key19 = "ig:TGROW"
        _ent19 = h._identify_result_entry(
            "identified", _TITLE, _ARTIST, "shazam+audd", kind="full",
            file_id="TURSO_FID", duration_sec=280)
        tc.store_identify_result(_key19, _ent19)
        _vpath19 = os.path.join(tempfile.mkdtemp(prefix="ident_bg_"), "video.mp4")
        with open(_vpath19, "wb") as _f:
            _f.write(vm._MP4)
        _ev19 = asyncio.Event()
        _ev19.set()
        h._IDENTIFY_BG_VIDEO_READY[_key19] = (_ev19, _vpath19)
        orig_ident = h._identify_audio
        calls19 = []

        async def _fake_ident19(data, filename, config, conn=None,
                                cache_url_key=None, req_id=None, t_req=None,
                                **_kw):
            calls19.append(1)
            return AudioIdResult(status="identified", title=_TITLE,
                                 artist=_ARTIST, confirmed=True,
                                 source="shazam+audd")

        h._identify_audio = _fake_ident19
        try:
            conn = vs._make_conn()
            await h._identify_bg_link(
                _key19, "https://example.invalid/audio.mp3", 572595158,
                conn, vw._config())
        finally:
            h._identify_audio = orig_ident
        check("C19a the bg does NOT re-identify when the Turso row already holds the sent file_id",
              calls19 == [], f"calls={len(calls19)}")
        check("C19b the bg stores nothing (memory stays empty — the Turso row serves the tap)",
              h._identify_result_get(_key19) is None,
              f"entry={h._identify_result_get(_key19)}")
        check("C19c the Turso row is untouched (still the sent file_id)",
              (tc.get_identify_result(_key19) or {}).get("file_id") == "TURSO_FID",
              f"row={tc.get_identify_result(_key19)}")
    except Exception as exc:
        check("C19 bg early return on Turso file_id", False, f"{type(exc).__name__}: {exc}")
    finally:
        vs._turso_off()

    # ---- C20: wrong-song unconfirmed stores the clip ------------------
    try:
        _clear_cache()
        vs._turso_on()
        LOG_LINES.clear()
        token, entry, _kb = vw._mint_video_token(link=_IG_LINK)
        _seed(_IG_KEY, title=vw._WRONG_TITLE, artist=vw._WRONG_ARTIST,
              source="shazam+audd", kind="full", file_id="WRONGFID")
        tc.store_identify_result(_IG_KEY, h._identify_result_get(_IG_KEY))
        msg = vw._VideoMsg()
        msg.reply_markup = None
        vm._attach_clip_video(msg)
        orig_ident = h._identify_audio
        orig_excl = h._exclusive_song_for
        calls20 = []

        async def _fake_ident20(data, filename, config, conn=None,
                                cache_url_key=None, req_id=None, t_req=None,
                                exclude=None, skip_bank=False, **_kw):
            calls20.append({"exclude": list(exclude or [])})
            return AudioIdResult(status="unconfirmed", title=_NEW_TITLE,
                                 artist=_NEW_ARTIST, confirmed=False,
                                 source="shazam")

        h._identify_audio = _fake_ident20
        h._exclusive_song_for = lambda t, a: None
        try:
            conn = vs._make_conn()
            cb = vw._FakeCallback(msg, f"wrong:{token}")
            await h.wrong_song(cb, conn, vw._config())
            await vw._drain()
        finally:
            h._identify_audio = orig_ident
            h._exclusive_song_for = orig_excl
        _ent20 = h._identify_result_get(_IG_KEY)
        check("C20a the unconfirmed re-check result is stored (not left empty)",
              _ent20 is not None and _ent20.get("title") == _NEW_TITLE
              and _ent20.get("artist") == _NEW_ARTIST,
              f"entry={_ent20}")
        check("C20b the stored entry is a staged clip (disk file exists, 10-min TTL)",
              _ent20 is not None and _ent20.get("kind") == "clip"
              and _ent20.get("status") == "unconfirmed"
              and _ent20.get("ttl") == 600
              and bool(_ent20.get("disk_path"))
              and os.path.exists(_ent20.get("disk_path") or ""),
              f"entry={_ent20}")
        check("C20c the disputed name is gone (never re-served)",
              _ent20 is not None and _ent20.get("title") != vw._WRONG_TITLE,
              f"entry={_ent20}")
    except Exception as exc:
        check("C20 wrong-song unconfirmed stores clip", False, f"{type(exc).__name__}: {exc}")
    finally:
        vs._turso_off()

    # ---- C21: Stage-0 not_found is a SHORT negative -------------------
    try:
        _conn21 = vs._make_conn()
        _now21 = int(time.time())

        def _backdate(key, secs):
            _conn21.execute(
                "UPDATE identify_cache SET created_at=? WHERE key=?",
                (_now21 - secs, key))
            _conn21.commit()

        dbmod.set_identify_cache(_conn21, "hash:fresh-id", "identified", "T1", "A1")
        dbmod.set_identify_cache(_conn21, "hash:fresh-nf", "not_found")
        dbmod.set_identify_cache(_conn21, "hash:old-nf", "not_found")
        dbmod.set_identify_cache(_conn21, "hash:old-id", "identified", "T3", "A3")
        dbmod.set_identify_cache(_conn21, "hash:ancient-id", "identified", "T5", "A5")
        dbmod.set_identify_cache(_conn21, "hash:bound-nf", "not_found")
        _backdate("hash:old-nf", 700)
        _backdate("hash:old-id", 700)
        _backdate("hash:ancient-id", 8 * 86400)
        _backdate("hash:bound-nf", 70)
        check("C21a fresh identified hits",
              (dbmod.get_identify_cache(_conn21, "hash:fresh-id") or {}).get("status") == "identified")
        check("C21b fresh not_found hits (inside the 10-min window)",
              (dbmod.get_identify_cache(_conn21, "hash:fresh-nf") or {}).get("status") == "not_found")
        check("C21c not_found older than 10 min reads as a miss (re-identified)",
              dbmod.get_identify_cache(_conn21, "hash:old-nf") is None)
        check("C21d identified of the same age still hits (7d TTL)",
              (dbmod.get_identify_cache(_conn21, "hash:old-id") or {}).get("status") == "identified")
        check("C21e identified older than 7d reads as a miss",
              dbmod.get_identify_cache(_conn21, "hash:ancient-id") is None)
        check("C21f a caller-supplied smaller max_age is still respected",
              dbmod.get_identify_cache(_conn21, "hash:bound-nf", max_age=60) is None)
    except Exception as exc:
        check("C21 Stage-0 not_found short TTL", False, f"{type(exc).__name__}: {exc}")

    # ---- C22: Turso song_file_cache writes ---------------------------
    _orig_song_store = tc.store_song_file_id
    try:
        _song_writes: list[dict] = []

        def _rec_song_store(artist, title, file_id, file_type="audio",
                            ttl_days=7, req_id=None, verified_title=None,
                            verified_artist=None, duration_sec=None):
            _song_writes.append({"artist": artist, "title": title,
                                 "file_id": file_id,
                                 "verified_title": verified_title,
                                 "verified_artist": verified_artist,
                                 "duration_sec": duration_sec})
            return True

        tc.store_song_file_id = _rec_song_store
        # (a) smart-tap found file mirrors to Turso with verified tags.
        orig_search_sf = sf.search_smart_full_version

        async def _fake_found_sf(artist, title, clip_fingerprint=None):
            td = tempfile.mkdtemp(prefix="c22_found_")
            copy = os.path.join(td, "found.mp3")
            with open(copy, "wb") as _fh:
                _fh.write(vm._make_mp3(_TITLE, _ARTIST, seconds=5))
            return sf.SmartOutcome(
                status="found",
                file=sf.SmartFile(
                    disk_path=copy, ext="mp3", duration_sec=5.0,
                    source="smart:javanmelody.ir", title=_TITLE,
                    artist=_ARTIST, size=os.path.getsize(copy),
                    url="https://example.invalid/found.mp3",
                    validation={"status": "match", "detail": "match"},
                    fingerprint_sim=None),
                detail="match", ms=120, checked=1,
                expected_duration_sec=5.0)

        sf.search_smart_full_version = _fake_found_sf
        try:
            _conn22 = vs._make_conn()
            _msg22 = vw._VideoMsg()
            _cb22 = vw._FakeCallback(_msg22, "vid:id:tok-c22")
            _hit22 = {"title": _TITLE, "artist": _ARTIST, "kind": "clip",
                      "status": "identified"}
            await h._run_smart_search_tap(
                _cb22, _conn22, {"kind": "link", "file_id": "VID123"},
                _hit22, _IG_KEY, "tok-c22", "fa")
        finally:
            sf.search_smart_full_version = orig_search_sf
        check("C22a smart-tap found mirrors the file_id to Turso song_file_cache",
              len(_song_writes) == 1
              and _song_writes[0]["file_id"] == "BYTES_FILE_ID"
              and _song_writes[0]["verified_title"] == _TITLE
              and _song_writes[0]["verified_artist"] == _ARTIST
              and _song_writes[0]["duration_sec"] == 5.0,
              f"writes={_song_writes}")
        # (b) disk -> file_id upgrade: a staged FULL song mirrors.
        _song_writes.clear()
        _full_path = os.path.join(tempfile.mkdtemp(prefix="c22_full_"), "full.mp3")
        with open(_full_path, "wb") as _fh:
            _fh.write(vm._make_mp3(_TITLE, _ARTIST, seconds=5))
        _clear_cache()
        _seed(_IG_KEY, kind="full", disk_path=_full_path, file_id="",
              status="identified", duration_sec=5.0)
        msg, cb, token = await _run_tap(
            AudioIdResult(status="identified", title=_TITLE, artist=_ARTIST,
                          confirmed=True, source="shazam+audd"),
            None)
        check("C22b disk-staged FULL song mirrors to Turso on send",
              len(_song_writes) == 1
              and _song_writes[0]["file_id"] == "BYTES_FILE_ID"
              and _song_writes[0]["verified_title"] == _TITLE,
              f"writes={_song_writes}")
        check("C22b2 disk-staged FULL song sent exactly once",
              len(msg.audio_calls) == 1, f"audio_calls={msg.audio_calls}")
        # (c) same flow, kind clip: the upgrade stores the 1b entry
        # but must NOT write a clip into song_file_cache.
        _song_writes.clear()
        _clip_path22 = os.path.join(tempfile.mkdtemp(prefix="c22_clip_"), "clip.mp3")
        with open(_clip_path22, "wb") as _fh:
            _fh.write(vm._make_mp3(_TITLE, _ARTIST, seconds=5))
        _clear_cache()
        _seed(_IG_KEY, kind="clip", disk_path=_clip_path22, file_id="",
              status="unconfirmed", duration_sec=5.0)
        msg, cb, token = await _run_tap(
            AudioIdResult(status="unconfirmed", title=_TITLE, artist=_ARTIST,
                          confirmed=False, source="shazam"),
            None)
        check("C22c disk-staged CLIP does not write Turso song_file_cache",
              _song_writes == [], f"writes={_song_writes}")
        check("C22c2 disk-staged CLIP sent exactly once",
              len(msg.audio_calls) == 1, f"audio_calls={msg.audio_calls}")
        tc.store_song_file_id = _orig_song_store
    except Exception as exc:
        check("C22 Turso song_file_cache writes", False, f"{type(exc).__name__}: {exc}")
        try:
            tc.store_song_file_id = _orig_song_store
        except Exception:
            pass

    # ---- C23: TTL-aware LRU eviction ----------------------------------
    try:
        _clear_cache()
        _now23 = time.time()
        for _i in range(199):
            _ent = h._identify_result_entry(
                "identified", f"T{_i}", f"A{_i}", source="x", kind="full",
                file_id=f"FID{_i}")
            h._identify_result_put(f"ig:K{_i}", _ent)
        _exp = h._identify_result_entry(
            "not_found", "EXP", "EXP", source="x", kind="clip")
        _exp["created_at"] = _now23 - 3600
        _exp["ttl"] = 600
        # Direct insert: _identify_result_put itself evicts expired
        # entries, so the expired entry must be seeded around it.
        h._IDENTIFY_RESULT_CACHE["ig:EXP"] = dict(_exp)
        check("C23 setup: cache holds 200 entries",
              len(h._IDENTIFY_RESULT_CACHE) == 200,
              f"len={len(h._IDENTIFY_RESULT_CACHE)}")
        _new = h._identify_result_entry(
            "identified", "NEW", "NEW", source="x", kind="full",
            file_id="FIDNEW")
        h._identify_result_put("ig:NEW", _new)
        check("C23a the expired entry was evicted first",
              h._identify_result_get("ig:EXP") is None)
        check("C23b the oldest LIVE entry survived",
              h._identify_result_get("ig:K0") is not None)
        check("C23c the new entry is stored",
              h._identify_result_get("ig:NEW") is not None)
        check("C23d the cap still holds (200 entries)",
              len(h._IDENTIFY_RESULT_CACHE) == 200,
              f"len={len(h._IDENTIFY_RESULT_CACHE)}")
    except Exception as exc:
        check("C23 TTL-aware LRU eviction", False, f"{type(exc).__name__}: {exc}")

    # ---- C24: smart negative cache cap + forget -----------------------
    try:
        sf._NEGATIVE.clear()
        for _i in range(1005):
            sf.remember_negative(f"Artist{_i}", "Song")
        check("C24a the negative cache is capped at 1000 entries",
              len(sf._NEGATIVE) == 1000, f"len={len(sf._NEGATIVE)}")
        sf.remember_negative("Q4 Artist", "Q4 Song")
        check("C24b a fresh negative is cached",
              sf.negative_cached("Q4 Artist", "Q4 Song") is True)
        sf.forget_negative("Q4 Artist", "Q4 Song")
        check("C24c forget_negative drops the stored negative",
              sf.negative_cached("Q4 Artist", "Q4 Song") is False)
        check("C24d forgetting an unknown song is a no-op",
              sf.negative_cached("Nobody", "Nothing") is False)
        sf._NEGATIVE.clear()
    except Exception as exc:
        check("C24 smart negative cache cap + forget", False, f"{type(exc).__name__}: {exc}")

    # ---- C25: URL normalization converges ----------------------------
    try:
        _u1 = "https://www.instagram.com/reel/ABC123/?igshid=xyz&utm_source=ig"
        _u2 = "https://instagram.com/p/ABC123"
        _u3 = "https://instagram.com/reel/ABC123/"
        check("C25a local url-cache keys converge across reel spellings",
              fc._url_cache_key(_u1) == fc._url_cache_key(_u2)
              == fc._url_cache_key(_u3),
              f"{fc._url_cache_key(_u1)} vs {fc._url_cache_key(_u2)}")
        check("C25b Turso url-cache keys converge across reel spellings",
              tc._url_key(_u1) == tc._url_key(_u2) == tc._url_key(_u3))
        check("C25c local and Turso keys agree with each other",
              fc._url_cache_key(_u1) == tc._url_key(_u1))
        check("C25d a different reel still gets a different key",
              fc._url_cache_key(_u1) != fc._url_cache_key(
                  "https://instagram.com/reel/ZZZ999/"))
    except Exception as exc:
        check("C25 URL normalization converges", False, f"{type(exc).__name__}: {exc}")

    print(f"\nidentify_cache: {len(RESULTS)} checks run")


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
