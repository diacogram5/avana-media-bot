"""Port-wiring tests: the integration layer added when porting the web
features into the Telegram bot (menu composition, version-aware
acceptance, full-version-first, bank arming, speech key gate, and the
visible/enabled feature decoupling). No network is used."""
import pytest

from app import db as dbmod
from app.bot import handlers, logic
from app.bot.logic import (
    can_use_feature, capabilities_menu, carries_version_tokens,
    identified_card_acceptable, identified_version_tokens,
    title_has_extra_tokens, token_overlap_ratio,
)
from app.config import Config


def test_menu_composition_web_order(db):
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


def test_feature_visible_enabled_decoupling(db):
    # Key-gated features are enabled (gate passes) but hidden from the
    # audience menu; link_fetch + audio_id are both visible AND
    # auto-triggering (web parity: 10 buttons).
    for key in ("tts", "voice_to_text", "audio_id", "link_fetch"):
        ok, _ = can_use_feature(db, 42, key)
        assert ok, key
    menu_keys = {f["key"] for f in capabilities_menu(db)}
    assert not ({"tts", "voice_to_text"} & menu_keys)
    assert {"audio_id", "link_fetch"} <= menu_keys
    # Disabling removes it from the menu too.
    dbmod.set_feature(db, "lyrics", enabled=False, by_id=1)
    assert "lyrics" not in {f["key"] for f in capabilities_menu(db)}


def test_version_tokens_preserved_not_stripped():
    vt = identified_version_tokens("Vay Az Dastet (Arash Mohseni Remix)")
    assert "remix" in vt and "arash" in vt
    assert identified_version_tokens("Dooset Daram") == []


def test_carries_version_tokens_required():
    assert carries_version_tokens("Shabe Mahtab (Live)", "Shabe Mahtab Live Version")
    assert not carries_version_tokens("Shabe Mahtab (Live)", "Shabe Mahtab")


def test_extra_title_tokens_rejected():
    assert title_has_extra_tokens("Dooset Daram", "Kheili Dooset Daram Ye Rooz")
    assert not title_has_extra_tokens("Dooset Daram", "Dooset Daram")


def test_persian_finglish_skeleton_meets():
    # The skeleton normalizes script variants: ي/ي and exact
    # transliterations meet; unrelated words never do.
    assert token_overlap_ratio("بابا", "Baba") >= 0.99
    assert token_overlap_ratio("علی", "علي") >= 0.99
    assert token_overlap_ratio("دوست دارم", "Something Else Entirely") < 0.99


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, routes):
        self.routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        for sub, resp in self.routes.items():
            if sub in str(url):
                return resp
        return _FakeResp(404)


def _fullsong_client(monkeypatch, routes):
    from app.search import fullsong
    monkeypatch.setattr(fullsong.httpx, "AsyncClient",
                        lambda *a, **k: _FakeClient(routes))
    monkeypatch.setattr(fullsong, "_fingerprint_duration", lambda data: 200.0)
    fullsong._CACHE.clear()
    return fullsong


def test_identified_card_acceptable_end_to_end():
    assert identified_card_acceptable(
        "Dooset Daram", "Mehdi Ahmadvand",
        "Dooset Daram", "Mehdi Ahmadvand")
    # Version phrase dropped -> rejected.
    assert not identified_card_acceptable(
        "Shabe Mahtab (Remix)", "X",
        "Shabe Mahtab", "X")
    # Extra words added -> rejected.
    assert not identified_card_acceptable(
        "Dooset Daram", "Mehdi Ahmadvand",
        "Dooset Daram Kheili", "Mehdi Ahmadvand")


@pytest.mark.asyncio
async def test_fullsong_finds_version_gated_file(monkeypatch):
    routes = {
        "wp/v2/search": _FakeResp(json_data=[
            {"id": 7, "title": "آهنگ Dooset Daram (DJ Remix) - Mehdi Ahmadvand"}]),
        "wp/v2/posts/7": _FakeResp(json_data={
            "content": {"rendered":
                        "<a href='https://cdn.example/Mehdi-Ahmadvand-Dooset-Daram-DJ-Remix.mp3'>دانلود</a>"}}),
        "cdn.example/": _FakeResp(content=b"ID3" + b"\x00" * 200_000),
    }
    fullsong = _fullsong_client(monkeypatch, routes)

    got = await fullsong.find_full_song_file("Mehdi Ahmadvand", "Dooset Daram (DJ Remix)")
    assert got is not None
    assert got["data"].startswith(b"ID3")
    assert got["ext"] == "mp3"
    assert got["duration_sec"] >= 85


@pytest.mark.asyncio
async def test_fullsong_rejects_wrong_version(monkeypatch):
    routes = {
        "wp/v2/search": _FakeResp(json_data=[
            {"id": 8, "title": "آهنگ Dooset Daram (Acoustic Cover) - Mehdi Ahmadvand"}]),
    }
    fullsong = _fullsong_client(monkeypatch, routes)
    assert await fullsong.find_full_song_file("A", "Dooset Daram (DJ Remix)") is None


@pytest.mark.asyncio
async def test_bank_arming_returns_deterministic_token(db, tmp_path, monkeypatch):
    from app.db import init_db

    def _fp(data):
        return {"status": "ok", "landmarks": list(range(200)),
                "landmark_count": 100, "duration_sec": 10.0, "message": ""}

    monkeypatch.setattr("app.search.landmark.fingerprint_bytes", _fp)
    bank_db = str(tmp_path / "bank.db")
    init_db(bank_db)
    config = Config()
    config.db_path = bank_db

    t1 = await handlers._arm_bank_sample(db, config, "file", "seed-1", b"fake-bytes", "", "")
    t2 = await handlers._arm_bank_sample(db, config, "file", "seed-1", b"fake-bytes", "", "")
    assert t1 and t1 == t2  # deterministic, re-arm reuses
    assert await handlers._arm_bank_sample(db, config, "file", "", b"fake-bytes", "", "") is None
    assert handlers._bank_button(None) is None
    kb = handlers._bank_button(t1)
    btn = kb.inline_keyboard[0][0]
    assert btn.text == "🔎 جست‌وجو در بانک هوش برتر"
    assert btn.callback_data == f"bank:search:{t1}"


def test_openai_ready_env_key_trusted(db):
    config = Config()
    config.openai_api_key = "sk-test"
    key, verified = handlers._openai_ready(db, config)
    assert (key, verified) == ("sk-test", True)


def test_openai_ready_db_key_needs_verification(db):
    config = Config()
    config.openai_api_key = ""
    dbmod.set_setting(db, "openai_api_key", "panel-key")
    key, verified = handlers._openai_ready(db, config)
    assert key == "panel-key" and verified is False
    dbmod.set_setting(db, "openai_key_verified", "1")
    key, verified = handlers._openai_ready(db, config)
    assert key == "panel-key" and verified is True


@pytest.mark.asyncio
async def test_speech_helpers_gate_unverified_key(monkeypatch, db):
    config = Config()
    config.openai_api_key = ""
    config.azure_speech_key = ""
    config.azure_speech_region = ""
    dbmod.set_setting(db, "openai_api_key", "panel-key")

    async def forbidden(*a, **k):
        raise AssertionError("no service call with unverified key")

    monkeypatch.setattr(handlers, "synthesize_openai", forbidden)
    monkeypatch.setattr(handlers, "transcribe_whisper", forbidden)
    assert await handlers._synthesize_voice("سلام", "female", config, db) == ("needs_verification", b"")
    assert await handlers._transcribe_voice(b"v", "v.ogg", config, db) == ("needs_verification", "")


def test_menu_callback_routes_to_feature_states(db):
    # The «قابلیت‌ها» menu buttons carry menu:<key> callbacks; each
    # menu key maps to a real feature row.
    keys = {f["key"] for f in dbmod.list_features(db)}
    for cb_key in ("menu:suggest", "menu:lyrics", "menu:downloader",
                   "menu:professional", "menu:history", "menu:clear_chat"):
        assert cb_key.split(":", 1)[1] in keys


def test_lyrics_translate_prefixes_do_not_collide():
    # lpg:/ltc: must not start with "ly:" (the lyrics-original handler).
    for cb in ("lpg:0:abc", "ltc:abc:fa"):
        assert not cb.startswith("ly:"), cb
