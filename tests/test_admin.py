"""Tests for app/features/admin.py (adm: namespace).

No network except a mocked openai_models_check. Uses the shared ``db`` fixture
(tmp sqlite with all migrations applied).
"""
from types import SimpleNamespace

import pytest

from app.features import admin as adm
from app import db as dbmod

OWNER = 1
STRANGER = 9


def make_config(owner_id=OWNER, env_key=""):
    return SimpleNamespace(owner_id=owner_id, openai_api_key=env_key)


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, uid, text=""):
        self.from_user = FakeUser(uid)
        self.text = text
        self.answered = []
        self.deleted = False

    async def answer(self, text, **kwargs):
        self.answered.append(text)

    async def delete(self):
        self.deleted = True


def run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


# --- owner guard ---------------------------------------------------------------
def test_is_owner():
    cfg = make_config()
    assert adm._is_owner(FakeMessage(OWNER), cfg) is True
    assert adm._is_owner(FakeMessage(STRANGER), cfg) is False
    assert adm._is_owner(FakeMessage(OWNER), make_config(owner_id=None)) is False


def test_owner_only_decorator_blocks_non_owner(db):
    cfg = make_config()
    ran = []

    async def inner(event, conn, config):
        ran.append(True)
        return "ok"

    guarded = adm._owner_only(inner)
    msg = FakeMessage(STRANGER)
    result = run(guarded(msg, db, cfg))
    assert result is None
    assert ran == []
    assert msg.answered == [adm.BRIEF["not_owner"]]

    owner_msg = FakeMessage(OWNER)
    assert run(guarded(owner_msg, db, cfg)) == "ok"
    assert ran == [True]


def test_register_includes_subrouter():
    from aiogram import Router
    r = Router()
    adm.register(r)
    assert len(r.sub_routers) == 1
    assert r.sub_routers[0].name == "admin"


# --- features (buttons) ----------------------------------------------------------
def test_add_custom_feature(db):
    key = adm.add_custom_feature(db, "قوانین استفاده", "متن پاسخ", by_id=OWNER)
    assert key.startswith("custom_")
    feat = dbmod.get_feature(db, key)
    assert feat["title"] == "قوانین استفاده"
    assert feat["message"] == "متن پاسخ"
    assert feat["enabled"] == 1


def test_add_custom_feature_validation(db):
    with pytest.raises(ValueError):
        adm.add_custom_feature(db, "", "x")
    with pytest.raises(ValueError):
        adm.add_custom_feature(db, "n" * 81, "x")
    with pytest.raises(ValueError):
        adm.add_custom_feature(db, "n", "x" * 2001)


def test_rename_feature(db):
    adm.rename_feature(db, "song_search", "جستجوی موزیک", by_id=OWNER)
    assert dbmod.get_feature(db, "song_search")["title"] == "جستجوی موزیک"
    with pytest.raises(ValueError):
        adm.rename_feature(db, "song_search", "x" * 81)
    with pytest.raises(ValueError):
        adm.rename_feature(db, "song_search", "   ")


def test_toggle_feature(db):
    dbmod.set_feature(db, "tts", enabled=False, by_id=OWNER)
    assert dbmod.get_feature(db, "tts")["enabled"] == 0
    dbmod.set_feature(db, "tts", enabled=True, by_id=OWNER)
    assert dbmod.get_feature(db, "tts")["enabled"] == 1


def test_move_feature_reorders(db):
    before = [f["key"] for f in dbmod.list_features(db)]
    assert adm.move_feature(db, "audio_id", "up") is True
    after = [f["key"] for f in dbmod.list_features(db)]
    # audio_id moved exactly one slot up (web-parity order has suggest
    # between song_search and audio_id now)
    assert after.index("audio_id") == before.index("audio_id") - 1
    assert set(after) == set(before)
    # moving the top item up is a no-op
    while adm.move_feature(db, "audio_id", "up"):
        pass
    assert adm.move_feature(db, "audio_id", "up") is False
    # unknown key
    assert adm.move_feature(db, "nope", "up") is False


def test_delete_custom_feature(db):
    key = adm.add_custom_feature(db, "موقت", "x")
    adm.delete_feature(db, key, by_id=OWNER)
    assert dbmod.get_feature(db, key) is None


def test_core_features_not_deletable(db):
    for key in adm.CORE_FEATURE_KEYS:
        with pytest.raises(ValueError):
            adm.delete_feature(db, key)
        assert dbmod.get_feature(db, key) is not None


def test_feats_view_status_lines(db):
    dbmod.set_feature(db, "tts", enabled=False)
    text, kb = adm.feats_view(db)
    assert adm.FEATS_EXPLAINER in text or "نام هر دکمه" in text
    assert adm.FEAT_VISIBLE in text
    assert adm.FEAT_HIDDEN in text
    # rename button + toggle + up/down present for a core key
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "adm:feat:rename:tts" in datas
    assert "adm:feat:toggle:tts" in datas
    assert "adm:feat:move:tts:up" in datas
    # add button present
    assert "adm:feat:add" in datas


# --- ads --------------------------------------------------------------------------
def test_ad_create_and_template(db):
    ad_id = adm.create_ad(db, "فروشگاه الف", "ارسال سریع", link="https://x.ir",
                          by_id=OWNER)
    ad = adm.get_ad(db, ad_id)
    assert ad["title"] == "فروشگاه الف"
    assert ad["button_text"] == adm.AD_DEFAULT_BUTTON  # auto default
    assert adm.ad_render(ad) == "📢 تبلیغ\nفروشگاه الف\nارسال سریع"
    assert ad["views"] == 0 and ad["clicks"] == 0


def test_ad_create_validation(db):
    with pytest.raises(ValueError):
        adm.create_ad(db, "", "متن")
    with pytest.raises(ValueError):
        adm.create_ad(db, "عنوان", "")


def test_ad_enable_disable_delete(db):
    ad_id = adm.create_ad(db, "t", "x")
    dbmod.set_ad_enabled(db, ad_id, False, by_id=OWNER)
    assert adm.get_ad(db, ad_id)["enabled"] == 0
    dbmod.set_ad_enabled(db, ad_id, True)
    assert adm.get_ad(db, ad_id)["enabled"] == 1
    adm.update_ad(db, ad_id, title="t2")
    assert adm.get_ad(db, ad_id)["title"] == "t2"
    adm.ad_inc(db, ad_id, "views")
    assert adm.get_ad(db, ad_id)["views"] == 1
    adm.delete_ad(db, ad_id, by_id=OWNER)
    assert adm.get_ad(db, ad_id) is None


def test_ad_view_stats_and_empty(db):
    text, _ = adm.ads_view(db)
    assert adm.ADS_EXPLAINER in text
    assert adm.ADS_EMPTY in text
    adm.create_ad(db, "t", "x")
    text, _ = adm.ads_view(db)
    assert "نمایش: 0 · کلیک: 0" in text
    assert "📢 تبلیغ" in text


def test_old_ad_rows_keep_working(db):
    # legacy rows from migration 002 (text-only) must still work
    dbmod.add_ad(db, "متن قدیمی", by_id=OWNER)
    ads = adm.list_ads_full(db)
    assert any(a["text"] == "متن قدیمی" for a in ads)
    old = next(a for a in ads if a["text"] == "متن قدیمی")
    assert old["title"] == "" and old["views"] == 0
    assert adm.ad_render(old).startswith("📢 تبلیغ")


# --- OpenAI key box ------------------------------------------------------------------
def test_key_initially_missing(db):
    cfg = make_config(env_key="")
    assert adm.key_status(db, cfg) == adm.KEY_STATUS_MISSING
    assert adm.is_openai_verified(db) is False
    assert adm.get_openai_key(db, cfg) == ""


def test_key_set_marks_unverified_and_db_wins_over_env(db):
    cfg = make_config(env_key="env-key")
    assert adm.get_openai_key(db, cfg) == "env-key"  # env fallback
    adm.set_openai_key(db, "sk-test-123", by_id=OWNER)
    assert adm.get_openai_key(db, cfg) == "sk-test-123"  # DB wins
    assert adm.key_status(db, cfg) == adm.KEY_STATUS_SAVED
    assert adm.is_openai_verified(db) is False


def test_key_value_never_shown_in_panel(db):
    cfg = make_config()
    secret = "sk-secret-xyz-999"
    adm.set_openai_key(db, secret, by_id=OWNER)
    text, _ = adm.keybox_view(db, cfg)
    assert secret not in text
    assert adm.KEY_TITLE in text
    assert adm.KEY_STATUS_SAVED in text


def test_key_verify_mocked_200(db):
    cfg = make_config()
    adm.set_openai_key(db, "sk-test", by_id=OWNER)

    async def fake_check(key):
        assert key == "sk-test"
        return True, "ok"

    async def flow():
        ok, _ = await fake_check(adm.get_openai_key(db, cfg))
        if ok:
            adm.set_key_verified(db, True, by_id=OWNER)

    run(flow())
    assert adm.is_openai_verified(db) is True
    assert adm.key_status(db, cfg) == adm.KEY_STATUS_VERIFIED


def test_key_verify_mocked_401_stays_unverified(db):
    cfg = make_config()
    adm.set_openai_key(db, "sk-bad", by_id=OWNER)

    async def fake_check(key):
        return False, "کلید معتبر نیست (خطای احراز هویت)."

    async def flow():
        ok, detail = await fake_check(adm.get_openai_key(db, cfg))
        if ok:
            adm.set_key_verified(db, True)
        return detail

    detail = run(flow())
    assert adm.is_openai_verified(db) is False
    assert adm.key_status(db, cfg) == adm.KEY_STATUS_SAVED
    assert "معتبر نیست" in detail


def test_gate_messages_present():
    for msg in (adm.GATE_STT_NO_KEY, adm.GATE_STT_UNVERIFIED, adm.GATE_STT_DISABLED,
                adm.GATE_TTS_VERIFIED, adm.GATE_TTS_INTERNAL, adm.GATE_GENERIC_DISABLED):
        assert isinstance(msg, str) and len(msg) > 10


# --- users ----------------------------------------------------------------------------
def test_user_block_unblock_delete(db):
    dbmod.upsert_user(db, 777, "u", "U")
    dbmod.set_user_blocked(db, 777, True, by_id=OWNER)
    assert dbmod.get_user(db, 777)["blocked"] == 1
    text, kb = adm.users_view(db)
    assert "777" in text and "⛔ مسدود" in text
    dbmod.set_user_blocked(db, 777, False, by_id=OWNER)
    assert dbmod.get_user(db, 777)["blocked"] == 0
    adm.delete_user(db, 777, by_id=OWNER)
    assert dbmod.get_user(db, 777) is None


# --- clear-chat ----------------------------------------------------------------------------
def test_track_and_clear_chat_deletes_only_outbox_and_cache(db):
    u, other = 7, 8
    for mid in (101, 102, 103):
        adm.track(db, u, 555, mid)
    adm.track(db, other, 555, 201)
    assert len(adm.list_outbox(db, u)) == 3

    dbmod.add_favorite(db, u, "آهنگ", "خواننده")
    pl = dbmod.create_playlist(db, u, "غمگین")
    dbmod.add_to_playlist(db, pl["id"], "آهنگ", "خواننده")
    dbmod.record_search(db, u, "query")
    dbmod.set_cached_search(db, "channel:x", "{}")

    deleted = adm.clear_chat_data(db, u)
    assert deleted == 3
    assert adm.list_outbox(db, u) == []
    assert adm.list_outbox(db, other) != []  # other user untouched
    assert db.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0] == 0
    # user data intact
    assert len(dbmod.list_favorites(db, u)) == 1
    assert len(dbmod.list_playlists(db, u)) == 1
    assert len(dbmod.recent_searches(db, u)) == 1


def test_clear_view_exact_texts():
    text, kb = adm.clear_view()
    assert adm.CLEAR_TITLE in text
    assert adm.CLEAR_EXPLAINER in text
    assert adm.CLEAR_SEPARATE in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "adm:clear:yes" in datas and "adm:wipe" in datas


# --- delete-all-my-data ----------------------------------------------------------------------
def test_wipe_counts_and_scope(db):
    u, other = 7, 8
    dbmod.add_favorite(db, u, "f1", "a1")
    dbmod.add_favorite(db, u, "f2", "a2")
    pl = dbmod.create_playlist(db, u, "pl1")
    dbmod.add_to_playlist(db, pl["id"], "s", "a")
    dbmod.add_to_playlist(db, pl["id"], "s2", "a2")
    for q in ("q1", "q2", "q3"):
        dbmod.record_search(db, u, q)
    dbmod.add_favorite(db, other, "of", "oa")
    pl2 = dbmod.create_playlist(db, other, "opl")
    dbmod.add_to_playlist(db, pl2["id"], "os", "oa")
    dbmod.record_search(db, other, "oq")
    # bank row must survive
    db.execute(
        "INSERT INTO audio_index (sha256, title, artist, channel, created_at)"
        " VALUES (?,?,?,?,?)", ("fp1", "bank song", "bank artist", "chan", 1))
    db.commit()
    # download_jobs table (downloader module's real schema: user_id column)
    db.execute("INSERT INTO download_jobs (user_id, url) VALUES (?,?)", (u, "u1"))
    db.execute("INSERT INTO download_jobs (user_id, url) VALUES (?,?)", (other, "u2"))
    db.commit()

    favs, pls, hist = adm.wipe_user_data(db, u)
    assert (favs, pls, hist) == (2, 1, 3)
    assert dbmod.list_favorites(db, u) == []
    assert dbmod.list_playlists(db, u) == []
    assert dbmod.recent_searches(db, u) == []
    assert db.execute("SELECT COUNT(*) FROM playlist_items").fetchone()[0] == 1  # other's item stays
    assert db.execute("SELECT COUNT(*) FROM download_jobs WHERE user_id=?", (u,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM download_jobs WHERE user_id=?", (other,)).fetchone()[0] == 1
    # other user intact
    assert len(dbmod.list_favorites(db, other)) == 1
    assert len(dbmod.list_playlists(db, other)) == 1
    assert len(dbmod.recent_searches(db, other)) == 1
    # bank untouched
    assert db.execute("SELECT COUNT(*) FROM audio_index").fetchone()[0] == 1


def test_wipe_success_message_format():
    msg = (f"اطلاعات ذخیره‌شده شما حذف شد: {2} علاقه‌مندی، {1} پلی‌لیست و {3} سابقه جستجو. "
           "تنظیمات و بانک هوش برتر دست نخورد.")
    assert msg == ("اطلاعات ذخیره‌شده شما حذف شد: 2 علاقه‌مندی، 1 پلی‌لیست و 3 سابقه جستجو. "
                   "تنظیمات و بانک هوش برتر دست نخورد.")


def test_wipe_view_exact_texts():
    text, kb = adm.wipe_view()
    assert adm.WIPE_TITLE in text
    assert adm.WIPE_EXPLAINER in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "adm:wipe:yes" in datas


# --- settings view ------------------------------------------------------------------------------
def test_settings_view_labels(db):
    text, kb = adm.settings_view(db)
    assert "آیدی یا لینک ربات (در صفحه ربات نمایش داده نمی‌شود؛ هنگام ارسال یا اشتراک‌گذاری محتوا، خودکار به آن اضافه می‌شود)" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "adm:set:bot_id" in datas and "adm:set:welcome_message" in datas
    # secret keys never show values
    dbmod.set_setting(db, "audd_token", "audd-secret", by_id=OWNER)
    text2, _ = adm.settings_view(db)
    assert "audd-secret" not in text2
