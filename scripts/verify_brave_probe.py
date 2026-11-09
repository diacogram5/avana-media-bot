"""Sandbox tests for the TEMPORARY Brave probe (app/services/brave_probe.py
+ the /brave-probe route in app/webpanel.py).

No network beyond localhost, no Telegram, no real Turso, no Brave API
key/account/payment anywhere. The HTTP layer is faked throughout.

Cases:
  B1  Both probe queries are attempted (Persian + Latin), exactly 2
      calls, to search.brave.com/search.
  B2  OK page: external result links counted, first 3 titles sampled,
      blocked_reason None, ms present.
  B3  Captcha body -> blocked / captcha.
  B4  HTTP 403 -> blocked / http_403.
  B5  HTTP 429 -> blocked / http_429.
  B6  Empty 200 body -> blocked / empty.
  B7  200 with no result links -> blocked / no_results.
  B8  Timeout -> error / timeout.
  B9  Transport error -> error / transport:<TypeName>.
  B10 No key/account/payment anywhere in brave_probe.py (source scan:
      no api_key/API_KEY/password/Authorization/x-api-key, no
      api.search.brave.com).
  B11 Brave-internal links skipped, duplicates deduped.
  B12 run_brave_probe: ok True only when BOTH queries ok; False when
      one is blocked.
  B13 /brave-probe route returns the probe JSON (real local HTTP server,
      run_search_probe_sync monkeypatched — no outbound traffic).
  B14 Second concurrent call while a probe runs returns {"busy": true}
      (carrying the last result); a later call runs the probe fresh.

Run: python3 scripts/verify_brave_probe.py ; exit 0 = all PASS.
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
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Fake httpx.AsyncClient: handler(query) -> _FakeResp or raises."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict | None]] = []

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        return self.handler(params["q"] if params else "")


_OK_HTML = (
    '<html><body>'
    '<a href="https://example.com/song1">Song One Title</a>'
    '<a href="https://music.example.org/song2">Song Two <b>Title</b></a>'
    '<a href="https://dl.example.net/song3">Song Three Title</a>'
    '<a href="https://example.com/song4">Song Four Title</a>'
    '</body></html>'
)
_CAPTCHA_HTML = (
    '<html><body><h1>Please verify you are not a robot</h1>'
    '<div class="captcha-challenge">captcha</div></body></html>'
)
_NORESULT_HTML = '<html><body><p>nothing here at all</p></body></html>'


def _ok_handler(query):
    return _FakeResp(200, _OK_HTML)


def _mixed_handler(query):
    if "تازیانه" in query:
        return _FakeResp(200, _OK_HTML)
    return _FakeResp(403, "Forbidden")


async def _run_cases() -> None:
    # B1: both queries attempted, exactly 2 calls, right endpoint.
    client = _FakeClient(_ok_handler)
    result = await bp.run_brave_probe(client=client)
    check("B1 both queries attempted",
          len(client.calls) == 2
          and [c[1]["q"] for c in client.calls] == list(bp.PROBE_QUERIES)
          and all(c[0] == bp.BRAVE_SEARCH_URL for c in client.calls),
          f"calls={client.calls}")

    # B2: OK page classification.
    q0 = result["queries"][0]
    check("B2 ok page classified",
          q0["status"] == "ok" and q0["result_count"] == 4
          and q0["blocked_reason"] is None
          and q0["sample_titles"] == [
              "Song One Title", "Song Two Title", "Song Three Title"]
          and isinstance(q0["ms"], int) and q0["ms"] >= 0,
          f"q0={q0}")
    check("B2b run ok flag + ms_total",
          result["ok"] is True and result["probe"] == "brave"
          and result["temporary"] is True
          and isinstance(result["ms_total"], int),
          f"result={result}")

    # B3–B9: one-query classifications.
    async def _one(handler):
        return await bp.probe_one_query("probe", client=_FakeClient(handler))

    r = await _one(lambda q: _FakeResp(200, _CAPTCHA_HTML))
    check("B3 captcha body -> blocked/captcha",
          r["status"] == "blocked" and r["blocked_reason"] == "captcha"
          and r["result_count"] == 0, f"r={r}")

    r = await _one(lambda q: _FakeResp(403, "Forbidden"))
    check("B4 http 403 -> blocked/http_403",
          r["status"] == "blocked" and r["blocked_reason"] == "http_403",
          f"r={r}")

    r = await _one(lambda q: _FakeResp(429, "Too Many Requests"))
    check("B5 http 429 -> blocked/http_429",
          r["status"] == "blocked" and r["blocked_reason"] == "http_429",
          f"r={r}")

    r = await _one(lambda q: _FakeResp(200, ""))
    check("B6 empty 200 body -> blocked/empty",
          r["status"] == "blocked" and r["blocked_reason"] == "empty",
          f"r={r}")

    r = await _one(lambda q: _FakeResp(200, _NORESULT_HTML))
    check("B7 200 no result links -> blocked/no_results",
          r["status"] == "blocked" and r["blocked_reason"] == "no_results",
          f"r={r}")

    def _raise_timeout(q):
        raise httpx.ConnectTimeout("timed out")
    r = await _one(_raise_timeout)
    check("B8 timeout -> error/timeout",
          r["status"] == "error" and r["blocked_reason"] == "timeout",
          f"r={r}")

    def _raise_transport(q):
        raise httpx.ConnectError("boom")
    r = await _one(_raise_transport)
    check("B9 transport error -> error/transport",
          r["status"] == "error"
          and r["blocked_reason"] == "transport:ConnectError",
          f"r={r}")

    # B10: no key / account / payment anywhere in the probe module.
    src = (ROOT / "app" / "services" / "brave_probe.py").read_text()
    lowered = src.lower()
    forbidden = ("api_key", "apikey", "password", "authorization",
                 "x-api-key", "api.search.brave.com", "rapidapi",
                 "secret", "credential")
    hits = [f for f in forbidden if f in lowered]
    check("B10 no key/account/payment in probe module",
          not hits, f"hits={hits}")

    # B11: Brave-internal links skipped, duplicates deduped.
    dup_html = (
        '<a href="https://search.brave.com/search?q=x">Brave Search</a>'
        '<a href="https://brave.com/privacy">Privacy</a>'
        '<a href="https://example.com/a">First</a>'
        '<a href="https://example.com/a">First again</a>'
        '<a href="https://example.com/b"></a>'
    )
    links = bp.parse_result_links(dup_html)
    check("B11 internal links skipped, dupes deduped",
          [l["url"] for l in links] == ["https://example.com/a"],
          f"links={links}")

    # B12: ok only when BOTH queries ok.
    mixed = await bp.run_brave_probe(client=_FakeClient(_mixed_handler))
    check("B12 mixed ok+blocked -> ok False",
          mixed["ok"] is False
          and mixed["queries"][0]["status"] == "ok"
          and mixed["queries"][1]["status"] == "blocked",
          f"mixed={mixed}")

    # B13 + B14: the real route over localhost, probe monkeypatched.
    _run_route_cases()


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
                {"query": bp.PROBE_QUERIES[0], "status": "ok",
                 "result_count": 3, "blocked_reason": None, "ms": 120,
                 "sample_titles": ["T1", "T2", "T3"]},
                {"query": bp.PROBE_QUERIES[1], "status": "ok",
                 "result_count": 2, "blocked_reason": None, "ms": 110,
                 "sample_titles": ["T1", "T2"]},
            ],
            "ms_total": 230,
            "ok": True,
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
        "ok": True,
    }
    handler_cls = webpanel.make_handler(
        ":memory:", types.SimpleNamespace(web_admin_token="x"))
    orig = webpanel.run_search_probe_sync

    # B13: route returns the probe JSON.
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
    check("B13 route returns probe JSON",
          status == 200 and payload == canned, f"status={status}")

    # B14: concurrent second call -> busy; later call -> last result.
    slow_calls: list[float] = []
    import time as _time

    def _slow():
        slow_calls.append(_time.monotonic())
        _time.sleep(0.6)
        return canned

    webpanel.run_search_probe_sync = _slow
    try:
        srv, port = _serve_once(handler_cls)
        try:
            results: list[tuple[int, dict]] = []

            def _hit():
                results.append(_get(port, "/brave-probe"))

            t1 = threading.Thread(target=_hit)
            t2 = threading.Thread(target=_hit)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            status3, payload3 = _get(port, "/brave-probe")
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        webpanel.run_search_probe_sync = orig
    busy_seen = any(p.get("busy") is True for _, p in results)
    busy_last_ok = any(
        p.get("busy") is True and p.get("last") == canned
        for _, p in results)
    done_seen = any(p == canned for _, p in results)
    # slow_calls == 2 is correct: one run for the concurrent pair, and
    # the third call (lock free) runs the probe fresh again.
    check("B14 concurrent call returns busy, later call re-runs probe",
          busy_seen and busy_last_ok and done_seen and len(slow_calls) == 2
          and status3 == 200 and payload3 == canned,
          f"results={results} slow_calls={len(slow_calls)}")


def main() -> int:
    asyncio.run(_run_cases())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
