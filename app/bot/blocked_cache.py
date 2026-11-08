"""In-memory blocked-user set (P1-16 + P1-14).

The blocked list is served from MEMORY — no DB and no Turso query in
any user waiting path. Source of truth is the local ``users.blocked``
column, which P1-14 mirrors to Turso and restores from Turso at
startup, so a restart never un-blocks anyone.

Refresh points (all cheap, none in a user path):
  * process startup (run_bot, before polling starts),
  * the 60 s heartbeat,
  * immediately when an admin blocks/unblocks a user (admin panel in
    Telegram, web panel) — so the change takes effect at once.

Fail-open on a refresh error: the previous set is kept. A user who was
never seen is never in the set, so lookups for new users cost nothing.
Thread-safe: the web panel runs in its own thread and updates the set
on block/unblock.
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_BLOCKED: set[int] = set()


def is_blocked(user_id: int | None) -> bool:
    """True when the user is blocked (memory only — no I/O)."""
    if user_id is None:
        return False
    with _LOCK:
        return int(user_id) in _BLOCKED


def set_blocked(user_id: int, blocked: bool) -> None:
    """Apply one block/unblock immediately (admin action path)."""
    with _LOCK:
        if blocked:
            _BLOCKED.add(int(user_id))
        else:
            _BLOCKED.discard(int(user_id))


def refresh(conn) -> int:
    """Reload the set from local ``users.blocked``. Returns its size.

    Local SQLite only (one tiny SELECT) — never Turso. Never raises:
    on any error the previous set is kept and its size is returned.
    """
    try:
        rows = conn.execute(
            "SELECT telegram_id FROM users WHERE blocked=1").fetchall()
        fresh = {int(r[0]) for r in rows}
    except Exception:
        with _LOCK:
            return len(_BLOCKED)
    with _LOCK:
        _BLOCKED.clear()
        _BLOCKED.update(fresh)
        return len(_BLOCKED)


def snapshot() -> frozenset:
    """Copy of the current set (tests / diagnostics)."""
    with _LOCK:
        return frozenset(_BLOCKED)
