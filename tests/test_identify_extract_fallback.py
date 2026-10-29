"""ITEM 4 — identify+extract hardening (final round).

Guarantee under test: whenever identification runs on a reachable video
WITH an audio track but Shazam/AudD finds nothing (or the lookup errors),
the pipeline extracts and sends the video's own audio as a REAL,
validated, playable MP3 — never skipped just because no full-version MP3
exists in the sources; never a renamed non-audio file; never a guessed
song title.

Covered branches:
  * one-click «🎵 شناسایی موسیقی» (video_identify): not_found + lookup_error
  * upload auto-identify (media_handler): not_found + error/lookup_error
    (video), error/undecodable (no extraction possible)
  * link flow (_answer_link_identify_result): error / not_found with heard
    bytes, needs_file (no bytes -> no extraction)
  * identified-but-no-full-version card: step 2 now really sends the
    extracted audio (the old caption falsely claimed an audio file while
    re-sending the video)
  * output validation: magic bytes, real ffmpeg transcode, correct
    filename, real duration/size, honest messages when extraction fails

Real ffmpeg/ffprobe is used for the byte-level tests (available in this
environment); no network is used.
"""
import asyncio
import subprocess

import pytest
from aiogram.types import BufferedInputFile

from app import db as dbmod
from app.bot import handlers
from app.config import Config
from app.features import admin as adminfeat
from app.i18n import t
from app.services.audio_id import AudioIdResult


# --- synthetic media ------------------------------------------------------------

@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    vid = d / "clip.mp4"
    silent = d / "silent.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(vid)], check=True, timeout=60)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(silent)],
        check=True, timeout=60)
    return {"video": vid.read_bytes(), "silent": silent.read_bytes(),
            "garbage": b"\x00\x01\x02not-a-media-file" * 100}


# --- fakes -----------------------------------------------------------------------

class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = "u"
        self.first_name = "U"


class _SentMessage:
    _next = 1000

    def __init__(self):
        _SentMessage._next += 1
        self.message_id = _SentMessage._next


class _FakeMedia:
    def __init__(self, file_id):
        self.file_id = file_id


class _FakeBotFiles:
    def __init__(self, data=b""):
        self._data = data

    async def get_file(self, file_id):
        return type("F", (), {"file_path": "fake/path"})()

    async def download_file(self, path, buf):
        buf.write(self._data)


class _FakeMessage:
    def __init__(self, uid=11, data=b"", video=False, audio=False):
        self.from_user = _FakeUser(uid)
        self.chat = type("C", (), {"id": uid})()
        self.message_id = 7
        self.caption = ""
        self.video = _FakeMedia("vid1") if video else None
        self.audio = _FakeMedia("aud1") if audio else None
        self.voice = None
        self.document = None
        self._bot = _FakeBotFiles(data)
        self.answers = []
        self.audios = []
        self.videos = []

    @property
    def bot(self):
        return self._bot

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _SentMessage()

    async def answer_audio(self, audio, **kwargs):
        self.audios.append((audio, kwargs))
        return _SentMessage()

    async def answer_video(self, video, **kwargs):
        self.videos.append((video, kwargs))
        return _SentMessage()

    async def answer_voice(self, voice, **kwargs):
        return _SentMessage()


class _FakeCallback:
    def __init__(self, uid, data, bot=None):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid)
        self._bot = bot or _FakeBotFiles()
        self.answered = []

    @property
    def bot(self):
        return self._bot

    async def answer(self, text="", **kwargs):
        self.answered.append((text, kwargs))


class _FakeState:
    async def get_state(self):
        return None

    async def clear(self):
        pass


def _cfg():
    return Config(bot_token="<redacted>", owner_id=1, max_file_mb=50)


@pytest.fixture(autouse=True)
def _clean_registries():
    yield
    handlers._VIDEO_IDENTIFY.clear()
    handlers._VIDEO_IDENTIFY_RUNNING.clear()


def _is_mp3(data: bytes) -> bool:
    return (data[:3] == b"ID3"
            or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0))


# --- the extract helper: real bytes in, validated MP3 out -------------------------

def test_extract_video_with_audio_sends_real_mp3(db, media):
    msg = _FakeMessage()
    sent = asyncio.run(
        handlers._extract_and_send_clip_audio(msg, db, media["video"]))
    assert sent is not None, "audible video must yield an extracted MP3"
    assert len(msg.audios) == 1
    audio, kwargs = msg.audios[0]
    assert isinstance(audio, BufferedInputFile)
    assert audio.filename == "صدای استخراج‌شده از ویدیو.mp3", audio.filename
    assert kwargs.get("title") == "صدای استخراج‌شده از ویدیو"
    assert _is_mp3(audio.data), "output must be real MP3 bytes, not renamed"
    assert len(audio.data) > 1024
    dur = kwargs.get("duration")
    assert dur and 1 <= dur <= 4, f"real duration expected, got {dur!r}"
    # tracked for clear-chat
    assert adminfeat.list_outbox(db, 11), "extracted audio must be tracked"


def test_extract_silent_video_sends_nothing(db, media):
    msg = _FakeMessage()
    sent = asyncio.run(
        handlers._extract_and_send_clip_audio(msg, db, media["silent"]))
    assert sent is None
    assert msg.audios == []


def test_extract_garbage_sends_nothing(db, media):
    msg = _FakeMessage()
    sent = asyncio.run(
        handlers._extract_and_send_clip_audio(msg, db, media["garbage"]))
    assert sent is None, "non-audio bytes must never become a fake MP3"
    assert msg.audios == []


def test_extract_empty_sends_nothing(db):
    msg = _FakeMessage()
    assert asyncio.run(
        handlers._extract_and_send_clip_audio(msg, db, b"")) is None


# --- one-click button fallback: honest text both ways ------------------------------

def test_clip_fallback_honest_when_extraction_fails(db, media, monkeypatch):
    async def _arm(*a, **k):
        return "tok123"

    monkeypatch.setattr(handlers, "_arm_bank_sample", _arm)
    msg = _FakeMessage()
    asyncio.run(handlers._answer_identify_clip_fallback(
        msg, db, _cfg(), media["garbage"], "f1"))
    assert msg.audios == []
    text, kwargs = msg.answers[0]
    assert "آماده شد" not in text, f"must not claim delivery: {text}"
    assert "ممکن نشد" in text, text
    kb = kwargs.get("reply_markup")
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "bank:search:tok123" in datas


def test_clip_fallback_sends_mp3_and_bank_button(db, media, monkeypatch):
    async def _arm(*a, **k):
        return "tok123"

    monkeypatch.setattr(handlers, "_arm_bank_sample", _arm)
    msg = _FakeMessage()
    asyncio.run(handlers._answer_identify_clip_fallback(
        msg, db, _cfg(), media["video"], "f1"))
    assert len(msg.audios) == 1
    audio, _ = msg.audios[0]
    assert _is_mp3(audio.data)
    text, kwargs = msg.answers[0]
    assert "شناسایی نشد" in text and "آماده شد" in text, text
    kb2 = kwargs.get("reply_markup")
    datas = [b.callback_data for row in kb2.inline_keyboard for b in row]
    assert "bank:search:tok123" in datas


# --- video_identify: lookup_error still extracts ------------------------------------

def test_video_identify_lookup_error_extracts(db, media, monkeypatch):
    async def _dl(url, config):
        return media["video"]

    async def _id(data, filename, config, conn=None, **kwargs):
        return AudioIdResult(status="error", detail="lookup_error")

    async def _arm(*a, **k):
        return "tok9"

    extracted = {}

    async def _extract(message, conn, data, caption=None):
        extracted["n"] = extracted.get("n", 0) + 1
        extracted["data"] = data
        return _SentMessage()

    monkeypatch.setattr(handlers, "_download_link_audio", _dl)
    monkeypatch.setattr(handlers, "_identify_audio", _id)
    monkeypatch.setattr(handlers, "_arm_bank_sample", _arm)
    monkeypatch.setattr(handlers, "_extract_and_send_clip_audio", _extract)

    handlers._VIDEO_IDENTIFY["t_err"] = {
        "user_id": 11, "file_id": "f1", "kind": "link",
        "audio_url": "http://x/audio"}
    cb = _FakeCallback(11, "vid:id:t_err")
    asyncio.run(handlers.video_identify(cb, db, _cfg()))

    assert extracted.get("n") == 1, "lookup_error must still extract the audio"
    assert extracted["data"] == media["video"]
    text, kwargs = cb.message.answers[0]
    assert "خطا" in text, text
    assert "شناسایی نشد" not in text, "lookup error is not a not-found"
    labels = [b.text for row in kwargs["reply_markup"].inline_keyboard
              for b in row]
    assert any("تلاش مجدد" in l for l in labels), labels


# --- upload flow branches ------------------------------------------------------------

def _run_media(msg, db, cfg, result, monkeypatch):
    async def _id(data, filename, config, conn=None, **kwargs):
        return result

    async def _arm(*a, **k):
        return "tok"

    extracted = {}

    async def _extract(message, conn, data, caption=None):
        extracted["n"] = extracted.get("n", 0) + 1
        extracted["caption"] = caption
        return _SentMessage()

    monkeypatch.setattr(handlers, "_identify_audio", _id)
    monkeypatch.setattr(handlers, "_arm_bank_sample", _arm)
    monkeypatch.setattr(handlers, "_extract_and_send_clip_audio", _extract)
    asyncio.run(handlers.media_handler(msg, db, cfg, _FakeState()))
    return extracted


def test_upload_not_found_video_extracts_mp3(db, media, monkeypatch):
    msg = _FakeMessage(video=True, data=media["video"])
    extracted = _run_media(
        msg, db, _cfg(), AudioIdResult(status="not_found"), monkeypatch)
    assert extracted.get("n") == 1, "not_found on a video must extract audio"
    assert msg.videos == [], "the video must not be echoed back anymore"
    texts = [a[0] for a in msg.answers]
    assert any("پیدا نشد" in x for x in texts)
    assert "ساخته هوش مصنوعی" in (extracted.get("caption") or "")


def test_upload_error_lookup_video_extracts_mp3(db, media, monkeypatch):
    msg = _FakeMessage(video=True, data=media["video"])
    extracted = _run_media(
        msg, db, _cfg(),
        AudioIdResult(status="error", detail="lookup_error"), monkeypatch)
    assert extracted.get("n") == 1, "lookup_error on a video must extract audio"
    texts = [a[0] for a in msg.answers]
    assert any("خطا" in x for x in texts)


def test_upload_error_undecodable_extracts_nothing(db, media, monkeypatch):
    msg = _FakeMessage(video=True, data=media["garbage"])
    extracted = _run_media(
        msg, db, _cfg(),
        AudioIdResult(status="error", detail="undecodable"), monkeypatch)
    assert extracted.get("n", 0) == 0, \
        "undecodable bytes cannot be extracted; honest error only"
    texts = [a[0] for a in msg.answers]
    assert any("خطا" in x for x in texts)


# --- link flow branches -----------------------------------------------------------------

class _FakeOutcome:
    def __init__(self, kind="instagram"):
        self.kind = kind
        self.official_url = "https://www.instagram.com/reel/ABC123/"


class _FakeVideoMsg:
    def __init__(self, delivered=True):
        self.video = object() if delivered else None
        self.audio = None


def _run_link(db, cfg, link_res, monkeypatch, video_msg=None, page_text=""):
    async def _arm(*a, **k):
        return "tok"

    async def _card(*a, **k):
        _card.calls.append((a, k))

    async def _channel(*a, **k):
        _channel.calls.append(True)

    _card.calls = []
    _channel.calls = []
    extracted = {}

    async def _extract(message, conn, data, caption=None):
        extracted["n"] = extracted.get("n", 0) + 1
        return _SentMessage()

    monkeypatch.setattr(handlers, "_arm_bank_sample", _arm)
    monkeypatch.setattr(handlers, "_send_professional_song_card", _card)
    monkeypatch.setattr(handlers, "_run_link_channel_stage", _channel)
    monkeypatch.setattr(handlers, "_extract_and_send_clip_audio", _extract)
    msg = _FakeMessage()
    asyncio.run(handlers._answer_link_identify_result(
        msg, db, cfg, _FakeOutcome(), link_res, video_msg))
    return msg, extracted, _card.calls, _channel.calls


def test_link_not_found_with_bytes_extracts(db, media, monkeypatch):
    link_res = ("not_found", "", "", False, "a nice caption words here",
                media["video"])
    msg, extracted, cards, _ = _run_link(db, _cfg(), link_res, monkeypatch)
    assert extracted.get("n") == 1
    assert cards == []
    text, kwargs = msg.answers[0]
    assert text == t("link_stage_not_found", "fa")
    rows = kwargs["reply_markup"].inline_keyboard
    datas = [b.callback_data for row in rows for b in row]
    labels = [b.text for row in rows for b in row]
    assert any(d.startswith("similar:") for d in datas), \
        "«آهنگ‌های مشابه» shortcut must follow delivery"
    assert "bank:search:tok" in datas, "«هوش برتر» shortcut must follow delivery"
    assert any("مشابه" in l for l in labels)


def test_link_error_with_bytes_extracts(db, media, monkeypatch):
    link_res = ("error", "", "", False, "a nice caption words here",
                media["video"])
    msg, extracted, _, _ = _run_link(db, _cfg(), link_res, monkeypatch)
    assert extracted.get("n") == 1
    text, _ = msg.answers[0]
    assert text == t("link_identify_error", "fa")
    assert "شناسایی نشد" not in text, "lookup error is never 'not found'"


def test_link_not_found_without_bytes_extracts_nothing(db, monkeypatch):
    link_res = ("not_found", "", "", False, "", b"")
    msg, extracted, _, _ = _run_link(db, _cfg(), link_res, monkeypatch)
    assert extracted.get("n", 0) == 0
    text, kwargs = msg.answers[0]
    assert text == t("link_stage_not_found", "fa")
    # short/empty caption -> no similar-songs button, but bank stays
    datas = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard
             for b in row]
    assert not any(d.startswith("similar:") for d in datas)
    assert "bank:search:tok" in datas


def test_link_needs_file_video_delivered(db, monkeypatch):
    link_res = ("needs_file", "", "", True, "cap", b"", "empty_shell")
    msg, extracted, _, _ = _run_link(
        db, _cfg(), link_res, monkeypatch, video_msg=_FakeVideoMsg(True))
    assert extracted.get("n", 0) == 0, "no bytes -> nothing to extract"
    text, _ = msg.answers[0]
    assert text == t("link_audio_missing", "fa")


def test_link_identified_sends_song_card(db, monkeypatch):
    link_res = ("identified", "Song", "Artist", False, "cap", b"")
    msg, extracted, cards, _ = _run_link(db, _cfg(), link_res, monkeypatch)
    assert extracted.get("n", 0) == 0
    assert len(cards) == 1
    assert cards[0][0][2] == "Song" and cards[0][0][3] == "Artist"


# --- identified card step 2: the caption must be true -------------------------------------

def test_song_card_step2_video_really_sends_audio(db, media, monkeypatch):
    async def _smart(engine, query, limit=10):
        return [], None

    async def _full(artist, title):
        return None

    async def _arm(*a, **k):
        return None

    extracted = {}

    async def _extract(message, conn, data, caption=None):
        extracted["n"] = extracted.get("n", 0) + 1
        extracted["caption"] = caption
        extracted["data"] = data
        return _SentMessage()

    monkeypatch.setattr(handlers, "smart_search", _smart)
    monkeypatch.setattr(handlers, "find_full_song_file", _full)
    monkeypatch.setattr(handlers, "_arm_bank_sample", _arm)
    monkeypatch.setattr(handlers, "_extract_and_send_clip_audio", _extract)

    msg = _FakeMessage(data=media["video"])
    asyncio.run(handlers._send_professional_song_card(
        msg, db, "Title", "Artist", bot_id="b", bank_token=None,
        own_media=("video", "f1")))
    assert extracted.get("n") == 1
    assert extracted["data"] == media["video"]
    assert "به‌عنوان فایل صوتی ارسال شد" in (extracted.get("caption") or ""), \
        "the caption claims an audio file was sent — one must really be sent"
    assert msg.videos == []


def test_song_card_step2_extraction_impossible_honest_video(db, media, monkeypatch):
    async def _smart(engine, query, limit=10):
        return [], None

    async def _full(artist, title):
        return None

    async def _arm(*a, **k):
        return None

    async def _extract(message, conn, data, caption=None):
        return None

    monkeypatch.setattr(handlers, "smart_search", _smart)
    monkeypatch.setattr(handlers, "find_full_song_file", _full)
    monkeypatch.setattr(handlers, "_arm_bank_sample", _arm)
    monkeypatch.setattr(handlers, "_extract_and_send_clip_audio", _extract)

    msg = _FakeMessage(data=media["silent"])
    asyncio.run(handlers._send_professional_song_card(
        msg, db, "Title", "Artist", bot_id="b", bank_token=None,
        own_media=("video", "f1")))
    assert len(msg.videos) == 1
    _vid, kwargs = msg.videos[0]
    cap = kwargs.get("caption") or ""
    assert "به‌عنوان فایل صوتی ارسال شد" not in cap, \
        f"must not claim an audio file was sent: {cap}"
    assert "استخراج صدای ویدیو ممکن نشد" in cap, cap
