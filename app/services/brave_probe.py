"""TEMPORARY probe: do Brave / Google Search HTML work from the Render host?

This module exists ONLY to answer one question before any smart-search
work: is web search reachable and usable from the bot's own runtime
(Render free tier, Oregon, shared datacenter IPv4)? Sandbox evidence
is NOT enough — the sandbox fetcher runs on a different egress IP, and
datacenter IPs get bot treatment elsewhere (YouTube is fully IP-blocked
from Render, Instagram returns 429s).

Engines (same per-query result shape for both):
- Brave: ``https://search.brave.com/search?q=<query>`` — ALREADY PROVEN
  BLOCKED on Render (2026-11-09 19:11: http_429 on both Persian and
  Latin Taziyane queries, twice, instant rejections). Brave stays in
  the probe only as the emergency fallback reference.
- Google: ``https://www.google.com/search?q=<query>`` — PRIORITY 1, the
  real page the bot's «🌐 جستجو در Google» button links to (same
  construction: ``"https://www.google.com/search?q="`` +
  ``urllib.parse.quote_plus(name)`` — see _video_result_kb in
  app/bot/handlers.py). The probe fetches the real Google results
  page with a normal browser User-Agent, 15s timeout per query, for
  exactly two queries (one Persian, one Latin — the Taziyane test
  case). Redirects are followed (httpx does NOT follow them by
  default, so follow_redirects=True is set explicitly) and the FINAL
  page is classified: a consent redirect (final host
  consent.google.com, or "Before you continue" / "consent" in the
  body) is a block, not a result page.

Result links are parsed minimally (regex over <a href> links, no new
parser dep). Google serves its result links as ``/url?q=<target>``
redirects, so those are unwrapped to their real http(s) target before
the google.com-internal skip list is applied; plain absolute links
are counted the same way as for Brave.

What it does NOT do: no account, no API key, no payment, no Google
or Brave keyed API, NO CAPTCHA bypass, no storage of results (only
the per-query summary — status, count, first 3 titles — is returned /
logged; search results are never persisted anywhere). No DB access,
no user data, no state changes.

TEMPORARY: the final smart-search implementation MUST remove this
module and its /brave-probe route. If no search engine works on
Render, the answer is reported and NO paid alternative is pursued.

Persian-sites section (free-only smart search, 2026-11-09): no search
engine works from any egress we can use (Google + Brave http_429 on
Render; every other free HTML search endpoint CAPTCHAs or 403s). The
only verified free path is direct Persian music-site adapters
(WordPress REST API search + direct mp3 links). This section answers,
from the Render host, per site: is the WP search reachable, does the
Taziyane query return hits, does the known direct mp3 answer a HEAD,
and how fast is a bounded prefix download. Endpoints come from the
read-only study ~/workspace/free-only-study/ (REPORT.md + sweep.json)
— nothing here is guessed. Sites with no known direct mp3 only get
the WP-search check; RadioJavan has no WP API, so it only gets a
home-page GET (the bot's RadioJavan path uses the radiojavanapi
package, not raw HTTP). Per-check timeout 10s, sequential, NO retries
on 429/403 (the code is recorded, not hammered). The mp3 prefix
download reads at most 256 KiB (Range request; if the server ignores
Range, the stream is cut after 256 KiB) — a speed sample, never a
full file. Nothing is stored.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from ..logging_config import get_logger, log_event

log = get_logger(__name__)

BRAVE_SEARCH_URL = "https://search.brave.com/search"
GOOGLE_SEARCH_URL = "https://www.google.com/search"
QUERY_TIMEOUT_S = 15.0

# The exact two probe queries (Taziyane test case, Persian + Latin).
PROBE_QUERIES: tuple[str, str] = (
    "تازیانه ریمیکس امیر تتلو",
    "Taziyane Remix Amir Tataloo",
)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
}

# Signals of a bot-check / block page inside the returned HTML.
_CAPTCHA_MARKERS = (
    "captcha",
    "are you a robot",
    "verify you are human",
    "confirm you are not a robot",
    "cf-challenge",
    "challenge-platform",
    "please verify",
)
_GOOGLE_CAPTCHA_MARKERS = (
    "unusual traffic",
    "not a robot",
    "are you a robot",
    "verify you are human",
    "captcha",
)
# Signals of Google's consent page (final page after a redirect).
_GOOGLE_CONSENT_MARKERS = (
    "before you continue",
    "consent",
)

# <a href="...">inner html</a> — minimal result-link scan.
_HREF_RE = re.compile(
    r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
# Brave-internal / non-result hosts — never counted as results.
_SKIP_HOSTS = ("brave.com", "search.brave.com")
# Google-internal / non-result hosts — never counted as results.
_GOOGLE_SKIP_HOSTS = (
    "google.com",
    "www.google.com",
    "consent.google.com",
    "maps.google.com",
    "accounts.google.com",
)


def _clean_title(raw: str) -> str:
    """Strip tags/entities/whitespace from one link's inner HTML."""
    text = _TAG_RE.sub("", raw or "")
    text = _html.unescape(text)
    return " ".join(text.split())


def _unwrap_href(href: str) -> str | None:
    """Return the external http(s) URL behind one href, or None.

    Plain absolute links pass through. Google serves result links as
    ``/url?q=<target>`` redirects — unwrap the ``q`` param to the real
    target. Anything else (relative, javascript:, #anchor) is not a
    result link.
    """
    href = (href or "").strip()
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("/url?"):
        try:
            qs = parse_qs(urlparse(_html.unescape(href)).query)
        except Exception:
            return None
        target = (qs.get("q") or [None])[0]
        if target:
            target = unquote(target).strip()
            if target.startswith(("http://", "https://")):
                return target
    return None


def _is_result_link(url: str, skip_hosts: tuple[str, ...]) -> bool:
    lowered = url.lower()
    if any(host in lowered for host in skip_hosts):
        return False
    return True


def parse_result_links(
    html_text: str,
    limit: int = 20,
    skip_hosts: tuple[str, ...] | None = None,
) -> list[dict]:
    """Extract distinct external result links (url + title) from HTML.

    Minimal regex scan — no parser dependency. Returns at most ``limit``
    entries, in document order. ``skip_hosts`` defaults to the Brave
    internal list; pass the Google list for Google pages.
    """
    if skip_hosts is None:
        skip_hosts = _SKIP_HOSTS
    seen: set[str] = set()
    results: list[dict] = []
    for match in _HREF_RE.finditer(html_text or ""):
        url = _unwrap_href(match.group(1))
        if url is None or not _is_result_link(url, skip_hosts):
            continue
        if url in seen:
            continue
        title = _clean_title(match.group(2))
        if not title:
            continue
        seen.add(url)
        results.append({"url": url, "title": title})
        if len(results) >= limit:
            break
    return results


def classify_block(
    html_text: str, status_code: int, final_url: str | None = None,
) -> str | None:
    """Brave: return a blocked_reason string, or None when not blocked.

    Checks the HTTP status first, then bot-check markers in the body,
    then emptiness (a 200 with no extractable content is a block too).
    ``final_url`` is accepted (ignored) so both engines share one
    call shape.
    """
    if status_code == 403:
        return "http_403"
    if status_code == 429:
        return "http_429"
    if 400 <= status_code < 500:
        return f"http_{status_code}"
    if status_code >= 500:
        return f"http_{status_code}"
    body = (html_text or "").lower()
    for marker in _CAPTCHA_MARKERS:
        if marker in body:
            return "captcha"
    if not (html_text or "").strip():
        return "empty"
    return None


def classify_google_block(
    html_text: str, status_code: int, final_url: str | None = None,
) -> str | None:
    """Google: return a blocked_reason string, or None when not blocked.

    Checks the HTTP status first, then the consent page (final URL
    host consent.google.com, or a consent marker in the body), then
    bot-check markers, then emptiness.
    """
    if status_code == 403:
        return "http_403"
    if status_code == 429:
        return "http_429"
    if 400 <= status_code < 500:
        return f"http_{status_code}"
    if status_code >= 500:
        return f"http_{status_code}"
    final_host = ""
    if final_url:
        try:
            final_host = urlparse(str(final_url)).hostname or ""
        except Exception:
            final_host = ""
    if final_host == "consent.google.com":
        return "consent"
    body = (html_text or "").lower()
    for marker in _GOOGLE_CONSENT_MARKERS:
        if marker in body:
            return "consent"
    for marker in _GOOGLE_CAPTCHA_MARKERS:
        if marker in body:
            return "captcha"
    if not (html_text or "").strip():
        return "empty"
    return None


async def _probe_one_query(
    query: str,
    *,
    base_url: str,
    skip_hosts: tuple[str, ...],
    classify,
    event_name: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Probe one query against one engine. Per-query summary dict."""
    own_client = client is None
    if own_client:
        # httpx does NOT follow redirects by default; the probe must
        # classify the FINAL page (e.g. Google's consent redirect).
        client = httpx.AsyncClient(
            timeout=QUERY_TIMEOUT_S, trust_env=False, follow_redirects=True,
        )
    started = time.monotonic()
    try:
        resp = await client.get(
            base_url,
            params={"q": query},
            headers=_HEADERS,
        )
        ms = int((time.monotonic() - started) * 1000)
        body = resp.text or ""
        final_url = getattr(resp, "url", None)
        blocked_reason = classify(body, resp.status_code, final_url)
        if blocked_reason is not None:
            status = "blocked"
            result_count = 0
            sample_titles: list[str] = []
        else:
            links = parse_result_links(body, skip_hosts=skip_hosts)
            result_count = len(links)
            if result_count == 0:
                status = "blocked"
                blocked_reason = "no_results"
                sample_titles = []
            else:
                status = "ok"
                sample_titles = [link["title"] for link in links[:3]]
        result = {
            "query": query,
            "status": status,
            "result_count": result_count,
            "blocked_reason": blocked_reason,
            "ms": ms,
            "sample_titles": sample_titles,
        }
    except httpx.TimeoutException:
        result = {
            "query": query,
            "status": "error",
            "result_count": 0,
            "blocked_reason": "timeout",
            "ms": int((time.monotonic() - started) * 1000),
            "sample_titles": [],
        }
    except httpx.TransportError as exc:
        result = {
            "query": query,
            "status": "error",
            "result_count": 0,
            "blocked_reason": f"transport:{type(exc).__name__}",
            "ms": int((time.monotonic() - started) * 1000),
            "sample_titles": [],
        }
    finally:
        if own_client:
            await client.aclose()
    log_event(
        log,
        event_name,
        query=query,
        status=result["status"],
        result_count=result["result_count"],
        blocked_reason=result["blocked_reason"],
        ms=result["ms"],
    )
    return result


async def probe_one_query(
    query: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Probe one Brave query. Returns the per-query summary dict."""
    return await _probe_one_query(
        query,
        base_url=BRAVE_SEARCH_URL,
        skip_hosts=_SKIP_HOSTS,
        classify=classify_block,
        event_name="brave_probe",
        client=client,
    )


async def probe_one_google_query(
    query: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Probe one Google query. Returns the per-query summary dict."""
    return await _probe_one_query(
        query,
        base_url=GOOGLE_SEARCH_URL,
        skip_hosts=_GOOGLE_SKIP_HOSTS,
        classify=classify_google_block,
        event_name="google_probe",
        client=client,
    )


async def _run_engine(engine: str, probe_fn, *, client=None) -> dict:
    """Run one engine over both probe queries (sequential).

    Returns ``{"probe": <engine>, "temporary": True, "queries": [...],
    "ms_total": int, "ok": bool}`` — ``ok`` is True only when BOTH
    queries returned usable results. No results are stored anywhere.
    """
    started = time.monotonic()
    queries = [
        await probe_fn(query, client=client)
        for query in PROBE_QUERIES
    ]
    total_ms = int((time.monotonic() - started) * 1000)
    result = {
        "probe": engine,
        "temporary": True,
        "queries": queries,
        "ms_total": total_ms,
        "ok": all(q["status"] == "ok" for q in queries),
    }
    log_event(log, f"{engine}_probe_done", ok=result["ok"], ms_total=total_ms)
    return result


async def run_brave_probe(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Run the full two-query Brave probe (sequential, 15s per query)."""
    return await _run_engine("brave", probe_one_query, client=client)


async def run_google_probe(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Run the full two-query Google probe (sequential, 15s per query)."""
    return await _run_engine("google", probe_one_google_query, client=client)


async def run_search_probe(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Run all three sections (Brave, then Google, then Persian) in one
    probe call.

    Returns ``{"probe": "search", "temporary": True,
    "brave": {...}, "google": {...}, "persian": {...}, "ok": bool}`` —
    the outer ``ok`` is True only when BOTH Google queries returned
    usable results (Google is priority 1; Brave is the emergency
    fallback). The Persian section has its own ``ok`` inside it.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=QUERY_TIMEOUT_S, trust_env=False, follow_redirects=True,
        )
        # Persian checks run on their own 10s-timeout client; a caller
        # that injects one client (tests) shares it for all sections.
        persian_client = httpx.AsyncClient(
            timeout=PERSIAN_CHECK_TIMEOUT_S, trust_env=False,
            follow_redirects=True,
        )
    else:
        persian_client = client
    try:
        brave = await run_brave_probe(client=client)
        google = await run_google_probe(client=client)
        persian = await run_persian_probe(client=persian_client)
    finally:
        if own_client:
            await client.aclose()
            await persian_client.aclose()
    return {
        "probe": "search",
        "temporary": True,
        "brave": brave,
        "google": google,
        "persian": persian,
        "ok": google["ok"],
    }


def run_brave_probe_sync() -> dict:
    """Sync wrapper for the stdlib http.server route (no loop there)."""
    return asyncio.run(run_brave_probe())


def run_search_probe_sync() -> dict:
    """Sync wrapper for the stdlib http.server route (no loop there)."""
    return asyncio.run(run_search_probe())


# ---------------------------------------------------------------------------
# TEMPORARY Persian-sites section (see the module docstring).
# ---------------------------------------------------------------------------

PERSIAN_CHECK_TIMEOUT_S = 10.0
_PERSIAN_PREFIX_BYTES = 262144  # 256 KiB prefix-download sample

# WP-search queries: Latin first (hit on javanmelody / melonmusic /
# fazremix / musicviral), Persian fallback when Latin returns 0 hits
# (msbmusic only answers the Persian query).
_PERSIAN_QUERY_LATIN = "Taziyane"
_PERSIAN_QUERY_FA = "تازیانه"

# (name, wp_search_url, direct_mp3_url, home_url).
# home_url is set ONLY for RadioJavan (no WP API): a home-page GET.
_PERSIAN_SITES: tuple[tuple[str, str | None, str | None, str | None], ...] = (
    ("javanmelody",
     "https://javanmelody.ir/wp-json/wp/v2/search",
     "https://dl.javanmelody.ir/dl/1402/03/Amir-Tataloo-Taziyane-(Remix)-abanmusics.com-320.mp3",
     None),
    ("melonmusic",
     "https://melonmusic.ir/wp-json/wp/v2/search",
     "https://dl.melonmusic.ir/Music/Amir%20Tataloo%20-%20Tazianeh%20%28Remix-320%29.mp3",
     None),
    ("fazremix",
     "https://fazremix.ir/wp-json/wp/v2/search",
     "https://s2.fazremix.ir/2020/03/Amir%20Tataloo%20-%20Taziyane%20Saeed%20Ansar%20Remix.mp3",
     None),
    ("msbmusic",
     "https://msbmusic.ir/wp-json/wp/v2/search",
     "https://vip.msbmusic.ir/1404/06/11/Amir%20tataloo%20-%20Taziyane.mp3",
     None),
    ("musicviral",
     "https://musicviral.ir/wp-json/wp/v2/search",
     "https://dl.musicviral.ir/Music/04-11/Amir%20Tataloo%20-%20Taziyane.mp3",
     None),
    ("musicdel",
     "https://musicdel.ir/wp-json/wp/v2/search",
     None, None),
    ("beys-music",
     "https://beys-music.ir/wp-json/wp/v2/search",
     None, None),
    ("mifa-music",
     "https://mifa-music.ir/wp-json/wp/v2/search",
     None, None),
    ("download1music",
     "https://download1music.ir/wp-json/wp/v2/search",
     None, None),
    ("minevisam",
     "https://minevisam.ir/wp-json/wp/v2/search",
     None, None),
    ("musicsweb",
     "https://musicsweb.ir/wp-json/wp/v2/search",
     None, None),
    ("musics-fa",
     "https://musics-fa.ir/wp-json/wp/v2/search",
     None, None),
    ("ezmusic",
     "https://ezmusic.ir/wp-json/wp/v2/search",
     None, None),
    ("mokhtalefmusic",
     "https://mokhtalefmusic.ir/wp-json/wp/v2/search",
     None, None),
    ("gisomusic",
     "https://gisomusic.ir/wp-json/wp/v2/search",
     None, None),
    ("radiojavan",
     None, None,
     "https://www.radiojavan.com"),
)


async def _persian_wp_search(
    client: httpx.AsyncClient, url: str,
) -> tuple[int | None, int, str | None]:
    """One WP search: Latin, Persian fallback on 0 hits.

    Returns (http_status | None, hit_count, error | None). No retries
    on 429/403 — the code is the answer. 0 hits on BOTH queries is a
    valid answer (error None), not a failure.
    """
    resp = None
    for query in (_PERSIAN_QUERY_LATIN, _PERSIAN_QUERY_FA):
        try:
            resp = await client.get(
                url,
                params={"search": query, "per_page": 5},
                headers=_HEADERS,
            )
        except httpx.TimeoutException:
            return None, 0, "search:timeout"
        except httpx.TransportError as exc:
            return None, 0, f"search:transport:{type(exc).__name__}"
        if resp.status_code != 200:
            return resp.status_code, 0, f"search:http_{resp.status_code}"
        try:
            data = resp.json()
        except Exception:
            return resp.status_code, 0, "search:non_json"
        if not isinstance(data, list):
            return resp.status_code, 0, "search:non_json"
        if len(data) > 0:
            return resp.status_code, len(data), None
        # 0 hits: try the other query once (Latin -> Persian).
    return (resp.status_code if resp is not None else None), 0, None


async def _persian_prefix_get(
    client: httpx.AsyncClient, url: str,
) -> tuple[int | None, int | None, str | None]:
    """Bounded prefix download: at most 256 KiB via Range (stream cut).

    Returns (http_status | None, ms | None, error | None). A server
    that ignores Range is cut after 256 KiB — never a full file.
    """
    started = time.monotonic()
    try:
        async with client.stream(
            "GET", url,
            headers={**_HEADERS,
                     "Range": f"bytes=0-{_PERSIAN_PREFIX_BYTES - 1}"},
        ) as resp:
            status = resp.status_code
            if status >= 400:
                return status, int((time.monotonic() - started) * 1000), (
                    f"mp3:download:http_{status}")
            n = 0
            async for chunk in resp.aiter_bytes(65536):
                n += len(chunk)
                if n >= _PERSIAN_PREFIX_BYTES:
                    break
            return status, int((time.monotonic() - started) * 1000), None
    except httpx.TimeoutException:
        return None, None, "mp3:download:timeout"
    except httpx.TransportError as exc:
        return None, None, f"mp3:download:transport:{type(exc).__name__}"


async def _persian_mp3_check(
    client: httpx.AsyncClient, url: str,
) -> tuple[int | None, int | None, int | None, str | None]:
    """HEAD (+ Range-GET fallback on 405/501) and a prefix download.

    Returns (mp3_head_status | None, mp3_ms | None,
    download_ms | None, error | None). download_ms is None when no
    prefix download ran (HEAD failed). On a 405/501 HEAD the Range GET
    doubles as the head check (mp3_ms == download_ms).
    """
    started = time.monotonic()
    try:
        resp = await client.head(url, headers=_HEADERS)
        head_ms = int((time.monotonic() - started) * 1000)
        head_status = resp.status_code
    except httpx.TimeoutException:
        return None, None, None, "mp3:timeout"
    except httpx.TransportError as exc:
        return None, None, None, f"mp3:transport:{type(exc).__name__}"
    if head_status in (405, 501):
        dl_status, dl_ms, dl_err = await _persian_prefix_get(client, url)
        if dl_err is not None:
            return None, None, None, dl_err
        return dl_status, dl_ms, dl_ms, None
    if head_status >= 400:
        return head_status, head_ms, None, f"mp3:http_{head_status}"
    dl_status, dl_ms, dl_err = await _persian_prefix_get(client, url)
    if dl_err is not None:
        return head_status, head_ms, None, dl_err
    return head_status, head_ms, dl_ms, None


async def _probe_one_persian_site(
    name: str,
    wp_url: str | None,
    mp3_url: str | None,
    home_url: str | None,
    *,
    client: httpx.AsyncClient,
) -> dict:
    """Probe one Persian site. Per-site result dict.

    ``reachable`` means an HTTP response was received at all (a 403 is
    reachable-but-blocking, not unreachable). ``http`` is the WP-search
    (or home-page) status; None when no HTTP response was received for
    that check. ``search_hits`` is None for RadioJavan (no WP API).
    ``error`` is the first failure detail, else None — 0 search hits
    is a valid answer, not an error.
    """
    started = time.monotonic()
    result: dict = {
        "reachable": False,
        "http": None,
        "search_hits": None,
        "mp3_head": None,
        "mp3_ms": None,
        "download_ms": None,
        "error": None,
    }
    if home_url is not None:
        # RadioJavan: no WP API — home-page GET only.
        try:
            resp = await client.get(home_url, headers=_HEADERS)
            result["reachable"] = True
            result["http"] = resp.status_code
            if resp.status_code >= 400:
                result["error"] = f"home:http_{resp.status_code}"
        except httpx.TimeoutException:
            result["error"] = "home:timeout"
        except httpx.TransportError as exc:
            result["error"] = f"home:transport:{type(exc).__name__}"
        result["note"] = (
            "home GET only — no WP API; bot uses radiojavanapi package")
    else:
        http, hits, err = await _persian_wp_search(client, wp_url or "")
        result["http"] = http
        result["search_hits"] = hits
        result["reachable"] = http is not None
        result["error"] = err
        if mp3_url is not None:
            head, head_ms, dl_ms, mp_err = await _persian_mp3_check(
                client, mp3_url)
            result["mp3_head"] = head
            result["mp3_ms"] = head_ms
            result["download_ms"] = dl_ms
            if head is not None:
                result["reachable"] = True
            if result["error"] is None:
                result["error"] = mp_err
    result["ms_total"] = int((time.monotonic() - started) * 1000)
    log_event(
        log, "persian_probe_site", site=name,
        reachable=result["reachable"], http=result["http"],
        search_hits=result["search_hits"], mp3_head=result["mp3_head"],
        ms=result["ms_total"], error=result["error"],
    )
    return result


async def run_persian_probe(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Run the Persian-sites probe (sequential, 10s per check).

    Returns ``{"probe": "persian", "temporary": True, "sites": {...},
    "ms_total": int, "ok": bool}`` — ``ok`` is True only when at least
    one site returned search hits AND at least one direct mp3 answered
    a HEAD with 200.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=PERSIAN_CHECK_TIMEOUT_S, trust_env=False,
            follow_redirects=True,
        )
    started = time.monotonic()
    try:
        sites: dict[str, dict] = {}
        for name, wp_url, mp3_url, home_url in _PERSIAN_SITES:
            sites[name] = await _probe_one_persian_site(
                name, wp_url, mp3_url, home_url, client=client)
    finally:
        if own_client:
            await client.aclose()
    total_ms = int((time.monotonic() - started) * 1000)
    any_hits = any((s.get("search_hits") or 0) > 0 for s in sites.values())
    any_mp3 = any(s.get("mp3_head") == 200 for s in sites.values())
    result = {
        "probe": "persian",
        "temporary": True,
        "sites": sites,
        "ms_total": total_ms,
        "ok": bool(any_hits and any_mp3),
    }
    log_event(log, "persian_probe_done", ok=result["ok"], ms_total=total_ms)
    return result


def run_persian_probe_sync() -> dict:
    """Sync wrapper for the stdlib http.server route (no loop there)."""
    return asyncio.run(run_persian_probe())
