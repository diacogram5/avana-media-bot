"""Integrated audience ads: the active ad is attached after every Nth bot
answer to a regular user.

Rules:
- Never shown to the owner.
- N is configurable in the admin ads section (setting ``ad_every_n``,
  default 5). ``ad_due`` counts bot answers per user.
- A disabled ad (``enabled=0``) or no ad at all is never shown.
- Unified template: optional photo, title, text, action button.

``ad_due`` / ``pick_active_ad`` are pure (no Bot needed) so they can be
unit-tested; ``send_ad`` takes explicit send callables for the same reason.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Awaitable, Callable

DEFAULT_EVERY_N = 5


def ad_every_n(conn: sqlite3.Connection) -> int:
    """Configured cadence: attach the ad after every Nth answer."""
    try:
        n = int((conn.execute(
            "SELECT value FROM settings WHERE key='ad_every_n'").fetchone() or ["5"])[0] or "5")
    except (ValueError, TypeError):
        return DEFAULT_EVERY_N
    return max(1, n)


def ad_due(conn: sqlite3.Connection, user_id: int) -> bool:
    """Record one bot answer for ``user_id``; True when an ad is due now."""
    n = ad_every_n(conn)
    row = conn.execute(
        "SELECT count FROM ad_counters WHERE user_id=?", (user_id,)).fetchone()
    count = (row[0] if row else 0) + 1
    conn.execute(
        "INSERT OR REPLACE INTO ad_counters(user_id, count) VALUES(?, ?)",
        (user_id, count))
    conn.commit()
    return count % n == 0


def pick_active_ad(conn: sqlite3.Connection) -> dict | None:
    """Newest enabled ad, or None when ads are off / none exist."""
    row = conn.execute(
        "SELECT * FROM ads WHERE enabled=1 ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def ad_text(ad: dict) -> str:
    return f"📢 تبلیغ\n{ad.get('title', '')}\n{ad.get('text', '')}".strip()


def ad_keyboard(ad: dict):
    link = (ad.get("link") or "").strip()
    if not link:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    label = (ad.get("button_text") or "مشاهده").strip() or "مشاهده"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, url=link)]])


async def send_ad(bot, chat_id: int, ad: dict,
                  _send_message: Callable[..., Awaitable[Any]] | None = None,
                  _send_photo: Callable[..., Awaitable[Any]] | None = None) -> bool:
    """Send one ad with the unified template. Returns True when sent."""
    send_message = _send_message or bot.send_message
    send_photo = _send_photo or bot.send_photo
    kb = ad_keyboard(ad)
    try:
        photo = (ad.get("photo_file_id") or "").strip()
        if photo:
            await send_photo(chat_id, photo, caption=ad_text(ad),
                             reply_markup=kb)
        else:
            await send_message(chat_id, ad_text(ad), reply_markup=kb)
        return True
    except Exception:
        return False
