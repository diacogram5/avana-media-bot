"""Sandbox tests for the EXTRACTED-audio UI (user order 2026-11-09 16:11).

The order refines ONLY the extracted-audio files (unidentified video):
exactly ONE button «🔍 جستجوی مجدد آهنگ» under an extracted file —
the Google button is removed from extracted audio entirely, and the
retry reuses the video's EXISTING identify button (vid:id:<token>);
the supplementary re-identification logic is unchanged. The full-song
button structure is LOCKED by the user's 16:11 order and must not
move at all.

Card width (§2): the extracted send and the adapter full-song send go
through the SAME unified pipeline (send_music -> answer_audio, no
thumbnail, no parse_mode in either); the bank send is answer_audio
with a stored file_id, also no thumbnail for the screenshot song.
The Bot API exposes NO width parameter for audio messages — the
bubble width is client-determined. The one standard-API lever is the
keyboard: a single-button row renders full-width by Telegram's
layout, so the extracted card's attached row now spans the chat
width. No caption change (byte-exact, locked), no thumbnail (no real
cover exists for a clip extract), no quality change, no fake metadata.

Cases (each driven end-to-end through the real handlers):
  X1  _extracted_audio_kb unit: token -> exactly [[retry]] one row,
      callback vid:id:<token>, NO url; registered file_id reuses the
      existing token; no token and no file -> None; name but no token
      -> None (no Google fallback); empty name + token -> [[retry]];
      en/ar labels exact.
  X2  Unconfirmed video tap (single-source name): exactly ONE final
      audio, caption byte-exact, keyboard exactly [retry] in one row,
      NO Google URL anywhere, retry reuses the video token, no
      full-song buttons; video keyboard still [Google, YouTube] +
      [report] with exact URLs (locked, unchanged).
  X3  Not-found tap: extracted audio, caption byte-exact, keyboard
      exactly [retry]; video keyboard ends EMPTY.
  X4  Confirmed tap (full song, LOCKED): ONE final audio with exactly
      [playlist, lyrics] in one row, no Google/retry/heart/wrong-song;
      video keyboard [Google, YouTube] + [report] unchanged.
  X5  Case (b) (name known, no full file, LOCKED): clip audio with
      the case-(b) caption + [playlist, lyrics] in one row; no retry,
      no Google.
  X6  §6 still holds: the only answer_voice left in handlers.py is
      the TTS feature; sendVoice appears 0 times; the extracted send
      carries a REAL duration + the exact title + performer "Avano".
  X7  i18n: fa/en/ar retry labels exact; fa extracted caption
      byte-exact (no middle line); the Google key is no longer
      referenced anywhere in handlers.py (dead key, button deleted).

Reuses the fake aiogram / fake libsql / fake message harness from
scripts/verify_speedfix.py, the clip fixtures from
scripts/verify_fullsong_match.py, the video-token helpers from
scripts/verify_wrong_song.py, the status helpers from
scripts/verify_unify_result.py and the video-tap driver from
scripts/verify_identify_ui_final.py. No network, no Telegram, no
real Turso.

Run: python3 scripts/verify_extracted_ui.py ; exit 0 = all PASS.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_speedfix as vs  # noqa: E402  (installs fake aiogram, imports handlers)
import verify_fullsong_match as vm  # noqa: E402  (clip fixtures, _Msg)
import verify_wrong_song as vw  # noqa: E402  (video-token helpers)
import verify_unify_result as vu  # noqa: E402  (status helpers)
import verify_identify_ui_final as vif  # noqa: E402  (video-tap driver)

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
_RETRY_FA = "🔍 جستجوی مجدد آهنگ"
_RETRY_EN = "🔍 Search the song again"
_RETRY_AR = "🔍 البحث عن الأغنية مجددًا"
_EGOOGLE_FA = "🌐 جستجوی این صدا در گوگل"
_NEW_EXACT = vm._NEW_EXACT
_B_FA = vu._B_FA
_EXTRACTED_TITLE = "صدای استخراج‌شده از ویدیو"


def _kb_texts(kb) -> list:
    return vw._kb_texts(kb)


def _kb_callbacks(kb) -> list:
    return vw._kb_callbacks(kb)


def _kb_urls(kb) -> list:
    return vw._kb_urls(kb)


def _kb_rows(kb) -> list:
    return [[getattr(b, "text", "") or "" for b in row]
            for row in getattr(kb, "inline_keyboard", []) or []]


async def main():
    vm._MP4 = vm._make_mp4()
    h._VIDEO_IDENTIFY.clear()

    # ---- X1: _extracted_audio_kb unit -------------------------------
    try:
        kb = h._extracted_audio_kb(_TITLE, _ARTIST, "fa", token="tok123")
        check("X1 token gives exactly [[retry]] in ONE row",
              _kb_texts(kb) == [_RETRY_FA]
              and len(kb.inline_keyboard) == 1,
              f"buttons={_kb_texts(kb)}")
        check("X1 retry callback is vid:id:<token>",
              _kb_callbacks(kb) == ["vid:id:tok123"],
              f"callbacks={_kb_callbacks(kb)}")
        check("X1 extracted keyboard carries NO URL at all",
              [u for u in _kb_urls(kb) if u] == [], f"urls={_kb_urls(kb)}")
        h._VIDEO_IDENTIFY.clear()
        kb0 = h._video_identify_kb(572595158, "VID123", "file")
        _tok = kb0.inline_keyboard[0][0].callback_data.split("vid:id:", 1)[1]
        kb2 = h._extracted_audio_kb(_TITLE, _ARTIST, "fa", file_id="VID123",
                                    user_id=572595158, kind="file")
        check("X1 registered file_id reuses the video's existing token",
              _kb_texts(kb2) == [_RETRY_FA]
              and _kb_callbacks(kb2) == [f"vid:id:{_tok}"],
              f"buttons={_kb_texts(kb2)} callbacks={_kb_callbacks(kb2)}")
        check("X1 no token and no file gives NO keyboard",
              h._extracted_audio_kb(_TITLE, _ARTIST, "fa") is None,
              "kb is not None")
        check("X1 name without a token gives NO keyboard (no Google fallback)",
              h._extracted_audio_kb(_TITLE, _ARTIST, "fa") is None
              and h._extracted_audio_kb("", "", "fa") is None,
              "kb is not None")
        kb3 = h._extracted_audio_kb("", "", "fa", token="tok123")
        check("X1 empty name + token still gives only the retry button",
              _kb_texts(kb3) == [_RETRY_FA]
              and _kb_callbacks(kb3) == ["vid:id:tok123"],
              f"buttons={_kb_texts(kb3)}")
        check("X1 en/ar retry labels are exact",
              TRANSLATIONS["en"]["m_extracted_retry_btn"] == _RETRY_EN
              and TRANSLATIONS["ar"]["m_extracted_retry_btn"] == _RETRY_AR,
              f"en={TRANSLATIONS['en']['m_extracted_retry_btn']!r} "
              f"ar={TRANSLATIONS['ar']['m_extracted_retry_btn']!r}")
    except Exception as exc:
        check("X1 extracted keyboard unit", False,
              f"{type(exc).__name__}: {exc}")

    # ---- X2: unconfirmed tap -> extracted audio, ONE retry button ----
    try:
        msg, cb, token = await vif._run_video_identify(
            AudioIdResult(status="unconfirmed", title=_TITLE,
                          artist=_ARTIST, confirmed=False,
                          source="shazam"),
            None)
        check("X2 exactly ONE final audio via answer_audio, never answer_voice",
              len(msg.audio_calls) == 1 and msg.voice_calls == [],
              f"audio={msg.audio_calls} voice={msg.voice_calls}")
        _akw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("X2 extracted caption is byte-exact",
              _akw.get("caption", "") == _NEW_EXACT,
              f"caption={_akw.get('caption')!r}")
        check("X2 extracted audio has ONLY the retry button in ONE row",
              _kb_texts(_akw.get("reply_markup")) == [_RETRY_FA]
              and len(_akw.get("reply_markup").inline_keyboard) == 1,
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        check("X2 no Google button and NO url on extracted audio",
              _EGOOGLE_FA not in _kb_texts(_akw.get("reply_markup"))
              and [u for u in _kb_urls(_akw.get("reply_markup")) if u] == [],
              f"buttons={_kb_texts(_akw.get('reply_markup'))} "
              f"urls={_kb_urls(_akw.get('reply_markup'))}")
        check("X2 retry reuses the video token",
              f"vid:id:{token}" in _kb_callbacks(_akw.get("reply_markup")),
              f"callbacks={_kb_callbacks(_akw.get('reply_markup'))}")
        check("X2 extracted audio has NO playlist/lyrics/wrong-song/heart",
              _PL_FA not in _kb_texts(_akw.get("reply_markup"))
              and _LYRICS_FA not in _kb_texts(_akw.get("reply_markup"))
              and "wrong:" not in "".join(_kb_callbacks(_akw.get("reply_markup")))
              and not any("❤️" in b for b in _kb_texts(_akw.get("reply_markup"))),
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        _vkb = msg.markup_edits[-1] if msg.markup_edits else None
        check("X2 video keyboard is LOCKED: [Google, YouTube] + [report]",
              _vkb is not None
              and _kb_rows(_vkb) == [[_VGOOGLE_FA, _VYT_FA], [_REPORT_FA]],
              f"rows={_kb_rows(_vkb) if _vkb is not None else None}")
    except Exception as exc:
        check("X2 unconfirmed tap", False, f"{type(exc).__name__}: {exc}")

    # ---- X3: not-found tap -> extracted audio, ONE retry button ------
    try:
        msg, cb, token = await vif._run_video_identify(
            AudioIdResult(status="not_found"), None)
        check("X3 exactly ONE final audio (extracted), never answer_voice",
              len(msg.audio_calls) == 1 and msg.voice_calls == [],
              f"audio={msg.audio_calls} voice={msg.voice_calls}")
        _akw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("X3 extracted caption is byte-exact (no middle line)",
              _akw.get("caption", "") == _NEW_EXACT,
              f"caption={_akw.get('caption')!r}")
        check("X3 extracted audio has ONLY the retry button (no name, no Google)",
              _kb_texts(_akw.get("reply_markup")) == [_RETRY_FA]
              and f"vid:id:{token}" in _kb_callbacks(_akw.get("reply_markup")),
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        check("X3 video keyboard ends EMPTY when nothing was identified",
              msg.markup_edits
              and _kb_texts(msg.markup_edits[-1]) == [],
              f"edits={[_kb_texts(k) for k in msg.markup_edits]}")
    except Exception as exc:
        check("X3 not-found tap", False, f"{type(exc).__name__}: {exc}")

    # ---- X4: confirmed tap -> full song, LOCKED layout ---------------
    try:
        mp3 = vm._make_mp3(_TITLE, _ARTIST, seconds=5)
        msg, cb, token = await vif._run_video_identify(
            AudioIdResult(status="identified", title=_TITLE,
                          artist=_ARTIST, confirmed=True,
                          source="shazam+audd"),
            vm._full_dict(mp3, "persian_music:test", duration_sec=5.0))
        check("X4 exactly ONE final audio via answer_audio, never answer_voice",
              len(msg.audio_calls) == 1 and msg.audio_calls[0][0] in ("bytes", "str")
              and msg.voice_calls == [],
              f"audio={msg.audio_calls} voice={msg.voice_calls}")
        _akw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("X4 full song has exactly [playlist, lyrics] in ONE row (LOCKED)",
              _kb_texts(_akw.get("reply_markup")) == [_PL_FA, _LYRICS_FA]
              and len(_akw.get("reply_markup").inline_keyboard) == 1,
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        check("X4 full song has NO Google/retry/heart/wrong-song",
              _EGOOGLE_FA not in _kb_texts(_akw.get("reply_markup"))
              and _RETRY_FA not in _kb_texts(_akw.get("reply_markup"))
              and not any("❤️" in b for b in _kb_texts(_akw.get("reply_markup")))
              and "wrong:" not in "".join(_kb_callbacks(_akw.get("reply_markup"))),
              f"buttons={_kb_texts(_akw.get('reply_markup'))}")
        _vkb = msg.markup_edits[-1] if msg.markup_edits else None
        check("X4 video keyboard is LOCKED: [Google, YouTube] + [report]",
              _vkb is not None
              and _kb_rows(_vkb) == [[_VGOOGLE_FA, _VYT_FA], [_REPORT_FA]],
              f"rows={_kb_rows(_vkb) if _vkb is not None else None}")
    except Exception as exc:
        check("X4 confirmed tap", False, f"{type(exc).__name__}: {exc}")

    # ---- X5: case (b) — clip audio, case-(b) caption, LOCKED ---------
    try:
        h._VIDEO_IDENTIFY.clear()
        token = vu._mint_video_token()
        msg = vm._Msg()
        vm._attach_clip_video(msg)
        conn = vs._make_conn()
        mp3 = vm._make_mp3("Bu Üräyin Dərdi Sənsən", "Canım Xəstə", seconds=5)
        status = vu._StatusSent()
        await vu._deliver_status(
            msg, conn, vm._full_dict(mp3, "persian_music:musicdel",
                                     duration_sec=209.0),
            "ATM", "Tiem & Salii", ("video", "VID123"), status)
        check("X5 exactly ONE final message (one clip audio, no text)",
              len(msg.audio_calls) == 1
              and msg.audio_calls[0][0] == "bytes" and msg.answers == [],
              f"audio={msg.audio_calls} answers={msg.answers}")
        cap = msg.audio_kwargs[0].get("caption", "") if msg.audio_kwargs else ""
        _b = _B_FA.format(title="ATM", artist="Tiem & Salii")
        check("X5 clip audio carries the case-(b) caption (LOCKED)",
              cap == _b, f"caption={cap!r}")
        _btns = _kb_texts(msg.audio_kwargs[0].get("reply_markup")) if msg.audio_kwargs else []
        check("X5 case-(b) audio has [playlist, lyrics] one row, no retry, no Google",
              _btns == [_PL_FA, _LYRICS_FA]
              and len(msg.audio_kwargs[0].get("reply_markup").inline_keyboard) == 1
              and _RETRY_FA not in _btns and _EGOOGLE_FA not in _btns,
              f"buttons={_btns}")
        check("X5 temp status is deleted once the final audio is out",
              status.deleted, f"deleted={status.deleted}")
    except Exception as exc:
        check("X5 case (b)", False, f"{type(exc).__name__}: {exc}")

    # ---- X6: §6 still holds -------------------------------------------
    try:
        src = Path(h.__file__).read_text(encoding="utf-8")
        _voice_sites = [ln for ln in src.splitlines() if "answer_voice" in ln]
        check("X6 the only answer_voice left in handlers.py is the TTS feature",
              len(_voice_sites) == 1,
              f"sites={_voice_sites}")
        check("X6 sendVoice appears 0 times in handlers.py",
              sum(1 for ln in src.splitlines() if "sendVoice" in ln) == 0,
              "sendVoice found")
        msg = vm._Msg()
        vm._attach_clip_video(msg)
        conn = vs._make_conn()
        sent = await h._extract_and_send_clip_audio(
            msg, conn, vm._MP4, caption=_NEW_EXACT, reply_markup=None,
            caption_key="m_extracted_audio_new")
        kw = msg.audio_kwargs[0] if msg.audio_kwargs else {}
        check("X6 extracted audio is sent via answer_audio with a REAL duration",
              sent is not None and len(msg.audio_calls) == 1
              and isinstance(kw.get("duration"), (int, float))
              and kw["duration"] > 0,
              f"kwargs={kw}")
        check("X6 extracted audio title/performer are exact (never Unknown artist)",
              kw.get("title", "") == _EXTRACTED_TITLE
              and kw.get("performer", "") == "Avano",
              f"kwargs={kw}")
    except Exception as exc:
        check("X6 §6 appearance", False, f"{type(exc).__name__}: {exc}")

    # ---- X7: i18n -------------------------------------------------------
    try:
        check("X7 fa retry text is exactly «🔍 جستجوی مجدد آهنگ»",
              TRANSLATIONS["fa"]["m_extracted_retry_btn"] == _RETRY_FA,
              f"value={TRANSLATIONS['fa']['m_extracted_retry_btn']!r}")
        check("X7 en/ar retry labels are exact",
              TRANSLATIONS["en"]["m_extracted_retry_btn"] == _RETRY_EN
              and TRANSLATIONS["ar"]["m_extracted_retry_btn"] == _RETRY_AR,
              f"en={TRANSLATIONS['en']['m_extracted_retry_btn']!r} "
              f"ar={TRANSLATIONS['ar']['m_extracted_retry_btn']!r}")
        check("X7 fa extracted caption is byte-exact (no middle line)",
              TRANSLATIONS["fa"]["m_extracted_audio_new"] == _NEW_EXACT,
              f"caption={TRANSLATIONS['fa']['m_extracted_audio_new']!r}")
        src = Path(h.__file__).read_text(encoding="utf-8")
        check("X7 the Google key is NOT referenced in handlers.py (button deleted)",
              "m_extracted_google_btn" not in src,
              "m_extracted_google_btn still referenced in handlers.py")
    except Exception as exc:
        check("X7 i18n", False, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
