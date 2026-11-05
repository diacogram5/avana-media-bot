"""iTunes Search API provider — free, no API key required.

Official endpoint: https://itunes.apple.com/search
Only metadata + the official preview/collection URLs are used. Nothing is
downloaded from iTunes here; Telegram sends only what the Bot API allows
(a lawful direct file or, otherwise, the official link).
"""
from __future__ import annotations

import httpx

from . import Provider, SearchResult

API_URL = "https://itunes.apple.com/search"


def parse_itunes_payload(payload: dict) -> list[SearchResult]:
    results: list[SearchResult] = []
    for item in payload.get("results", []) or []:
        track = item.get("trackName") or item.get("collectionName") or ""
        if not track:
            continue
        duration_ms = item.get("trackTimeMillis")
        results.append(
            SearchResult(
                source="itunes",
                id=str(item.get("trackId") or item.get("collectionId") or ""),
                title=track,
                artist=item.get("artistName", ""),
                duration=int(duration_ms // 1000) if duration_ms else None,
                thumbnail=item.get("artworkUrl100", ""),
                url=item.get("trackViewUrl") or item.get("collectionViewUrl") or "",
                preview_url=item.get("previewUrl", "") or "",
                kind="music",
            )
        )
    return results


class ITunesProvider(Provider):
    """One source, two entity kinds: song (default) or musicVideo."""

    def __init__(self, client: httpx.AsyncClient | None = None, entity: str = "song"):
        super().__init__(name="itunes", enabled=True, priority=10)
        self._client = client
        self.entity = entity

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        params = {"term": query, "media": "music", "entity": self.entity,
                  "limit": max(1, min(limit, 50))}
        if self._client is not None:
            response = await self._client.get(API_URL, params=params, timeout=15)
            response.raise_for_status()
            return parse_itunes_payload(response.json())
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(API_URL, params=params)
            response.raise_for_status()
            return parse_itunes_payload(response.json())
