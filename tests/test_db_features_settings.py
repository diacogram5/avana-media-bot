from app import db as dbmod
from app.bot.logic import main_menu_labels


def test_migrations_apply_twice_idempotently(db, tmp_path):
    first = dbmod.apply_migrations(db)
    second = dbmod.apply_migrations(db)
    assert second == []  # nothing re-applied
    assert "001_initial" in dbmod.applied_versions(db)


def test_feature_toggle_hides_from_menu(db):
    assert dbmod.feature_enabled(db, "song_search")
    dbmod.set_feature(db, "song_search", enabled=False, by_id=1)
    assert not dbmod.feature_enabled(db, "song_search")
    labels = main_menu_labels(db)
    assert "🎵 جستجوی آهنگ" not in labels
    dbmod.set_feature(db, "song_search", enabled=True, by_id=1)
    assert "🎵 جستجوی آهنگ" in main_menu_labels(db)


def test_settings_persist_and_history(db):
    dbmod.set_setting(db, "bot_name", "ربات آزمایشی", by_id=7)
    assert dbmod.get_setting(db, "bot_name") == "ربات آزمایشی"
    # Simulate a restart: new connection to the same file.
    path = db.execute("PRAGMA database_list").fetchone()[2]
    conn2 = dbmod.connect(path)
    try:
        assert dbmod.get_setting(conn2, "bot_name") == "ربات آزمایشی"
    finally:
        conn2.close()
    rows = db.execute("SELECT * FROM settings_history WHERE key='bot_name'").fetchall()
    assert len(rows) >= 1


def test_reset_settings_section(db):
    dbmod.set_setting(db, "bot_name", "تغییر کرده", by_id=1)
    dbmod.reset_settings_section(db, ["bot_name"], by_id=1)
    assert dbmod.get_setting(db, "bot_name") == dbmod.DEFAULT_SETTINGS["bot_name"]


def test_user_registered(db):
    user = dbmod.upsert_user(db, 123, "ali", "Ali")
    assert user["telegram_id"] == 123
    assert dbmod.get_user(db, 123) is not None
