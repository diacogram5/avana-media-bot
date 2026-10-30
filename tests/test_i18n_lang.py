"""Item 7 — language selection FA/EN/AR.

- First /start for a fresh user shows the language picker (no welcome yet).
- Second /start (stored choice) skips the picker and shows the welcome/menu.
- All three languages render the main menu + admin views without missing keys.
- Language-change flow (lang:change -> lang:set:en) works.
- Song/artist names are never translated (Latin title under ar/fa UI).
- DB round-trip incl. migration 016.
"""
import asyncio

import pytest

from app import db as dbmod
from app.i18n import FA, EN, AR, t


# ---------------------------------------------------------------------------
# DB round-trip + migration 016
# ---------------------------------------------------------------------------
def test_language_db_roundtrip(db):
    uid = 4242
    assert dbmod.user_language(db, uid) is None
    assert dbmod.user_lang_or_default(db, uid) == "fa"
    dbmod.set_user_language(db, uid, "en")
    assert dbmod.user_language(db, uid) == "en"
    assert dbmod.user_lang_or_default(db, uid) == "en"
    dbmod.set_user_language(db, uid, "ar")
    assert dbmod.user_language(db, uid) == "ar"
    # invalid code is rejected
    with pytest.raises(ValueError):
        dbmod.set_user_language(db, uid, "xx")
    assert dbmod.user_language(db, uid) == "ar"


def test_migration_016_applied(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(user_language)").fetchall()]
    assert "telegram_id" in cols and "lang" in cols


# ---------------------------------------------------------------------------
# i18n catalog completeness
# ---------------------------------------------------------------------------
def test_key_counts():
    assert len(FA) > 550
    assert len(EN) == len(FA), f"en missing {len(FA) - len(EN)} keys"
    assert len(AR) == len(FA), f"ar missing {len(FA) - len(AR)} keys"


def test_fallback_for_missing_key():
    assert t("definitely_not_a_key_xyz", "en") == t("definitely_not_a_key_xyz", "fa")
    assert t("definitely_not_a_key_xyz", "ar") == t("definitely_not_a_key_xyz", "fa")


def test_no_empty_translations():
    for key in FA:
        assert EN[key].strip(), f"en empty for {key}"
        assert AR[key].strip(), f"ar empty for {key}"


# ---------------------------------------------------------------------------
# First /start -> picker; second /start -> welcome, no picker
# ---------------------------------------------------------------------------
class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = ""
        self.first_name = "Test"


class _FakeMessage:
    def __init__(self, uid):
        self.from_user = _FakeUser(uid)
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class _FakeCallback:
    def __init__(self, uid, data):
        self.from_user = _FakeUser(uid)
        self.data = data
        self.message = _FakeMessage(uid)
        self.answered = []

    async def answer(self, text=None, **kwargs):
        self.answered.append(text)


def _run(coro):
    return asyncio.run(coro)


def test_first_start_shows_picker(db):
    from app.bot import handlers as h

    msg = _FakeMessage(777001)
    _run(h.cmd_start(msg, db, _FakeConfig()))
    assert msg.answers, "first /start must answer something"
    text, kwargs = msg.answers[-1]
    kb = kwargs.get("reply_markup")
    assert kb is not None
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "lang:set:fa" in datas and "lang:set:en" in datas and "lang:set:ar" in datas
    # welcome text must NOT be shown yet
    assert t("lang_pick_title", "fa") in text


def test_second_start_skips_picker(db):
    from app.bot import handlers as h

    uid = 777002
    dbmod.set_user_language(db, uid, "en")
    msg = _FakeMessage(uid)
    _run(h.cmd_start(msg, db, _FakeConfig()))
    assert msg.answers
    text, kwargs = msg.answers[-1]
    kb = kwargs.get("reply_markup")
    # reply keyboard (main menu), not the inline picker
    assert "lang:set:fa" not in str(kwargs)
    assert "welcome" in text.lower() or "آوانو" in text or "Avano" in text


def test_lang_set_stores_and_welcomes(db):
    from app.bot import handlers as h

    uid = 777003
    cb = _FakeCallback(uid, "lang:set:ar")
    _run(h.lang_set(cb, db, _FakeConfig()))
    assert dbmod.user_language(db, uid) == "ar"
    assert cb.answered, "must confirm the pick"


def test_lang_set_invalid_rejected(db):
    from app.bot import handlers as h

    cb = _FakeCallback(777004, "lang:set:xx")
    _run(h.lang_set(cb, db, _FakeConfig()))
    assert dbmod.user_language(db, 777004) is None
    assert cb.answered


def test_lang_change_reopens_picker(db):
    from app.bot import handlers as h

    uid = 777005
    dbmod.set_user_language(db, uid, "en")
    cb = _FakeCallback(uid, "lang:change")
    _run(h.lang_change(cb, db, _FakeConfig()))
    assert cb.message.answers
    text, kwargs = cb.message.answers[0]
    kb = kwargs.get("reply_markup")
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "lang:set:fa" in datas


class _FakeConfig:
    bot_token = "<redacted>"
    owner_id = 1
    bot_id = "999"


# ---------------------------------------------------------------------------
# Menus render in all three languages without missing-key crashes
# ---------------------------------------------------------------------------
def test_main_menu_all_languages(db):
    from app.bot import handlers as h

    for lang, uid in (("fa", 778001), ("en", 778002), ("ar", 778003)):
        dbmod.set_user_language(db, uid, lang)
        kb = h._main_reply_kb(db, uid)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        assert len(texts) >= 10, f"{lang}: menu too short"
        # (toggle removed with ReplyKeyboard 2026-10-30)


def test_admin_views_all_languages(db):
    from app.features import admin as a

    class Cfg:
        pass

    cfg = Cfg()
    views = [
        lambda l: a._panel_view(db, cfg, l),
        lambda l: a.feats_view(db, l),
        lambda l: a.ads_view(db, l),
        lambda l: a.keybox_view(db, cfg, l),
        lambda l: a.users_view(db, l),
        lambda l: a.settings_view(db, l),
        lambda l: a.clear_view(l),
        lambda l: a.wipe_view(l),
        lambda l: a._watermark_view(db, l),
        lambda l: a.clear_msgs_view(3, l),
    ]
    for lang in ("fa", "en", "ar"):
        for v in views:
            text, kb = v(lang)
            assert text and kb is not None, lang


def test_downloader_bank_pro_render_all_languages(db):
    from app.features import downloader as dl
    from app.features import bank as bk

    for lang in ("fa", "en", "ar"):
        assert dl.download_error_text("generic", lang)
        assert dl.render_queue([], lang) is not None
        assert bk.bank_search_button("tok", lang) is not None
        text, kb = bk.render_bank_panel(db, None, lang)
        assert text


# ---------------------------------------------------------------------------
# Song/artist names are never translated
# ---------------------------------------------------------------------------
def test_song_names_never_translated():
    title = "Bohemian Rhapsody"
    artist = "Queen"
    for lang in ("fa", "en", "ar"):
        card = t("h_song_full_caption", lang).format(
            title=title, artist=artist, label="")
        assert title in card and artist in card
        # lyrics card keeps the original title verbatim
        head = t("h_lyrics_head", lang).format(title=title, artist_part=f" — {artist}")
        assert title in head and artist in head


def test_toggle_texts_all_languages():
    from app.bot import logic as L

    assert L.toggle_close_text("fa") == t("m_toggle_close", "fa")
    assert L.toggle_close_text("en") == t("m_toggle_close", "en")
    assert L.toggle_close_text("ar") == t("m_toggle_close", "ar")
    assert L.is_menu_toggle_close(t("m_toggle_close", "en"))
    assert L.is_menu_toggle_close(t("m_toggle_close", "ar"))
    assert L.is_menu_toggle(t("m_toggle_open", "en"))
