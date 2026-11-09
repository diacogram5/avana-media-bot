"""Sandbox tests for the FINAL identify UI (user order 2026-11-09 14:54).

The order supersedes the 09:49/09:50 button layouts: the heart button is
gone from the audio, wrong-song lives ONLY under the video, the video
carries Google + YouTube search links after the result is known, and
the extracted-audio caption lost its middle line.

Cases (each driven end-to-end through the real handlers):
  F1  Scenario 1 (video) + scenario 2 (full song), one confirmed tap:
      the tap CLEARS the video keyboard at once; after the result the
      video shows [Google, YouTube] + [report wrong identification]
      (same token, exact quote_plus URLs); the ONE final audio has
      exactly [playlist, lyrics] in one row; no heart, no wrong-song,
      no Google/retry under the audio; temp status deleted; no
      «شناسایی شد» text message.
  F1b Scenario 1 with NO name (not identified): the video keyboard
      ends EMPTY; the one final audio is the extracted clip audio with
      the exact new caption and ONLY the retry button (no Google —
      no name to search for).
  F3  Scenario 3 (extracted, single-source name): the send is
      answer_audio (never answer_voice); caption byte-exact; ONLY
      the retry button in one row (no Google button at all); retry
      reuses the video's identify token; no full-song buttons; video
      keyboard shows Google + YouTube + report.
  F4  Scenario 4 (exclusive bank): same audio layout as scenario 2
      (playlist + lyrics, one row), bank marker + real duration in
      the caption, status deleted.
  F5  Case (b) (name known, no full file): clip audio with the
      case-(b) caption (independently typed) + [playlist, lyrics]
      in one row; status deleted.
  F6  §6 (extracted-audio appearance): the only answer_voice left in
      handlers.py is the TTS feature (the identify paths never send
      a voice bubble); _extract_and_send_clip_audio passes the REAL
      probed duration + title + performer "Avano" via answer_audio.
  F7  i18n: the extracted caption has no middle line in fa/en/ar.
  F8  Aggregate: no TEXT answer in any driven flow contains
      «شناسایی شد» (the name is announced only inside the one final
      audio's caption, never as a card).

Reuses the fake aiogram / fake libsql / fake message harness from
scripts/verify_speedfix.py, the clip fixtures from
scripts/verify_fullsong_match.py, the video-token helpers from
scripts/verify_wrong_song.py and the status helpers from
scripts/verify_unify_result.py. No network, no Telegram, no real Turso.

Run: python3 scripts/verify_identify_ui_final.py ; exit 0 = all PASS.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_speedfix as vs  # noqa: E402  (installs fake aiogram, imports handlers)
import verify_fullsong_match as vm  # noqa: E402  (clip fixtures, _Msg)
import verify_wrong_song as vw  # noqa: E402  (video-token helpers)
import verify_unify_result as vu  # noqa: E402  (status helpers)

import app.bot.handlers as h  # noqa: E402
from app.i18n import TRANSLATIONS  # noqa: E402
from app.services.audio_id import AudioIdResult  # noqa: E402

check = vs.check
RESULTS = vs.RESULTS
LOG_LINES = vs.LOG_LINES
adapters_mod = vs.adapters_mod

_TITLE = "Flick Shot!"
_ARTIST = "Ali Geramy"
_PL_FA = "🎵 افزودن به پلی‌لیست"
_LYRICS_FA = "📝 متن آهنگ و ترجمه"
_REPORT_FA = "⚠️ گزارش شناسایی اشتباه ⚠️"
_VGOOGLE_FA = "🌐 جستجو در Google"
_VYT_FA = "▶️ جستجو در YouTube"
_EGOOGLE_FA = "🌐 جستجوی این صدا در گوگل"
_RETRY_FA = "🔍 جستجوی مجدد آهنگ"
_STATUS_FA = "در حال جستجوی موسیقی..."
# Independently typed expected captions (never read from i18n.py).
_NEW_EXACT = vm._NEW_EXACT
_B_EXACT = ("«{title}» از {artist} شناسایی شد، اما نسخه کامل در منابع "
            "پیدا نشد؛ صدای همین کلیپ را فرستادیم.")
_EXTRACTED_TITLE = "صدای استخراج‌شده از ویدیو"

_ALL_TEXT: list[str] = []


def _kb_texts(kb) -> list:
    return vw._kb_texts(kb)


def _kb_callbacks(kb) -> list:
    return vw._kb_callbacks(kb)


def _kb_urls(kb) -> list:
    return vw._kb_urls(kb)


def _kb_rows(kb) -> list:
    return [[getattr(b, "text", "") or "" for b in row]
            for row in getattr(kb, "inline_keyboard", []) or []]


class _FMsg(vw._VideoMsg):
    """Video message that also records answer_voice calls (must stay 0
    in every identify-result path — a voice bubble renders smaller
    than an audio card)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.voice_calls: list = []

    async def answer_voice(self, voice, **kw):
        self.voice_calls.append((voice, kw))
        return None


def _fresh_video_msg():
    """A bot-sent video message with a fresh identify token (no result
    recorded yet, as before the first tap). Returns (msg, token)."""
    kb0 = h._video_identify_kb(572595158, "VID123", "file")
    token = kb0.inline_keyboard[0][0].callback_data.split("vid:id:", 1)[1]
    msg = _FMsg()
    msg.reply_markup = kb0
    vm._attach_clip_video(msg)
    return msg, token


async def _run_video_identify(scripted, find_result, msg=None, token=None):
    """Drive one video-button tap with a scripted _identify_audio
    result and a mocked full-song find. Returns (msg, cb, token)."""
    if msg is None:
        msg, token = _fresh_video_msg()
    orig_ident = h._identify_audio
    orig_find = adapters_mod.find_full_song_file_v2
    orig_search = h.smart_search
    orig_excl = h._exclusive_song_for

    async def _fake_ident(data, filename, config, conn=None,
                          cache_url_key=None, req_id=None, t_req=None,
                          **_kw):
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


def _collect_answers(msg):
    # TEXT answers only: the one final AUDIO's caption legitimately
    # contains «شناسایی شد» (identified caption, case (a) and (b)) —
    # the ban is on a separate «شناسایی شد» text/card message.
    _ALL_TEXT.extend(msg.answers)


async def main():
    vm._MP4 = vm._make_mp4()
    h._VIDEO_IDENTIFY.clear()

    # ---- F1 + F2: confirmed tap -> full song -------------------------
    try:
        mp3 = vm._make_mp3(_TITLE, _ARTIST, seconds=5)
        msg, cb, token = await _run_video_identify(
            AudioIdResult(status="identified", title=_TITLE,
                          artist=_ARTIST, confirmed=True,
                          source="shazam+audd"),
            vm._full_dict(mp3, "persian_music:test", duration_sec=5.0))
        _collect_answers(msg)
        check("F1a video keyboard is CLEARED first (no buttons during identification)",
              msg.markup_edits
              and _kb_texts(msg.markup_edits[0]) == [],
              f"edits={[_kb_texts(k) for k in msg.markup_edits]}")
        _vkb = msg.markup_edits[-1] if msg.markup_edits else None
        _q = quote_plus(f"{_TITLE} {_ARTIST}")
        check("F1b video keyboard after the result is [Google, YouTube] + [report]",
              _vkb is not None
              and _kb_rows(_vkb) == [[_VGOOGLE_FA, _VYT_FA], [_REPORT_FA]]
              and f"wrong:{token}" in _kb_callbacks(_vkb),
              f"rows={_kb_rows(_vkb) if _vkb is not None else None}")
        check("F1c video search URLs are exact quote_plus(title artist)",
              _vkb is not None
              and _kb_urls(_vkb)[0] == f"https://www.google.com/search?q={_q}"
              and _kb_urls(_vkb)[1] == f"https://www.youtube.com/results?search_query={_q}",
              f"urls={_kb_urls(_vkb) if _vkb is not None else None}")
        check("F1d exactly ONE final audio via answer_audio, never answer_voice",
              len(msg.audio_calls) == 1 and msg.audio_calls[0][0] in ("bytes", "str")
              and msg.voice_calls == [],
              f"audio={msg.audio_calls} voice={msg.voice_calls}")
        _akw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("F2 full song has exactly [playlist, lyrics] in ONE row",
              _kb_texts(_akw.get("reply_markup")) == [_PL_FA, _LYRICS_FA]
              and len(_akw.get("reply_markup").inline_keyboard) == 1,
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        check("F2 full song has NO heart, NO wrong-song, NO Google/retry",
              "wrong:" not in "".join(_kb_callbacks(_akw.get("reply_markup")))
              and not any("❤️" in b for b in _kb_texts(_akw.get("reply_markup")))
              and _EGOOGLE_FA not in _kb_texts(_akw.get("reply_markup"))
              and _RETRY_FA not in _kb_texts(_akw.get("reply_markup")),
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        check("F2 full-song caption names the song with its real duration",
              _TITLE in _akw.get("caption", "")
              and _ARTIST in _akw.get("caption", "")
              and "0:05" in _akw.get("caption", ""),
              f"caption={_akw.get('caption')!r}")
        check("F1e temp status is deleted once the final audio is out",
              msg.sent_messages and msg.sent_messages[0].deleted,
              f"deleted={[s.deleted for s in msg.sent_messages]}")
        check("F1f video entry records the shown result (wrong-song source)",
              h._VIDEO_IDENTIFY.get(token, {}).get("last_title") == _TITLE
              and h._VIDEO_IDENTIFY.get(token, {}).get("last_source") == "shazam+audd",
              f"entry={h._VIDEO_IDENTIFY.get(token)}")
    except Exception as exc:
        check("F1/F2 confirmed tap", False, f"{type(exc).__name__}: {exc}")

    # ---- F1b: no name at all -> empty video keyboard ------------------
    try:
        msg, cb, token = await _run_video_identify(
            AudioIdResult(status="not_found"), None)
        _collect_answers(msg)
        check("F1b video keyboard ends EMPTY when nothing was identified",
              msg.markup_edits
              and _kb_texts(msg.markup_edits[-1]) == [],
              f"edits={[_kb_texts(k) for k in msg.markup_edits]}")
        check("F1b exactly ONE final audio (extracted), never answer_voice",
              len(msg.audio_calls) == 1 and msg.voice_calls == [],
              f"audio={msg.audio_calls} voice={msg.voice_calls}")
        _akw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("F1b extracted caption is byte-exact (no middle line)",
              _akw.get("caption", "") == _NEW_EXACT,
              f"caption={_akw.get('caption')!r}")
        check("F1b extracted audio has ONLY the retry button (no name, no Google)",
              _kb_texts(_akw.get("reply_markup")) == [_RETRY_FA]
              and f"vid:id:{token}" in _kb_callbacks(_akw.get("reply_markup")),
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        check("F1b temp status is deleted",
              msg.sent_messages and msg.sent_messages[0].deleted,
              f"deleted={[s.deleted for s in msg.sent_messages]}")
    except Exception as exc:
        check("F1b no-name tap", False, f"{type(exc).__name__}: {exc}")

    # ---- F3: single-source name -> extracted audio --------------------
    try:
        msg, cb, token = await _run_video_identify(
            AudioIdResult(status="unconfirmed", title=_TITLE,
                          artist=_ARTIST, confirmed=False,
                          source="shazam"),
            None)
        _collect_answers(msg)
        check("F3 exactly ONE final audio via answer_audio, never answer_voice",
              len(msg.audio_calls) == 1 and msg.voice_calls == [],
              f"audio={msg.audio_calls} voice={msg.voice_calls}")
        _akw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("F3 extracted caption is byte-exact",
              _akw.get("caption", "") == _NEW_EXACT,
              f"caption={_akw.get('caption')!r}")
        check("F3 extracted audio has ONLY the retry button in ONE row (no Google)",
              _kb_texts(_akw.get("reply_markup")) == [_RETRY_FA]
              and len(_akw.get("reply_markup").inline_keyboard) == 1,
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        check("F3 no Google URL anywhere; retry reuses the video token",
              [u for u in _kb_urls(_akw.get("reply_markup")) if u] == []
              and f"vid:id:{token}" in _kb_callbacks(_akw.get("reply_markup")),
              f"urls={_kb_urls(_akw.get('reply_markup'))} "
              f"callbacks={_kb_callbacks(_akw.get('reply_markup'))}")
        check("F3 extracted audio has NO playlist/lyrics/wrong-song/heart",
              _PL_FA not in _kb_texts(_akw.get("reply_markup"))
              and _LYRICS_FA not in _kb_texts(_akw.get("reply_markup"))
              and "wrong:" not in "".join(_kb_callbacks(_akw.get("reply_markup")))
              and not any("❤️" in b for b in _kb_texts(_akw.get("reply_markup"))),
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        _vkb = msg.markup_edits[-1] if msg.markup_edits else None
        check("F3 video keyboard shows Google + YouTube + report (name exists)",
              _vkb is not None
              and _kb_rows(_vkb) == [[_VGOOGLE_FA, _VYT_FA], [_REPORT_FA]],
              f"rows={_kb_rows(_vkb) if _vkb is not None else None}")
        check("F3 temp status is deleted",
              msg.sent_messages and msg.sent_messages[0].deleted,
              f"deleted={[s.deleted for s in msg.sent_messages]}")
    except Exception as exc:
        check("F3 single-source tap", False, f"{type(exc).__name__}: {exc}")

    # ---- F4: exclusive bank -> same audio layout as scenario 2 -------
    try:
        msg = _FMsg()
        vm._attach_clip_video(msg)
        conn = vs._make_conn()
        song = {"id": 7, "title": "Bank Song", "artist": "Bank Artist",
                "ready_file_id": "BANKFID", "ready_bitrate_kbps": 320,
                "duration_sec": 180, "cover_file_id": None}
        kb, _key = h._identified_song_kb(msg, conn, "Bank Song",
                                         "Bank Artist", None, "bank", "fa")
        status = vu._StatusSent()
        ok = await h._send_exclusive_song(
            msg, song, status_msg=status, kb=kb,
            title="Bank Song", artist="Bank Artist", ulang="fa")
        _collect_answers(msg)
        check("F4 bank song sends as ONE final audio, never answer_voice",
              ok and len(msg.audio_calls) == 1 and msg.voice_calls == [],
              f"ok={ok} audio={msg.audio_calls} voice={msg.voice_calls}")
        _akw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("F4 bank audio has exactly [playlist, lyrics] in ONE row",
              _kb_texts(_akw.get("reply_markup")) == [_PL_FA, _LYRICS_FA]
              and len(_akw.get("reply_markup").inline_keyboard) == 1,
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        check("F4 bank caption has the bank marker + real duration",
              "💎" in _akw.get("caption", "")
              and "3:00" in _akw.get("caption", ""),
              f"caption={_akw.get('caption')!r}")
        check("F4 temp status is deleted once the bank audio is out",
              status.deleted, f"deleted={status.deleted}")
    except Exception as exc:
        check("F4 exclusive bank", False, f"{type(exc).__name__}: {exc}")

    # ---- F5: case (b) — name known, no full file ----------------------
    try:
        msg = vm._Msg()
        vm._attach_clip_video(msg)
        conn = vs._make_conn()
        status = vu._StatusSent()
        await vu._deliver_status(msg, conn, None, _TITLE, _ARTIST,
                                 ("video", "VID123"), status)
        _collect_answers(msg)
        check("F5 exactly ONE final message (one clip audio, no text)",
              len(msg.audio_calls) == 1
              and msg.audio_calls[0][0] == "bytes" and msg.answers == [],
              f"audio={msg.audio_calls} answers={msg.answers}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        check("F5 clip audio carries the case-(b) caption (byte-exact)",
              cap == _B_EXACT.format(title=_TITLE, artist=_ARTIST),
              f"caption={cap!r}")
        _btns = _kb_texts(msg.audio_kwargs[0].get("reply_markup")) if msg.audio_kwargs else []
        check("F5 case-(b) audio has [playlist, lyrics] in ONE row, no wrong-song",
              _btns == [_PL_FA, _LYRICS_FA]
              and len(msg.audio_kwargs[0].get("reply_markup").inline_keyboard) == 1
              and "wrong:" not in "".join(_kb_callbacks(msg.audio_kwargs[0].get("reply_markup"))),
              f"buttons={_btns}")
        check("F5 temp status is deleted",
              status.deleted, f"deleted={status.deleted}")
    except Exception as exc:
        check("F5 case (b)", False, f"{type(exc).__name__}: {exc}")

    # ---- F6: §6 — real duration + never sendVoice ----------------------
    try:
        src = Path(h.__file__).read_text(encoding="utf-8")
        _voice_sites = [ln for ln in src.splitlines() if "answer_voice" in ln]
        check("F6a the only answer_voice left in handlers.py is the TTS feature",
              len(_voice_sites) == 1,
              f"sites={_voice_sites}")
        msg = vm._Msg()
        vm._attach_clip_video(msg)
        conn = vs._make_conn()
        sent = await h._extract_and_send_clip_audio(
            msg, conn, vm._MP4, caption=_NEW_EXACT, reply_markup=None,
            caption_key="m_extracted_audio_new")
        _collect_answers(msg)
        kw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("F6b extracted audio is sent via answer_audio with a REAL duration",
              sent is not None and len(msg.audio_calls) == 1
              and isinstance(kw.get("duration"), (int, float))
              and kw["duration"] > 0,
              f"kwargs={kw}")
        check("F6c extracted audio title/performer are exact (never Unknown artist)",
              kw.get("title", "") == _EXTRACTED_TITLE
              and kw.get("performer", "") == "Avano",
              f"kwargs={kw}")
    except Exception as exc:
        check("F6 §6 appearance", False, f"{type(exc).__name__}: {exc}")

    # ---- F7: i18n — no middle line in any language --------------------
    try:
        check("F7 fa/en/ar extracted captions have NO middle line",
              "آماده شنیدن" not in TRANSLATIONS["fa"]["m_extracted_audio_new"]
              and "ready to listen" not in TRANSLATIONS["en"]["m_extracted_audio_new"].lower()
              and "جاهز للاستماع" not in TRANSLATIONS["ar"]["m_extracted_audio_new"],
              f"fa={TRANSLATIONS['fa']['m_extracted_audio_new']!r}")
        check("F7 fa retry text is exactly «🔍 جستجوی مجدد آهنگ»",
              TRANSLATIONS["fa"]["m_extracted_retry_btn"] == _RETRY_FA,
              f"value={TRANSLATIONS['fa']['m_extracted_retry_btn']!r}")
    except Exception as exc:
        check("F7 i18n caption", False, f"{type(exc).__name__}: {exc}")

    # ---- F8: aggregate — no «شناسایی شد» card text anywhere -----------
    try:
        _bad = [a for a in _ALL_TEXT if "شناسایی شد" in a]
        check("F8 no «شناسایی شد» text message in any driven flow",
              not _bad, f"offenders={_bad}")
    except Exception as exc:
        check("F8 aggregate", False, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
