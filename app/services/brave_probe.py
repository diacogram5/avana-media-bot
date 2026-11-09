"""TEMPORARY probe: does Brave Search HTML work from the Render host?

This module exists ONLY to answer one question before any smart-search
work: is ``search.brave.com`` reachable and usable from the bot's own
runtime (Render free tier, Oregon, shared datacenter IPv4)? Sandbox
evidence is NOT enough — the sandbox fetcher runs on a different egress
IP, and datacenter IPs get bot treatment elsewhere (YouTube is fully
IP-blocked from Render, Instagram returns 429s).

What it does: fetches ``https://search.brave.com/search?q=<query>``
with a normal browser User-Agent, 15s timeout per query, for exactly
two queries (one Persian, one Latin — the Taziyane test case). It parses
the HTML minimally (regex over result links/titles, no new parser dep)
and classifies each query as ok / blocked / error.

What it does NOT do: no Brave API key, no account, no payment, no
Google, no storage of results (only the per-query summary — status,
count, first 3 titles — is returned/logged; search results are never
persisted anywhere). No DB access, no user data, no state changes.

TEMPORARY: the final smart-search implementation MUST remove this
module and its /brave-probe route. If Brave is blocked or unstable on
Render, the answer is reported and NO paid alternative is pursued.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re
import time
from urllib.parse import quote_plus

import httpx

from ..logging_config import get_logger, log_event

log = get_logger(__name__)

BRAVE_SEARCH_URL = "https://search.brave.com/search"
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

# <a href="https://...">inner html</a> — minimal result-link scan.
_LINK_RE = re.compile(
    r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
# Brave-internal / non-result hosts — never counted as results.
_SKIP_HOSTS = ("brave.com", "search.brave.com")


def _clean_title(raw: str) -> str:
    """Strip tags/entities/whitespace from one link's inner HTML."""
    text = _TAG_RE.sub("", raw or "")
    text = _html.unescape(text)
    return " ".join(text.split())


def _is_result_link(url: str) -> bool:
    lowered = url.lower()
    if any(host in lowered for host in _SKIP_HOSTS):
        return False
    return True


def parse_result_links(html_text: str, limit: int = 20) -> list[dict]:
    """Extract distinct external result links (url + title) from HTML.

    Minimal regex scan — no parser dependency. Returns at most ``limit``
    entries, in document order.
    """
    seen: set[str] = set()
    results: list[dict] = []
    for match in _LINK_RE.finditer(html_text or ""):
        url = match.group(1).strip()
        if not _is_result_link(url) or url in seen:
            continue
        title = _clean_title(match.group(2))
        if not title:
            continue
        seen.add(url)
        results.append({"url": url, "title": title})
        if len(results) >= limit:
            break
    return results


def classify_block(html_text: str, status_code: int) -> str | None:
    """Return a blocked_reason string, or None when not blocked.

    Checks the HTTP status first, then bot-check markers in the body,
    then emptiness (a 200 with no extractable content is a block too).
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


async def probe_one_query(
    query: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Probe one Brave query. Returns the per-query summary dict."""
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=QUERY_TIMEOUT_S, trust_env=False)
    started = time.monotonic()
    try:
        resp = await client.get(
            BRAVE_SEARCH_URL,
            params={"q": query},
            headers=_HEADERS,
        )
        ms = int((time.monotonic() - started) * 1000)
        body = resp.text or ""
        blocked_reason = classify_block(body, resp.status_code)
        if blocked_reason is not None:
            status = "blocked"
            result_count = 0
            sample_titles: list[str] = []
        else:
            links = parse_result_links(body)
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
        "brave_probe",
        query=query,
        status=result["status"],
        result_count=result["result_count"],
        blocked_reason=result["blocked_reason"],
        ms=result["ms"],
    )
    return result


async def run_brave_probe(
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Run the full two-query probe (sequential, 15s per query).

    Returns ``{"probe": "brave", "temporary": True, "queries": [...],
    "ms_total": int, "ok": bool}`` — ``ok`` is True only when BOTH
    queries returned usable results. No results are stored anywhere.
    """
    started = time.monotonic()
    queries = [
        await probe_one_query(query, client=client)
        for query in PROBE_QUERIES
    ]
    total_ms = int((time.monotonic() - started) * 1000)
    result = {
        "probe": "brave",
        "temporary": True,
        "queries": queries,
        "ms_total": total_ms,
        "ok": all(q["status"] == "ok" for q in queries),
    }
    log_event(log, "brave_probe_done", ok=result["ok"], ms_total=total_ms)
    return result


def run_brave_probe_sync() -> dict:
    """Sync wrapper for the stdlib http.server route (no loop there)."""
    return asyncio.run(run_brave_probe())
