"""Sandbox tests for the result-unification fix (2026-11-09): NO
«شناسایی شد» card is ever sent before the file status is known. A
temporary «در حال جستجوی موسیقی...» status stays until the ONE final
message, then is deleted. Three final cases:

  (a) identified + verified full file -> ONE audio, identified caption
      + real duration, identified buttons (wrong-song / lyrics /
      playlist — the heart is replaced by wrong-song, 2026-11-09
      09:50);
  (b) confirmed name + NO verified full file -> ONE audio from the
      clip's own audio (performer Avano + real duration) with the
      case-(b) caption (name + artist + "full version not found") and
      the same 3 identified buttons — never the corporate caption;
  (c) not identified at all -> the CURRENT corporate caption
      (byte-exact) + ONLY Google + retry, no identified buttons.

Cases:
  U1  Case (a): one audio, identified caption + real duration, the 3
      identified buttons (wrong-song first, lyrics, playlist; NO
      heart, NO Google/retry); temp status deleted; no text message.
  U2  Case (b) mismatch: confirmed name + mismatched file -> one
      audio (clip bytes) with the case-(b) caption + the same 3
      buttons; performer Avano + real duration; status deleted.
  U3  Case (b) no file: confirmed name + no file at all -> same as
      U2 (clip audio, case-(b) caption, 3 buttons).
  U4  Case (c): _send_unconfirmed_media -> corporate caption
      byte-exact + ONLY Google + retry; performer Avano + real
      duration; no name, no identified buttons.
  U5  Gate: a single-source (Shazam-only) video-button tap -> NO
      identified message at all (no «شناسایی شد» anywhere), exactly
      one audio with the corporate caption + Google/retry, and the
      temp status deleted.
  U6  Caption limit: very long title/artist -> the identified caption
      and the case-(b) caption are both <= 1024 chars and carry an
      ellipsis; the names are shortened, never the fixed wording.
  U7  No card before file status, all paths: _send_professional_song_card
      and _send_exclusive_song send no text message; the exclusive
      bank audio is the one final message with the bank caption +
      measured bitrate; the status is deleted.
  U8  i18n: the two new keys exist in fa/en/ar; fa m_identify_searching
      is byte-exact; fa m_identified_no_full_clip formats the pattern.

Reuses the fake aiogram / fake libsql / fake message harness from
scripts/verify_speedfix.py and the clip fixtures from
scripts/verify_fullsong_match.py. No network, no Telegram, no real Turso.

Run: python3 scripts/verify_unify_result.py ; exit 0 = all PASS.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_speedfix as vs  # noqa: E402  (installs fake aiogram, imports handlers)
import verify_fullsong_match as vm  # noqa: E402  (clip fixtures, _Msg, _deliver)

import app.bot.handlers as h  # noqa: E402
from app.i18n import TRANSLATIONS  # noqa: E402
from app.services.audio_id import AudioIdResult  # noqa: E402

check = vs.check
RESULTS = vs.RESULTS
LOG_LINES = vs.LOG_LINES
adapters_mod = vs.adapters_mod

_TITLE = "Test Song"
_ARTIST = "Test Artist"
_NEW_FA = vm._NEW_FA
_WRONG_FA = "❌ آهنگ اشتباه"
_LYRICS_FA = "📝 متن آهنگ"
_PLAYLIST_FA = "🎶 افزودن به پلی‌لیست"
_GOOGLE_FA = "🌐 جستجوی این صدا در گوگل"
_RETRY_FA = "🎧 تلاش مجدد برای شناسایی آهنگ"
_SEARCHING_FA = "در حال جستجوی موسیقی..."
_B_FA = "«{title}» از {artist} شناسایی شد، اما نسخه کامل در منابع پیدا نشد؛ صدای همین کلیپ را فرستادیم."


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


class _StatusSent(vs._FakeSent):
    """Sent message recording deletes (the temporary status)."""

    def __init__(self):
        super().__init__()
        self.deleted = False

    async def delete(self):
        self.deleted = True
        return None


def _mint_video_token(user_id=572595158, file_id="VID123", kind="file"):
    """Register a video identify token so the wrong-song button can be
    wired to it (as video_identify would). Returns the token."""
    kb0 = h._video_identify_kb(user_id, file_id, kind)
    return kb0.inline_keyboard[0][0].callback_data.split("vid:id:", 1)[1]


async def _deliver_status(msg, conn, find_result, title, artist,
                          own_media, status):
    """Run _deliver_fullsong_bg with a mocked find, a status message,
    and the REAL identified keyboard (as _send_professional_song_card
    builds it via _identified_song_kb)."""
    kb, _key = h._identified_song_kb(msg, conn, title, artist, own_media,
                                     "shazam+audd", "fa")
    orig = adapters_mod.find_full_song_file_v2

    async def _fake_find(a, t):
        return find_result

    adapters_mod.find_full_song_file_v2 = _fake_find
    try:
        await h._deliver_fullsong_bg(msg, conn, title, artist, None, kb,
                                     own_media, None, "test query", "fa",
                                     status_msg=status)
    finally:
        adapters_mod.find_full_song_file_v2 = orig


class _StatusMsg(vm._Msg):
    """_Msg that records every text answer as a deletable sent message
    (so the temp-status deletion can be observed)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.sent_messages: list = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        sent = _StatusSent()
        self.sent_messages.append(sent)
        return sent


class _FakeCallback:
    """Minimal CallbackQuery stand-in (mirrors verify_confirm_fix)."""

    def __init__(self, msg, data, user_id=572595158):
        self.data = data
        self.message = msg
        self.bot = msg.bot
        self.from_user = SimpleNamespace(id=user_id, username="testuser",
                                         first_name="Test", last_name=None,
                                         is_bot=False)
        self.answered: list = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answered.append(text)
        return None


def _config(audd_token="tok"):
    return SimpleNamespace(
        audd_token=audd_token, acrcloud_host="", acrcloud_key="",
        acrcloud_secret="", max_file_mb=50, bot_id="@AvanoMediaBot",
        owner_id=572595158, spotify_client_id="")


async def _drain(timeout=5.0):
    """Let background tasks finish (both task dicts empty) or time out."""
    import time
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if not h._AFTER_CARD_TASKS and not h._FULLSONG_TASKS:
            return True
        await asyncio.sleep(0.05)
    return False


async def main():
    vm._MP4 = vm._make_mp4()
    orig_excl = h._exclusive_song_for
    orig_search = h.smart_search

    async def _fake_search(engine, query, limit=10):
        return ([], "")

    h.smart_search = _fake_search
    h._exclusive_song_for = lambda t, a: None
    try:
        # ---- U1: case (a) — identified + verified full file -----------
        try:
            h._VIDEO_IDENTIFY.clear()
            token = _mint_video_token()
            msg = vm._Msg()
            vm._attach_clip_video(msg)
            conn = vs._make_conn()
            mp3 = vm._make_mp3(_TITLE, _ARTIST, seconds=5)
            status = _StatusSent()
            await _deliver_status(
                msg, conn, vm._full_dict(mp3, "persian_music:test",
                                         duration_sec=5.0),
                _TITLE, _ARTIST, ("video", "VID123"), status)
            check("U1 exactly ONE final message (one audio, no text)",
                  len(msg.audio_calls) == 1 and msg.answers == [],
                  f"audio={msg.audio_calls} answers={msg.answers}")
            cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
            check("U1 final audio caption is the identified caption + real duration",
                  _TITLE in cap and _ARTIST in cap and "شناسایی شد" in cap
                  and "0:05" in cap and _NEW_FA not in cap,
                  f"caption={cap!r}")
            _btns = _kb_texts(msg.audio_kwargs[0].get("reply_markup")) if msg.audio_kwargs else []
            check("U1 final audio has the 3 identified buttons (wrong-song/lyrics/playlist)",
                  _WRONG_FA in _btns and _LYRICS_FA in _btns
                  and _PLAYLIST_FA in _btns,
                  f"buttons={_btns}")
            check("U1 final audio has NO heart and NEITHER new button",
                  not any("❤️" in b for b in _btns)
                  and _GOOGLE_FA not in _btns and _RETRY_FA not in _btns,
                  f"buttons={_btns}")
            check("U1 wrong-song button reuses the video's token",
                  f"wrong:{token}" in _kb_callbacks(msg.audio_kwargs[0].get("reply_markup")),
                  f"callbacks={_kb_callbacks(msg.audio_kwargs[0].get('reply_markup'))}")
            check("U1 temp status is deleted once the final audio is out",
                  status.deleted, f"deleted={status.deleted}")
        except Exception as exc:
            check("U1 case (a) one final audio", False,
                  f"{type(exc).__name__}: {exc}")

        # ---- U2: case (b) — mismatch -> clip audio, case-(b) caption ---
        try:
            h._VIDEO_IDENTIFY.clear()
            token = _mint_video_token()
            msg = vm._Msg()
            vm._attach_clip_video(msg)
            conn = vs._make_conn()
            mp3 = vm._make_mp3("Bu Üräyin Dərdi Sənsən", "Canım Xəstə", seconds=5)
            status = _StatusSent()
            await _deliver_status(
                msg, conn, vm._full_dict(mp3, "persian_music:musicdel",
                                         duration_sec=209.0),
                "ATM", "Tiem & Salii", ("video", "VID123"), status)
            check("U2 exactly ONE final message (one clip audio, no text)",
                  len(msg.audio_calls) == 1
                  and msg.audio_calls[0][0] == "bytes" and msg.answers == [],
                  f"audio={msg.audio_calls} answers={msg.answers}")
            cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
            _b = _B_FA.format(title="ATM", artist="Tiem & Salii")
            check("U2 clip audio carries the case-(b) caption",
                  cap == _b, f"caption={cap!r}")
            _btns = _kb_texts(msg.audio_kwargs[0].get("reply_markup")) if msg.audio_kwargs else []
            check("U2 clip audio has the 3 identified buttons, no heart, no new buttons",
                  _WRONG_FA in _btns and _LYRICS_FA in _btns
                  and _PLAYLIST_FA in _btns
                  and not any("❤️" in b for b in _btns)
                  and _GOOGLE_FA not in _btns and _RETRY_FA not in _btns,
                  f"buttons={_btns}")
            kw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
            check("U2 clip audio performer is Avano with a real duration",
                  kw.get("performer") == "Avano"
                  and isinstance(kw.get("duration"), int) and kw["duration"] > 0,
                  f"kwargs={kw}")
            check("U2 temp status is deleted once the final audio is out",
                  status.deleted, f"deleted={status.deleted}")
            check("U2 mismatch is logged", vs._log_has("fullsong_mismatch"))
        except Exception as exc:
            check("U2 case (b) mismatch", False,
                  f"{type(exc).__name__}: {exc}")

        # ---- U3: case (b) — no file at all -> clip audio ---------------
        try:
            h._VIDEO_IDENTIFY.clear()
            token = _mint_video_token()
            msg = vm._Msg()
            vm._attach_clip_video(msg)
            conn = vs._make_conn()
            status = _StatusSent()
            await _deliver_status(msg, conn, None, _TITLE, _ARTIST,
                                  ("video", "VID123"), status)
            check("U3 exactly ONE final message (one clip audio, no text)",
                  len(msg.audio_calls) == 1
                  and msg.audio_calls[0][0] == "bytes" and msg.answers == [],
                  f"audio={msg.audio_calls} answers={msg.answers}")
            cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
            _b = _B_FA.format(title=_TITLE, artist=_ARTIST)
            check("U3 clip audio carries the case-(b) caption",
                  cap == _b, f"caption={cap!r}")
            _btns = _kb_texts(msg.audio_kwargs[0].get("reply_markup")) if msg.audio_kwargs else []
            check("U3 clip audio has the 3 identified buttons",
                  _WRONG_FA in _btns and _LYRICS_FA in _btns
                  and _PLAYLIST_FA in _btns,
                  f"buttons={_btns}")
            kw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
            check("U3 clip audio performer is Avano with a real duration",
                  kw.get("performer") == "Avano"
                  and isinstance(kw.get("duration"), int) and kw["duration"] > 0,
                  f"kwargs={kw}")
            check("U3 temp status is deleted once the final audio is out",
                  status.deleted, f"deleted={status.deleted}")
        except Exception as exc:
            check("U3 case (b) no file", False,
                  f"{type(exc).__name__}: {exc}")

        # ---- U4: case (c) — not identified, corporate caption ----------
        try:
            msg = vm._Msg()
            vm._attach_clip_video(msg)
            conn = vs._make_conn()
            sent = await h._send_unconfirmed_media(
                msg, conn, vm._MP4, "Flick Shot!", "Ali Geramy", "fa",
                media_kind="video", file_id="VID123", user_id=572595158)
            check("U4 exactly ONE final message (one clip audio, no text)",
                  sent is not None and len(msg.audio_calls) == 1
                  and msg.answers == [],
                  f"audio={msg.audio_calls} answers={msg.answers}")
            kw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
            check("U4 corporate caption is byte-exact",
                  kw.get("caption", "") == _NEW_FA,
                  f"caption={kw.get('caption')!r}")
            _btns = _kb_texts(kw.get("reply_markup"))
            check("U4 ONLY Google + retry (no identified buttons)",
                  _btns == [_GOOGLE_FA, _RETRY_FA],
                  f"buttons={_btns}")
            check("U4 no name in the caption",
                  "Flick Shot!" not in kw.get("caption", "")
                  and "Ali Geramy" not in kw.get("caption", ""),
                  f"caption={kw.get('caption')!r}")
            check("U4 clip audio performer is Avano with a real duration",
                  kw.get("performer") == "Avano"
                  and isinstance(kw.get("duration"), int) and kw["duration"] > 0,
                  f"kwargs={kw}")
        except Exception as exc:
            check("U4 case (c) corporate caption", False,
                  f"{type(exc).__name__}: {exc}")

        # ---- U5: gate — single-source tap sends NO identified message ---
        try:
            LOG_LINES.clear()
            h._VIDEO_IDENTIFY.clear()
            kb0 = h._video_identify_kb(572595158, "VID123", "file")
            token = kb0.inline_keyboard[0][0].callback_data.split("vid:id:", 1)[1]
            msg = _StatusMsg()
            vm._attach_clip_video(msg)
            conn = vs._make_conn()
            orig_ident = h._identify_audio

            async def _fake_ident(data, filename, config, conn=None,
                                  cache_url_key=None, req_id=None, t_req=None,
                                  **_kw):
                return AudioIdResult(status="unconfirmed", title="Flick Shot!",
                                     artist="Ali Geramy", confirmed=False,
                                     source="shazam")

            h._identify_audio = _fake_ident
            try:
                cb = _FakeCallback(msg, f"vid:id:{token}")
                await h.video_identify(cb, conn, _config())
            finally:
                h._identify_audio = orig_ident
            check("U5 NO «شناسایی شد» anywhere on a single-source tap",
                  not any("شناسایی شد" in a for a in msg.answers),
                  f"answers={msg.answers}")
            check("U5 exactly one audio (the clip's own) with the corporate caption",
                  len(msg.audio_calls) == 1 and msg.audio_calls[0][0] == "bytes"
                  and msg.audio_kwargs
                  and msg.audio_kwargs[0].get("caption", "") == _NEW_FA,
                  f"calls={msg.audio_calls}")
            _btns = _kb_texts(msg.audio_kwargs[0].get("reply_markup")) if msg.audio_kwargs else []
            check("U5 single-source audio has ONLY Google + retry",
                  _btns == [_GOOGLE_FA, _RETRY_FA],
                  f"buttons={_btns}")
            check("U5 temp status was sent once and deleted",
                  len(msg.answers) == 1
                  and msg.answers[0] == _SEARCHING_FA
                  and msg.sent_messages and msg.sent_messages[0].deleted,
                  f"answers={msg.answers} deleted={[s.deleted for s in msg.sent_messages]}")
        except Exception as exc:
            check("U5 single-source gate", False,
                  f"{type(exc).__name__}: {exc}")

        # ---- U6: caption limit — long names never exceed 1024 ----------
        try:
            long_title = "T" * 600
            long_artist = "A" * 600
            cap_a = h._final_audio_caption(long_title, long_artist, "fa",
                                           duration_sec=200)
            check("U6 identified caption with long names is <= 1024 chars",
                  len(cap_a) <= 1024, f"len={len(cap_a)}")
            check("U6 long identified caption shortens the names with an ellipsis",
                  "…" in cap_a and "شناسایی شد" in cap_a,
                  f"caption={cap_a!r}")
            cap_b = h._identified_no_full_caption(long_title, long_artist, "fa")
            check("U6 case-(b) caption with long names is <= 1024 chars",
                  len(cap_b) <= 1024, f"len={len(cap_b)}")
            check("U6 long case-(b) caption keeps the fixed wording + ellipsis",
                  "…" in cap_b and "شناسایی شد" in cap_b
                  and "نسخه کامل در منابع پیدا نشد" in cap_b,
                  f"caption={cap_b!r}")
            check("U6 short names are untouched (no ellipsis added)",
                  "…" not in h._final_audio_caption(_TITLE, _ARTIST, "fa",
                                                    duration_sec=200)
                  and "…" not in h._identified_no_full_caption(_TITLE, _ARTIST, "fa"),
                  "short caption gained an ellipsis")
        except Exception as exc:
            check("U6 caption limit", False, f"{type(exc).__name__}: {exc}")

        # ---- U7: no card before file status, all shared paths ----------
        try:
            msg = vm._Msg()
            conn = vs._make_conn()
            mp3 = vm._make_mp3(_TITLE, _ARTIST, seconds=5)
            orig_find = adapters_mod.find_full_song_file_v2

            async def _fake_find7(a, t):
                return vm._full_dict(mp3, "persian_music:test", duration_sec=5.0)

            adapters_mod.find_full_song_file_v2 = _fake_find7
            try:
                status = _StatusSent()
                spawned = await h._send_professional_song_card(
                    msg, conn, _TITLE, _ARTIST, bot_id="@AvanoMediaBot",
                    config=None, arm=None, status_msg=status)
                drained = await _drain()
            finally:
                adapters_mod.find_full_song_file_v2 = orig_find
            check("U7 _send_professional_song_card sends NO text message",
                  spawned and drained and msg.answers == [],
                  f"answers={msg.answers}")
            check("U7 status is deleted once the file is sent",
                  status.deleted, f"deleted={status.deleted}")
            check("U7 exactly one audio is sent by the shared path",
                  len(msg.audio_calls) == 1,
                  f"calls={msg.audio_calls}")
            # Exclusive-bank path: the bank audio IS the one final message.
            msg2 = vm._Msg()
            song = {"id": 1, "title": "Bank Song", "artist": "Bank Artist",
                    "file_id": "bankfid123", "ready_file_id": "",
                    "duration_sec": 200, "bitrate_kbps": 320,
                    "cover_file_id": None}
            status2 = _StatusSent()
            ok = await h._send_exclusive_song(msg2, song, status_msg=status2,
                                              title="Bank Song",
                                              artist="Bank Artist", ulang="fa")
            cap2 = msg2.audio_kwargs[0].get("caption", "") if msg2.audio_kwargs else ""
            check("U7 bank audio is the one final message (no text, bank caption)",
                  ok and len(msg2.audio_calls) == 1 and msg2.answers == []
                  and "Bank Song" in cap2 and "شناسایی شد" in cap2
                  and "💎 نسخه کامل اختصاصی آوانو" in cap2
                  and "320kbps" in cap2,
                  f"caption={cap2!r} answers={msg2.answers}")
            check("U7 bank status is deleted once the file is sent",
                  status2.deleted, f"deleted={status2.deleted}")
        except Exception as exc:
            check("U7 no card before file status", False,
                  f"{type(exc).__name__}: {exc}")

        # ---- U8: the two new i18n keys exist in fa/en/ar ---------------
        try:
            for lang in ("fa", "en", "ar"):
                check(f"U8 m_identify_searching exists in {lang}",
                      "m_identify_searching" in TRANSLATIONS[lang],
                      f"keys={list(TRANSLATIONS[lang])[:5]}...")
                check(f"U8 m_identified_no_full_clip exists in {lang}",
                      "m_identified_no_full_clip" in TRANSLATIONS[lang],
                      f"keys={list(TRANSLATIONS[lang])[:5]}...")
            check("U8 fa m_identify_searching is byte-exact",
                  TRANSLATIONS["fa"]["m_identify_searching"] == _SEARCHING_FA,
                  f"value={TRANSLATIONS['fa']['m_identify_searching']!r}")
            check("U8 fa m_identified_no_full_clip formats the exact pattern",
                  TRANSLATIONS["fa"]["m_identified_no_full_clip"].format(
                      title="X", artist="Y") == _B_FA.format(title="X", artist="Y"),
                  f"value={TRANSLATIONS['fa']['m_identified_no_full_clip']!r}")
        except Exception as exc:
            check("U8 new i18n keys", False, f"{type(exc).__name__}: {exc}")
    finally:
        h.smart_search = orig_search
        h._exclusive_song_for = orig_excl


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
