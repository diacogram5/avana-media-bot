"""Deezer search provider — free, no key. Second catalog source.

Kept beside iTunes so a failure here never breaks iTunes, and vice
versa. Only metadata + preview URLs are used, never a full file.
"""
from __future__ import annotations

import httpx

from . import Provider, SearchResult

API_URL = "https://api.deezer.com/search"


def parse_deezer_payload(payload: dict) -> list[SearchResult]:
    out: list[SearchResult] = []
    for item in payload.get("data", []) or []:
        title = item.get("title") or ""
        if not title:
            continue
        artist = (item.get("artist") or {}).get("name", "")
        out.append(SearchResult(
            source="deezer", id=str(item.get("id", "")),
            title=title, artist=artist,
            duration=item.get("duration"),
            thumbnail=(item.get("album") or {}).get("cover", ""),
            url=item.get("link", ""), preview_url=item.get("preview", "") or "",
            kind="music"))
    return out


class DeezerProvider(Provider):
    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(name="deezer", enabled=True, priority=20)
        self._client = client

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        params = {"q": query}
        if self._client is not None:
            resp = await self._client.get(API_URL, params=params, timeout=15)
            resp.raise_for_status()
            return parse_deezer_payload(resp.json())[:limit]
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(API_URL, params=params)
            resp.raise_for_status()
            return parse_deezer_payload(resp.json())[:limit]
