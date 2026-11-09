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
  C6  Tap cache-MISS: the normal flow runs (_identify_audio called
      once), the full song is sent, and the store hook leaves the
      FINAL result in the memory cache; name_sent cache_hit=false.
  C7  Tap unconfirmed: the clip send is stored with the 10-min TTL
      (status unconfirmed, kind clip) for the next tap.
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
      (atomic — a replaced row survives), expiry deletes the row.
  C12 BG wait: _await_identify_bg returns True when the bg finishes
      in time and False on timeout WITHOUT cancelling the bg task;
      _spawn_identify_bg reuses the in-flight task per key.
  C13 Wiring: the bg identify is spawned at resolve (not after the
      video is sent) and _send_link_video signals the video-ready
      event (source checks, same precedent as F6).

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
                   file_id="VID123", calls=None):
    """Drive one video-button tap with a RECORDING _identify_audio
    fake and a mocked full-song find. Returns (msg, cb, token)."""
    kb = h._video_identify_kb(572595158, file_id, "file", link_url=link)
    token = kb.inline_keyboard[0][0].callback_data.split("vid:id:", 1)[1]
    msg = vw._VideoMsg()
    msg.reply_markup = kb
    vm._attach_clip_video(msg)
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
        return find_result

    async def _fake_search(engine, query, limit=10):
        return ([], "")

    h._identify_audio = _fake_ident
    adapters_mod.find_full_song_file_v2 = _fake_find
    h.smart_search = _fake_search
    h._exclusive_song_for = lambda t, a: None
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
        cover_file_id=kw.pop("cover_file_id", ""))
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

    # ---- C6: tap cache-MISS runs the normal flow + stores --------------
    try:
        _clear_cache()
        LOG_LINES.clear()
        mp3 = vm._make_mp3(_TITLE, _ARTIST, seconds=5)
        calls = []
        msg, cb, token = await _run_tap(
            AudioIdResult(status="identified", title=_TITLE,
                          artist=_ARTIST, confirmed=True,
                          source="shazam+audd"),
            vm._full_dict(mp3, "persian_music:test", duration_sec=5.0),
            calls=calls)
        check("C6a cache miss: _identify_audio IS called once",
              len(calls) == 1, f"calls={len(calls)}")
        check("C6b full song is sent (bytes path)",
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
    except Exception as exc:
        check("C6 tap cache-miss", False, f"{type(exc).__name__}: {exc}")

    # ---- C7: tap unconfirmed stores the clip (10-min TTL) --------------
    try:
        _clear_cache()
        LOG_LINES.clear()
        calls = []
        msg, cb, token = await _run_tap(
            AudioIdResult(status="unconfirmed", title=_TITLE,
                          artist=_ARTIST, confirmed=False,
                          source="shazam"),
            None, calls=calls)
        check("C7a unconfirmed tap sends exactly ONE extracted audio",
              len(msg.audio_calls) == 1,
              f"audio={msg.audio_calls}")
        _ent = h._identify_result_get(_IG_KEY)
        check("C7b the clip result is stored (status unconfirmed, kind clip, 10-min TTL)",
              _ent is not None and _ent.get("status") == "unconfirmed"
              and _ent.get("kind") == "clip" and _ent.get("ttl") == 600
              and bool(_ent.get("file_id")),
              f"entry={_ent}")
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
    except Exception as exc:
        check("C13 wiring", False, f"{type(exc).__name__}: {exc}")

    print(f"\nidentify_cache: {len(RESULTS)} checks run")


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
