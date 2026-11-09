"""Sandbox tests for the TEMPORARY Google probe (app/services/brave_probe.py
Google engine + the /brave-probe route in app/webpanel.py, which now
runs all three sections: Brave, Google AND Persian).

No network beyond localhost, no Telegram, no real Turso, no Google
account / API key / payment anywhere. The HTTP layer is faked
throughout.

Cases:
  G1  Both Google queries are attempted (Persian + Latin), exactly 2
      calls, to www.google.com/search.
  G2  OK page: Google /url?q= result links unwrapped + counted, first
      3 titles sampled, blocked_reason None, ms present.
  G3  Redirect to consent.google.com (final URL) -> blocked / consent.
  G4  Consent body marker ("Before you continue") -> blocked / consent.
  G5  Captcha body ("unusual traffic") -> blocked / captcha.
  G6  HTTP 403 -> blocked / http_403.
  G7  HTTP 429 -> blocked / http_429.
  G8  Empty 200 body -> blocked / empty.
  G9  200 with no result links -> blocked / no_results.
  G10 Timeout -> error / timeout.
  G11 Transport error -> error / transport:<TypeName>.
  G12 Google-internal links skipped (/url?q= unwrap + dedupe).
  G13 run_google_probe: ok True only when BOTH queries ok.
  G14 run_search_probe: combined shape {probe: search, brave, google,
      persian, ok}; outer ok follows Google only (Brave ok + Google
      blocked -> ok False; both ok -> ok True; persian section present
      with probe "persian", ok False on empty results).
  G15 No key/account/payment anywhere in brave_probe.py (source scan:
      no api_key/API_KEY/password/Authorization/x-api-key/googleapis/
      oauth/client_secret/rapidapi/secret/credential).
  G16 /brave-probe route returns the combined JSON with all three
      sections (brave + google + persian; real local HTTP server,
      run_search_probe_sync monkeypatched — no outbound traffic).
  G17 run_search_probe calls Brave first (2), then Google (2), then
      Persian (first 4 calls are Brave, Brave, Google, Google).

Run: python3 scripts/verify_google_probe.py ; exit 0 = all PASS.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import types
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Strip proxy env vars (sandbox httpx gotcha).
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)

import httpx  # noqa: E402

from app.services import brave_probe as bp  # noqa: E402
from app import webpanel  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name
          + (f" — {detail}" if detail and not ok else ""))


class _FakeResp:
    def __init__(self, status_code: int, text: str, url: str | None = None):
        self.status_code = status_code
        self.text = text
        # Final URL after redirects (httpx Response.url); None when the
        # fake does not model a redirect.
        self.url = url

    def json(self):
        return json.loads(self.text)


class _FakeClient:
    """Fake httpx.AsyncClient: handler(query) -> _FakeResp or raises."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict | None]] = []

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        return self.handler(params["q"] if params else "")


class _RouteClient:
    """Fake client routing by engine URL: 2 handlers (Brave, Google).

    Also routes the Persian section (added 2026-11-09, render probe):
    WP searches return an empty list (0 hits), the RadioJavan home
    GET returns 200, mp3 HEADs return 404 (so no prefix download ever
    runs). Unknown URLs still raise.
    """

    def __init__(self, brave_handler, google_handler):
        self.brave_handler = brave_handler
        self.google_handler = google_handler
        self.calls: list[tuple[str, dict | None]] = []

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        if url == bp.BRAVE_SEARCH_URL:
            return self.brave_handler(params["q"])
        if url == bp.GOOGLE_SEARCH_URL:
            return self.google_handler(params["q"])
        if url == "https://www.radiojavan.com":
            return _FakeResp(200, "<html>ok</html>")
        if url in {w for _n, w, _m, _h in bp._PERSIAN_SITES if w}:
            return _FakeResp(200, "[]")
        raise AssertionError(f"unexpected url {url}")

    async def head(self, url, headers=None):
        self.calls.append(("HEAD", url, None))
        if url in {m for _n, _w, m, _h in bp._PERSIAN_SITES if m}:
            return _FakeResp(404, "Not Found")
        raise AssertionError(f"unexpected HEAD url {url}")

    def stream(self, method, url, headers=None):
        raise AssertionError("stream must not run when HEAD is 404")


_GOOGLE_OK_HTML = (
    '<html><body>'
    '<a href="/url?q=https://example.com/song1&amp;sa=U&amp;ved=1">'
    'Song One Title</a>'
    '<a href="/url?q=https://music.example.org/song2&amp;sa=U">'
    'Song Two <b>Title</b></a>'
    '<a href="https://dl.example.net/song3">Song Three Title</a>'
    '<a href="/url?q=https://example.com/song4&amp;sa=U">'
    'Song Four Title</a>'
    '</body></html>'
)
_CONSENT_BODY_HTML = (
    '<html><body><h1>Before you continue to Google Search</h1>'
    '<p>Google uses cookies</p></body></html>'
)
_CAPTCHA_HTML = (
    '<html><body><h1>Our systems have detected unusual traffic '
    'from your computer network</h1></body></html>'
)
_NORESULT_HTML = '<html><body><p>nothing here at all</p></body></html>'


def _google_url(q: str) -> str:
    return f"https://www.google.com/search?q={q}"


def _ok_handler(query):
    return _FakeResp(200, _GOOGLE_OK_HTML, url=_google_url(query))


def _mixed_handler(query):
    if "تازیانه" in query:
        return _FakeResp(200, _GOOGLE_OK_HTML, url=_google_url(query))
    return _FakeResp(429, "Too Many Requests", url=_google_url(query))


async def _run_cases() -> None:
    # G1: both queries attempted, exactly 2 calls, right endpoint.
    client = _FakeClient(_ok_handler)
    result = await bp.run_google_probe(client=client)
    check("G1 both Google queries attempted",
          len(client.calls) == 2
          and [c[1]["q"] for c in client.calls] == list(bp.PROBE_QUERIES)
          and all(c[0] == bp.GOOGLE_SEARCH_URL for c in client.calls),
          f"calls={client.calls}")

    # G2: OK page classification (/url?q= unwrapped).
    q0 = result["queries"][0]
    check("G2 ok page classified (/url?q= unwrapped)",
          q0["status"] == "ok" and q0["result_count"] == 4
          and q0["blocked_reason"] is None
          and q0["sample_titles"] == [
              "Song One Title", "Song Two Title", "Song Three Title"]
          and isinstance(q0["ms"], int) and q0["ms"] >= 0,
          f"q0={q0}")
    check("G2b run ok flag + ms_total",
          result["ok"] is True and result["probe"] == "google"
          and result["temporary"] is True
          and isinstance(result["ms_total"], int),
          f"result={result}")

    # G3–G11: one-query classifications.
    async def _one(handler):
        return await bp.probe_one_google_query(
            "probe", client=_FakeClient(handler))

    r = await _one(lambda q: _FakeResp(
        200, _CONSENT_BODY_HTML,
        url="https://consent.google.com/ml?continue="
             + _google_url(q)))
    check("G3 consent redirect (final URL) -> blocked/consent",
          r["status"] == "blocked" and r["blocked_reason"] == "consent"
          and r["result_count"] == 0, f"r={r}")

    r = await _one(lambda q: _FakeResp(
        200, _CONSENT_BODY_HTML, url=_google_url(q)))
    check("G4 consent body marker -> blocked/consent",
          r["status"] == "blocked" and r["blocked_reason"] == "consent",
          f"r={r}")

    r = await _one(lambda q: _FakeResp(
        200, _CAPTCHA_HTML, url=_google_url(q)))
    check("G5 captcha body -> blocked/captcha",
          r["status"] == "blocked" and r["blocked_reason"] == "captcha",
          f"r={r}")

    r = await _one(lambda q: _FakeResp(
        403, "Forbidden", url=_google_url(q)))
    check("G6 http 403 -> blocked/http_403",
          r["status"] == "blocked" and r["blocked_reason"] == "http_403",
          f"r={r}")

    r = await _one(lambda q: _FakeResp(
        429, "Too Many Requests", url=_google_url(q)))
    check("G7 http 429 -> blocked/http_429",
          r["status"] == "blocked" and r["blocked_reason"] == "http_429",
          f"r={r}")

    r = await _one(lambda q: _FakeResp(200, "", url=_google_url(q)))
    check("G8 empty 200 body -> blocked/empty",
          r["status"] == "blocked" and r["blocked_reason"] == "empty",
          f"r={r}")

    r = await _one(lambda q: _FakeResp(
        200, _NORESULT_HTML, url=_google_url(q)))
    check("G9 200 no result links -> blocked/no_results",
          r["status"] == "blocked" and r["blocked_reason"] == "no_results",
          f"r={r}")

    def _raise_timeout(q):
        raise httpx.ConnectTimeout("timed out")
    r = await _one(_raise_timeout)
    check("G10 timeout -> error/timeout",
          r["status"] == "error" and r["blocked_reason"] == "timeout",
          f"r={r}")

    def _raise_transport(q):
        raise httpx.ConnectError("boom")
    r = await _one(_raise_transport)
    check("G11 transport error -> error/transport",
          r["status"] == "error"
          and r["blocked_reason"] == "transport:ConnectError",
          f"r={r}")

    # G12: Google-internal links skipped, /url?q= unwrapped, dupes deduped.
    skip_html = (
        '<a href="https://www.google.com/search?q=x">Google Search</a>'
        '<a href="https://consent.google.com/ml?x=1">Consent</a>'
        '<a href="https://maps.google.com/maps?q=x">Maps</a>'
        '<a href="https://accounts.google.com/signin">Sign in</a>'
        '<a href="/url?q=https://example.com/a&amp;sa=U">First</a>'
        '<a href="/url?q=https://example.com/a&amp;sa=U">First again</a>'
        '<a href="/url?q=https://example.com/b&amp;sa=U"></a>'
        '<a href="/search?q=relative">Relative</a>'
    )
    links = bp.parse_result_links(
        skip_html, skip_hosts=bp._GOOGLE_SKIP_HOSTS)
    check("G12 google-internal links skipped, /url?q= unwrapped",
          [l["url"] for l in links] == ["https://example.com/a"],
          f"links={links}")

    # G13: ok only when BOTH queries ok.
    mixed = await bp.run_google_probe(client=_FakeClient(_mixed_handler))
    check("G13 mixed ok+blocked -> ok False",
          mixed["ok"] is False
          and mixed["queries"][0]["status"] == "ok"
          and mixed["queries"][1]["status"] == "blocked",
          f"mixed={mixed}")

    # G14: combined run_search_probe shape; outer ok follows Google.
    route = _RouteClient(_ok_handler, _mixed_handler)
    combined = await bp.run_search_probe(client=route)
    check("G14 combined shape, Brave ok + Google blocked -> ok False",
          combined["probe"] == "search"
          and combined["temporary"] is True
          and combined["brave"]["probe"] == "brave"
          and combined["brave"]["ok"] is True
          and combined["google"]["probe"] == "google"
          and combined["google"]["ok"] is False
          and combined["ok"] is False
          and combined["persian"]["probe"] == "persian"
          and combined["persian"]["ok"] is False,
          f"combined={combined}")

    route2 = _RouteClient(_ok_handler, _ok_handler)
    combined2 = await bp.run_search_probe(client=route2)
    check("G14b both engines ok -> outer ok True",
          combined2["ok"] is True
          and combined2["brave"]["ok"] is True
          and combined2["google"]["ok"] is True
          and combined2["persian"]["probe"] == "persian",
          f"combined2={combined2}")

    # G15: no key / account / payment anywhere in the probe module.
    src = (ROOT / "app" / "services" / "brave_probe.py").read_text()
    lowered = src.lower()
    forbidden = ("api_key", "apikey", "password", "authorization",
                 "x-api-key", "googleapis", "oauth", "client_secret",
                 "rapidapi", "secret", "credential")
    hits = [f for f in forbidden if f in lowered]
    check("G15 no key/account/payment in probe module",
          not hits, f"hits={hits}")

    # G16 + G17: route + call routing.
    _run_route_cases()
    check("G17 run_search_probe calls Brave then Google then Persian",
          [c[0] for c in route2.calls][:4] == [
              bp.BRAVE_SEARCH_URL, bp.BRAVE_SEARCH_URL,
              bp.GOOGLE_SEARCH_URL, bp.GOOGLE_SEARCH_URL]
          and any(c[0] not in (bp.BRAVE_SEARCH_URL, bp.GOOGLE_SEARCH_URL)
                  for c in route2.calls[4:]),
          f"calls={route2.calls}")


def _serve_once(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, srv.server_address[1]


def _get(port: int, path: str) -> tuple[int, dict]:
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _run_route_cases() -> None:
    canned = {
        "probe": "search",
        "temporary": True,
        "brave": {
            "probe": "brave",
            "temporary": True,
            "queries": [
                {"query": bp.PROBE_QUERIES[0], "status": "blocked",
                 "result_count": 0, "blocked_reason": "http_429",
                 "ms": 145, "sample_titles": []},
                {"query": bp.PROBE_QUERIES[1], "status": "blocked",
                 "result_count": 0, "blocked_reason": "http_429",
                 "ms": 142, "sample_titles": []},
            ],
            "ms_total": 287,
            "ok": False,
        },
        "google": {
            "probe": "google",
            "temporary": True,
            "queries": [
                {"query": bp.PROBE_QUERIES[0], "status": "ok",
                 "result_count": 3, "blocked_reason": None, "ms": 130,
                 "sample_titles": ["T1", "T2", "T3"]},
                {"query": bp.PROBE_QUERIES[1], "status": "ok",
                 "result_count": 2, "blocked_reason": None, "ms": 120,
                 "sample_titles": ["T1", "T2"]},
            ],
            "ms_total": 250,
            "ok": True,
        },
        "persian": {
            "probe": "persian",
            "temporary": True,
            "sites": {
                "javanmelody": {"reachable": True, "http": 200,
                                "search_hits": 2, "mp3_head": 200,
                                "mp3_ms": 120, "download_ms": 900,
                                "error": None},
            },
            "ms_total": 1020,
            "ok": True,
        },
        "ok": True,
    }
    handler_cls = webpanel.make_handler(
        ":memory:", types.SimpleNamespace(web_admin_token="x"))
    orig = webpanel.run_search_probe_sync

    # G16: route returns the combined JSON with all three sections.
    webpanel.run_search_probe_sync = lambda: canned
    try:
        srv, port = _serve_once(handler_cls)
        try:
            status, payload = _get(port, "/brave-probe")
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        webpanel.run_search_probe_sync = orig
    check("G16 route returns combined JSON (brave+google+persian)",
          status == 200 and payload == canned
          and "brave" in payload and "google" in payload
          and "persian" in payload
          and payload["persian"]["probe"] == "persian"
          and payload["probe"] == "search",
          f"status={status}")


def main() -> int:
    asyncio.run(_run_cases())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
