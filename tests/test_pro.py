"""Tests for app/features/pro.py — «🎵 حرفه‌ای» (favorites + playlists).
No network; uses the tmp-sqlite `db` fixture from tests/conftest.py.
"""
import re

from app import db as dbmod
from app.features import pro


def test_remember_get_result_roundtrip():
    key = pro.remember_result("دوست دارم", "مهدی احمدوند",
                              url="https://example.com/x", preview_url="https://example.com/p.mp3")
    assert re.fullmatch(r"[0-9a-f]{12}", key), key
    got = pro.get_result(key)
    assert got == {"title": "دوست دارم", "artist": "مهدی احمدوند",
                   "url": "https://example.com/x", "preview_url": "https://example.com/p.mp3"}
    assert pro.get_result("deadbeefdead") is None


def test_build_song_card_buttons_structure():
    rows = pro.build_song_card_buttons("a1b2c3d4e5f6", is_fav=False)
    assert len(rows) == 2
    # row1: single heart toggle
    assert len(rows[0]) == 1
    assert rows[0][0].text == "🤍"
    assert rows[0][0].callback_data == "fav:toggle:a1b2c3d4e5f6"
    # row2: lyrics + add-to-playlist
    assert len(rows[1]) == 2
    assert rows[1][0].text == "📝 متن آهنگ"
    assert rows[1][0].callback_data == "ly:a1b2c3d4e5f6"
    assert rows[1][1].text == "🎶 افزودن به پلی‌لیست"
    assert rows[1][1].callback_data == "pro:pick:a1b2c3d4e5f6"
    # heart flips when already favorite
    rows2 = pro.build_song_card_buttons("a1b2c3d4e5f6", is_fav=True)
    assert rows2[0][0].text == "❤️"
    assert rows2[0][0].callback_data == "fav:toggle:a1b2c3d4e5f6"


def test_playlist_crud(db):
    pl = dbmod.create_playlist(db, 111, "غمگین", "برای شب", "آبی")
    assert pl["name"] == "غمگین" and pl["color"] == "آبی"
    pls = dbmod.list_playlists(db, 111)
    assert len(pls) == 1 and pls[0]["id"] == pl["id"]
    # rename + recolor
    dbmod.rename_playlist(db, pl["id"], "غمگین‌تر", "توضیح تازه")
    pro.set_playlist_color(db, pl["id"], "بنفش")
    got = dbmod.get_playlist(db, pl["id"])
    assert got["name"] == "غمگین‌تر" and got["description"] == "توضیح تازه" and got["color"] == "بنفش"
    # delete removes playlist and its items
    dbmod.add_to_playlist(db, pl["id"], "آهنگ ۱", "خواننده ۱")
    dbmod.delete_playlist(db, pl["id"])
    assert dbmod.get_playlist(db, pl["id"]) is None
    assert dbmod.playlist_items(db, pl["id"]) == []


def test_playlist_items_add_remove_reorder(db):
    pl = dbmod.create_playlist(db, 111, "باشگاه")
    assert dbmod.add_to_playlist(db, pl["id"], "یک", "الف", preview_url="u1") is True
    assert dbmod.add_to_playlist(db, pl["id"], "دو", "ب", preview_url="u2") is True
    assert dbmod.add_to_playlist(db, pl["id"], "سه", "ج", preview_url="u3") is True
    # duplicate refused
    assert dbmod.add_to_playlist(db, pl["id"], "یک", "الف") is False
    items = dbmod.playlist_items(db, pl["id"])
    assert [i["title"] for i in items] == ["یک", "دو", "سه"]
    # reorder: move «سه» to the front
    dbmod.reorder_playlist(db, pl["id"], [items[2]["id"], items[0]["id"], items[1]["id"]])
    items = dbmod.playlist_items(db, pl["id"])
    assert [i["title"] for i in items] == ["سه", "یک", "دو"]
    # remove middle
    dbmod.remove_from_playlist(db, items[1]["id"])
    items = dbmod.playlist_items(db, pl["id"])
    assert [i["title"] for i in items] == ["سه", "دو"]


def test_favorites_add_remove_list(db):
    assert dbmod.add_favorite(db, 111, "دوست دارم", "مهدی احمدوند",
                              preview_url="https://example.com/p.mp3") is True
    # duplicate add returns False (INSERT OR IGNORE)
    assert dbmod.add_favorite(db, 111, "دوست دارم", "مهدی احمدوند") is False
    favs = dbmod.list_favorites(db, 111)
    assert len(favs) == 1
    assert favs[0]["preview_url"] == "https://example.com/p.mp3"
    dbmod.remove_favorite(db, favs[0]["id"])
    assert dbmod.list_favorites(db, 111) == []


def test_ownership_guard_user_b_cannot_touch_user_a(db):
    # user A owns a playlist + item + favorite
    pl = dbmod.create_playlist(db, 111, "خصوصی")
    dbmod.add_to_playlist(db, pl["id"], "یک", "الف")
    item = dbmod.playlist_items(db, pl["id"])[0]
    dbmod.add_favorite(db, 111, "دوست دارم", "مهدی احمدوند")
    fav = dbmod.list_favorites(db, 111)[0]

    # user B (222) cannot see/touch any of them
    assert dbmod.list_playlists(db, 222) == []
    assert dbmod.list_favorites(db, 222) == []
    assert pro._owns_playlist(db, pl["id"], 222) is None
    assert pro._owns_favorite(db, fav["id"], 222) is None
    assert pro._owns_item(db, item["id"], 222) is None
    assert pro._find_favorite(db, 222, "دوست دارم", "مهدی احمدوند") is None
    # user A still owns them
    assert pro._owns_playlist(db, pl["id"], 111) is not None
    assert pro._owns_favorite(db, fav["id"], 111) is not None
    assert pro._owns_item(db, item["id"], 111) is not None


def test_colors_and_spec_texts():
    assert pro.COLORS == ["نارنجی", "آبی", "سبز", "بنفش", "صورتی", "زرد", "فیروزه‌ای", "خاکستری"]
    assert pro.T_SECTION_TITLE == "🎵 حرفه‌ای — علاقه‌مندی‌ها"
    assert pro.T_FAV_EMPTY.startswith("هنوز آهنگی ذخیره نشده است.")
    assert "پلی‌لیست جدید" in pro.T_PL_EMPTY
    assert pro.T_PL_VIEW_EMPTY.startswith("هنوز آهنگی در این پلی‌لیست نیست؛")
    assert pro.T_PICK_TITLE.format(title="دوست دارم") == "افزودن «دوست دارم» به کدام پلی‌لیست؟"
    assert pro.T_NO_PLAYLISTS == "هنوز پلی‌لیستی ندارید؛ از «قابلیت‌ها ← 🎵 حرفه‌ای» اول یکی بسازید."
    assert pro.T_ADDED.format(title="ت", name="ن") == "«ت» به پلی‌لیست «ن» اضافه شد."
    assert pro.T_ALREADY.format(title="ت", name="ن") == "«ت» از قبل در پلی‌لیست «ن» هست."
    assert pro.T_FAV_ADD.format(title="ت") == "افزودن «ت» به علاقه‌مندی‌ها"
    assert pro.T_FAV_DEL.format(title="ت") == "حذف «ت» از علاقه‌مندی‌ها"


def test_pro_view_renders_favorites_and_playlists(db):
    dbmod.add_favorite(db, 111, "دوست دارم", "مهدی احمدوند")
    pl = dbmod.create_playlist(db, 111, "غمگین")
    dbmod.add_to_playlist(db, pl["id"], "یک", "الف")
    text, kb = pro._render_pro_view(db, 111)
    assert "🎵 حرفه‌ای — علاقه‌مندی‌ها" in text
    assert "پلی‌لیست‌های من" in text
    assert "دوست دارم – مهدی احمدوند" in text
    assert "غمگین — 1 آهنگ" in text
    # callbacks present
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(d.startswith("fav:play:") for d in datas)
    assert any(d.startswith("fav:del:") for d in datas)
    assert any(d == f"plv:{pl['id']}" for d in datas)
    assert "pro:new" in datas


def test_pro_view_empty_texts(db):
    text, kb = pro._render_pro_view(db, 999)
    assert pro.T_FAV_EMPTY in text
    assert pro.T_PL_EMPTY in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas == ["pro:new", "menu:main"]


def test_playlist_view_render(db):
    pl = dbmod.create_playlist(db, 111, "رانندگی", "برای جاده")
    text, kb = pro._render_playlist_view(db, pl)
    assert pro.T_PL_VIEW_EMPTY in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"pro:playall:{pl['id']}" in datas
    assert f"pro:edit:{pl['id']}" in datas
    assert f"pro:del:{pl['id']}" in datas
    assert "pro:open" in datas
    # with items, reorder/remove buttons appear
    dbmod.add_to_playlist(db, pl["id"], "یک", "الف")
    item = dbmod.playlist_items(db, pl["id"])[0]
    text2, kb2 = pro._render_playlist_view(db, pro._owns_playlist(db, pl["id"], 111))
    datas2 = [b.callback_data for row in kb2.inline_keyboard for b in row]
    assert f"pro:up:{item['id']}" in datas2
    assert f"pro:down:{item['id']}" in datas2
    assert f"pro:rm:{item['id']}" in datas2
    assert "1. یک – الف" in text2
