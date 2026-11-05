"""Simple Persian web admin panel (stdlib http.server only, no new deps).

Runs in a thread alongside Telegram polling when WEB_ADMIN_ENABLED=1 and
WEB_ADMIN_TOKEN is set. Auth is deliberately simple: every request must
carry ?token=<WEB_ADMIN_TOKEN>; the token is never logged and never
rendered in HTML. The panel edits DB settings, bot texts, feature
toggles, ads and user blocking — the same DB the bot reads live, so
changes apply without restarting the bot. Service keys (BOT_TOKEN,
AUDD_API_TOKEN, AZURE_SPEECH_KEY) are NEVER shown or editable here.
"""
from __future__ import annotations

import html
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db as dbmod


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def make_handler(db_path, config):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # never log URLs (they may carry ?token=)
            pass

        def _conn(self):
            # SQLite connections are thread-bound: open a fresh one per request.
            return dbmod.connect(db_path)

        def _auth(self) -> bool:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return bool(config.web_admin_token) and q.get("token", [""])[0] == config.web_admin_token

        def _send(self, body: str, status: int = 200):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._auth():
                self._send("<h1>⛔ دسترسی غیرمجاز</h1>", 403); return
            conn = self._conn()
            try:
                return self._do_get(conn)
            finally:
                conn.close()

        def _do_get(self, conn):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = q.get("token", [""])[0]
            # Actions via GET with token (panel is owner-only by token).
            action = q.get("action", [""])[0]
            if action == "toggle_feature" and q.get("key"):
                f = dbmod.get_feature(conn, q["key"][0])
                if f:
                    dbmod.set_feature(conn, q["key"][0], enabled=not bool(f["enabled"]))
            if action == "toggle_ad" and q.get("id"):
                ads = {a["id"]: a for a in dbmod.list_ads(conn)}
                if int(q["id"][0]) in ads:
                    a = ads[int(q["id"][0])]
                    dbmod.set_ad_enabled(conn, a["id"], not bool(a["enabled"]))
            if action == "block_user" and q.get("id"):
                u = dbmod.get_user(conn, int(q["id"][0]))
                if u:
                    dbmod.set_user_blocked(conn, u["telegram_id"], not bool(u.get("blocked")))
            settings = dbmod.list_settings(conn)
            feats = dbmod.list_features(conn)
            ads = dbmod.list_ads(conn)
            users = dbmod.list_users(conn, limit=20)
            rows = "".join(
                f"<tr><td>{_esc(f['key'])}</td><td>{_esc(f['title'])}</td>"
                f"<td>{'✅' if f['enabled'] else '❌'}</td>"
                f"<td><a href='?token={_esc(token)}&action=toggle_feature&key={_esc(f['key'])}'>تغییر</a></td></tr>"
                for f in feats)
            ad_rows = "".join(
                f"<tr><td>{_esc(a['text'][:80])}</td><td>{'✅' if a['enabled'] else '❌'}</td>"
                f"<td><a href='?token={_esc(token)}&action=toggle_ad&id={a['id']}'>تغییر</a></td></tr>"
                for a in ads)
            user_rows = "".join(
                f"<tr><td>{u['telegram_id']}</td><td>{_esc(u.get('username') or '')}</td>"
                f"<td>{'⛔ مسدود' if u.get('blocked') else '✅ فعال'}</td>"
                f"<td><a href='?token={_esc(token)}&action=block_user&id={u['telegram_id']}'>تغییر</a></td></tr>"
                for u in users)
            settings_rows = "".join(
                f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in settings.items())
            body = f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<title>پنل مدیریت ربات</title></head><body>
<h1>🛡 پنل مدیریت ربات</h1>
<p>کاربران: {dbmod.count_users(conn)} — تغییرها بدون راه‌اندازی مجدد اعمال می‌شوند.</p>
<h2>تنظیمات و متن‌ها</h2>
<form method="post" action="?token={_esc(token)}">
<input name="key" placeholder="نام تنظیم (مثل welcome_message)">
<input name="value" placeholder="مقدار جدید" style="width:50%">
<button>ذخیره</button></form>
<table border="1">{settings_rows}</table>
<h2>قابلیت‌ها</h2><table border="1">{rows}</table>
<h2>تبلیغات</h2>
<form method="post" action="?token={_esc(token)}">
<input name="ad_text" placeholder="متن تبلیغ" style="width:60%"><button>افزودن تبلیغ</button></form>
<table border="1">{ad_rows}</table>
<h2>کاربران</h2><table border="1">{user_rows}</table>
</body></html>"""
            self._send(body)

        def do_POST(self):
            if not self._auth():
                self._send("<h1>⛔ دسترسی غیرمجاز</h1>", 403); return
            conn = self._conn()
            try:
                return self._do_post(conn)
            finally:
                conn.close()

        def _do_post(self, conn):
            length = int(self.headers.get("Content-Length", "0") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "ignore"))
            if form.get("key") and form.get("value") is not None:
                dbmod.set_setting(conn, form["key"][0], form["value"][0])
            if form.get("ad_text"):
                dbmod.add_ad(conn, form["ad_text"][0])
            self.send_response(303)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.send_header("Location", f"/?token={q.get('token', [''])[0]}")
            self.end_headers()

    return Handler


def start_web_panel(db_path, config):
    server = ThreadingHTTPServer(("0.0.0.0", config.web_admin_port), make_handler(db_path, config))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
