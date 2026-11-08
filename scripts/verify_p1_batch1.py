"""P1 batch 1 gate (security): P1-8 SSRF, P1-14 Turso user data,
P1-16 blocked-users on callbacks, P1-17 OpenAI key masking.

Proves each item behaviourally (no live network, no Telegram):
  S1  a blocked/SSRF URL is rejected; a legitimate URL passes; the
      DNS verdict is cached (one lookup per host, not one per fetch).
  S2  a blocked user is refused on callback AND on message (via the
      dispatcher middleware); unblocked users and the owner pass.
  S3  the blocked list is served from memory (no Turso call in the
      waiting path) and _register performs no Turso I/O at all.
  S4  the user save is mirrored to Turso only by the background flush
      (after the answer); a fresh local DB restored from Turso keeps
      the user count, the blocked flag, playlists, favourites and
      language — the P1-14 root-cause fix — with no id reuse.
  S5  the OpenAI key is masked in the web panel, in settings_history
      and in the audit line, while the settings table keeps the real
      value.
  S6  the wiring exists in the live code paths (middleware registered,
      restore at startup, validation at the fetch sites).

Run: python3 scripts/verify_p1_batch1.py ; exit 0 = all PASS.
DNS note: the sandbox resolver answers every host with a 198.18/15
(CGNAT) address, which Python marks private — so the allow-cases use
a mocked getaddrinfo returning a public IP, and the logic (not the
sandbox's fake DNS) is what is tested. On Render, real DNS returns
real public IPs.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Strip proxy env vars (sandbox httpx gotcha) and Turso config.
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "TURSO_URL", "TURSO_AUTH_TOKEN", "TURSO_DATABASE_URL"):
    os.environ.pop(_k, None)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))


# Fake aiogram + the real handlers module, via the smoke test's
# installer (it installs the fakes and imports handlers as `h`).
import verify_smoke_text_handler as smoke  # noqa: E402

h = smoke.h
dbmod = smoke.dbmod
from app import webpanel as webpanel_mod  # noqa: E402
from app.bot import blocked_cache  # noqa: E402
from app.features import admin as adminfeat  # noqa: E402
from app.i18n import t  # noqa: E402
from app.security import (  # noqa: E402
    clear_dns_verdict_cache,
    validate_final_url,
    validate_media_url_for_fetch,
    validate_url_for_fetch,
)
from app import db as dbmod2  # noqa: E402  (same module as smoke.dbmod)

assert dbmod is dbmod2

_PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
_PRIVATE = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]


class _CountingResolver:
    """Fake getaddrinfo: returns the configured verdict, counts calls."""
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def __call__(self, host, port, *a, **k):
        self.calls += 1
        return self.verdict


async def _run_s1() -> None:
    from app.security import validate_link
    # Literal private IPs / hostnames: no DNS needed to refuse.
    check("S1 private-IP literal rejected",
          not validate_link("http://127.0.0.1/x.mp3", resolve_dns=True).ok)
    check("S1 metadata IP rejected",
          not validate_link("http://169.254.169.254/latest/meta-data/",
                            resolve_dns=True).ok)
    check("S1 localhost rejected",
          not validate_link("http://localhost/x.mp3").ok)
    check("S1 10/8 literal rejected",
          not (await validate_url_for_fetch("http://10.0.0.5/video.mp4")).ok)
    check("S1 192.168 media URL rejected",
          not (await validate_media_url_for_fetch("http://192.168.1.10/v.mp4")).ok)

    orig = socket.getaddrinfo
    try:
        # Legitimate Instagram URL passes with a public DNS answer.
        socket.getaddrinfo = _CountingResolver(_PUBLIC)
        clear_dns_verdict_cache()
        v = await validate_url_for_fetch("https://www.instagram.com/p/DfcRx2fudEE/")
        check("S1 legitimate instagram URL allowed", bool(v), f"reason={v.reason}")
        # Host resolving to a private IP is refused.
        socket.getaddrinfo = _CountingResolver(_PRIVATE)
        clear_dns_verdict_cache()
        v = await validate_url_for_fetch("https://evil.example.com/x.mp4")
        check("S1 host resolving to private IP refused",
              (not v.ok) and v.reason == "resolves_to_private_ip", f"reason={v.reason}")
        # CDN media URL without an extension passes (media validator).
        socket.getaddrinfo = _CountingResolver(_PUBLIC)
        clear_dns_verdict_cache()
        v = await validate_media_url_for_fetch(
            "https://cdn.example.com/file-noext?token=abc")
        check("S1 CDN media URL without extension allowed", bool(v), f"reason={v.reason}")
        v = await validate_media_url_for_fetch(
            "https://scontent.cdninstagram.com/v/t50.2886-16/123.mp4?x=1")
        check("S1 CDN mp4 URL allowed", bool(v), f"reason={v.reason}")
        # Redirect target: the final URL is re-validated (sync form).
        v = validate_final_url("https://www.instagram.com/p/x/")
        check("S1 final URL (redirect target) allowed", bool(v), f"reason={v.reason}")
        socket.getaddrinfo = _CountingResolver(_PRIVATE)
        clear_dns_verdict_cache()
        v = validate_final_url("https://evil.example.com/x.mp4")
        check("S1 final URL to private IP refused", not v.ok, f"reason={v.reason}")
        # DNS verdict is cached: one lookup per host, not one per fetch.
        resolver = _CountingResolver(_PUBLIC)
        socket.getaddrinfo = resolver
        clear_dns_verdict_cache()
        v1 = await validate_url_for_fetch("https://www.instagram.com/p/AAA/")
        v2 = await validate_url_for_fetch("https://www.instagram.com/reel/BBB/")
        check("S1 DNS verdict cached (1 lookup for 2 fetches)",
              bool(v1) and bool(v2) and resolver.calls == 1,
              f"calls={resolver.calls}")
        # ... and the cached verdict wins even when DNS now disagrees.
        socket.getaddrinfo = _CountingResolver(_PRIVATE)
        v3 = await validate_url_for_fetch("https://www.instagram.com/p/CCC/")
        check("S1 cached verdict reused (no re-lookup)", bool(v3))
        clear_dns_verdict_cache()
        v4 = await validate_url_for_fetch("https://www.instagram.com/p/DDD/")
        check("S1 cache clear re-checks DNS", not v4.ok)
    finally:
        socket.getaddrinfo = orig
        clear_dns_verdict_cache()


from aiogram.types import CallbackQuery as _CQBase  # noqa: E402
from aiogram.types import Message as _MsgBase  # noqa: E402


class _FakeCallbackEvent(_CQBase):
    """Fake CallbackQuery carrying a recorder answer()."""
    def __init__(self, uid):
        self.from_user = SimpleNamespace(id=uid)
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


class _FakeMessageEvent(_MsgBase):
    """Fake Message carrying a recorder answer()."""
    def __init__(self, uid):
        self.from_user = SimpleNamespace(id=uid)
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


async def _run_s2_s3() -> None:
    conn = smoke._make_conn()
    dbmod.upsert_user(conn, 111, "blocked_user", "Blocked")
    dbmod.upsert_user(conn, 222, "ok_user", "Ok")
    dbmod.set_user_blocked(conn, 111, True)
    blocked_cache.refresh(conn)
    check("S2 blocked set loaded from local DB",
          blocked_cache.is_blocked(111) and not blocked_cache.is_blocked(222))

    gate = h._BlockedGate(owner_id=572595158)
    handled: list = []

    async def _next(event, data):
        handled.append(event)
        return "HANDLED"

    # Blocked user, callback: refused, handler never runs, alert sent.
    cb = _FakeCallbackEvent(111)
    res = await gate(_next, cb, {"conn": conn})
    check("S2 blocked user refused on callback",
          res is None and not handled and len(cb.answers) == 1
          and cb.answers[0][0] == t("blocked", "fa")
          and cb.answers[0][1].get("show_alert") is True,
          f"res={res} handled={len(handled)} answers={cb.answers}")
    # Blocked user, message: refused, handler never runs, text sent.
    msg = _FakeMessageEvent(111)
    res = await gate(_next, msg, {"conn": conn})
    check("S2 blocked user refused on message",
          res is None and not handled and len(msg.answers) == 1
          and msg.answers[0][0] == t("blocked", "fa"),
          f"res={res} handled={len(handled)} answers={msg.answers}")
    # Unblocked user passes through.
    cb2 = _FakeCallbackEvent(222)
    res = await gate(_next, cb2, {"conn": conn})
    check("S2 unblocked user passes callback",
          res == "HANDLED" and len(handled) == 1 and not cb2.answers)
    # Owner is never gated, even if (wrongly) in the blocked set.
    blocked_cache.set_blocked(572595158, True)
    cb3 = _FakeCallbackEvent(572595158)
    res = await gate(_next, cb3, {"conn": conn})
    check("S2 owner never gated",
          res == "HANDLED" and len(handled) == 2)
    blocked_cache.set_blocked(572595158, False)
    # Update with no user passes through.
    res = await gate(_next, SimpleNamespace(), {"conn": conn})
    check("S2 update without user passes through", res == "HANDLED")

    # S3: the blocked decision needs no Turso — prove it by making
    # every Turso access explode, then using the gate + _register.
    orig_turso_conn = dbmod._turso_conn

    def _boom():
        raise AssertionError("turso touched in the waiting path")

    dbmod._turso_conn = _boom
    try:
        check("S3 blocked list served from memory (Turso dead)",
              blocked_cache.is_blocked(111) and not blocked_cache.is_blocked(222))
        fake_msg = smoke._FakeMessage("hello")
        fake_msg.from_user = SimpleNamespace(id=333, username="new",
                                            first_name="New")
        h._register(fake_msg, conn)
        row = dbmod.get_user(conn, 333)
        check("S3 _register writes locally with Turso dead",
              row is not None and row.get("telegram_id") == 333)
        check("S3 _register marks the user dirty (mirror deferred)",
              333 in dbmod.dirty_keys().get("users", frozenset()))
    finally:
        dbmod._turso_conn = orig_turso_conn


def _fake_turso_conn():
    """In-memory sqlite3 standing in for Turso (same SQL dialect)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _run_s4() -> None:
    # Clean dirty state left by earlier sections (other temp DBs).
    with dbmod._DIRTY_LOCK:
        dbmod._DIRTY.clear()
        dbmod._DIRTY_DELETED.clear()
    dbmod._RESTORE_DONE = False
    fake = _fake_turso_conn()
    orig = dbmod._turso_conn
    dbmod._turso_conn = lambda: fake
    try:
        td1 = tempfile.mkdtemp(prefix="p1b_db1_")
        db1_path = os.path.join(td1, "bot.db")
        conn1 = dbmod.init_db(db1_path)
        dbmod.upsert_user(conn1, 111, "blocked_user", "Blocked")
        dbmod.set_user_blocked(conn1, 111, True)
        dbmod.set_user_language(conn1, 111, "en")
        pl = dbmod.create_playlist(conn1, 111, "gym", "workout", "red")
        dbmod.add_to_playlist(conn1, pl["id"], "Song A", "Artist A")
        dbmod.add_favorite(conn1, 111, "Song B", "Artist B")
        dbmod.set_setting(conn1, "openai_api_key", "sk-test-123", by_id=1)
        res = dbmod.flush_dirty_to_turso(db1_path)
        check("S4 background flush mirrors dirty rows",
              res.get("mirrored", 0) >= 6, f"res={res}")
        tusers = fake.execute("SELECT * FROM users WHERE telegram_id=111").fetchone()
        check("S4 Turso holds the user row (blocked=1)",
              tusers is not None and tusers["blocked"] == 1
              and tusers["username"] == "blocked_user")
        check("S4 Turso holds playlist + item + favorite + language",
              fake.execute("SELECT COUNT(*) FROM playlists").fetchone()[0] == 1
              and fake.execute("SELECT COUNT(*) FROM playlist_items").fetchone()[0] == 1
              and fake.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 1
              and fake.execute(
                  "SELECT lang FROM user_language WHERE telegram_id=111"
              ).fetchone()[0] == "en")
        check("S4 Turso holds the setting (real value, durable store)",
              fake.execute(
                  "SELECT value FROM settings WHERE key='openai_api_key'"
              ).fetchone()[0] == "sk-test-123")

        # Restart simulation: a FRESH local DB, restored from Turso.
        td2 = tempfile.mkdtemp(prefix="p1b_db2_")
        db2_path = os.path.join(td2, "bot.db")
        conn2 = dbmod.init_db(db2_path)
        check("S4 fresh DB starts empty (the wipe)",
              dbmod.count_users(conn2) == 0)
        dbmod._RESTORE_DONE = False
        res = dbmod.restore_from_turso(conn2)
        check("S4 restore repopulates the fresh DB",
              dbmod.count_users(conn2) == 1
              and dbmod.get_user(conn2, 111).get("blocked") is True
              or dbmod.get_user(conn2, 111).get("blocked") == 1,
              f"count={dbmod.count_users(conn2)} res={res}")
        check("S4 admin user count survives restart (root cause)",
              dbmod.count_users(conn2) == 1)
        check("S4 playlists/favourites/language survive restart",
              len(dbmod.list_playlists(conn2, 111)) == 1
              and len(dbmod.list_favorites(conn2, 111)) == 1
              and dbmod.user_language(conn2, 111) == "en"
              and dbmod.get_setting(conn2, "openai_api_key", "") == "sk-test-123")
        blocked_cache.refresh(conn2)
        check("S4 blocked flag survives restart (cache sees it)",
              blocked_cache.is_blocked(111))
        # No id reuse: a fresh playlist in the restored DB gets a new id.
        pl2 = dbmod.create_playlist(conn2, 111, "drive", "", "")
        check("S4 no AUTOINCREMENT id reuse after restore",
              pl2["id"] != pl["id"], f"old={pl['id']} new={pl2['id']}")
        # Tombstone: deleting the playlist removes it from Turso too.
        dbmod.delete_playlist(conn2, pl["id"])
        res = dbmod.flush_dirty_to_turso(db2_path)
        check("S4 deleted playlist removed from Turso",
              fake.execute("SELECT COUNT(*) FROM playlists WHERE id=?",
                           (pl["id"],)).fetchone()[0] == 0
              and fake.execute("SELECT COUNT(*) FROM playlist_items "
                               "WHERE playlist_id=?", (pl["id"],)).fetchone()[0] == 0,
              f"res={res}")
    finally:
        dbmod._turso_conn = orig


def _run_s5() -> None:
    check("S5 openai_api_key is sensitive",
          dbmod.is_sensitive_setting("openai_api_key"))
    check("S5 audd_token / *_secret are sensitive",
          dbmod.is_sensitive_setting("audd_token")
          and dbmod.is_sensitive_setting("acrcloud_access_secret"))
    check("S5 plain settings are not sensitive",
          not dbmod.is_sensitive_setting("welcome_message")
          and not dbmod.is_sensitive_setting("daily_search_limit"))
    check("S5 mask keeps empty values empty",
          dbmod.mask_setting_value("openai_api_key", "") == "")
    check("S5 mask replaces the secret",
          dbmod.mask_setting_value("openai_api_key", "sk-live-123") == "••••••"
          and dbmod.mask_setting_value("welcome_message", "hi") == "hi")
    conn = smoke._make_conn()
    dbmod.set_setting(conn, "openai_api_key", "sk-abc-123", by_id=1)
    hist = conn.execute(
        "SELECT old_value, new_value FROM settings_history "
        "WHERE key='openai_api_key' ORDER BY changed_at DESC LIMIT 1").fetchone()
    check("S5 settings_history holds the masked value, never the key",
          hist is not None and hist["new_value"] == "••••••"
          and "sk-abc-123" not in (hist["old_value"] + hist["new_value"]),
          f"hist={dict(hist) if hist else None}")
    check("S5 settings table keeps the real value (still usable)",
          dbmod.get_setting(conn, "openai_api_key", "") == "sk-abc-123")
    audit_row = conn.execute(
        "SELECT detail FROM audit_log WHERE action='set_setting' "
        "ORDER BY created_at DESC LIMIT 1").fetchone()
    check("S5 audit line holds the masked value",
          audit_row is not None and "sk-abc-123" not in audit_row["detail"]
          and "••••••" in audit_row["detail"],
          f"detail={audit_row['detail'] if audit_row else None}")
    html_rows = webpanel_mod.settings_rows_html(
        {"openai_api_key": "sk-live-999", "bot_name": "Avano"})
    check("S5 web panel masks the key in the rendered rows",
          "••••••" in html_rows and "sk-live-999" not in html_rows
          and "Avano" in html_rows)
    check("S5 admin helper flags openai_api_key sensitive",
          adminfeat.is_sensitive_setting_key("openai_api_key")
          and not adminfeat.is_sensitive_setting_key("welcome_message"))
    dbmod.set_setting(conn, "audd_token", "audd-secret-1", by_id=1)
    text, _kb = adminfeat.settings_view(conn, "fa")
    check("S5 in-bot settings view never shows a sensitive value",
          "audd-secret-1" not in text)


def _run_s6() -> None:
    src = (ROOT / "app" / "bot" / "handlers.py").read_text(encoding="utf-8")
    check("S6 blocked gate registered on the dispatcher",
          "_BlockedGate(config.owner_id)" in src)
    check("S6 user restore runs at startup",
          "restore_from_turso(conn)" in src)
    check("S6 blocked cache refreshed at startup + heartbeat",
          src.count("blocked_cache.refresh(conn)") >= 2)
    check("S6 Turso flush runs after the answer (heartbeat + link)",
          src.count("flush_dirty_to_turso") >= 2)
    ig = (ROOT / "app" / "services" / "instagram.py").read_text(encoding="utf-8")
    dl = (ROOT / "app" / "features" / "downloader.py").read_text(encoding="utf-8")
    check("S6 SSRF validation at the instagram fetch sites",
          "validate_url_for_fetch(page_url)" in ig
          and "validate_url_for_fetch(str(resp.url))" in ig)
    check("S6 SSRF validation at the downloader fetch sites",
          "validate_url_for_fetch(url)" in dl
          and "validate_media_url_for_fetch(url)" in dl)
    check("S6 SSRF validation at the handlers fetch sites",
          "validate_media_url_for_fetch(media_url)" in src
          and "validate_url_for_fetch(url)" in src)


async def main() -> None:
    await _run_s1()
    await _run_s2_s3()
    _run_s4()
    _run_s5()
    _run_s6()


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
