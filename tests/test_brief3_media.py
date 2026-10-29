"""Tests for brief #3 items 1+4 and the second brief's fixes:

- Suggest: 3-5 real suggestions, each with its own button; honest counts;
  more/retry/similar; no duplicates.
- Video quality picker: real multi-quality options only; chosen quality
  becomes the download URL.
- Persistent menu, menu escape, one-click copy honesty, ads, pro fixes.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import CopyTextButton, InlineKeyboardButton

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bot import handlers as H  # noqa: E402
from app.bot import media_send as ms  # noqa: E402
from app.bot.ads import ad_due, ad_every_n, pick_active_ad  # noqa: E402
from app.bot.logic import capabilities_menu, menu_escape, menu_title_key  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.features import downloader as dl  # noqa: E402
from app.features import pro as profeat  # noqa: E402
from app.search import SearchResult  # noqa: E402
from app.services.link_media import extract_video_qualities  # noqa: E402
from app.services.link_media import extract_dash_video_url  # noqa: E402


class _SentMessage:
    def __init__(self, message_id):
        self.message_id = message_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeMessage:
    def __init__(self, text="", user_id=42):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id)
        self.calls = []
        self.sent_messages = []
        self.video = None
        self.audio = None

    async def answer(self, text, **kwargs):
        sent = _SentMessage(len(self.calls) + 1)
        self.calls.append(("answer", text, kwargs))
        self.sent_messages.append(sent)
        return sent

    async def answer_audio(self, audio, **kwargs):
        self.calls.append(("answer_audio", audio, kwargs))
        return SimpleNamespace(message_id=2)

    async def answer_video(self, video, **kwargs):
        self.calls.append(("answer_video", video, kwargs))
        return SimpleNamespace(message_id=3)

    async def edit_text(self, text, **kwargs):
        self.calls.append(("edit_text", text, kwargs))

    async def edit_caption(self, caption=None, **kwargs):
        self.calls.append(("edit_caption", caption, kwargs))


class FakeState:
    def __init__(self):
        self.cleared = False
        self.state = None

    async def clear(self):
        self.cleared = True

    async def set_state(self, value):
        self.state = value

    async def get_state(self):
        return self.state


def _res(title, artist="Artist", duration=200):
    return SearchResult(source="itunes", id=title, title=title,
                        artist=artist, kind="music",
                        url=f"https://example.com/{title}",
                        preview_url=f"https://example.com/{title}.m4a",
                        duration=duration)


# ------------------------------------------------------------------ suggest

@pytest.mark.asyncio
async def test_suggest_each_result_gets_own_button(monkeypatch, db):
    async def fake_suggest(text, cat):
        return "غمگین", [_res("Song A", "A1"), _res("Song B", "B1"),
                         _res("Song C", "C1")]
    monkeypatch.setattr(H, "suggest_songs", fake_suggest)
    msg = FakeMessage("یه آهنگ غمگین")
    await H._run_suggest(msg, db, "یه آهنگ غمگین", SimpleNamespace(owner_id=1, bot_id=""))
    # ONE progress status, dismissed before the result card lands.
    assert len(msg.calls) == 2
    _k0, status_text, _kw0 = msg.calls[0]
    assert "در حال پیدا کردن" in status_text
    assert msg.sent_messages[0].deleted is True  # status dismissed
    assert msg.sent_messages[1].deleted is False  # result card stays
    _k, text, kwargs = msg.calls[1]
    assert "3 پیشنهاد" in text
    kb = kwargs["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert sum(d.startswith("sg:get:") for d in datas) == 3
    assert any(d.startswith("similar:") for d in datas)
    assert any(d == "sg:retry" for d in datas)
    # "more" only when there are more than 5.
    assert not any(d.startswith("sg:more:") for d in datas)


@pytest.mark.asyncio
async def test_suggest_dedupes_and_is_honest_about_count(monkeypatch, db):
    async def fake_suggest(text, cat):
        return "شاد", [_res("Same", "X"), _res("Same", "X"),
                       _res("Other", "Y")]
    monkeypatch.setattr(H, "suggest_songs", fake_suggest)
    msg = FakeMessage("شاد")
    await H._run_suggest(msg, db, "شاد", SimpleNamespace(owner_id=1, bot_id=""))
    _k, text, kwargs = msg.calls[1]
    assert "2 پیشنهاد" in text  # duplicate collapsed, honestly 2
    kb2 = kwargs["reply_markup"]
    datas = [b.callback_data for row in kb2.inline_keyboard for b in row]
    assert sum(d.startswith("sg:get:") for d in datas) == 2


@pytest.mark.asyncio
async def test_suggest_more_pagination(monkeypatch, db):
    async def fake_suggest(text, cat):
        return "mood", [_res(f"Song {i}") for i in range(7)]
    monkeypatch.setattr(H, "suggest_songs", fake_suggest)
    msg = FakeMessage("x")
    await H._run_suggest(msg, db, "x", SimpleNamespace(owner_id=1, bot_id=""))
    datas = [b.callback_data for row in msg.calls[1][2]["reply_markup"].inline_keyboard
             for b in row]
    more = next(d for d in datas if d.startswith("sg:more:"))
    tok = more.split("sg:more:", 1)[1]

    cb = SimpleNamespace(data=more, from_user=SimpleNamespace(id=42),
                         message=FakeMessage(), answer=AsyncMock())
    await H.suggest_more(cb, db, SimpleNamespace(owner_id=1, bot_id=""))
    _k, text, kwargs = cb.message.calls[0]
    assert "2 پیشنهاد بیشتر" in text
    datas2 = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard
              for b in row]
    assert sum(d.startswith("sg:get:") for d in datas2) == 2
    assert not any(d.startswith("sg:more:") for d in datas2)  # exhausted


@pytest.mark.asyncio
async def test_suggest_get_delivers_that_song(monkeypatch, db):
    key = profeat.remember_result("Picked", "Singer", "", "")
    called = {}

    async def fake_card(message, conn, title, artist, **kw):
        called["args"] = (title, artist)

    monkeypatch.setattr(H, "_send_professional_song_card", fake_card)
    cb = SimpleNamespace(data=f"sg:get:{key}",
                         from_user=SimpleNamespace(id=42),
                         message=FakeMessage(), answer=AsyncMock())
    await H.suggest_get(cb, db, SimpleNamespace(owner_id=1, bot_id=""))
    assert called["args"] == ("Picked", "Singer")


# ---------------------------------------------------------- quality picker

def test_extract_video_qualities_multi():
    html = ('<meta property="og:video" content="https://cdn.example.com/v_720p.mp4">'
            '<meta property="og:video:secure_url" content="https://cdn.example.com/v_1080p.mp4">'
            '<script>{"video_url":"https:\\/\\/cdn.example.com\\/v_480p.mp4"}</script>')
    qs = extract_video_qualities(html)
    urls = [q["url"] for q in qs]
    assert len(qs) == 3
    assert "https://cdn.example.com/v_1080p.mp4" in urls
    hints = {q["url"]: q["hint"] for q in qs}
    assert hints["https://cdn.example.com/v_1080p.mp4"] == "1080p"
    assert hints["https://cdn.example.com/v_720p.mp4"] == "720p"


def test_extract_video_qualities_single_or_none():
    assert extract_video_qualities("") == []
    html = '<meta property="og:video" content="https://cdn.example.com/v.mp4">'
    qs = extract_video_qualities(html)
    assert len(qs) == 1 and qs[0]["hint"] == ""
    # Non-video URLs are ignored.
    html2 = '<meta property="og:video" content="https://example.com/watch">'
    assert extract_video_qualities(html2) == []


def test_extract_dash_video_url_picks_best():
    mpd = ('<AdaptationSet contentType="video">'
           '<Representation bandwidth="800000"><BaseURL>https://cdn.example.com/lo.mp4</BaseURL></Representation>'
           '<Representation bandwidth="2500000"><BaseURL>https://cdn.example.com/hi.mp4</BaseURL></Representation>'
           '</AdaptationSet>')
    assert extract_dash_video_url(mpd) == "https://cdn.example.com/hi.mp4"
    assert extract_dash_video_url("<html></html>") is None


@pytest.mark.asyncio
async def test_detect_formats_multi_quality_picker(monkeypatch):
    html = ('<meta property="og:video" content="https://cdn.example.com/a_480p.mp4">'
            '<meta property="og:video:secure_url" content="https://cdn.example.com/a_1080p.mp4">')

    async def fake_fetch(url):
        return html

    async def fake_head(url):
        return None

    monkeypatch.setattr(dl, "_fetch_page", fake_fetch)
    monkeypatch.setattr(dl, "_head_media", fake_head)
    res = await dl.detect_formats("https://www.aparat.com/v/xyz", "aparat")
    assert not res.error
    ids = [f["id"] for f in res.formats]
    assert ids == ["q0", "q1", "qbest"]
    assert "1080p" in res.formats[0]["label"]  # best first
    assert res.formats[0]["url"] == "https://cdn.example.com/a_1080p.mp4"
    assert res.formats[2]["url"] == "https://cdn.example.com/a_1080p.mp4"


@pytest.mark.asyncio
async def test_detect_formats_single_quality_no_picker(monkeypatch):
    html = '<meta property="og:video" content="https://cdn.example.com/only.mp4">'

    async def fake_fetch(url):
        return html

    monkeypatch.setattr(dl, "_fetch_page", fake_fetch)
    res = await dl.detect_formats("https://www.aparat.com/v/xyz", "aparat")
    assert [f["id"] for f in res.formats] == ["file"]
    assert res.file_url == "https://cdn.example.com/only.mp4"


@pytest.mark.asyncio
async def test_dl_pick_format_uses_chosen_quality_url(db):
    # The exact download URL becomes the picked quality's own file URL.
    formats = [
        {"id": "q0", "label": "🎬 1080p", "url": "https://cdn.example.com/hi.mp4"},
        {"id": "q1", "label": "🎬 480p", "url": "https://cdn.example.com/lo.mp4"},
        {"id": "qbest", "label": "⭐ بالاترین کیفیت موجود",
         "url": "https://cdn.example.com/hi.mp4"},
    ]
    job_id = dl.create_job(db, 42, "https://www.aparat.com/v/xyz", "aparat",
                           "video", formats)
    assert job_id
    job = dl.pick_format(db, job_id, 42, "q1")
    assert job is not None
    assert job["chosen_format"] == "q1"
    assert job["result_url"] == "https://cdn.example.com/lo.mp4"
    # Invalid choices are rejected honestly.
    assert dl.pick_format(db, job_id, 42, "qX") is None
    assert dl.pick_format(db, job_id, 43, "q1") is None
    assert dl.pick_format(db, 999999, 42, "q1") is None


# --------------------------------------------------------------- menu/ads

def test_persistent_menu_has_ten_buttons(db):
    # OPEN (default) state: the 10 admin-managed feature buttons EXACTLY
    # as before (same count/order/columns) plus one small toggle row.
    kb = H._main_reply_kb(db)
    assert kb.is_persistent is True
    assert kb.resize_keyboard is True
    buttons = [b.text for row in kb.keyboard for b in row]
    assert len(buttons) == 11
    assert "دریافت از لینک" in buttons and "شناسایی آهنگ از فایل" in buttons
    assert buttons[-1] == "🔽 بستن منو"
    # The feature rows are untouched: pairs of two in menu order.
    feature_buttons = buttons[:-1]
    assert len(feature_buttons) == 10
    for row in kb.keyboard[:-1]:
        assert len(row) == 2
    # A fresh user (and unknown user_id) gets the open menu.
    assert not dbmod.menu_collapsed(db, 12345)


def test_bot_commands_minimal():
    # Regular users never see /admin in their Menu button; the owner gets
    # the same menu plus /admin, scoped to their chat only.
    user_cmds = [(c.command, c.description) for c in H._user_commands()]
    assert user_cmds == [("start", "شروع"), ("help", "راهنما")]
    owner_cmds = [(c.command, c.description) for c in H._owner_commands()]
    assert owner_cmds == [
        ("start", "شروع"), ("help", "راهنما"), ("admin", "پنل مدیریت")]


def test_menu_title_key_maps_all_visible(db):
    for f in capabilities_menu(db):
        assert menu_title_key(db, f["title"]) == f["key"]
    assert menu_title_key(db, "متن ناموجود") is None


@pytest.mark.asyncio
async def test_run_menu_title_performs_real_operations(db):
    config = SimpleNamespace(owner_id=999, bot_id="", openai_api_key="")
    msg = FakeMessage("دانلودر هوشمند")
    st = FakeState()
    await H._run_menu_title(msg, db, config, st, "downloader")
    assert st.state is not None  # DlStates.await_url
    assert "لینک" in msg.calls[0][1]

    msg2 = FakeMessage(" 🎵 حرفه ای")
    await H._run_menu_title(msg2, db, config, FakeState(), "professional")
    assert "حرفه" in msg2.calls[0][1]


@pytest.mark.asyncio
async def test_menu_escape_runs_title_not_flow(db):
    from app.bot import handlers as _H  # noqa

    @menu_escape
    async def fake_flow(message, conn, config, state):
        await message.answer("flow-consumed")

    msg = FakeMessage("متن آهنگ")
    st = FakeState()
    await fake_flow(msg, conn=db, config=SimpleNamespace(owner_id=1),
                    state=st)
    assert st.cleared
    assert msg.calls and msg.calls[0][1].startswith("📝")
    assert "flow-consumed" not in [c[1] for c in msg.calls]

    msg2 = FakeMessage("نام پلی‌لیست من")
    st2 = FakeState()
    await fake_flow(msg2, conn=db, config=SimpleNamespace(owner_id=1),
                    state=st2)
    assert not st2.cleared
    assert msg2.calls[0][1] == "flow-consumed"


@pytest.mark.asyncio
async def test_menu_main_resets_to_main_menu(db):
    config = SimpleNamespace(owner_id=1, bot_id="@TestBot")
    cb_msg = FakeMessage()
    cb = SimpleNamespace(data="menu:main", from_user=SimpleNamespace(id=42),
                         message=cb_msg, answer=AsyncMock())
    st = FakeState()
    st.state = "something"
    await H.menu_main(cb, db, config, st)
    assert st.cleared
    _k, text, kwargs = cb_msg.calls[0]
    assert "🤖 @TestBot" in text  # the one deliberate signature place
    buttons = [b.text for row in kwargs["reply_markup"].keyboard for b in row]
    assert len(buttons) == 11
    assert buttons[-1] == "🔽 بستن منو"


# ------------------------------------------------------------------ copy

def test_copy_button_short_text_is_real_one_tap_copy():
    btn = H._copy_button("📋 کپی", "short lyrics", "cp:abc")
    assert isinstance(btn.copy_text, CopyTextButton)
    assert btn.copy_text.text == "short lyrics"
    assert btn.callback_data is None


def test_copy_button_long_text_falls_back_honestly():
    btn = H._copy_button("📋 کپی", "x" * 500, "cp:abc")
    assert btn.copy_text is None
    assert btn.callback_data == "cp:abc"


@pytest.mark.asyncio
async def test_copy_callback_never_claims_fake_copy(db):
    H.LYRICS_CACHE["k1"] = ("Title", "Artist", "long lyrics " * 60)
    cb_msg = FakeMessage()
    cb = SimpleNamespace(data="cp:k1", from_user=SimpleNamespace(id=42),
                         message=cb_msg, answer=AsyncMock())
    with patch.object(H.dbmod, "get_bot_id", return_value=""):
        await H.copy_callback(cb, db, SimpleNamespace())
    # No "کپی شد" toast: answer() called with no text.
    cb.answer.assert_called_with()
    _k, text, kwargs = cb_msg.calls[0]
    assert "نگه دارید" in text  # honest long-press instruction
    assert kwargs.get("parse_mode") == "Markdown"


# ------------------------------------------------------------------- ads

def test_ad_schedule_every_nth(db):
    db.execute("INSERT INTO ads (title, text, enabled, created_at) VALUES (?,?,1,1)",
               ("T", "X"))
    db.commit()
    assert ad_every_n(db) == 5
    results = [ad_due(db, 42) for _ in range(5)]
    assert results == [False, False, False, False, True]
    assert ad_due(db, 42) is False  # 6th: not due


def test_ad_disabled_never_shown(db):
    assert pick_active_ad(db) is None
    db.execute("INSERT INTO ads (title, text, enabled, created_at) VALUES (?,?,0,1)",
               ("T", "X"))
    db.commit()
    assert pick_active_ad(db) is None


@pytest.mark.asyncio
async def test_ad_hook_never_to_owner(db):
    config = SimpleNamespace(owner_id=999)
    db.execute("INSERT INTO ads (title, text, enabled, created_at) VALUES (?,?,1,1)",
               ("T", "X"))
    db.commit()
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append(("message", chat_id, text))
            return SimpleNamespace(message_id=1)

        async def send_photo(self, chat_id, photo, **kwargs):
            sent.append(("photo", chat_id, photo))
            return SimpleNamespace(message_id=2)

    bot = FakeBot()
    H._install_ad_hook(bot, db, config)
    for _ in range(5):
        await bot.send_message(999, "owner answer")
    assert not [s for s in sent if s[0] in ("message", "photo") and "📢" in str(s[2])]
    for _ in range(5):
        await bot.send_message(42, "user answer")
    assert any("📢" in str(s[2]) for s in sent)


# ------------------------------------------------- pro dead-end + escape

@pytest.mark.asyncio
async def test_pro_edit_or_answer_falls_back():
    cb_msg = FakeMessage()

    async def boom(*a, **k):
        raise RuntimeError("message is not modified")

    cb_msg.edit_text = boom
    cb = SimpleNamespace(message=cb_msg,
                         from_user=SimpleNamespace(id=42))
    await profeat._edit_or_answer(cb, "hello", None)
    assert cb_msg.calls and cb_msg.calls[0][0] == "answer"


def test_link_detection_finds_url_anywhere():
    assert H._extract_link("ببین اینو https://www.instagram.com/reel/ABC123/ خوبه") == \
        "https://www.instagram.com/reel/ABC123/"
    assert H._extract_link("instagram.com/reel/ABC123") == \
        "https://instagram.com/reel/ABC123"
    assert H._extract_link("https://www.tiktok.com/@u/video/123") == \
        "https://www.tiktok.com/@u/video/123"
    assert H._extract_link("سلام چطوری") is None


def test_tiktok_domain_allowed_and_kind():
    from app.security import validate_link
    v = validate_link("https://www.tiktok.com/@u/video/123")
    assert v.ok and v.kind == "tiktok"
    assert dl.detect_source("https://www.tiktok.com/@u/video/123") == "tiktok"


def test_short_link_resolution(mock=None):
    from app.security import resolve_short_link
    import httpx as _httpx

    class FakeResp:
        url = "https://www.instagram.com/reel/REAL/"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def head(self, url, follow_redirects=True):
            return FakeResp()

    with patch.object(_httpx, "Client", FakeClient):
        out = resolve_short_link("https://bit.ly/abc123")
    assert out == "https://www.instagram.com/reel/REAL/"
    # Unknown hosts pass through untouched.
    assert resolve_short_link("https://www.instagram.com/reel/X/") == \
        "https://www.instagram.com/reel/X/"
