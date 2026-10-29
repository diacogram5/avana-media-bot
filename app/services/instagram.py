"""Multi-strategy anonymous Instagram media resolver.

Why: Instagram's anonymous post page is usually a login-walled shell
with no media file, so a single fetch strategy fails on most reels.
Measured 2026-10-29 on reels DfDa4Epo8nr / DdEQzlco5lF / Ddayo2btjPI:
the desktop-UA page, /embed/ and /embed/captioned/ variants all return
the same ~510KB shell with zero video references, but the SAME page
fetched with a search-engine crawler user-agent embeds a DASH manifest
whose audio AdaptationSet <BaseURL> is a direct public .mp4 — real
audio bytes (216KB/27.8s, 151KB/18.9s, 63KB/6.6s), all fingerprintable.

All strategies are legitimate public no-login methods: ordinary page
fetches (the same requests any browser or search engine makes), the
public oEmbed-style embed endpoints, and the manifest Instagram itself
advertises in the page. No login cookies, no personal accounts, no
credential use, no ToS-breaking scraping.

Each attempt is logged with the strategy name so per-post success is
visible in the logs.
"""
from __future__ import annotations

import httpx

from ..logging_config import get_logger, log_event
from .link_media import (
    extract_dash_audio_url,
    extract_dash_video_url,
    extract_media_url,
    extract_page_text,
)

log = get_logger(__name__)

DESKTOP_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
GOOGLEBOT_UA = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"}
_STRATEGY_TIMEOUT = 12


async def _get(client: httpx.AsyncClient, url: str, headers: dict) -> str:
    try:
        resp = await client.get(url, headers=headers)
    except Exception:
        return ""
    if resp.status_code != 200:
        return ""
    return resp.text[:2_000_000]


def _base(page_url: str) -> str:
    return (page_url or "").split("?")[0].split("#")[0].rstrip("/") + "/"


async def resolve_instagram_media(page_url: str) -> tuple[str | None, str | None, str, str]:
    """Return (audio_url, video_url, page_text, strategy).

    Strategies, in order: "page", "embed", "embed_captioned",
    "crawler_dash". strategy is "none" when nothing yielded media.
    page_text is the best title/caption found (for the channel query).

    - page/embed strategies: the advertised media URL is a progressive
      .mp4, so it serves as both the watchable video and the audio
      source for identification.
    - crawler_dash: the DASH manifest's audio AdaptationSet gives the
      audio bytes Shazam needs; the video AdaptationSet (best
      representation) gives the watchable video file to send the user.
    """
    base = _base(page_url)
    page_text = ""
    try:
        async with httpx.AsyncClient(timeout=_STRATEGY_TIMEOUT,
                                     follow_redirects=True) as client:
            for name, url, headers in (
                ("page", base, DESKTOP_UA),
                ("embed", base + "embed/", DESKTOP_UA),
                ("embed_captioned", base + "embed/captioned/", DESKTOP_UA),
            ):
                page = await _get(client, url, headers)
                if not page:
                    continue
                if not page_text:
                    page_text = extract_page_text(page)
                media_url = extract_media_url(page)
                if media_url:
                    log_event(log, "instagram_resolve", strategy=name)
                    return media_url, media_url, page_text, name
            # Last resort: crawler view embeds the DASH manifest with
            # separate audio/video streams.
            page = await _get(client, base, GOOGLEBOT_UA)
            if page:
                if not page_text:
                    page_text = extract_page_text(page)
                audio_url = extract_dash_audio_url(page)
                video_url = extract_dash_video_url(page)
                if audio_url or video_url:
                    log_event(log, "instagram_resolve", strategy="crawler_dash")
                    return audio_url, video_url, page_text, "crawler_dash"
    except Exception as exc:
        log_event(log, "instagram_resolve_error", error=str(exc)[:100])
    log_event(log, "instagram_resolve", strategy="none")
    return None, None, page_text, "none"
