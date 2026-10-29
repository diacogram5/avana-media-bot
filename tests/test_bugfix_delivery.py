"""Regression tests for the live-bug fixes (2026-10-29):

- BUG 1: identified songs must arrive as Telegram AUDIO (music player),
  never as a document. Root cause: a remote preview URL passed straight
  to answer_audio lets Telegram's servers fetch it, and some CDN URLs
  (Apple's mzaf_*.plus.aac.p.m4a) get classified as documents.
- BUG 2: an Instagram link whose media is blocked must produce ONE
  compact card, not three stacked messages.
- BUG 3: the «قابلیت‌ها» menu must expose the 8 web-parity items.
- BUG 4: full-version-first ordering — find_full_song_file (artist,
  title) runs before any preview fallback; a preview is never
  presented as the song.
"""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiogram.types import BufferedInputFile  # noqa: E402

from app.bot import audio_send  # noqa: E402
from app.bot import handlers as H  # noqa: E402
from app.bot.logic import capabilities_menu  # noqa: E402


class FakeMessage:
    """Minimal stand-in for aiogram Message, recording send calls."""

    def __init__(self, user_id=1):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id)
        self.calls = []

    async def answer(self, text, **kwargs):
        self.calls.append(("answer", text, kwargs))
        return SimpleNamespace(message_id=1)

    async def answer_audio(self, audio, **kwargs):
        self.calls.append(("answer_audio", audio, kwargs))
        return SimpleNamespace(message_id=2)

    async def answer_video(self, video, **kwargs):
        self.calls.append(("answer_video", video, kwargs))
        return SimpleNamespace(message_id=3)

    async def answer_voice(self, voice, **kwargs):
        self.calls.append(("answer_voice", voice, kwargs))
        return SimpleNamespace(message_id=4)


# ---------------------------------------------------------------- BUG 1


@pytest.mark.asyncio
async def test_send_song_audio_uploads_bytes_with_metadata():
    async def fake_download(url):
        return b"FAKEAUDIO" * 1000, "m4a"

    msg = FakeMessage()
    with patch.object(audio_send, "_download", side_effect=fake_download):
        sent = await audio_send.send_song_audio(
            msg, "https://audio-ssl.itunes.apple.com/mzaf_123.plus.aac.p.m4a",
            "Tardid", "Saman Jalili", caption="cap", duration=200)
    assert sent is not None
    assert len(msg.calls) == 1
    kind, audio, kwargs = msg.calls[0]
    assert kind == "answer_audio"
    # Must be an uploaded file with a clean name — never the raw CDN URL.
    assert isinstance(audio, BufferedInputFile)
    assert "mzaf_" not in audio.filename
    assert kwargs.get("title") == "Tardid"
    assert kwargs.get("performer") == "Saman Jalili"
    assert kwargs.get("duration") == 200
    # The unified pipeline appends the honest quality label to the caption.
    assert kwargs.get("caption", "").startswith("cap")
    assert "🔊" in kwargs.get("caption", "")


@pytest.mark.asyncio
async def test_send_song_audio_never_passes_remote_url_on_download_failure():
    # BUG 1 root cause: a remote URL passed straight to answer_audio is
    # what made Telegram classify songs as documents. On download
    # failure the pipeline returns None (honest) instead of the URL.
    msg = FakeMessage()
    with patch.object(audio_send, "_download", return_value=None):
        sent = await audio_send.send_song_audio(
            msg, "https://example.com/x.m4a", "T", "A")
    assert sent is None
    assert not [c for c in msg.calls if c[0] == "answer_audio"]


@pytest.mark.asyncio
async def test_send_song_bytes_is_audio_not_document():
    msg = FakeMessage()
    # Real MP3 magic bytes: the pipeline must detect and keep .mp3.
    mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"DATA" * 500
    sent = await audio_send.send_song_bytes(
        msg, mp3, "mp3", "Song", "Artist", duration=180)
    assert sent is not None
    kind, audio, kwargs = msg.calls[0]
    assert kind == "answer_audio"
    assert isinstance(audio, BufferedInputFile)
    assert audio.filename.endswith(".mp3")
    assert audio.filename == "Song - Artist.mp3"
    assert kwargs.get("title") == "Song"
    assert kwargs.get("performer") == "Artist"


# ---------------------------------------------------------------- BUG 4


def _patch_card(monkeypatch):
    """Neutralise network/DB-touching parts of _send_professional_song_card."""
    async def fake_smart_search(engine, query, limit=10):
        return [], None
    monkeypatch.setattr(H, "smart_search", fake_smart_search)
    monkeypatch.setattr(H, "match_identified_song", lambda results, t, a: None)
    fake_full = AsyncMock()
    monkeypatch.setattr(H, "find_full_song_file", fake_full)
    return fake_full


@pytest.mark.asyncio
async def test_fullsong_called_with_artist_title_order_and_sent_as_audio(db, monkeypatch):
    fake_full = _patch_card(monkeypatch)
    fake_full.return_value = {"data": b"FULL" * 10000, "ext": "mp3",
                              "duration_sec": 200.0, "source": "s"}
    msg = FakeMessage()
    await H._send_professional_song_card(msg, db, "Tardid", "Saman Jalili", bot_id="")
    # Argument order must be (artist, title) — the swapped call searched
    # the wrong query and could never find the full file.
    assert fake_full.await_args is not None
    args, _kwargs = fake_full.await_args
    assert args == ("Saman Jalili", "Tardid"), args
    kinds = [c[0] for c in msg.calls]
    assert "answer_audio" in kinds
    _kind, audio, kwargs = next(c for c in msg.calls if c[0] == "answer_audio")
    assert isinstance(audio, BufferedInputFile)
    assert kwargs.get("title") == "Tardid"
    assert kwargs.get("performer") == "Saman Jalili"
    # No preview-as-song: the caption must be the full-version label.
    assert "نسخه کامل" in (kwargs.get("caption") or "")


@pytest.mark.asyncio
async def test_no_full_no_media_honest_message_with_search_options(db, monkeypatch):
    fake_full = _patch_card(monkeypatch)
    fake_full.return_value = None
    msg = FakeMessage()
    await H._send_professional_song_card(msg, db, "Tardid", "Saman Jalili",
                                       bot_id="", bank_token="tok123",
                                       own_media=None)
    answers = [c for c in msg.calls if c[0] == "answer"]
    assert answers, "expected the honest not-found message"
    text = answers[0][1]
    assert "نسخه کامل" in text and "پیدا نشد" in text
    # A preview must never be presented as the song.
    assert not any(c[0] == "answer_audio" for c in msg.calls)
    kb = answers[0][2].get("reply_markup")
    assert kb is not None
    flat = [b.text for row in kb.inline_keyboard for b in row]
    assert any("مشابه" in t_ for t_ in flat)
    assert any("بانک هوش برتر" in t_ for t_ in flat)


@pytest.mark.asyncio
async def test_no_full_own_media_sent_as_labelled_replacement(db, monkeypatch):
    fake_full = _patch_card(monkeypatch)
    fake_full.return_value = None
    msg = FakeMessage()
    await H._send_professional_song_card(msg, db, "Tardid", "Saman Jalili",
                                       bot_id="", own_media=("audio", "fileABC"))
    audios = [c for c in msg.calls if c[0] == "answer_audio"]
    assert audios, "expected the user's own audio as replacement"
    _kind, audio, kwargs = audios[0]
    assert audio == "fileABC"
    assert "نسخه کامل" in (kwargs.get("caption") or "")


# ---------------------------------------------------------------- BUG 2


def test_needs_file_card_is_single_compact_message():
    outcome = SimpleNamespace(
        official_url="https://www.instagram.com/reel/DfCL3mZIQ2H/",
        kind="instagram")
    card = H._link_needs_file_card(outcome)
    assert isinstance(card, str)
    assert "🎬 ویدیوی همان لینک:" in card
    assert outcome.official_url in card
    assert "مستقیم بفرستید" in card
    # One message: no stacked repeats of the header.
    assert card.count("🎬 ویدیوی همان لینک:") == 1


def test_dash_audio_url_extraction():
    from app.services.link_media import extract_dash_audio_url
    # Manifest wrapped the way Instagram embeds it (escaped).
    page = ('<AdaptationSet id=\\"1\\" contentType=\\"audio\\">'
            '<BaseURL>https:\\\\/\\\\/scontent-mia5-1.cdninstagram.com\\\\/o1\\\\/v\\\\/t16\\\\/f2\\\\/m84\\\\/A.mp4?x=1&amp;y=2</BaseURL>')
    url = extract_dash_audio_url(page)
    assert url is not None
    assert url.startswith("https://scontent-mia5-1.cdninstagram.com/")
    assert url.endswith(".mp4?x=1&y=2")
    # Video-only adaptation sets are ignored.
    page2 = ('<AdaptationSet id=\\"2\\" contentType=\\"video\\">'
             '<BaseURL>https://scontent-x.cdninstagram.com/v.mp4</BaseURL>')
    assert extract_dash_audio_url(page2) is None
    assert extract_dash_audio_url("") is None
    assert extract_dash_audio_url("<html></html>") is None


@pytest.mark.asyncio
async def test_instagram_resolver_strategy_chain(monkeypatch):
    """Strategies tried in order; first hit wins; crawler DASH audio is
    the last resort. HTTP fully mocked — no live Instagram claimed."""
    from app.services import instagram as I

    dash_audio = ('<AdaptationSet id=\\"1\\" contentType=\\"audio\\">'
                  '<BaseURL>https://scontent-x.cdninstagram.com/a.mp4</BaseURL>')

    class FakeResp:
        def __init__(self, text):
            self.text = text
            self.status_code = 200

    def make_client(pages):
        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                ua = (headers or {}).get("User-Agent", "")
                if "Googlebot" in ua:
                    return FakeResp(pages["crawler"])
                if url.endswith("/embed/"):
                    return FakeResp(pages["embed"])
                if url.endswith("/embed/captioned/"):
                    return FakeResp(pages["captioned"])
                return FakeResp(pages["page"])
        return FakeClient

    # Case 1: nothing until the crawler DASH audio.
    pages = {"page": "<html><title>Instagram</title></html>",
             "embed": "<html></html>", "captioned": "<html></html>",
             "crawler": dash_audio}
    monkeypatch.setattr(I.httpx, "AsyncClient", make_client(pages))
    audio_url, video_url, _text, strategy = await I.resolve_instagram_media(
        "https://www.instagram.com/reel/ABC/?x=1")
    assert strategy == "crawler_dash"
    assert audio_url == "https://scontent-x.cdninstagram.com/a.mp4"
    assert video_url is None  # dash_audio fixture has no video AdaptationSet

    # Case 2: og:video on the main page wins immediately.
    pages["page"] = ('<meta property="og:video" '
                     'content="https://cdn.example.com/v.mp4">')
    audio_url, video_url, _text, strategy = await I.resolve_instagram_media(
        "https://www.instagram.com/reel/ABC/")
    assert strategy == "page"
    assert audio_url == video_url == "https://cdn.example.com/v.mp4"

    # Case 3: total miss -> honest "none".
    pages = {"page": "", "embed": "", "captioned": "", "crawler": ""}
    monkeypatch.setattr(I.httpx, "AsyncClient", make_client(pages))
    audio_url, video_url, _text, strategy = await I.resolve_instagram_media(
        "https://www.instagram.com/reel/ABC/")
    assert strategy == "none" and audio_url is None and video_url is None


# ---------------------------------------------------------------- BUG 3


def test_menu_has_all_ten_web_parity_items_in_order(db):
    titles = [f["title"] for f in capabilities_menu(db)]
    assert titles == [
        "جستجوی آهنگ و ویدیو",
        "درخواست آهنگ",
        "دانلودر هوشمند",
        " 🎵 حرفه ای",
        "متن آهنگ",
        "پاک‌سازی گفتگو",
        "تاریخچه جستجو",
        "راهنما و پشتیبانی",
        "شناسایی آهنگ از فایل",
        "دریافت از لینک",
    ]


def test_menu_callbacks_all_wired():
    mapping = {
        "song_search": "menu:search",
        "suggest": "suggest:start",
        "downloader": "dl:open",
        "professional": "pro:open",
        "lyrics": "menu:lyrics",
        "clear_chat": "adm:clear",
        "history": "menu:history",
        "help_support": "menu:help",
        "link_fetch": "menu:link",
        "audio_id": "menu:file",
    }
    for key, cb in mapping.items():
        assert H._menu_callback(key) == cb, key
