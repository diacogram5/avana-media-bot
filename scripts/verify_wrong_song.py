"""Sandbox tests for the wrong-song button (batch F, 2026-11-09):
«❌ آهنگ اشتباه» under a sent video and under the result card.

Cases:
  W1  Engine exclusion in _identify_audio: a rejected name is dropped
      at the cache, in the bank (skip_bank), at the two-source gate,
      and as a single-source name; a different name still confirms;
      all-excluded answers "not_found" WITHOUT caching it; without
      exclude/skip_bank the behavior is unchanged.
  W2  Button placement: the card for a video identification carries
      «❌ آهنگ اشتباه» (same token as the video's identify button);
      a catalog card (no own_media) carries none; a video-button tap
      on a confirmed result edits the video message to [identify,
      wrong-song]; the button records the shown result on the entry.
  W3  Handler outcomes: different confirmed -> instant card + full
      song (status message deleted); different single-source -> the
      searching message becomes a «شاید...» lead (never a
      «شناسایی شد» card); nothing different -> the searching message
      becomes «پیدا نشد» + the type-the-name invitation; audio-fetch
      failure -> the honest error line; cap 2 -> limit alert, no
      re-check, button removed; expired token / wrong user -> honest
      alerts, no re-check; a second attempt excludes BOTH rejected
      names; skip_bank follows the disputed source (bank -> skip,
      shazam+audd -> run); exactly one outcome message per tap (no
      automatic follow-up).
  W4  Turso report: one row per tap (link, wrong result, source,
      outcome, new result) via the fire-and-forget store; Turso
      unconfigured -> False, never raises. Each outcome also writes
      one `wrong_song_report` log line.

Reuses the fake aiogram / fake libsql / fake message harness from
scripts/verify_speedfix.py and the clip fixtures from
scripts/verify_fullsong_match.py. No network, no Telegram, no real Turso.

Run: python3 scripts/verify_wrong_song.py ; exit 0 = all PASS.
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
import verify_fullsong_match as vm  # noqa: E402  (clip fixtures, _Msg)

import app.bot.handlers as h  # noqa: E402
from app.features import pro as profeat  # noqa: E402
from app.services.audio_id import AudioIdResult  # noqa: E402

check = vs.check
RESULTS = vs.RESULTS
LOG_LINES = vs.LOG_LINES
adapters_mod = vs.adapters_mod

_WRONG_FA = "❌ آهنگ اشتباه"
_CHECKING_FA = "دوباره بررسی می‌کنم…"
_NOTFOUND_FA = "پیدا نشد. اگر نام آهنگ یا خواننده را می‌دانید، بنویسید تا جستجو کنیم."
_LIMIT_FA = "برای این ویدیو دو بار بررسی مجدد انجام شد."
_MAYBE_FA = "شاید «{title}» از {artist} باشد؟"
_ERROR_FA = "بررسی مجدد ممکن نشد؛ لطفاً دوباره تلاش کنید."

_WRONG_TITLE = "Flick Shot!"
_WRONG_ARTIST = "Ali Geramy"
_NEW_TITLE = "Test Song"
_NEW_ARTIST = "Test Artist"


def _kb_texts(kb) -> list:
    texts = []
    for row in getattr(kb, "inline_keyboard", []) or []:
        for b in row:
            texts.append(getattr(b, "text", "") or "")
    return texts


def _kb_callbacks(kb) -> list:
    cbs = []
    for row in getattr(kb, "inline_keyboard", []) or []:
        for b in row:
            cbs.append(getattr(b, "callback_data", "") or "")
    return cbs


def _config(audd_token="tok"):
    return SimpleNamespace(
        audd_token=audd_token, acrcloud_host="", acrcloud_key="",
        acrcloud_secret="", max_file_mb=50, bot_id="@AvanoMediaBot",
        owner_id=572595158, spotify_client_id="")


async def _drain(timeout=5.0):
    """Let background tasks finish (both task dicts empty) or time out."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if not h._AFTER_CARD_TASKS and not h._FULLSONG_TASKS:
            return True
        await asyncio.sleep(0.05)
    return False


def _stub_lookups(shazam, audd, calls):
    """Monkeypatch handlers' Shazam/AudD with recording fakes."""
    async def _fake_shazam(probe, prepared_wav=None, req_id=None):
        calls.append("shazam")
        return shazam if shazam is not None else AudioIdResult(status="not_found")

    async def _fake_audd(data, filename, token):
        calls.append("audd")
        return audd if audd is not None else AudioIdResult(status="not_found")

    h.identify_bytes_shazam = _fake_shazam
    h.identify_bytes = _fake_audd


class _StatusSent(vs._FakeSent):
    """Sent message recording edits and deletes (the searching message)."""

    def __init__(self):
        super().__init__()
        self.deleted = False
        self.edited_text = None
        self.edited_kb = None

    async def edit_text(self, text, **kw):
        self.edited_text = text
        self.edited_kb = kw.get("reply_markup")
        return self

    async def edit_reply_markup(self, **kw):
        self.edited_kb = kw.get("reply_markup")
        return self

    async def delete(self):
        self.deleted = True
        return None


class _VideoMsg(vm._Msg):
    """Bot-sent video message: .video, .reply_markup, records edits."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.video = SimpleNamespace(file_id="VID123")
        self.reply_markup = None
        self.markup_edits: list = []
        self.sent_messages: list = []
        self.last_answer_text = ""
        self.last_answer_kb = None

    async def answer(self, text, **kw):
        self.answers.append(text)
        sent = _StatusSent()
        self.sent_messages.append(sent)
        self.last_answer_text = text
        self.last_answer_kb = kw.get("reply_markup")
        return sent

    async def edit_reply_markup(self, **kw):
        self.markup_edits.append(kw.get("reply_markup"))
        self.reply_markup = kw.get("reply_markup")
        return None


class _FakeCallback:
    """Minimal CallbackQuery stand-in with a parametrized callback_data."""

    def __init__(self, msg, data, user_id=572595158):
        self.data = data
        self.message = msg
        self.bot = msg.bot
        self.from_user = SimpleNamespace(id=user_id, username="testuser",
                                         first_name="Test", last_name=None,
                                         is_bot=False)
        self.answered: list = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answered.append((text, show_alert))
        return None


def _mint_video_token(user_id=572595158, file_id="VID123", kind="file",
                      link="https://www.instagram.com/reel/TEST123/"):
    """Register a video token and record a shown confirmed result on it
    (as _send_professional_song_card / video_identify would)."""
    h._VIDEO_IDENTIFY.clear()
    h._WRONG_SONG_RUNNING.clear()
    kb = h._video_identify_kb(user_id, file_id, kind, link_url=link)
    token = kb.inline_keyboard[0][0].callback_data.split("vid:id:", 1)[1]
    entry = h._VIDEO_IDENTIFY[token]
    entry["last_title"] = _WRONG_TITLE
    entry["last_artist"] = _WRONG_ARTIST
    entry["last_source"] = "shazam+audd"
    entry.setdefault("wrong", [])
    entry.setdefault("attempts", 0)
    return token, entry, kb


def _turso_rows():
    con = sqlite3.connect(vs._FAKE_TURSO_PATH)
    try:
        return con.execute(
            "SELECT user_id, link, wrong_title, wrong_artist, wrong_source,"
            " new_title, new_artist, new_source, outcome"
            " FROM wrong_song_reports ORDER BY id").fetchall()
    except Exception:
        return []


async def _rows_with(outcome, timeout=2.0):
    """Wait until a wrong_song_reports row with this outcome exists
    (rows accumulate across taps, so waiting for ANY row would return
    a stale one from an earlier tap)."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        rows = _turso_rows()
        if any(r[8] == outcome for r in rows):
            return rows
        await asyncio.sleep(0.1)
    return _turso_rows()


async def main():
    vm._MP4 = vm._make_mp4()
    orig_shazam = h.identify_bytes_shazam
    orig_audd = h.identify_bytes
    orig_xbank_ok = h._XBANK_OK
    orig_ident = h._identify_audio
    vs._turso_on()

    # ---- W1: engine exclusion ------------------------------------------
    try:
        h._XBANK_OK = False
        data = vm._MP4
        # W1a: Shazam+AudD agree on the REJECTED name -> not_found.
        calls: list = []
        _stub_lookups(
            AudioIdResult(status="identified", title=_WRONG_TITLE,
                          artist=_WRONG_ARTIST),
            AudioIdResult(status="identified", title=_WRONG_TITLE,
                          artist=_WRONG_ARTIST), calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None,
                                    exclude=[(_WRONG_TITLE, _WRONG_ARTIST)])
        check("W1a agreement on the rejected name is not_found, never identified",
              r.status == "not_found",
              f"status={r.status} title={r.title!r}")
        # W1b: agreement on a DIFFERENT name still confirms.
        calls.clear()
        _stub_lookups(
            AudioIdResult(status="identified", title=_NEW_TITLE,
                          artist=_NEW_ARTIST),
            AudioIdResult(status="identified", title=_NEW_TITLE,
                          artist=_NEW_ARTIST), calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None,
                                    exclude=[(_WRONG_TITLE, _WRONG_ARTIST)])
        check("W1b agreement on a different name still confirms",
              r.status == "identified" and r.confirmed
              and r.title == _NEW_TITLE and r.source == "shazam+audd",
              f"status={r.status} title={r.title!r} source={r.source}")
        # W1c: Shazam rejected, AudD different -> unconfirmed AudD name.
        _stub_lookups(
            AudioIdResult(status="identified", title=_WRONG_TITLE,
                          artist=_WRONG_ARTIST),
            AudioIdResult(status="identified", title=_NEW_TITLE,
                          artist=_NEW_ARTIST), calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None,
                                    exclude=[(_WRONG_TITLE, _WRONG_ARTIST)])
        check("W1c rejected Shazam + different AudD -> unconfirmed AudD name",
              r.status == "unconfirmed" and r.title == _NEW_TITLE
              and r.source == "audd",
              f"status={r.status} title={r.title!r} source={r.source}")
        # W1d: Shazam different, AudD rejected -> unconfirmed Shazam name.
        _stub_lookups(
            AudioIdResult(status="identified", title=_NEW_TITLE,
                          artist=_NEW_ARTIST),
            AudioIdResult(status="identified", title=_WRONG_TITLE,
                          artist=_WRONG_ARTIST), calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None,
                                    exclude=[(_WRONG_TITLE, _WRONG_ARTIST)])
        check("W1d different Shazam + rejected AudD -> unconfirmed Shazam name",
              r.status == "unconfirmed" and r.title == _NEW_TITLE
              and r.source == "shazam",
              f"status={r.status} title={r.title!r} source={r.source}")
        # W1e: no exclusion -> baseline unchanged (agreement confirms).
        _stub_lookups(
            AudioIdResult(status="identified", title=_WRONG_TITLE,
                          artist=_WRONG_ARTIST),
            AudioIdResult(status="identified", title=_WRONG_TITLE,
                          artist=_WRONG_ARTIST), calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None)
        check("W1e without exclusion the same agreement confirms (no regression)",
              r.status == "identified" and r.confirmed
              and r.title == _WRONG_TITLE,
              f"status={r.status} title={r.title!r}")
        # W1f: bracket-insensitive exclusion ("(Remix)" vs plain).
        _stub_lookups(
            AudioIdResult(status="identified", title=_WRONG_TITLE,
                          artist=_WRONG_ARTIST),
            AudioIdResult(status="identified", title=_WRONG_TITLE,
                          artist=_WRONG_ARTIST), calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None,
                                    exclude=[(_WRONG_TITLE + " (Remix)",
                                             _WRONG_ARTIST)])
        check("W1f bracket-insensitive exclusion drops the rejected name",
              r.status == "not_found",
              f"status={r.status} title={r.title!r}")
        # W1g: skip_bank logs the skip and the chain still confirms.
        LOG_LINES.clear()
        _stub_lookups(
            AudioIdResult(status="identified", title=_NEW_TITLE,
                          artist=_NEW_ARTIST),
            AudioIdResult(status="identified", title=_NEW_TITLE,
                          artist=_NEW_ARTIST), calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None,
                                    skip_bank=True)
        check("W1g skip_bank logs the skip and still confirms via Shazam+AudD",
              r.status == "identified" and r.confirmed
              and vs._log_has("exclusive_bank")
              and any("skipped_wrong_song" in line for line in LOG_LINES),
              f"status={r.status}")
        LOG_LINES.clear()
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None,
                                    skip_bank=False)
        check("W1g without skip_bank there is no skip line",
              r.status == "identified"
              and not any("skipped_wrong_song" in line for line in LOG_LINES),
              f"status={r.status}")
        # W1h: cached identified + excluded -> live pipeline, different name.
        conn = vs._make_conn()
        import hashlib as _hashlib
        from app import db as dbmod
        dbmod.set_identify_cache(
            conn, f"hash:{_hashlib.sha256(data).hexdigest()}",
            "identified", _WRONG_TITLE, _WRONG_ARTIST)
        calls.clear()
        _stub_lookups(
            AudioIdResult(status="identified", title=_NEW_TITLE,
                          artist=_NEW_ARTIST),
            AudioIdResult(status="identified", title=_NEW_TITLE,
                          artist=_NEW_ARTIST), calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=conn,
                                    exclude=[(_WRONG_TITLE, _WRONG_ARTIST)])
        check("W1h rejected cached name falls through to a different live result",
              r.status == "identified" and r.title == _NEW_TITLE
              and "shazam" in calls and "audd" in calls,
              f"status={r.status} title={r.title!r} calls={calls}")
        # W1h control: cached identified + NOT excluded -> cache hit, no API calls.
        dbmod.set_identify_cache(
            conn, f"hash:{_hashlib.sha256(data).hexdigest()}",
            "identified", _WRONG_TITLE, _WRONG_ARTIST)
        calls.clear()
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=conn)
        check("W1h control: unrejected cached name still answers from cache",
              r.status == "identified" and r.title == _WRONG_TITLE
              and r.source == "cache" and not calls,
              f"status={r.status} source={r.source} calls={calls}")
    except Exception as exc:
        check("W1 engine exclusion", False, f"{type(exc).__name__}: {exc}")
    finally:
        h.identify_bytes_shazam = orig_shazam
        h.identify_bytes = orig_audd
        h._XBANK_OK = orig_xbank_ok

    # ---- W2: button placement ------------------------------------------
    try:
        orig_find = adapters_mod.find_full_song_file_v2
        orig_search = h.smart_search
        orig_excl = h._exclusive_song_for
        adapters_mod.find_full_song_file_v2 = None  # restored below; unused here

        async def _fake_find(a, t):
            return None

        async def _fake_search(engine, query, limit=10):
            return ([], "")

        adapters_mod.find_full_song_file_v2 = _fake_find
        h.smart_search = _fake_search
        h._exclusive_song_for = lambda t, a: None
        try:
            # W2a: card WITH own_media carries the wrong-song button.
            h._VIDEO_IDENTIFY.clear()
            kb0 = h._video_identify_kb(572595158, "VID123", "file")
            token0 = kb0.inline_keyboard[0][0].callback_data.split("vid:id:", 1)[1]
            msg = _VideoMsg()
            conn = vs._make_conn()
            card_msg = await h._send_professional_song_card(
                msg, conn, _WRONG_TITLE, _WRONG_ARTIST,
                bot_id="@AvanoMediaBot", own_media=("video", "VID123"),
                config=None, arm=None, source="shazam+audd")
            # Snapshot NOW: the background full-song delivery runs after
            # this and may send its own text answer (overwriting
            # last_answer_kb).
            card_kb = msg.last_answer_kb
            drained = await _drain()
            _texts = _kb_texts(card_kb)
            _cbs = _kb_callbacks(card_kb)
            check("W2a video card carries «❌ آهنگ اشتباه»",
                  _WRONG_FA in _texts,
                  f"buttons={_texts}")
            check("W2a card button reuses the video's identify token",
                  f"wrong:{token0}" in _cbs,
                  f"callbacks={_cbs} token={token0}")
            check("W2a card button records the shown result on the entry",
                  h._VIDEO_IDENTIFY.get(token0, {}).get("last_title") == _WRONG_TITLE
                  and h._VIDEO_IDENTIFY.get(token0, {}).get("last_source") == "shazam+audd",
                  f"entry={h._VIDEO_IDENTIFY.get(token0)}")
            # W2b: catalog card (no own_media) carries NO wrong-song button.
            msg2 = _VideoMsg()
            await h._send_professional_song_card(
                msg2, conn, _WRONG_TITLE, _WRONG_ARTIST,
                bot_id="@AvanoMediaBot", config=None, arm=None,
                exclusive_check=False)
            card_kb2 = msg2.last_answer_kb  # snapshot before the bg task
            await _drain()
            _texts2 = _kb_texts(card_kb2)
            check("W2b catalog card (no video) has NO wrong-song button",
                  _WRONG_FA not in _texts2,
                  f"buttons={_texts2}")
        finally:
            adapters_mod.find_full_song_file_v2 = orig_find
            h.smart_search = orig_search
            h._exclusive_song_for = orig_excl
    except Exception as exc:
        check("W2 button placement (card)", False,
              f"{type(exc).__name__}: {exc}")

    # ---- W2c: video-button tap adds the row under the video ------------
    try:
        orig_find = adapters_mod.find_full_song_file_v2
        orig_search = h.smart_search
        orig_excl = h._exclusive_song_for

        async def _fake_find2(a, t):
            return None

        async def _fake_search2(engine, query, limit=10):
            return ([], "")

        adapters_mod.find_full_song_file_v2 = _fake_find2
        h.smart_search = _fake_search2
        h._exclusive_song_for = lambda t, a: None
        orig_ident = h._identify_audio

        async def _fake_ident2(data, filename, config, conn=None,
                               cache_url_key=None, req_id=None, t_req=None,
                               **_kw):
            return AudioIdResult(status="identified", title=_WRONG_TITLE,
                                 artist=_WRONG_ARTIST, confirmed=True,
                                 source="shazam+audd")

        h._identify_audio = _fake_ident2
        try:
            token, entry, kb0 = _mint_video_token()
            msg = _VideoMsg()
            msg.reply_markup = kb0
            vm._attach_clip_video(msg)
            conn = vs._make_conn()
            cb = _FakeCallback(msg, f"vid:id:{token}")
            await h.video_identify(cb, conn, _config())
            card_kb = msg.last_answer_kb  # snapshot before the bg task
            await _drain()
            check("W2c video tap answers the identify tap at once",
                  any("در حال شناسایی" in a[0] for a in cb.answered),
                  f"answered={cb.answered}")
            check("W2c video message gains the wrong-song row, keeps identify",
                  bool(msg.markup_edits)
                  and _kb_callbacks(msg.markup_edits[-1]) == [f"vid:id:{token}",
                                                             f"wrong:{token}"],
                  f"edits={[_kb_callbacks(k) for k in msg.markup_edits]}")
            check("W2c card for the same video carries the same wrong token",
                  f"wrong:{token}" in _kb_callbacks(card_kb),
                  f"card_callbacks={_kb_callbacks(card_kb)}")
        finally:
            adapters_mod.find_full_song_file_v2 = orig_find
            h.smart_search = orig_search
            h._exclusive_song_for = orig_excl
            h._identify_audio = orig_ident
    except Exception as exc:
        check("W2c video message row", False, f"{type(exc).__name__}: {exc}")

    # ---- W3: handler outcomes ------------------------------------------
    async def _run_wrong(token, entry, scripted, kind="file",
                         user_id=572595158, audio_url=None):
        """Run one wrong-song tap with a scripted _identify_audio result.

        Returns (msg, cb, calls, conn)."""
        msg = _VideoMsg()
        msg.reply_markup = None
        vm._attach_clip_video(msg)
        conn = vs._make_conn()
        calls: list = []

        async def _fake_ident3(data, filename, config, conn=None,
                               cache_url_key=None, req_id=None, t_req=None,
                               exclude=None, skip_bank=False, **_kw):
            calls.append({"exclude": list(exclude or []),
                          "skip_bank": skip_bank,
                          "bytes": len(data or b"")})
            return scripted

        h._identify_audio = _fake_ident3
        try:
            cb = _FakeCallback(msg, f"wrong:{token}", user_id=user_id)
            await h.wrong_song(cb, conn, _config())
            # Snapshot the card NOW: the background full-song delivery
            # runs after this and may send its own text answer
            # (overwriting last_answer_text/_kb).
            msg.card_text_snapshot = msg.last_answer_text
            msg.card_kb_snapshot = msg.last_answer_kb
            await _drain()
        finally:
            h._identify_audio = orig_ident
        return msg, cb, calls, conn

    # W3a: different confirmed -> card + full song, status deleted.
    try:
        LOG_LINES.clear()
        orig_find = adapters_mod.find_full_song_file_v2
        orig_search = h.smart_search
        orig_excl = h._exclusive_song_for
        mp3 = vm._make_mp3(_NEW_TITLE, _NEW_ARTIST, seconds=5)

        async def _fake_find3(a, t):
            return vm._full_dict(mp3, "persian_music:test", duration_sec=5.0)

        async def _fake_search3(engine, query, limit=10):
            return ([], "")

        adapters_mod.find_full_song_file_v2 = _fake_find3
        h.smart_search = _fake_search3
        h._exclusive_song_for = lambda t, a: None
        try:
            token, entry, _kb = _mint_video_token()
            msg, cb, calls, _conn = await _run_wrong(
                token, entry,
                AudioIdResult(status="identified", title=_NEW_TITLE,
                              artist=_NEW_ARTIST, confirmed=True,
                              source="shazam+audd"))
            check("W3a tap is answered at once with the checking line",
                  cb.answered and cb.answered[0][0] == _CHECKING_FA,
                  f"answered={cb.answered}")
            check("W3a re-check ran on the cached clip bytes with the exclusion",
                  len(calls) == 1 and calls[0]["bytes"] > 0
                  and calls[0]["exclude"] == [(_WRONG_TITLE, _WRONG_ARTIST)]
                  and calls[0]["skip_bank"] is False,
                  f"calls={calls}")
            check("W3a different confirmed name gets its «شناسایی شد» card",
                  "شناسایی شد" in msg.card_text_snapshot
                  and _NEW_TITLE in msg.card_text_snapshot
                  and _WRONG_TITLE not in msg.card_text_snapshot,
                  f"card={msg.card_text_snapshot!r}")
            check("W3a searching message is deleted once the card is out",
                  msg.sent_messages and msg.sent_messages[0].deleted,
                  f"deleted={[s.deleted for s in msg.sent_messages]}")
            check("W3a entry now disputes the NEW result, attempts == 1",
                  entry.get("last_title") == _NEW_TITLE
                  and entry.get("last_source") == "shazam+audd"
                  and entry.get("attempts") == 1
                  and entry.get("wrong") == [(_WRONG_TITLE, _WRONG_ARTIST)],
                  f"entry={entry}")
            check("W3a new card carries the wrong-song button (same token)",
                  f"wrong:{token}" in _kb_callbacks(msg.card_kb_snapshot),
                  f"callbacks={_kb_callbacks(msg.card_kb_snapshot)}")
            rows = await _rows_with("different_confirmed")
            check("W3a Turso report row: wrong result + source + outcome",
                  any(r[2] == _WRONG_TITLE and r[3] == _WRONG_ARTIST
                      and r[4] == "shazam+audd" and r[5] == _NEW_TITLE
                      and r[8] == "different_confirmed" for r in rows),
                  f"rows={rows}")
            check("W3a wrong_song_report log line written",
                  vs._log_has("wrong_song_report"),
                  f"lines={LOG_LINES[-2:] if LOG_LINES else []}")
        finally:
            adapters_mod.find_full_song_file_v2 = orig_find
            h.smart_search = orig_search
            h._exclusive_song_for = orig_excl
    except Exception as exc:
        check("W3a different confirmed", False, f"{type(exc).__name__}: {exc}")

    # W3b: different single-source -> «شاید...» lead, never a card.
    try:
        LOG_LINES.clear()
        token, entry, _kb = _mint_video_token()
        msg, cb, calls, _conn = await _run_wrong(
            token, entry,
            AudioIdResult(status="unconfirmed", title=_NEW_TITLE,
                          artist=_NEW_ARTIST, confirmed=False,
                          source="audd"))
        check("W3b searching message becomes the «شاید...» lead",
              msg.sent_messages and msg.sent_messages[0].edited_text
              == _MAYBE_FA.format(title=_NEW_TITLE, artist=_NEW_ARTIST),
              f"edited={[s.edited_text for s in msg.sent_messages]}")
        check("W3b no «شناسایی شد» card for a single-source name",
              not any("شناسایی شد" in a for a in msg.answers),
              f"answers={msg.answers}")
        check("W3b maybe message keeps the wrong-song button",
              msg.sent_messages
              and f"wrong:{token}" in _kb_callbacks(msg.sent_messages[0].edited_kb),
              f"callbacks={_kb_callbacks(msg.sent_messages[0].edited_kb) if msg.sent_messages else []}")
        check("W3b entry now disputes the maybe name, attempts == 1",
              entry.get("last_title") == _NEW_TITLE
              and entry.get("attempts") == 1,
              f"entry={entry}")
        rows = await _rows_with("different_maybe")
        check("W3b Turso report outcome is different_maybe",
              any(r[5] == _NEW_TITLE and r[8] == "different_maybe"
                  for r in rows),
              f"rows={rows}")
    except Exception as exc:
        check("W3b different maybe", False, f"{type(exc).__name__}: {exc}")

    # W3c: nothing different -> «پیدا نشد» + type-the-name invitation.
    try:
        LOG_LINES.clear()
        token, entry, _kb = _mint_video_token()
        msg, cb, calls, _conn = await _run_wrong(
            token, entry, AudioIdResult(status="not_found"))
        check("W3c searching message becomes the exact not-found text",
              msg.sent_messages and msg.sent_messages[0].edited_text == _NOTFOUND_FA,
              f"edited={[s.edited_text for s in msg.sent_messages]}")
        check("W3c no card and no audio on not_found (one outcome only)",
              not any("شناسایی شد" in a for a in msg.answers)
              and len(msg.audio_calls) == 0 and len(msg.answers) == 1,
              f"answers={msg.answers} audio={msg.audio_calls}")
        rows = await _rows_with("not_found")
        check("W3c Turso report outcome is not_found",
              any(r[8] == "not_found" for r in rows),
              f"rows={rows}")
    except Exception as exc:
        check("W3c not found", False, f"{type(exc).__name__}: {exc}")

    # W3d: audio fetch failure -> honest error line.
    try:
        token, entry, _kb = _mint_video_token()
        msg = _VideoMsg()
        vm._attach_clip_video(msg)

        async def _fail_get_file(file_id):
            raise RuntimeError("telegram fetch failed")

        msg.bot.get_file = _fail_get_file
        conn = vs._make_conn()
        h._identify_audio = orig_ident  # must NOT be reached
        cb = _FakeCallback(msg, f"wrong:{token}")
        await h.wrong_song(cb, conn, _config())
        check("W3d fetch failure edits the searching message into the error line",
              msg.sent_messages and msg.sent_messages[0].edited_text == _ERROR_FA,
              f"edited={[s.edited_text for s in msg.sent_messages]}")
        rows = await _rows_with("error")
        check("W3d Turso report outcome is error",
              any(r[8] == "error" for r in rows),
              f"rows={rows}")
    except Exception as exc:
        check("W3d fetch failure", False, f"{type(exc).__name__}: {exc}")

    # W3e: cap 2 -> limit alert, no re-check, button removed.
    try:
        LOG_LINES.clear()
        token, entry, _kb = _mint_video_token()
        entry["attempts"] = 2
        msg = _VideoMsg()
        kb0 = h._video_identify_kb(572595158, "VID123", "file")
        # rebuild the video markup as it looks after a confirmed result
        msg.reply_markup = kb0
        vm._attach_clip_video(msg)
        conn = vs._make_conn()
        calls: list = []

        async def _fake_ident4(data, filename, config, conn=None,
                               cache_url_key=None, req_id=None, t_req=None,
                               **_kw):
            calls.append("called")
            return AudioIdResult(status="not_found")

        h._identify_audio = _fake_ident4
        try:
            cb = _FakeCallback(msg, f"wrong:{token}")
            await h.wrong_song(cb, conn, _config())
        finally:
            h._identify_audio = orig_ident
        check("W3e third tap answers the limit alert",
              cb.answered and cb.answered[0][0] == _LIMIT_FA
              and cb.answered[0][1] is True,
              f"answered={cb.answered}")
        check("W3e no re-check runs past the cap",
              not calls, f"calls={calls}")
        check("W3e wrong-song row is removed from the video message",
              msg.markup_edits
              and f"wrong:{token}" not in _kb_callbacks(msg.markup_edits[-1]),
              f"edits={[_kb_callbacks(k) for k in msg.markup_edits]}")
        rows = await _rows_with("limit")
        check("W3e Turso report outcome is limit",
              any(r[8] == "limit" for r in rows),
              f"rows={rows}")
    except Exception as exc:
        check("W3e cap", False, f"{type(exc).__name__}: {exc}")

    # W3f/g: expired token / wrong user -> honest alerts, no re-check.
    try:
        token, entry, _kb = _mint_video_token()
        msg = _VideoMsg()
        vm._attach_clip_video(msg)
        conn = vs._make_conn()
        calls: list = []

        async def _fake_ident5(data, filename, config, conn=None,
                               cache_url_key=None, req_id=None, t_req=None,
                               **_kw):
            calls.append("called")
            return AudioIdResult(status="not_found")

        h._identify_audio = _fake_ident5
        try:
            cb = _FakeCallback(msg, "wrong:deadbeefdeadbeef")
            await h.wrong_song(cb, conn, _config())
            check("W3f expired token answers the expiry alert, no re-check",
                  cb.answered and "منقضی" in cb.answered[0][0]
                  and not calls,
                  f"answered={cb.answered} calls={calls}")
            cb2 = _FakeCallback(msg, f"wrong:{token}", user_id=111111111)
            await h.wrong_song(cb2, conn, _config())
            check("W3g another user's tap answers the not-yours alert, no re-check",
                  cb2.answered and "برای شما نیست" in cb2.answered[0][0]
                  and not calls,
                  f"answered={cb2.answered} calls={calls}")
        finally:
            h._identify_audio = orig_ident
    except Exception as exc:
        check("W3f/g guards", False, f"{type(exc).__name__}: {exc}")

    # W3h: second attempt excludes BOTH rejected names.
    try:
        token, entry, _kb = _mint_video_token()
        # Attempt 1: disputes A, gets maybe B.
        msg1, cb1, calls1, _c1 = await _run_wrong(
            token, entry,
            AudioIdResult(status="unconfirmed", title=_NEW_TITLE,
                          artist=_NEW_ARTIST, confirmed=False,
                          source="audd"))
        check("W3h attempt 1 excludes only the first rejected name",
              calls1 and calls1[0]["exclude"] == [(_WRONG_TITLE, _WRONG_ARTIST)],
              f"calls={calls1}")
        # Attempt 2: disputes B, must exclude A and B.
        msg2, cb2, calls2, _c2 = await _run_wrong(
            token, entry, AudioIdResult(status="not_found"))
        check("W3h attempt 2 excludes BOTH rejected names",
              calls2 and calls2[0]["exclude"] == [(_WRONG_TITLE, _WRONG_ARTIST),
                                                  (_NEW_TITLE, _NEW_ARTIST)],
              f"calls={calls2}")
        check("W3h attempts counted to 2 after two taps",
              entry.get("attempts") == 2, f"entry={entry}")
    except Exception as exc:
        check("W3h exclusion accumulation", False,
              f"{type(exc).__name__}: {exc}")

    # W3i: skip_bank follows the disputed source.
    try:
        # Wrong source == bank -> skip_bank True.
        token, entry, _kb = _mint_video_token()
        entry["last_source"] = "bank"
        msg, cb, calls, _conn = await _run_wrong(
            token, entry, AudioIdResult(status="not_found"))
        check("W3i bank-disputed re-check skips the bank",
              calls and calls[0]["skip_bank"] is True,
              f"calls={calls}")
        # Wrong source == shazam+audd -> skip_bank False.
        token2, entry2, _kb2 = _mint_video_token()
        entry2["last_source"] = "shazam+audd"
        msg2, cb2, calls2, _c2 = await _run_wrong(
            token2, entry2, AudioIdResult(status="not_found"))
        check("W3i shazam+audd-disputed re-check runs the bank",
              calls2 and calls2[0]["skip_bank"] is False,
              f"calls={calls2}")
    except Exception as exc:
        check("W3i skip_bank mapping", False, f"{type(exc).__name__}: {exc}")

    # ---- W4: Turso report store ----------------------------------------
    try:
        from app.bot import turso_cache as tcm
        vs._turso_on()
        ok = tcm.store_wrong_song_report(
            572595158, "https://www.instagram.com/reel/TEST123/",
            _WRONG_TITLE, _WRONG_ARTIST, "bank",
            _NEW_TITLE, _NEW_ARTIST, "shazam+audd", "different_confirmed")
        rows = _turso_rows()
        check("W4a direct store returns True and the row reads back whole",
              ok and any(r[0] == 572595158 and r[1].endswith("/reel/TEST123/")
                         and r[2] == _WRONG_TITLE and r[4] == "bank"
                         and r[5] == _NEW_TITLE and r[8] == "different_confirmed"
                         for r in rows),
              f"ok={ok} rows={rows}")
        vs._turso_off()
        ok2 = tcm.store_wrong_song_report(
            572595158, "x", _WRONG_TITLE, _WRONG_ARTIST, "bank")
        check("W4b Turso unconfigured -> False, never raises",
              ok2 is False, f"ok={ok2}")
        vs._turso_on()
    except Exception as exc:
        check("W4 Turso store", False, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
