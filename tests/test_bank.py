"""Superior-AI bank: two-stage identification (app/features/bank.py).

All tests are offline: httpx and app.search.landmark are monkeypatched.
pack/unpack are exercised for real (pure functions, no network).
"""
import hashlib

import pytest
from aiogram import Router

from app.features import bank


def _ok_fp(landmarks):
    def _fp(data):
        return {"status": "ok", "landmarks": list(landmarks),
                "landmark_count": len(landmarks) // 2,
                "duration_sec": 10.0, "message": ""}
    return _fp


def _arm(db, monkeypatch, seed="seed1", landmarks=None):
    landmarks = list(range(200)) if landmarks is None else landmarks
    monkeypatch.setattr("app.search.landmark.fingerprint_bytes",
                        _ok_fp(landmarks))
    return bank.arm_sample("file", seed, b"fake-bytes", "", "clue text", db)


def _insert_bank_row(db, source="channel", channel="AiMusical",
                     landmarks=None, title="Test Song", artist="Test Artist"):
    landmarks = list(range(200)) if landmarks is None else landmarks
    chroma = bank._pack(landmarks)
    cur = db.execute(
        """INSERT INTO audio_bank
           (source, channel, title, artist, chroma, file_url, post_url, created_at)
           VALUES (?,?,?,?,?,?,?,1)""",
        (source, channel, title, artist, chroma,
         "https://example.com/f.mp3", "https://t.me/x/1"))
    db.commit()
    return cur.lastrowid


# --- arm_sample -----------------------------------------------------------
def test_arm_sample_stores_and_rearms(db, monkeypatch):
    t1 = _arm(db, monkeypatch)
    assert t1 == hashlib.sha256(b"seed1").hexdigest()[:40]
    row = bank.get_bank_sample(db, t1)
    assert row["state"] == "ready"
    first_chroma = row["chroma"]
    assert first_chroma
    # Re-arm with the same seed: fresh chroma, state back to ready.
    monkeypatch.setattr("app.search.landmark.fingerprint_bytes",
                        _ok_fp(list(range(300, 500))))
    t2 = bank.arm_sample("file", "seed1", b"other-bytes", "", "new clue", db)
    assert t2 == t1
    row = bank.get_bank_sample(db, t2)
    assert row["state"] == "ready"
    assert row["clue"] == "new clue"
    assert row["chroma"] != first_chroma


def test_arm_sample_rejects_bad_fingerprint(db, monkeypatch):
    monkeypatch.setattr("app.search.landmark.fingerprint_bytes",
                        lambda data: {"status": "no_audio", "landmarks": [],
                                      "landmark_count": 0, "duration_sec": 0.0,
                                      "message": "x"})
    assert bank.arm_sample("file", "s", b"data", "", "", db) is None
    # Oversized data with no url -> None.
    assert bank.arm_sample("file", "s", b"x" * (4 * 1024 * 1024), "", "", db) is None


def test_arm_sample_downloads_from_url(db, monkeypatch):
    monkeypatch.setattr(bank, "_download_bytes", lambda *a, **k: b"fake")
    monkeypatch.setattr("app.search.landmark.fingerprint_bytes",
                        _ok_fp(list(range(200))))
    token = bank.arm_sample("link", "urlseed", None, "https://example.com/a.mp3",
                            "clue", db)
    assert token == hashlib.sha256(b"urlseed").hexdigest()[:40]


def test_arm_sample_download_failure_returns_none(db, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("net down")
    monkeypatch.setattr(bank, "_download_bytes", _boom)
    assert bank.arm_sample("link", "s2", None, "https://example.com/a.mp3",
                           "", db) is None


# --- search_bank: stored outcomes -----------------------------------------
def _noop_indexers(monkeypatch):
    monkeypatch.setattr(bank, "index_channels",
                        lambda conn: {"added": 0, "total": 0, "blocked": []})
    monkeypatch.setattr(bank, "index_suno",
                        lambda conn, clue: {"added": 0})


def test_search_serves_stored_outcome_without_research(db, monkeypatch):
    token = _arm(db, monkeypatch)
    stored = {"status": "found", "message": bank.MSG_FOUND,
              "hits": [{"id": 7, "title": "T", "artist": "A", "source": "channel",
                        "channel": "", "post_url": "", "file_url": "u",
                        "score": 0.9, "pct": 90}], "bank_count": 1}
    bank.set_sample_state(db, token, "found", stored)

    def _boom(conn):
        raise AssertionError("must not re-search")
    monkeypatch.setattr(bank, "index_channels", _boom)
    monkeypatch.setattr(bank, "index_suno", _boom)
    out = bank.search_bank(token, db)
    assert out == stored


def test_search_in_progress_not_researched(db, monkeypatch):
    token = _arm(db, monkeypatch)
    bank.set_sample_state(db, token, "searching")
    def _boom(conn):
        raise AssertionError("must not re-search")
    monkeypatch.setattr(bank, "index_channels", _boom)
    out = bank.search_bank(token, db)
    assert out["status"] == "error"
    assert out["message"] == bank.MSG_IN_PROGRESS


def test_search_no_sample(db):
    out = bank.search_bank("deadbeef" * 5, db)
    assert out["status"] == "error"
    assert out["message"] == bank.MSG_NO_SAMPLE


# --- search_bank: empty bank ----------------------------------------------
def test_search_empty_bank_generic_error(db, monkeypatch):
    token = _arm(db, monkeypatch)
    _noop_indexers(monkeypatch)
    out = bank.search_bank(token, db)
    assert out["status"] == "error"
    assert out["message"] == "بانک در حال حاضر فایلی برای جست‌وجو ندارد"
    # Public message: no channel names, no @, ever.
    assert "@" not in out["message"]
    for ch, _label in bank.SUGGESTED_BANK_CHANNELS:
        assert ch.lower() not in out["message"].lower()
    # Outcome is stored as error.
    assert bank.get_bank_sample(db, token)["state"] == "error"


# --- search_bank: match / no match ----------------------------------------
def test_search_match_true_found_with_pct(db, monkeypatch):
    bank.add_bank_channel(db, "AiMusical")
    _insert_bank_row(db)
    token = _arm(db, monkeypatch)
    _noop_indexers(monkeypatch)
    monkeypatch.setattr("app.search.landmark.landmark_similarity",
                        lambda q, b: {"votes": 500, "ratio": 0.5,
                                      "match": True})
    out = bank.search_bank(token, db)
    assert out["status"] == "found"
    assert out["message"] == "آهنگ با تطبیق محتوای صوتی در بانک هوش برتر پیدا شد."
    assert out["hits"][0]["pct"] == 50
    assert out["hits"][0]["score"] == 0.5
    # Repeat click serves the stored outcome, never re-searches.
    monkeypatch.setattr("app.search.landmark.landmark_similarity",
                        lambda q, b: (_ for _ in ()).throw(
                            AssertionError("must not re-search")))
    out2 = bank.search_bank(token, db)
    assert out2 == out


def test_search_match_false_genuine_not_found(db, monkeypatch):
    bank.add_bank_channel(db, "AiMusical")
    _insert_bank_row(db, title="One")
    _insert_bank_row(db, title="Two")
    token = _arm(db, monkeypatch)
    _noop_indexers(monkeypatch)
    monkeypatch.setattr("app.search.landmark.landmark_similarity",
                        lambda q, b: {"votes": 10, "ratio": 0.01,
                                      "match": False})
    out = bank.search_bank(token, db)
    assert out["status"] == "not_found"
    assert out["message"] == (
        "در بانک هوش برتر (2 فایل ایندکس‌شده) با تطبیق محتوای صوتی پیدا نشد.")


def test_search_suno_source_included(db, monkeypatch):
    bank.add_bank_source(db, "site", "suno.com")
    _insert_bank_row(db, source="suno", channel="")
    token = _arm(db, monkeypatch)
    _noop_indexers(monkeypatch)
    monkeypatch.setattr("app.search.landmark.landmark_similarity",
                        lambda q, b: {"votes": 400, "ratio": 0.2,
                                      "match": True})
    out = bank.search_bank(token, db)
    assert out["status"] == "found"
    assert out["hits"][0]["source"] == "suno"


# --- source management ----------------------------------------------------
def test_channel_cap_and_duplicate(db):
    for i, ch in enumerate(["chan_a", "chan_b", "chan_c", "chan_d", "chan_e"]):
        res = bank.add_bank_channel(db, ch)
        assert res["ok"] is True
    dup = bank.add_bank_channel(db, "chan_a")
    assert dup["ok"] is False
    assert dup["message"] == "این چنل قبلاً در چنل‌های بانک ثبت شده است."
    sixth = bank.add_bank_channel(db, "chan_f")
    assert sixth["ok"] is False
    assert sixth["message"] == (
        "سقف ۵ چنل بانک پر است؛ اول یک چنل را از "
        "«تنظیمات ← چنل‌های تلگرام» حذف کنید.")


def test_bad_username_rejected(db):
    res = bank.add_bank_channel(db, "ab")
    assert res["ok"] is False
    assert res["message"] == "یوزرنیم چنل معتبر نیست."
    res = bank.add_bank_channel(db, "@AiMusical")
    assert res["ok"] is True
    assert bank.list_bank_sources(db)[0]["identifier"] == "AiMusical"


def test_disabled_source_excluded(db, monkeypatch):
    res = bank.add_bank_channel(db, "AiMusical")
    assert res["ok"] is True
    _insert_bank_row(db)
    assert bank.toggle_bank_source(db, res["id"]) == 0
    token = _arm(db, monkeypatch)
    _noop_indexers(monkeypatch)
    out = bank.search_bank(token, db)
    assert out["status"] == "error"
    assert out["message"] == "بانک در حال حاضر فایلی برای جست‌وجو ندارد"


def test_legacy_source_never_searched(db, monkeypatch):
    bank.add_bank_channel(db, "AiMusical")
    _insert_bank_row(db, source="legacy")
    _insert_bank_row(db, source="channel")
    token = _arm(db, monkeypatch)
    _noop_indexers(monkeypatch)
    monkeypatch.setattr("app.search.landmark.landmark_similarity",
                        lambda q, b: {"votes": 10, "ratio": 0.01,
                                      "match": False})
    out = bank.search_bank(token, db)
    assert out["bank_count"] == 1
    assert "(1 فایل ایندکس‌شده)" in out["message"]


def test_source_edit_delete_toggle_move(db):
    a = bank.add_bank_channel(db, "chan_a")["id"]
    b = bank.add_bank_channel(db, "chan_b")["id"]
    res = bank.update_bank_source(db, a, "chan_z")
    assert res["ok"] is True
    assert res["message"] == "منبع ویرایش شد."
    dup = bank.update_bank_source(db, b, "chan_z")
    assert dup["ok"] is False
    assert dup["message"] == "منبع دیگری با همین شناسه ثبت شده است."
    assert bank.move_bank_source(db, a, "down") is True
    order = [s["identifier"] for s in bank.list_bank_sources(db)]
    assert order == ["chan_b", "chan_z"]
    assert bank.toggle_bank_source(db, b) == 0
    del_res = bank.delete_bank_source(db, a)
    assert del_res["message"] == "منبع از بانک حذف شد."
    assert len(bank.list_bank_sources(db)) == 1


def test_delete_bank_item(db):
    item_id = _insert_bank_row(db)
    assert bank.bank_item_count(db) == 1
    assert bank.delete_bank_item(db, item_id) is True
    assert bank.bank_item_count(db) == 0
    assert bank.delete_bank_item(db, 99999) is False


def test_suggested_channels_count():
    assert len(bank.SUGGESTED_BANK_CHANNELS) == 7
    names = [c for c, _ in bank.SUGGESTED_BANK_CHANNELS]
    assert names == ["AiMusical", "ghadimy_ai", "radiojavan", "esssi_ai",
                     "PersianPopAi", "icyremix", "djsoroushmiix"]


def test_register_attaches_router():
    r = Router()
    bank.register(r)
    assert len(r.sub_routers) >= 1
