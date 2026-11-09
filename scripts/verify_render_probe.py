"""Sandbox tests for the TEMPORARY Persian-sites probe
(app/services/brave_probe.py Persian section + the /brave-probe route,
which now runs Brave, Google AND Persian).

No network beyond localhost, no Telegram, no real Turso, no account /
API key / payment anywhere. The HTTP layer is faked throughout.

Cases:
  R1  All 16 sites attempted; per-site result has the required fields
      (reachable, http, search_hits, mp3_head, mp3_ms, download_ms,
      error) and only the documented extras (note on radiojavan,
      ms_total).
  R2  WP search OK with Latin hits: search_hits counted, NO Persian
      fallback (1 search call), mp3 HEAD 200 + prefix download,
      reachable True, error None (javanmelody).
  R3  Latin 0 hits -> Persian fallback attempted (2 search calls),
      hits counted; mp3 HEAD 404 -> download_ms None,
      error mp3:http_404 (msbmusic).
  R4  Search HTTP 429 -> http 429, search_hits 0, error
      search:http_429, NO Persian fallback (1 call, musicdel).
  R5  Search transport error -> reachable False, http None,
      error search:transport:ConnectError (musics-fa).
  R6  Search OK but mp3 HEAD transport error -> mp3_head None,
      download_ms None, error mp3:transport:ConnectError, reachable
      True (search answered; fazremix).
  R7  mp3 HEAD 405 -> Range-GET fallback: mp3_head 206,
      mp3_ms == download_ms (musicviral).
  R8  Prefix download capped at 256 KiB even when the server offers
      more (javanmelody stream: 10 x 64 KiB offered, 256 KiB read).
  R9  RadioJavan home GET 200 -> reachable True, http 200,
      search_hits None, mp3_head None, note present, error None.
  R10 ok flag: True only when >=1 site has hits AND >=1 mp3 HEAD is
      200 (canned run True; all-fail run False; hits-but-no-mp3 run
      False).
  R11 Constants: per-check timeout 10.0s, prefix 262144 B; 16 sites;
      every site has a WP URL or a home URL; every mp3 URL is https.
  R12 No key/account/payment anywhere in brave_probe.py (source scan,
      same forbidden list as the Google suite).
  R13 /brave-probe route returns the combined JSON with brave + google
      + persian sections (real local HTTP server,
      run_search_probe_sync monkeypatched — no outbound traffic).
  R14 run_search_probe calls Brave (2), then Google (2), then Persian;
      total calls: 27 GET + 5 HEAD + 3 stream in the canned run.

Run: python3 scripts/verify_render_probe.py ; exit 0 = all PASS.
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
    def __init__(self, status_code: int, text: str = "", url: str | None = None):
        self.status_code = status_code
        self.text = text
        self.url = url

    def json(self):
        return json.loads(self.text)


class _FakeStreamResp:
    """Fake httpx stream response: async CM with aiter_bytes."""

    def __init__(self, status_code: int, chunks: list[bytes]):
        self.status_code = status_code
        self.chunks = list(chunks)
        self.consumed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_bytes(self, chunk_size=None):
        for chunk in self.chunks:
            self.consumed += len(chunk)
            yield chunk


_SITE_OF_URL = {}
for _name, _wp, _mp3, _home in bp._PERSIAN_SITES:
    if _wp:
        _SITE_OF_URL[_wp] = (_name, "wp")
    if _mp3:
        _SITE_OF_URL[_mp3] = (_name, "mp3")
    if _home:
        _SITE_OF_URL[_home] = (_name, "home")


def _wp_json(hits: list[str]) -> str:
    return json.dumps(
        [{"title": t, "url": f"https://example.ir/{i}/", "type": "post",
          "subtype": "post"} for i, t in enumerate(hits)],
        ensure_ascii=False)


class _CannedPersian:
    """Fake httpx client routing by URL for the canned (mixed) run.

    Behaviours: javanmelody Latin 2 hits + mp3 200; melonmusic Latin
    1 hit + mp3 200; fazremix Latin 1 hit + mp3 HEAD transport error;
    msbmusic Latin 0 -> Persian 1 hit + mp3 404; musicviral Latin 1
    hit + mp3 HEAD 405 (Range fallback); musicdel search 429;
    beys/mifa/minevisam/musicsweb/mokhtalef/gisomusic Latin 0 ->
    Persian 0; download1music Latin 1 hit; musics-fa search transport
    error; ezmusic search transport error; radiojavan home 200.
    """

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.streams: list[_FakeStreamResp] = []

    def _wp(self, name: str, query: str):
        if name == "javanmelody":
            return _FakeResp(200, _wp_json(["A", "B"]))
        if name == "melonmusic":
            return _FakeResp(200, _wp_json(["A"]))
        if name == "fazremix":
            return _FakeResp(200, _wp_json(["A"]))
        if name == "msbmusic":
            if query == bp._PERSIAN_QUERY_LATIN:
                return _FakeResp(200, _wp_json([]))
            return _FakeResp(200, _wp_json(["A"]))
        if name == "musicviral":
            return _FakeResp(200, _wp_json(["A"]))
        if name == "musicdel":
            return _FakeResp(429, "Too Many Requests")
        if name in ("beys-music", "mifa-music", "minevisam",
                    "musicsweb", "mokhtalefmusic", "gisomusic"):
            return _FakeResp(200, _wp_json([]))
        if name == "download1music":
            return _FakeResp(200, _wp_json(["A"]))
        if name == "musics-fa":
            # httpx 0.28 / httpcore 1.x expose no RemoteDisconnected
            # (the sweep's name came from the requests-based fetcher);
            # any TransportError subclass exercises the same branch.
            raise httpx.ConnectError("closed")
        if name == "ezmusic":
            raise httpx.ConnectError("boom")
        raise AssertionError(f"unexpected wp site {name}")

    async def get(self, url, params=None, headers=None):
        self.calls.append(("GET", url, params))
        if url == bp.BRAVE_SEARCH_URL:
            return _FakeResp(429, "Too Many Requests")
        if url == bp.GOOGLE_SEARCH_URL:
            return _FakeResp(429, "Too Many Requests")
        site = _SITE_OF_URL.get(url)
        if site is None:
            raise AssertionError(f"unexpected GET url {url}")
        name, kind = site
        if kind == "home":
            return _FakeResp(200, "<html>Radio Javan</html>")
        return self._wp(name, (params or {}).get("search", ""))

    async def head(self, url, headers=None):
        self.calls.append(("HEAD", url, None))
        site = _SITE_OF_URL.get(url)
        if site is None:
            raise AssertionError(f"unexpected HEAD url {url}")
        name, _kind = site
        if name == "javanmelody":
            return _FakeResp(200, "")
        if name == "melonmusic":
            return _FakeResp(200, "")
        if name == "fazremix":
            raise httpx.ConnectError("boom")
        if name == "msbmusic":
            return _FakeResp(404, "Not Found")
        if name == "musicviral":
            return _FakeResp(405, "Method Not Allowed")
        raise AssertionError(f"unexpected mp3 site {name}")

    def stream(self, method, url, headers=None):
        self.calls.append(("STREAM", url, None))
        site = _SITE_OF_URL.get(url)
        if site is None:
            raise AssertionError(f"unexpected STREAM url {url}")
        name, _kind = site
        if name not in ("javanmelody", "melonmusic", "musicviral"):
            raise AssertionError(f"unexpected stream site {name}")
        resp = _FakeStreamResp(206, [b"x" * 65536] * 10)
        self.streams.append(resp)
        return resp


class _AllFailPersian:
    """Every search 429, every mp3 HEAD 404, home 403."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    async def get(self, url, params=None, headers=None):
        self.calls.append(("GET", url, params))
        site = _SITE_OF_URL.get(url)
        if site is not None and site[1] == "home":
            return _FakeResp(403, "Forbidden")
        return _FakeResp(429, "Too Many Requests")

    async def head(self, url, headers=None):
        self.calls.append(("HEAD", url, None))
        return _FakeResp(404, "Not Found")

    def stream(self, method, url, headers=None):
        self.calls.append(("STREAM", url, None))
        raise AssertionError("stream must not run when HEAD is 404")


class _HitsNoMp3Persian:
    """Every search returns 1 hit; every mp3 HEAD 404."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    async def get(self, url, params=None, headers=None):
        self.calls.append(("GET", url, params))
        site = _SITE_OF_URL.get(url)
        if site is not None and site[1] == "home":
            return _FakeResp(200, "<html>ok</html>")
        return _FakeResp(200, _wp_json(["A"]))

    async def head(self, url, headers=None):
        self.calls.append(("HEAD", url, None))
        return _FakeResp(404, "Not Found")

    def stream(self, method, url, headers=None):
        self.calls.append(("STREAM", url, None))
        raise AssertionError("stream must not run when HEAD is 404")


def _calls_for(client, method: str, url: str) -> int:
    return sum(1 for m, u, _p in client.calls if m == method and u == url)


def _wp_url(name: str) -> str:
    for n, wp, _mp3, _home in bp._PERSIAN_SITES:
        if n == name:
            return wp or ""
    raise KeyError(name)


async def _run_cases() -> None:
    client = _CannedPersian()
    result = await bp.run_persian_probe(client=client)
    sites = result["sites"]

    # R1: all 16 sites, required fields.
    expected_names = {n for n, _w, _m, _h in bp._PERSIAN_SITES}
    required = {"reachable", "http", "search_hits", "mp3_head",
                "mp3_ms", "download_ms", "error"}
    check("R1 all 16 sites present with required fields",
          set(sites) == expected_names
          and all(required <= set(s) for s in sites.values())
          and all(set(s) <= required | {"note", "ms_total"}
                  for s in sites.values()),
          f"sites={sorted(sites)}")

    # R2: javanmelody — Latin hits, no fallback, mp3 200 + download.
    j = sites["javanmelody"]
    check("R2 javanmelody: Latin hits, no fallback, mp3 200 + download",
          j["search_hits"] == 2 and j["http"] == 200
          and _calls_for(client, "GET", _wp_url("javanmelody")) == 1
          and j["mp3_head"] == 200
          and isinstance(j["mp3_ms"], int) and j["mp3_ms"] >= 0
          and isinstance(j["download_ms"], int) and j["download_ms"] >= 0
          and j["reachable"] is True and j["error"] is None,
          f"j={j}")

    # R3: msbmusic — Latin 0 -> Persian fallback, mp3 404.
    m = sites["msbmusic"]
    check("R3 msbmusic: Persian fallback, mp3 404, no download",
          m["search_hits"] == 1
          and _calls_for(client, "GET", _wp_url("msbmusic")) == 2
          and m["mp3_head"] == 404 and m["download_ms"] is None
          and m["error"] == "mp3:http_404",
          f"m={m}")

    # R4: musicdel 429 — no fallback.
    d = sites["musicdel"]
    check("R4 musicdel 429: no retry, error recorded",
          d["http"] == 429 and d["search_hits"] == 0
          and d["error"] == "search:http_429"
          and _calls_for(client, "GET", _wp_url("musicdel")) == 1,
          f"d={d}")

    # R5: musics-fa transport error.
    f = sites["musics-fa"]
    check("R5 musics-fa transport error -> unreachable",
          f["reachable"] is False and f["http"] is None
          and f["error"] == "search:transport:ConnectError",
          f"f={f}")

    # R6: fazremix search ok, mp3 HEAD transport error.
    z = sites["fazremix"]
    check("R6 fazremix: search ok, mp3 HEAD transport error",
          z["search_hits"] == 1 and z["mp3_head"] is None
          and z["download_ms"] is None
          and z["error"] == "mp3:transport:ConnectError"
          and z["reachable"] is True,
          f"z={z}")

    # R7: musicviral HEAD 405 -> Range fallback (mp3_ms == download_ms).
    v = sites["musicviral"]
    check("R7 musicviral HEAD 405 -> Range fallback 206",
          v["mp3_head"] == 206
          and v["mp3_ms"] == v["download_ms"]
          and isinstance(v["download_ms"], int)
          and v["error"] is None,
          f"v={v}")

    # R8: prefix download capped at 256 KiB.
    check("R8 prefix download capped at 256 KiB",
          all(s.consumed == 262144 for s in client.streams)
          and len(client.streams) == 3,
          f"consumed={[s.consumed for s in client.streams]}")

    # R9: radiojavan home GET.
    r = sites["radiojavan"]
    check("R9 radiojavan home GET only",
          r["reachable"] is True and r["http"] == 200
          and r["search_hits"] is None and r["mp3_head"] is None
          and r["error"] is None and "note" in r,
          f"r={r}")

    # R10: ok flag.
    check("R10a canned run ok True (hits + mp3 200)",
          result["ok"] is True and result["probe"] == "persian"
          and result["temporary"] is True
          and isinstance(result["ms_total"], int),
          f"ok={result['ok']}")
    allfail = await bp.run_persian_probe(client=_AllFailPersian())
    check("R10b all-fail run ok False",
          allfail["ok"] is False
          and all(s["error"] is not None for s in allfail["sites"].values()),
          f"allfail ok={allfail['ok']}")
    hitsnomp3 = await bp.run_persian_probe(client=_HitsNoMp3Persian())
    check("R10c hits but no mp3 200 -> ok False",
          hitsnomp3["ok"] is False,
          f"ok={hitsnomp3['ok']}")

    # R11: constants + site table sanity.
    check("R11 constants + site table sanity",
          bp.PERSIAN_CHECK_TIMEOUT_S == 10.0
          and bp._PERSIAN_PREFIX_BYTES == 262144
          and len(bp._PERSIAN_SITES) == 16
          and all(w or h for _n, w, _m, h in bp._PERSIAN_SITES)
          and all((m is None or m.startswith("https://"))
                  for _n, _w, m, _h in bp._PERSIAN_SITES),
          f"sites={len(bp._PERSIAN_SITES)}")

    # R12: no key/account/payment in the probe module.
    src = (ROOT / "app" / "services" / "brave_probe.py").read_text()
    lowered = src.lower()
    forbidden = ("api_key", "apikey", "password", "authorization",
                 "x-api-key", "googleapis", "oauth", "client_secret",
                 "rapidapi", "secret", "credential")
    hits = [f for f in forbidden if f in lowered]
    check("R12 no key/account/payment in probe module",
          not hits, f"hits={hits}")

    # R13 + R14: route + combined run order.
    _run_route_cases()
    client2 = _CannedPersian()
    combined = await bp.run_search_probe(client=client2)
    gets = [u for m, u, _p in client2.calls if m == "GET"]
    heads = [u for m, u, _p in client2.calls if m == "HEAD"]
    streams = [u for m, u, _p in client2.calls if m == "STREAM"]
    check("R14 run_search_probe: Brave, Google, then Persian",
          combined["probe"] == "search"
          and "persian" in combined and combined["persian"]["ok"] is True
          and combined["ok"] is False  # both engines 429 in the fake
          and gets[:4] == [bp.BRAVE_SEARCH_URL, bp.BRAVE_SEARCH_URL,
                           bp.GOOGLE_SEARCH_URL, bp.GOOGLE_SEARCH_URL]
          and len(gets) == 27 and len(heads) == 5 and len(streams) == 3,
          f"gets={len(gets)} heads={len(heads)} streams={len(streams)}")


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
        "brave": {"probe": "brave", "temporary": True, "queries": [],
                  "ms_total": 0, "ok": False},
        "google": {"probe": "google", "temporary": True, "queries": [],
                   "ms_total": 0, "ok": False},
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
        "ok": False,
    }
    handler_cls = webpanel.make_handler(
        ":memory:", types.SimpleNamespace(web_admin_token="x"))
    orig = webpanel.run_search_probe_sync

    # R13: route returns the combined JSON with all three sections.
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
    check("R13 route returns combined JSON (brave+google+persian)",
          status == 200 and payload == canned
          and payload["persian"]["probe"] == "persian"
          and payload["persian"]["ok"] is True,
          f"status={status}")


def main() -> int:
    asyncio.run(_run_cases())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
