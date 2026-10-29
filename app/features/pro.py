# «🎵 حرفه ای» — merged user section: favorites + playlists.
#
# Owner: pro agent (see INTEGRATION_CONTRACT.md).
# The integrator wires song-card buttons to remember_result()/get_result()
# and build_song_card_buttons(); the `ly:` callbacks already exist in the
# main handlers.
import secrets
import sqlite3
from typing import Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..bot.logic import attach_bot_id

# ---------------------------------------------------------------------------
# Song-result store (integrator wires song-card buttons to this)
# ---------------------------------------------------------------------------

RESULTS: dict[str, dict] = {}


def remember_result(title: str, artist: str = "", url: str = "", preview_url: str = "") -> str:
    """Store a song-card result; return a short key (12 hex chars) for callbacks."""
    key = secrets.token_hex(6)
    RESULTS[key] = {"title": title, "artist": artist, "url": url, "preview_url": preview_url}
    return key


def get_result(key: str) -> dict | None:
    """Return the stored result for a key, or None if unknown/expired."""
    return RESULTS.get(key)


def build_song_card_buttons(key: str, is_fav: bool) -> list[list[InlineKeyboardButton]]:
    """Minimal song-card buttons: favorite heart, then lyrics + add-to-playlist."""
    heart = "❤️" if is_fav else "🤍"
    return [
        [InlineKeyboardButton(text=heart, callback_data=f"fav:toggle:{key}")],
        [
            InlineKeyboardButton(text="📝 متن آهنگ", callback_data=f"ly:{key}"),
            InlineKeyboardButton(text="🎶 افزودن به پلی‌لیست", callback_data=f"pro:pick:{key}"),
        ],
    ]


# ---------------------------------------------------------------------------
# Exact texts (from web-ui-spec.md section 10)
# ---------------------------------------------------------------------------

T_SECTION_TITLE = "🎵 حرفه ای — علاقه‌مندی‌ها"
T_PANEL_TITLE = "پلی‌لیست‌های من"
T_FAV_EMPTY = (
    "هنوز آهنگی ذخیره نشده است. کنار هر آهنگ در نتایج جستجو یا شناسایی، "
    "دکمه قلب را بزنید تا اینجا ذخیره شود و بعد بدون جستجوی دوباره پخش و دانلود کنید."
)
T_PL_EMPTY = (
    "هنوز پلی‌لیستی ندارید. با «پلی‌لیست جدید» یکی بسازید؛ "
    "بعد کنار هر آهنگ، دکمه «افزودن به پلی‌لیست» را بزنید تا داخلش قرار بگیرد."
)
T_PL_VIEW_EMPTY = "هنوز آهنگی در این پلی‌لیست نیست؛ کنار هر آهنگ در نتایج، «افزودن به پلی‌لیست» را بزنید."
T_NEW_NAME = "نام پلی‌لیست؛ مثلاً: غمگین، باشگاه، رانندگی"
T_NEW_DESC = "توضیح کوتاه (اختیاری)"
T_PICK_TITLE = "افزودن «{title}» به کدام پلی‌لیست؟"
T_NO_PLAYLISTS = "هنوز پلی‌لیستی ندارید؛ از «قابلیت‌ها ← 🎵 حرفه ای» اول یکی بسازید."
T_ADDED = "«{title}» به پلی‌لیست «{name}» اضافه شد."
T_ALREADY = "«{title}» از قبل در پلی‌لیست «{name}» هست."
T_FAV_ADD = "افزودن «{title}» به علاقه‌مندی‌ها"
T_FAV_DEL = "حذف «{title}» از علاقه‌مندی‌ها"

BRIEF = {
    "fav_save": "ذخیره نشد؛ دوباره تلاش کنید.",
    "playlist_add": "افزودن به پلی‌لیست انجام نشد؛ دوباره تلاش کنید.",
    "not_owner": "این مورد متعلق به شما نیست.",
    "generic": "انجام نشد؛ دوباره تلاش کنید.",
}

COLORS = ["نارنجی", "آبی", "سبز", "بنفش", "صورتی", "زرد", "فیروزه‌ای", "خاکستری"]

MAX_FAV_ROWS = 20
MAX_PL_ROWS = 20
MAX_ITEM_ROWS = 30


# ---------------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------------

class ProStates(StatesGroup):
    create_name = State(state="pro:create_name")
    create_desc = State(state="pro:create_desc")
    create_color = State(state="pro:create_color")
    edit_name = State(state="pro:edit_name")
    edit_desc = State(state="pro:edit_desc")


# ---------------------------------------------------------------------------
# Local DB helpers (contract: feature-module helpers taking conn)
# ---------------------------------------------------------------------------

def _owns_playlist(conn: sqlite3.Connection, playlist_id: int, telegram_id: int) -> dict | None:
    pl = dbmod.get_playlist(conn, playlist_id)
    if pl is not None and int(pl.get("telegram_id", -1)) == int(telegram_id):
        return pl
    return None


def _owns_favorite(conn: sqlite3.Connection, fav_id: int, telegram_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM favorites WHERE id=? AND telegram_id=?", (fav_id, telegram_id)
    ).fetchone()
    return dict(row) if row else None


def _find_favorite(conn: sqlite3.Connection, telegram_id: int, title: str, artist: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM favorites WHERE telegram_id=? AND title=? AND artist=?",
        (telegram_id, title, artist),
    ).fetchone()
    return dict(row) if row else None


def is_favorite(conn: sqlite3.Connection, telegram_id: int, title: str, artist: str) -> bool:
    """Public helper for the integrator: is this song already favorited?"""
    return _find_favorite(conn, telegram_id, title, artist) is not None


def _owns_item(conn: sqlite3.Connection, item_id: int, telegram_id: int) -> dict | None:
    row = conn.execute(
        "SELECT pi.*, p.telegram_id AS owner FROM playlist_items pi "
        "JOIN playlists p ON p.id=pi.playlist_id WHERE pi.id=?", (item_id,)
    ).fetchone()
    if row and int(row["owner"]) == int(telegram_id):
        return dict(row)
    return None


def set_playlist_color(conn: sqlite3.Connection, playlist_id: int, color: str) -> None:
    conn.execute("UPDATE playlists SET color=? WHERE id=?", (color, playlist_id))
    conn.commit()


def _title_line(title: str, artist: str) -> str:
    title = (title or "").strip()
    artist = (artist or "").strip()
    return f"{title} – {artist}" if artist else title


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="لغو", callback_data="pro:cancel")],
    ])


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _render_pro_view(conn: sqlite3.Connection, telegram_id: int):
    """Return (text, keyboard) for the merged «🎵 حرفه ای» section."""
    favs = dbmod.list_favorites(conn, telegram_id)[:MAX_FAV_ROWS]
    pls = dbmod.list_playlists(conn, telegram_id)[:MAX_PL_ROWS]

    lines = [T_SECTION_TITLE, ""]
    kb: list[list[InlineKeyboardButton]] = []

    if favs:
        for f in favs:
            lines.append("♥ " + _title_line(f["title"], f["artist"]))
            kb.append([
                InlineKeyboardButton(text="▶️", callback_data=f"fav:play:{f['id']}"),
                InlineKeyboardButton(text="❌", callback_data=f"fav:del:{f['id']}"),
            ])
    else:
        lines.append(T_FAV_EMPTY)

    lines += ["", T_PANEL_TITLE, ""]
    if pls:
        for pl in pls:
            n = len(dbmod.playlist_items(conn, pl["id"]))
            lines.append(f"▫️ {pl['name']} — {n} آهنگ")
            kb.append([InlineKeyboardButton(
                text=f"📂 {pl['name']} — {n} آهنگ", callback_data=f"plv:{pl['id']}")])
    else:
        lines.append(T_PL_EMPTY)

    kb.append([InlineKeyboardButton(text="➕ پلی‌لیست جدید", callback_data="pro:new")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


def _render_playlist_view(conn: sqlite3.Connection, pl: dict):
    """Return (text, keyboard) for one playlist."""
    items = dbmod.playlist_items(conn, pl["id"])[:MAX_ITEM_ROWS]
    lines = [f"📂 {pl['name']} — {len(items)} آهنگ"]
    if pl.get("description"):
        lines.append(pl["description"])

    kb: list[list[InlineKeyboardButton]] = []
    if items:
        lines.append("")
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {_title_line(it['title'], it['artist'])}")
            kb.append([
                InlineKeyboardButton(text="⬆️", callback_data=f"pro:up:{it['id']}"),
                InlineKeyboardButton(text="⬇️", callback_data=f"pro:down:{it['id']}"),
                InlineKeyboardButton(text="❌", callback_data=f"pro:rm:{it['id']}"),
            ])
    else:
        lines += ["", T_PL_VIEW_EMPTY]

    kb.append([
        InlineKeyboardButton(text="▶️ پخش پشت سر هم", callback_data=f"pro:playall:{pl['id']}"),
    ])
    kb.append([
        InlineKeyboardButton(text="✏️ ویرایش", callback_data=f"pro:edit:{pl['id']}"),
        InlineKeyboardButton(text="🗑 حذف", callback_data=f"pro:del:{pl['id']}"),
    ])
    kb.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="pro:open")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


async def _edit_or_answer(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def register(router: Router) -> None:
    r = Router()

    # --- favorites ---------------------------------------------------------
    @r.callback_query(F.data == "pro:open")
    async def pro_open(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        text, kb = _render_pro_view(conn, callback.from_user.id)
        await _edit_or_answer(callback, text, kb)
        await callback.answer()

    @r.callback_query(F.data.startswith("fav:toggle:"))
    async def fav_toggle(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        key = callback.data.split("fav:toggle:", 1)[1]
        res = get_result(key)
        if not res:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        uid = callback.from_user.id
        title, artist = res["title"], res.get("artist", "")
        try:
            existing = _find_favorite(conn, uid, title, artist)
            if existing:
                dbmod.remove_favorite(conn, existing["id"])
                is_fav = False
                msg = T_FAV_DEL.format(title=title)
            else:
                ok = dbmod.add_favorite(
                    conn, uid, title, artist, res.get("url", ""), res.get("preview_url", ""))
                if not ok:
                    await callback.answer(BRIEF["fav_save"], show_alert=True)
                    return
                is_fav = True
                msg = T_FAV_ADD.format(title=title)
        except Exception:
            await callback.answer(BRIEF["fav_save"], show_alert=True)
            return
        await callback.answer(msg)
        try:
            await callback.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=build_song_card_buttons(key, is_fav)))
        except Exception:
            pass

    @r.callback_query(F.data.startswith("fav:play:"))
    async def fav_play(callback: CallbackQuery, conn: sqlite3.Connection,
                      config) -> None:
        try:
            fav_id = int(callback.data.split("fav:play:", 1)[1])
        except ValueError:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        fav = _owns_favorite(conn, fav_id, callback.from_user.id)
        if not fav:
            await callback.answer(BRIEF["not_owner"], show_alert=True)
            return
        audio = (fav.get("preview_url") or fav.get("url") or "").strip()
        if not audio:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        bot_id = dbmod.get_bot_id(conn, config)
        try:
            await callback.message.answer_audio(
                audio=audio, caption=attach_bot_id(_title_line(fav["title"], fav["artist"]), bot_id))
        except Exception:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        await callback.answer()

    @r.callback_query(F.data.startswith("fav:del:"))
    async def fav_del(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        try:
            fav_id = int(callback.data.split("fav:del:", 1)[1])
        except ValueError:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        fav = _owns_favorite(conn, fav_id, callback.from_user.id)
        if not fav:
            await callback.answer(BRIEF["not_owner"], show_alert=True)
            return
        dbmod.remove_favorite(conn, fav_id)
        await callback.answer(T_FAV_DEL.format(title=fav["title"]))
        text, kb = _render_pro_view(conn, callback.from_user.id)
        await _edit_or_answer(callback, text, kb)

    # --- create playlist ---------------------------------------------------
    @r.callback_query(F.data == "pro:new")
    async def pro_new(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ProStates.create_name)
        await _edit_or_answer(callback, T_NEW_NAME, _cancel_kb())
        await callback.answer()

    @r.callback_query(F.data == "pro:cancel")
    async def pro_cancel(callback: CallbackQuery, state: FSMContext,
                        conn: sqlite3.Connection) -> None:
        await state.clear()
        text, kb = _render_pro_view(conn, callback.from_user.id)
        await _edit_or_answer(callback, text, kb)
        await callback.answer()

    @r.message(ProStates.create_name)
    async def pro_got_name(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if not name:
            await message.answer(BRIEF["generic"])
            return
        await state.update_data(name=name)
        await state.set_state(ProStates.create_desc)
        skip_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="رد کردن", callback_data="pro:skipdesc")],
            [InlineKeyboardButton(text="لغو", callback_data="pro:cancel")],
        ])
        await message.answer(T_NEW_DESC, reply_markup=skip_kb)

    @r.callback_query(ProStates.create_desc, F.data == "pro:skipdesc")
    async def pro_skip_desc(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(desc="")
        await _ask_color(callback, state)

    @r.message(ProStates.create_desc)
    async def pro_got_desc(message: Message, state: FSMContext) -> None:
        await state.update_data(desc=(message.text or "").strip())
        await state.set_state(ProStates.create_color)
        rows = [[InlineKeyboardButton(text=c, callback_data=f"pro:color:{c}")]
                for c in COLORS]
        rows.append([InlineKeyboardButton(text="بدون رنگ", callback_data="pro:color:")])
        rows.append([InlineKeyboardButton(text="لغو", callback_data="pro:cancel")])
        await message.answer("🎨 رنگ پلی‌لیست (اختیاری):",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def _ask_color(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ProStates.create_color)
        rows = [[InlineKeyboardButton(text=c, callback_data=f"pro:color:{c}")]
                for c in COLORS]
        rows.append([InlineKeyboardButton(text="بدون رنگ", callback_data="pro:color:")])
        rows.append([InlineKeyboardButton(text="لغو", callback_data="pro:cancel")])
        await _edit_or_answer(callback, "🎨 رنگ پلی‌لیست (اختیاری):",
                              InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @r.callback_query(ProStates.create_color, F.data.startswith("pro:color:"))
    async def pro_got_color(callback: CallbackQuery, state: FSMContext,
                            conn: sqlite3.Connection) -> None:
        color = callback.data.split("pro:color:", 1)[1]
        if color not in COLORS:
            color = ""
        data = await state.get_data()
        await state.clear()
        try:
            pl = dbmod.create_playlist(
                conn, callback.from_user.id, data.get("name", ""),
                data.get("desc", ""), color)
        except Exception:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        text = f"✅ پلی‌لیست «{pl['name']}» ساخته شد."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📂 مشاهده", callback_data=f"plv:{pl['id']}")],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="pro:open")],
        ])
        await _edit_or_answer(callback, text, kb)
        await callback.answer()

    # --- playlist view ------------------------------------------------------
    @r.callback_query(F.data.startswith("plv:"))
    async def pl_view(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        try:
            pid = int(callback.data.split("plv:", 1)[1])
        except ValueError:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        pl = _owns_playlist(conn, pid, callback.from_user.id)
        if not pl:
            await callback.answer(BRIEF["not_owner"], show_alert=True)
            return
        text, kb = _render_playlist_view(conn, pl)
        await _edit_or_answer(callback, text, kb)
        await callback.answer()

    @r.callback_query(F.data.startswith("pro:rm:"))
    async def pl_remove_item(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        try:
            item_id = int(callback.data.split("pro:rm:", 1)[1])
        except ValueError:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        item = _owns_item(conn, item_id, callback.from_user.id)
        if not item:
            await callback.answer(BRIEF["not_owner"], show_alert=True)
            return
        dbmod.remove_from_playlist(conn, item_id)
        pl = _owns_playlist(conn, item["playlist_id"], callback.from_user.id)
        await callback.answer("حذف شد.")
        if pl:
            text, kb = _render_playlist_view(conn, pl)
            await _edit_or_answer(callback, text, kb)

    @r.callback_query(F.data.startswith("pro:up:"))
    async def pl_move_up(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        await _move_item(callback, conn, -1)

    @r.callback_query(F.data.startswith("pro:down:"))
    async def pl_move_down(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        await _move_item(callback, conn, +1)

    async def _move_item(callback: CallbackQuery, conn: sqlite3.Connection, delta: int) -> None:
        try:
            item_id = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        item = _owns_item(conn, item_id, callback.from_user.id)
        if not item:
            await callback.answer(BRIEF["not_owner"], show_alert=True)
            return
        items = dbmod.playlist_items(conn, item["playlist_id"])
        idx = next((i for i, it in enumerate(items) if it["id"] == item_id), None)
        if idx is None:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        j = idx + delta
        if not (0 <= j < len(items)):
            await callback.answer()
            return
        items[idx], items[j] = items[j], items[idx]
        dbmod.reorder_playlist(conn, item["playlist_id"], [it["id"] for it in items])
        pl = _owns_playlist(conn, item["playlist_id"], callback.from_user.id)
        if pl:
            text, kb = _render_playlist_view(conn, pl)
            await _edit_or_answer(callback, text, kb)
        await callback.answer()

    @r.callback_query(F.data.startswith("pro:playall:"))
    async def pl_play_all(callback: CallbackQuery, conn: sqlite3.Connection,
                         config) -> None:
        try:
            pid = int(callback.data.split("pro:playall:", 1)[1])
        except ValueError:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        pl = _owns_playlist(conn, pid, callback.from_user.id)
        if not pl:
            await callback.answer(BRIEF["not_owner"], show_alert=True)
            return
        items = dbmod.playlist_items(conn, pid)
        if not items:
            await callback.answer(T_PL_VIEW_EMPTY, show_alert=True)
            return
        bot_id = dbmod.get_bot_id(conn, config)
        await callback.answer("در حال پخش…")
        for it in items:
            audio = (it.get("preview_url") or it.get("url") or "").strip()
            if not audio:
                continue
            try:
                await callback.message.answer_audio(
                    audio=audio,
                    caption=attach_bot_id(_title_line(it["title"], it["artist"]), bot_id))
            except Exception:
                break

    # --- edit playlist ------------------------------------------------------
    @r.callback_query(F.data.startswith("pro:edit:"))
    async def pl_edit_menu(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        try:
            pid = int(callback.data.split("pro:edit:", 1)[1])
        except ValueError:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        pl = _owns_playlist(conn, pid, callback.from_user.id)
        if not pl:
            await callback.answer(BRIEF["not_owner"], show_alert=True)
            return
        text = (f"✏️ ویرایش «{pl['name']}»\n"
                f"نام: {pl['name']}\n"
                f"توضیح: {pl['description'] or '—'}\n"
                f"رنگ: {pl['color'] or '—'}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="ویرایش نام", callback_data=f"pro:editname:{pid}")],
            [InlineKeyboardButton(text="ویرایش توضیح", callback_data=f"pro:editdesc:{pid}")],
            [InlineKeyboardButton(text="تغییر رنگ", callback_data=f"pro:editcolor:{pid}")],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"plv:{pid}")],
        ])
        await _edit_or_answer(callback, text, kb)
        await callback.answer()

    @r.callback_query(F.data.startswith("pro:editname:"))
    async def pl_edit_name_start(callback: CallbackQuery, state: FSMContext,
                                conn: sqlite3.Connection) -> None:
        try:
            pid = int(callback.data.split("pro:editname:", 1)[1])
        except ValueError:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        if not _owns_playlist(conn, pid, callback.from_user.id):
            await callback.answer(BRIEF["not_owner"], show_alert=True)
            return
        await state.update_data(pid=pid)
        await state.set_state(ProStates.edit_name)
        await _edit_or_answer(callback, "نام جدید پلی‌لیست", _cancel_kb())
        await callback.answer()

    @r.callback_query(F.data.startswith("pro:editdesc:"))
    async def pl_edit_desc_start(callback: CallbackQuery, state: FSMContext,
                                conn: sqlite3.Connection) -> None:
        try:
            pid = int(callback.data.split("pro:editdesc:", 1)[1])
        except ValueError:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        if not _owns_playlist(conn, pid, callback.from_user.id):
            await callback.answer(BRIEF["not_owner"], show_alert=True)
            return
        await state.update_data(pid=pid)
        await state.set_state(ProStates.edit_desc)
        await _edit_or_answer(callback, "توضیح جدید پلی‌لیست", _cancel_kb())
        await callback.answer()

    @r.callback_query(F.data.startswith("pro:editcolor:"))
    async def pl_edit_color_start(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        try:
            pid = int(callback.data.split("pro:editcolor:", 1)[1])
        except ValueError:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        if not _owns_playlist(conn, pid, callback.from_user.id):
            await callback.answer(BRIEF["not_owner"], show_alert=True)
            return
        rows = [[InlineKeyboardButton(text=c, callback_data=f"pro:setcolor:{c}:{pid}")]
                for c in COLORS]
        rows.append([InlineKeyboardButton(text="بدون رنگ", callback_data=f"pro:setcolor::{pid}")])
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pro:edit:{pid}")])
        await _edit_or_answer(callback, "🎨 رنگ پلی‌لیست (اختیاری):",
                              InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()

    @r.callback_query(F.data.startswith("pro:setcolor:"))
    async def pl_set_color(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        parts = callback.data.split("pro:setcolor:", 1)[1].rsplit(":", 1)
        if len(parts) != 2:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        color, pid_s = parts
        try:
            pid = int(pid_s)
        except ValueError:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        pl = _owns_playlist(conn, pid, callback.from_user.id)
        if not pl:
            await callback.answer(BRIEF["not_owner"], show_alert=True)
            return
        if color and color not in COLORS:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        set_playlist_color(conn, pid, color)
        await callback.answer("ذخیره شد.")
        text, kb = _render_playlist_view(conn, _owns_playlist(conn, pid, callback.from_user.id))
        await _edit_or_answer(callback, text, kb)

    @r.message(ProStates.edit_name)
    async def pl_edit_name_done(message: Message, state: FSMContext,
                               conn: sqlite3.Connection) -> None:
        data = await state.get_data()
        pid = data.get("pid")
        await state.clear()
        if not pid or not _owns_playlist(conn, pid, message.from_user.id):
            await message.answer(BRIEF["not_owner"])
            return
        name = (message.text or "").strip()
        if not name:
            await message.answer(BRIEF["generic"])
            return
        dbmod.rename_playlist(conn, pid, name)
        pl = _owns_playlist(conn, pid, message.from_user.id)
        text, kb = _render_playlist_view(conn, pl)
        await message.answer("ذخیره شد.")
        await message.answer(text, reply_markup=kb)

    @r.message(ProStates.edit_desc)
    async def pl_edit_desc_done(message: Message, state: FSMContext,
                               conn: sqlite3.Connection) -> None:
        data = await state.get_data()
        pid = data.get("pid")
        await state.clear()
        if not pid or not _owns_playlist(conn, pid, message.from_user.id):
            await message.answer(BRIEF["not_owner"])
            return
        pl = _owns_playlist(conn, pid, message.from_user.id)
        dbmod.rename_playlist(conn, pid, pl["name"], (message.text or "").strip())
        pl = _owns_playlist(conn, pid, message.from_user.id)
        text, kb = _render_playlist_view(conn, pl)
        await message.answer("ذخیره شد.")
        await message.answer(text, reply_markup=kb)

    # --- delete playlist ----------------------------------------------------
    @r.callback_query(F.data.startswith("pro:del:"))
    async def pl_del_confirm(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        rest = callback.data.split("pro:del:", 1)[1]
        if rest.startswith("yes:"):
            try:
                pid = int(rest.split("yes:", 1)[1])
            except ValueError:
                await callback.answer(BRIEF["generic"], show_alert=True)
                return
            pl = _owns_playlist(conn, pid, callback.from_user.id)
            if not pl:
                await callback.answer(BRIEF["not_owner"], show_alert=True)
                return
            dbmod.delete_playlist(conn, pid)
            await callback.answer("حذف شد.")
            text, kb = _render_pro_view(conn, callback.from_user.id)
            await _edit_or_answer(callback, text, kb)
            return
        try:
            pid = int(rest)
        except ValueError:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        pl = _owns_playlist(conn, pid, callback.from_user.id)
        if not pl:
            await callback.answer(BRIEF["not_owner"], show_alert=True)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="بله، حذف شود",
                                  callback_data=f"pro:del:yes:{pid}")],
            [InlineKeyboardButton(text="لغو", callback_data=f"plv:{pid}")],
        ])
        await _edit_or_answer(callback, f"«{pl['name']}» حذف شود؟", kb)
        await callback.answer()

    # --- add to playlist -----------------------------------------------------
    @r.callback_query(F.data.startswith("pro:pick:"))
    async def pl_pick(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        key = callback.data.split("pro:pick:", 1)[1]
        res = get_result(key)
        if not res:
            await callback.answer(BRIEF["generic"], show_alert=True)
            return
        pls = dbmod.list_playlists(conn, callback.from_user.id)
        title = res["title"]
        if not pls:
            await callback.answer(T_NO_PLAYLISTS, show_alert=True)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=pl["name"], callback_data=f"pro:add:{pl['id']}:{key}")]
            for pl in pls
        ])
        await _edit_or_answer(callback, T_PICK_TITLE.format(title=title), kb)
        await callback.answer()

    @r.callback_query(F.data.startswith("pro:add:"))
    async def pl_add(callback: CallbackQuery, conn: sqlite3.Connection) -> None:
        rest = callback.data.split("pro:add:", 1)[1]
        try:
            pid_s, key = rest.split(":", 1)
            pid = int(pid_s)
        except ValueError:
            await callback.answer(BRIEF["playlist_add"], show_alert=True)
            return
        res = get_result(key)
        pl = _owns_playlist(conn, pid, callback.from_user.id)
        if not res or not pl:
            await callback.answer(BRIEF["playlist_add"], show_alert=True)
            return
        try:
            added = dbmod.add_to_playlist(
                conn, pid, res["title"], res.get("artist", ""),
                res.get("url", ""), res.get("preview_url", ""))
        except Exception:
            await callback.answer(BRIEF["playlist_add"], show_alert=True)
            return
        if added:
            await callback.answer(T_ADDED.format(title=res["title"], name=pl["name"]),
                                  show_alert=True)
        else:
            await callback.answer(T_ALREADY.format(title=res["title"], name=pl["name"]),
                                  show_alert=True)
        text, kb = _render_playlist_view(conn, pl)
        await _edit_or_answer(callback, text, kb)

    router.include_router(r)
