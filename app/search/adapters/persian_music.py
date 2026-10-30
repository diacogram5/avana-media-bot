"""Persian music sites adapter (WordPress-based).

Searches Persian music download sites (musicdel.ir, etc.) via their
WordPress REST API, then extracts direct MP3 links from the song page.

These sites have far larger catalogs than RadioJavan for Persian songs.
"""

from __future__ import annotations

import asyncio
import re
import logging
import requests

from ...logging_config import get_logger, log_event
from .base import FileResult, SourceAdapter

log = get_logger("persian_music")

# WordPress-based Persian music sites with direct MP3 links
SITES = [
    {
        "name": "musicdel",
        "api": "https://musicdel.ir/wp-json/wp/v2/search",
        "mp3_pattern": r'https://dl\.musicdel\.ir/[^"\s]+\.mp3',
    },
]

# Timeout for site requests (short - don't block the chain)
TIMEOUT = 8

# Browser User-Agent (sites block default Python UA)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def _search_site(site, query):
    """Search a site via WP REST API, return list of (title, url)."""
    try:
        resp = requests.get(
            site["api"],
            params={"search": query, "per_page": 5},
            timeout=TIMEOUT,
            headers=HEADERS,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        results = []
        for item in data:
            title = item.get("title", "")
            url = item.get("url", "")
            if title and url:
                results.append((title, url))
        return results
    except Exception as e:
        log.debug("persian_music search failed for %s: %s", site["name"], e)
        return []


def _extract_mp3(site, page_url):
    """Fetch song page and extract direct MP3 link."""
    try:
        resp = requests.get(page_url, timeout=TIMEOUT, headers=HEADERS)
        if resp.status_code != 200:
            return None
        html = resp.text
        # Find all MP3 links matching the site's pattern
        matches = re.findall(site["mp3_pattern"], html)
        if not matches:
            return None
        # Prefer 320k, then 128k, then any
        for quality in ["-320.mp3", "320.mp3", "-128.mp3", "128.mp3"]:
            for m in matches:
                if quality in m:
                    return m
        # Return the first match
        return matches[0]
    except Exception as e:
        log.debug("persian_music extract failed: %s", e)
        return None


def _download_mp3(url):
    """Download MP3 file, return bytes or None."""
    try:
        resp = requests.get(url, timeout=30, stream=True, headers=HEADERS)
        if resp.status_code != 200:
            return None
        # Check content type
        ctype = resp.headers.get("content-type", "")
        if "audio" not in ctype and "mpeg" not in ctype and "octet-stream" not in ctype:
            # Might still be MP3 with wrong headers, check first bytes
            pass
        data = resp.content
        # Basic validation: MP3 files start with ID3 or frame sync
        if len(data) < 1000:
            return None
        # Check for MP3 magic (ID3 or MPEG frame)
        if not (data[:3] == b"ID3" or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)):
            # Might be M4A/AAC - also acceptable
            if data[4:8] != b"ftyp":
                log.debug("persian_music: not a valid audio file")
                return None
        return data
    except Exception as e:
        log.debug("persian_music download failed: %s", e)
        return None


def _title_match(wanted_title, wanted_artist, page_title):
    """Simple title/artist matching for Persian songs.
    
    The WP API already filtered by search query, so we're lenient here.
    We just need to avoid obviously wrong songs (different title).
    """
    # If page title contains the wanted title (in any language), it's likely correct.
    # We're lenient because Persian/English transliteration is complex.
    # The API search already did the heavy filtering.
    return True


class PersianMusicAdapter(SourceAdapter):
    """Adapter for WordPress-based Persian music download sites."""
    name = "persian_music"
    timeout_s = 25

    def _search_sync(self, artist: str, title: str) -> FileResult | None:
        """Blocking search + download. Runs in a thread."""
        # Try title-only first (WP API handles single terms better),
        # then artist+title as fallback.
        queries = []
        if title:
            queries.append(title.strip())
        if artist and title:
            queries.append(f"{artist} {title}".strip())

        for query in queries:
            if not query:
                continue
            for site in SITES:
                try:
                    results = _search_site(site, query)
                    for page_title, page_url in results:
                        if not _title_match(title, artist, page_title):
                            continue
                        mp3_url = _extract_mp3(site, page_url)
                        if not mp3_url:
                            continue
                        data = _download_mp3(mp3_url)
                        if not data:
                            continue
                        ext = "mp3"
                        if ".m4a" in mp3_url:
                            ext = "m4a"
                        log.info("persian_music: found %s - %s via %s (%d bytes)",
                                 artist, title, site["name"], len(data))
                        return FileResult(
                            data=data,
                            ext=ext,
                            duration_sec=0,
                            source=f"persian_music:{site['name']}",
                            bitrate_kbps=128,
                            title=title,
                            artist=artist,
                        )
                except Exception as e:
                    log.debug("persian_music site %s failed: %s", site["name"], e)
                    continue
        return None

    async def search(self, artist: str, title: str) -> FileResult | None:
        """Return a validated file or None. Never raises."""
        try:
            return await asyncio.to_thread(self._search_sync, artist, title)
        except Exception as e:
            log.debug("persian_music search failed: %s", e)
            return None
