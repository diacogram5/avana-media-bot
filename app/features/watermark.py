"""Watermark / text-overlay removal — DISABLED STUB ONLY.

Scaffolding for a future "remove or hide text & ID overlays on video"
capability plus its special access methods. Contract (non-negotiable):

- Default state is DISABLED.
- While disabled: NO AI processing, NO forced ads, NO forced membership,
  NO payments, and NO paid API/service is ever called for this feature.
  The stub entry point returns the honest disabled message and touches
  nothing external — provable by test.
- Normal video download never consults this module and stays fully
  independent of it.
- The flag lives in the DB settings table, so the disabled state
  persists across restarts.
- Future activation happens only through the explicit admin toggle in
  the management panel — never automatically, never from a user button.
"""
from __future__ import annotations

import sqlite3

from .. import db as dbmod

FLAG_KEY = "watermark_removal_enabled"
DISABLED_MSG = "این قابلیت فعلاً غیرفعال است."
NOT_IMPLEMENTED_MSG = "این قابلیت هنوز پیاده‌سازی نشده است."


def is_enabled(conn: sqlite3.Connection) -> bool:
    """Read the persisted flag; default 0 (disabled)."""
    return dbmod.get_setting(conn, FLAG_KEY, "0") == "1"


def set_enabled(conn: sqlite3.Connection, enabled: bool,
                by_id: int | None = None) -> None:
    """Admin-only toggle. Persists across restarts via the settings table."""
    dbmod.set_setting(conn, FLAG_KEY, "1" if enabled else "0", by_id=by_id)


async def request_removal(conn: sqlite3.Connection,
                          video: bytes | None = None) -> tuple[str, bytes | None]:
    """Stub entry point for the future capability.

    Returns (message, processed_bytes). While disabled — the only state
    this release supports — it returns the honest disabled message and
    makes ZERO external calls: no network, no AI, no paid service. The
    ``video`` bytes are never even read.
    """
    if not is_enabled(conn):
        return DISABLED_MSG, None
    # Enabled but not yet implemented: stay honest, still no external calls.
    return NOT_IMPLEMENTED_MSG, None
