"""Sandbox tests for the confirm fix (2026-11-09) as reworked by the
result-unification fix (2026-11-09): a song name is shown as
«شناسایی شد» ONLY when TWO sources agree (exclusive bank, or
Shazam+AudD). A single source, a disagreement, or no identification
never gets a card — ONLY the clip's own audio with the new caption +
Google/retry buttons. NO card is ever sent before the file status is
known: a temporary «در حال جستجوی موسیقی...» status stays until the
ONE final message, then is deleted. A confirmed song's ONE final
message is the audio with the identified caption + real duration +
the identified buttons (lyrics/playlist, wrong-song in place of the
heart when the song came from a video).

Cases:
  C1  Two-source gate in _identify_audio: Shazam-only -> "unconfirmed"
      (AudD still ran, in parallel); Shazam+AudD agreement ->
      "identified"; disagreement -> "unconfirmed" (Shazam's name);
      AudD-only -> "unconfirmed"; AudD unconfigured -> "unconfirmed"
      (strict: no second source can ever confirm); bracket-insensitive
      agreement ("(Remix)" vs plain) -> "identified". Plus the
      end-to-end handler proof: a video-button tap on an unconfirmed
      result sends NO card, only the extracted audio with the new
      caption + both buttons.
  C2  Agreement -> NO card is sent; the temporary status is deleted
      when the file is ready and ONE audio message carries the
      identified caption + real duration + the identified buttons
      (lyrics/playlist here — no video, so no wrong-song button).
  C3  Bank match -> the bank file IS the one final message: the real
      _send_exclusive_song sends the stored file_id with the
      identified caption + real duration + bank marker + measured
      bitrate, the passed buttons, and deletes the status. The
      caption is ALWAYS the final identified caption — with or
      without a status (the old exclusive card text is gone).
  C4  Extracted audio has performer "Avano" and a real (non-zero)
      duration — both the stream-copy helper and the MP3 re-encode
      helper — and each send writes one `sent_extracted_audio` log
      line (bytes + caption key + button labels).
  C5  Google URL contains the name query (or the button is omitted
      when no name exists); the retry button carries vid:id:<token>.
  C6  The retry button triggers the EXISTING identify flow: its
      callback_data is the video's registered vid:id token (same token
      the original identify button carries), resolvable in
      _VIDEO_IDENTIFY.
  C7  Identified/bank sends use the identified caption (never the old
      full-song caption, never the new extracted caption) and
      NEITHER new button.

Reuses the fake aiogram / fake libsql / fake message harness from
scripts/verify_speedfix.py and the clip fixtures from
scripts/verify_fullsong_match.py. No network, no Telegram, no real Turso.

Run: python3 scripts/verify_confirm_fix.py ; exit 0 = all PASS.
"""

from __future__ import annotations

import asyncio
import sys
import time
import urllib.parse as _up
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_speedfix as vs  # noqa: E402  (installs fake aiogram, imports handlers)
import verify_fullsong_match as vm  # noqa: E402  (clip fixtures, _Msg, _deliver)

import app.bot.handlers as h  # noqa: E402
from app.features import pro as profeat  # noqa: E402
from app.services.audio_id import AudioIdResult  # noqa: E402

check = vs.check
RESULTS = vs.RESULTS
LOG_LINES = vs.LOG_LINES
adapters_mod = vs.adapters_mod

_TITLE = "Test Song"
_ARTIST = "Test Artist"
_NEW_FA = vm._NEW_FA
_GOOGLE_FA = "🌐 جستجوی این صدا در گوگل"
_RETRY_FA = "🔄 شناسایی مجدد"


class _CardSent(vs._FakeSent):
    """Sent message recording delete/edit attempts."""

    def __init__(self):
        super().__init__()
        self.deleted = False
        self.edited = False

    async def edit_text(self, text, **kw):
        self.edited = True
        return self

    async def edit_reply_markup(self, **kw):
        self.edited = True
        return self

    async def delete(self):
        self.deleted = True
        return None


class _CardMsg(vs._FakeMessage):
    """Fake message that records the card send (text + keyboard) and
    every answer_audio's kwargs."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.card_sent = None
        self.card_text = ""
        self.card_kb = None
        self.audio_kwargs: list[dict] = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        self.card_sent = _CardSent()
        self.card_text = text
        self.card_kb = kw.get("reply_markup")
        return self.card_sent

    async def answer_audio(self, audio, **kw):
        self.audio_kwargs.append(kw)
        return await super().answer_audio(audio, **kw)


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


def _kb_urls(kb) -> list:
    urls = []
    for row in getattr(kb, "inline_keyboard", []) or []:
        for b in row:
            urls.append(getattr(b, "url", "") or "")
    return urls


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
    """Monkeypatch handlers' Shazam/AudD with recording fakes.

    shazam/audd: AudioIdResult or None (None = the stub returns
    not_found). calls: list receiving (name, start, end) per call, so
    the test can prove the two ran concurrently.
    """
    async def _fake_shazam(probe, prepared_wav=None, req_id=None):
        t0 = time.perf_counter()
        await asyncio.sleep(0.3)
        calls.append(("shazam", t0, time.perf_counter()))
        return shazam if shazam is not None else AudioIdResult(status="not_found")

    async def _fake_audd(data, filename, token):
        t0 = time.perf_counter()
        await asyncio.sleep(0.3)
        calls.append(("audd", t0, time.perf_counter()))
        return audd if audd is not None else AudioIdResult(status="not_found")

    h.identify_bytes_shazam = _fake_shazam
    h.identify_bytes = _fake_audd


class _FakeCallback:
    """Minimal CallbackQuery stand-in for video_identify."""

    def __init__(self, msg, token):
        self.data = f"vid:id:{token}"
        self.message = msg
        self.bot = msg.bot
        self.from_user = msg.from_user
        self.answered = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answered.append(text)
        return None


async def main():
    vm._MP4 = vm._make_mp4()
    orig_shazam = h.identify_bytes_shazam
    orig_audd = h.identify_bytes
    orig_xbank_ok = h._XBANK_OK

    # ---- C1: the two-source gate in _identify_audio ----------------------
    try:
        h._XBANK_OK = False  # bank is a separate path (C3); gate only
        data = vm._MP4
        # C1a: Shazam-only -> unconfirmed, AudD still ran (in parallel).
        calls: list = []
        _stub_lookups(
            AudioIdResult(status="identified", title="Flick Shot!",
                          artist="Ali Geramy"),
            AudioIdResult(status="not_found"), calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None)
        check("C1a Shazam-only is unconfirmed, never identified",
              r.status == "unconfirmed" and not r.confirmed
              and r.title == "Flick Shot!" and r.artist == "Ali Geramy"
              and r.source == "shazam",
              f"status={r.status} confirmed={r.confirmed} source={r.source}")
        _sh = [c for c in calls if c[0] == "shazam"]
        _au = [c for c in calls if c[0] == "audd"]
        check("C1a AudD ran even though Shazam identified (no short-circuit)",
              len(_au) == 1, f"calls={calls}")
        check("C1a Shazam and AudD ran concurrently (intervals overlap)",
              bool(_sh and _au)
              and max(_sh[0][1], _au[0][1]) < min(_sh[0][2], _au[0][2]),
              f"calls={calls}")
        # C1b: agreement -> identified.
        calls.clear()
        _stub_lookups(
            AudioIdResult(status="identified", title=_TITLE, artist=_ARTIST),
            AudioIdResult(status="identified", title=_TITLE, artist=_ARTIST),
            calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None)
        check("C1b Shazam+AudD agreement is identified and confirmed",
              r.status == "identified" and r.confirmed
              and r.source == "shazam+audd"
              and r.title == _TITLE and r.artist == _ARTIST,
              f"status={r.status} confirmed={r.confirmed} source={r.source}")
        # C1c: disagreement -> unconfirmed, Shazam's name kept.
        _stub_lookups(
            AudioIdResult(status="identified", title=_TITLE, artist=_ARTIST),
            AudioIdResult(status="identified", title="Other Song",
                          artist="Other Artist"), calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None)
        check("C1c disagreement is unconfirmed with Shazam's name",
              r.status == "unconfirmed" and not r.confirmed
              and r.title == _TITLE and r.source == "shazam",
              f"status={r.status} title={r.title!r} source={r.source}")
        # C1d: AudD-only -> unconfirmed.
        _stub_lookups(
            AudioIdResult(status="not_found"),
            AudioIdResult(status="identified", title="Aud Song",
                          artist="Aud Artist"), calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None)
        check("C1d AudD-only is unconfirmed with AudD's name",
              r.status == "unconfirmed" and not r.confirmed
              and r.title == "Aud Song" and r.source == "audd",
              f"status={r.status} title={r.title!r} source={r.source}")
        # C1e: AudD unconfigured -> unconfirmed (strict gate).
        calls.clear()
        _stub_lookups(
            AudioIdResult(status="identified", title=_TITLE, artist=_ARTIST),
            AudioIdResult(status="identified", title=_TITLE, artist=_ARTIST),
            calls)
        r = await h._identify_audio(data, "clip.mp4", _config(audd_token=""),
                                    conn=None)
        check("C1e no AudD token means no confirmation is possible",
              r.status == "unconfirmed" and not r.confirmed
              and r.source == "shazam",
              f"status={r.status} source={r.source}")
        check("C1e AudD was not called without a token",
              not any(c[0] == "audd" for c in calls), f"calls={calls}")
        # C1f: bracket-insensitive agreement ("(Remix)" vs plain).
        _stub_lookups(
            AudioIdResult(status="identified", title="Test Song (Remix)",
                          artist=_ARTIST),
            AudioIdResult(status="identified", title=_TITLE, artist=_ARTIST),
            calls)
        r = await h._identify_audio(data, "clip.mp4", _config(), conn=None)
        check("C1f bracket-insensitive agreement confirms",
              r.status == "identified" and r.confirmed
              and r.source == "shazam+audd",
              f"status={r.status} confirmed={r.confirmed}")
    except Exception as exc:
        check("C1 two-source gate", False, f"{type(exc).__name__}: {exc}")
    finally:
        h.identify_bytes_shazam = orig_shazam
        h.identify_bytes = orig_audd
        h._XBANK_OK = orig_xbank_ok

    # ---- C1 (handler): unconfirmed tap sends NO card ---------------------
    try:
        LOG_LINES.clear()
        h._VIDEO_IDENTIFY.clear()
        kb0 = h._video_identify_kb(572595158, "VID123", "file")
        token = kb0.inline_keyboard[0][0].callback_data.split("vid:id:", 1)[1]
        msg = vm._Msg()
        vm._attach_clip_video(msg)
        conn = vs._make_conn()
        orig_ident = h._identify_audio

        async def _fake_ident(data, filename, config, conn=None,
                              cache_url_key=None, req_id=None, t_req=None,
                              **_kw):  # batch F passes exclude= (ignored here)
            return AudioIdResult(status="unconfirmed", title="Flick Shot!",
                                 artist="Ali Geramy", confirmed=False,
                                 source="shazam")

        h._identify_audio = _fake_ident
        try:
            cb = _FakeCallback(msg, token)
            await h.video_identify(cb, conn, _config())
        finally:
            h._identify_audio = orig_ident
        check("C1g unconfirmed tap sends NO «شناسایی شد» card",
              not any("شناسایی شد" in a for a in msg.answers),
              f"answers={msg.answers}")
        check("C1g unconfirmed tap sends exactly one audio (the clip's own)",
              len(msg.audio_calls) == 1 and msg.audio_calls[0][0] == "bytes",
              f"calls={msg.audio_calls}")
        kw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("C1g extracted audio carries the new caption",
              kw.get("caption", "") == _NEW_FA, f"caption={kw.get('caption')!r}")
        _texts = _kb_texts(kw.get("reply_markup"))
        check("C1g extracted audio has BOTH new buttons, in ONE row",
              _GOOGLE_FA in _texts and _RETRY_FA in _texts
              and len(kw.get("reply_markup").inline_keyboard) == 1,
              f"buttons={_texts}")
        check("C1g retry button reuses the video's existing token",
              f"vid:id:{token}" in _kb_callbacks(kw.get("reply_markup")),
              f"callbacks={_kb_callbacks(kw.get('reply_markup'))}")
        check("C1g extracted audio performer is Avano, duration is real",
              kw.get("performer") == "Avano"
              and isinstance(kw.get("duration"), int) and kw["duration"] > 0,
              f"kwargs={kw}")
        check("C1g extracted-audio send is logged",
              vs._log_has("sent_extracted_audio"),
              f"lines={LOG_LINES[-3:] if LOG_LINES else []}")
    except Exception as exc:
        check("C1 unconfirmed handler response", False,
              f"{type(exc).__name__}: {exc}")

    # ---- C2: agreement -> NO card, status deleted, ONE final audio -----
    try:
        LOG_LINES.clear()
        msg = _CardMsg()
        conn = vs._make_conn()
        mp3 = vm._make_mp3(_TITLE, _ARTIST, seconds=5)
        orig_find = adapters_mod.find_full_song_file_v2

        async def _fake_find(a, t):
            return vm._full_dict(mp3, "persian_music:test", duration_sec=5.0)

        adapters_mod.find_full_song_file_v2 = _fake_find
        orig_search = h.smart_search
        orig_excl = h._exclusive_song_for

        async def _fake_search(engine, query, limit=10):
            return ([], "")

        h.smart_search = _fake_search
        h._exclusive_song_for = lambda t, a: None
        status = _CardSent()
        try:
            spawned = await h._send_professional_song_card(
                msg, conn, _TITLE, _ARTIST, bot_id="@AvanoMediaBot",
                config=None, arm=None, status_msg=status)
            drained = await _drain()
        finally:
            adapters_mod.find_full_song_file_v2 = orig_find
            h.smart_search = orig_search
            h._exclusive_song_for = orig_excl
        check("C2 NO card is sent for a confirmed name",
              spawned and msg.card_sent is None and msg.card_text == "",
              f"card_text={msg.card_text!r}")
        check("C2 background delivery drained", drained)
        check("C2 status is deleted once the file is ready",
              status.deleted, f"deleted={status.deleted}")
        check("C2 exactly one audio message (the file) is sent",
              len(msg.audio_calls) == 1 and msg.audio_calls[0][0] == "bytes",
              f"calls={msg.audio_calls}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("C2 final audio carries the identified caption + real duration",
              _TITLE in cap and _ARTIST in cap and "شناسایی شد" in cap
              and "0:05" in cap, f"caption={cap!r}")
        _btns = _kb_texts(msg.audio_kwargs[0].get("reply_markup")) if msg.audio_kwargs else []
        check("C2 final audio has exactly the 2 identified buttons (playlist/lyrics), one row, no heart, no wrong-song",
              _btns == ["🎵 افزودن به پلی‌لیست", "📝 متن آهنگ و ترجمه"]
              and len(msg.audio_kwargs[0].get("reply_markup").inline_keyboard) == 1
              and not any("❤️" in b for b in _btns)
              and "wrong:" not in "".join(_kb_callbacks(msg.audio_kwargs[0].get("reply_markup"))),
              f"buttons={_btns}")
        check("C2 final audio has NEITHER new button",
              msg.audio_kwargs
              and _GOOGLE_FA not in _btns and _RETRY_FA not in _btns,
              f"buttons={_btns}")
        check("C2 final audio caption is not the new extracted caption",
              _NEW_FA not in cap, f"caption={cap!r}")
    except Exception as exc:
        check("C2 confirmed card replacement", False,
              f"{type(exc).__name__}: {exc}")

    # ---- C3: bank match -> bank file IS the one final message -----------
    try:
        msg = _CardMsg()
        conn = vs._make_conn()
        song = {"id": 1, "title": "Bank Song", "artist": "Bank Artist",
                "file_id": "bankfid123", "ready_file_id": "",
                "duration_sec": 200, "bitrate_kbps": 320,
                "cover_file_id": None}
        key = profeat.remember_result("Bank Song", "Bank Artist", "", "")
        kb = __import__("aiogram.types", fromlist=["InlineKeyboardMarkup"]).InlineKeyboardMarkup(
            inline_keyboard=profeat.build_song_card_buttons(key, False))
        status = _CardSent()
        ok = await h._send_exclusive_song(msg, song, status_msg=status, kb=kb,
                                          title="Bank Song",
                                          artist="Bank Artist", ulang="fa")
        check("C3 bank file is sent", ok and len(msg.audio_calls) == 1
              and msg.audio_calls[0] == ("str", "bankfid123"),
              f"ok={ok} calls={msg.audio_calls}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("C3 bank audio caption is the identified caption + duration + bank marker + bitrate",
              "Bank Song" in cap and "Bank Artist" in cap
              and "شناسایی شد" in cap and "3:20" in cap
              and "💎 نسخه کامل اختصاصی آوانو" in cap
              and "320kbps" in cap, f"caption={cap!r}")
        check("C3 bank audio has the passed buttons",
              msg.audio_kwargs
              and msg.audio_kwargs[0].get("reply_markup") is kb,
              "reply_markup is not the passed keyboard")
        check("C3 bank audio has NEITHER new button",
              msg.audio_kwargs
              and _GOOGLE_FA not in _kb_texts(msg.audio_kwargs[0].get("reply_markup"))
              and _RETRY_FA not in _kb_texts(msg.audio_kwargs[0].get("reply_markup")),
              f"buttons={_kb_texts(msg.audio_kwargs[0].get('reply_markup')) if msg.audio_kwargs else []}")
        check("C3 status is deleted once the bank file is sent",
              status.deleted, f"deleted={status.deleted}")
        # Without a status the caption is STILL the final identified
        # caption (unify fix: the old exclusive card text is gone).
        msg2 = _CardMsg()
        ok2 = await h._send_exclusive_song(msg2, song)
        cap2 = msg2.audio_kwargs[0].get("caption", "") if msg2.audio_kwargs else ""
        check("C7 bank send without a status still uses the final identified caption",
              ok2 and "Bank Song" in cap2 and "شناسایی شد" in cap2
              and "3:20" in cap2 and "💎 نسخه کامل اختصاصی آوانو" in cap2
              and "320kbps" in cap2,
              f"caption={cap2!r}")
    except Exception as exc:
        check("C3 bank one-final-message", False, f"{type(exc).__name__}: {exc}")

    # ---- C4: performer Avano + real duration, both helpers ---------------
    try:
        LOG_LINES.clear()
        msg = vm._Msg()
        vm._attach_clip_video(msg)
        conn = vs._make_conn()
        sent = await h._extract_and_send_clip_audio_copy(
            msg, conn, vm._MP4, _NEW_FA, "fa",
            reply_markup=h._extracted_audio_kb(_TITLE, _ARTIST, "fa",
                                               token="tok123"),
            caption_key="m_extracted_audio_new")
        kw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("C4 stream-copy performer is Avano, duration is real",
              sent is not None and kw.get("performer") == "Avano"
              and isinstance(kw.get("duration"), int) and kw["duration"] > 0,
              f"kwargs={kw}")
        check("C4 stream-copy keeps the extracted-audio title",
              kw.get("title", "") == "صدای استخراج\u200cشده از ویدیو",
              f"title={kw.get('title')!r}")
        check("C4 stream-copy send is logged",
              vs._log_has("sent_extracted_audio"))
        LOG_LINES.clear()
        msg2 = vm._Msg()
        conn2 = vs._make_conn()
        sent2 = await h._extract_and_send_clip_audio(
            msg2, conn2, vm._MP4, caption=_NEW_FA,
            reply_markup=h._extracted_audio_kb(_TITLE, _ARTIST, "fa",
                                               token="tok123"),
            caption_key="m_extracted_audio_new")
        kw2 = msg2.audio_kwargs[0] if msg2.audio_kwargs else {}
        check("C4 MP3 re-encode performer is Avano, duration is real",
              sent2 is not None and kw2.get("performer") == "Avano"
              and isinstance(kw2.get("duration"), int) and kw2["duration"] > 0,
              f"kwargs={kw2}")
        check("C4 MP3 re-encode send is logged",
              vs._log_has("sent_extracted_audio"))
    except Exception as exc:
        check("C4 extracted metadata", False, f"{type(exc).__name__}: {exc}")

    # ---- C5: Google URL ---------------------------------------------------
    try:
        kb = h._extracted_audio_kb(_TITLE, _ARTIST, "fa", token="tok123")
        urls = [u for u in _kb_urls(kb) if u]
        check("C5 Google URL contains the name query",
              len(urls) == 1
              and urls[0] == "https://www.google.com/search?q="
              + _up.quote_plus(f"{_TITLE} {_ARTIST}"),
              f"urls={urls}")
        check("C5 Google URL decodes back to the name",
              _up.parse_qs(_up.urlparse(urls[0]).query).get("q") == [f"{_TITLE} {_ARTIST}"],
              f"urls={urls}")
        kb_fa = h._extracted_audio_kb("آهنگ تست", "خواننده تست", "fa",
                                      token="tok123")
        _u = [u for u in _kb_urls(kb_fa) if u][0]
        check("C5 Persian name is URL-encoded in the Google query",
              _up.parse_qs(_up.urlparse(_u).query).get("q") == ["آهنگ تست خواننده تست"],
              f"url={_u}")
        kb_none = h._extracted_audio_kb("", "", "fa")
        check("C5 no name at all omits the Google button",
              kb_none is None, f"kb={kb_none}")
        kb_name_only = h._extracted_audio_kb(_TITLE, _ARTIST, "fa")
        check("C5 name without a token gives only the Google button",
              _kb_texts(kb_name_only) == [_GOOGLE_FA],
              f"buttons={_kb_texts(kb_name_only)}")
        check("C5 retry button carries vid:id:<token>",
              [c for c in _kb_callbacks(kb) if c] == ["vid:id:tok123"],
              f"callbacks={_kb_callbacks(kb)}")
    except Exception as exc:
        check("C5 Google button", False, f"{type(exc).__name__}: {exc}")

    # ---- C6: retry triggers the existing identify flow --------------------
    try:
        h._VIDEO_IDENTIFY.clear()
        kb0 = h._video_identify_kb(572595158, "VID123", "file")
        token = kb0.inline_keyboard[0][0].callback_data.split("vid:id:", 1)[1]
        kb = h._extracted_audio_kb(_TITLE, _ARTIST, "fa", file_id="VID123",
                                   user_id=572595158, kind="file")
        cbs = [c for c in _kb_callbacks(kb) if c]
        check("C6 retry reuses the video's existing identify token",
              cbs == [f"vid:id:{token}"],
              f"callbacks={cbs} token={token}")
        check("C6 the token resolves in the existing registry",
              token in h._VIDEO_IDENTIFY
              and h._VIDEO_IDENTIFY[token].get("file_id") == "VID123"
              and h._VIDEO_IDENTIFY[token].get("user_id") == 572595158,
              f"entry={h._VIDEO_IDENTIFY.get(token)}")
        check("C6 the retry callback is the router's vid:id format",
              cbs and cbs[0].startswith("vid:id:"),
              f"callbacks={cbs}")
    except Exception as exc:
        check("C6 retry button", False, f"{type(exc).__name__}: {exc}")

    # ---- C7: identified sends use the identified caption ----------------
    try:
        msg = vm._Msg()
        conn = vs._make_conn()
        mp3 = vm._make_mp3(_TITLE, _ARTIST, seconds=5)
        await vm._deliver(msg, conn,
                          vm._full_dict(mp3, "persian_music:test",
                                        duration_sec=5.0),
                          _TITLE, _ARTIST)
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("C7 full-song send uses the identified caption (not the old full-song caption)",
              "شناسایی شد" in cap and _TITLE in cap and "0:05" in cap
              and "نسخه کامل آهنگ پیدا" not in cap and _NEW_FA not in cap,
              f"caption={cap!r}")
        check("C7 full-song send has NEITHER new button",
              _GOOGLE_FA not in _kb_texts(msg.audio_kwargs[0].get("reply_markup"))
              and _RETRY_FA not in _kb_texts(msg.audio_kwargs[0].get("reply_markup")),
              f"buttons={_kb_texts(msg.audio_kwargs[0].get('reply_markup')) if msg.audio_kwargs else []}")
    except Exception as exc:
        check("C7 identified keeps old caption", False,
              f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
