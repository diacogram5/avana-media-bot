"""Search provider interface + engine.

Every provider is independent: it can be enabled/disabled, has a priority
and a rate limit, logs its own errors, and its failure never breaks the
other providers. The engine de-duplicates and sorts by relevance.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SearchResult:
    source: str
    id: str
    title: str
    artist: str = ""
    duration: int | None = None
    thumbnail: str = ""
    url: str = ""
    preview_url: str = ""  # legal catalog preview, if the source provides one
    kind: str = "music"  # music | video

    def dedup_key(self) -> str:
        return f"{self.source}:{self.id}" if self.id else f"{self.source}:{self.title.lower()}:{self.artist.lower()}"


@dataclass
class Provider:
    name: str
    enabled: bool = True
    priority: int = 100
    rate_limit_per_minute: int = 30

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        raise NotImplementedError

    @property
    def available(self) -> bool:
        return self.enabled


def dedup(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    out: list[SearchResult] = []
    for item in results:
        key = item.dedup_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def relevance_score(query: str, item: SearchResult) -> int:
    """Simple, explainable relevance: exact/word matches in title/artist."""
    q = query.lower().strip()
    title = item.title.lower()
    artist = item.artist.lower()
    score = 0
    if q and q in title:
        score += 10
    if q and q in artist:
        score += 8
    for word in q.split():
        if word in title:
            score += 2
        if word in artist:
            score += 1
    return score


def sort_by_relevance(query: str, results: list[SearchResult]) -> list[SearchResult]:
    return sorted(results, key=lambda r: relevance_score(query, r), reverse=True)


def paginate(items: list, page: int, page_size: int = 5) -> tuple[list, int]:
    """Return (page_items, total_pages). Page is 1-based and clamped."""
    if page_size <= 0:
        page_size = 5
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return items[start:start + page_size], total_pages


class SearchEngine:
    def __init__(self, providers: list[Provider]):
        self.providers = sorted(
            [p for p in providers if p.enabled], key=lambda p: p.priority
        )

    async def search(self, query: str, limit: int = 10) -> tuple[list[SearchResult], list[str]]:
        """Search all enabled providers. Returns (results, failed_provider_names).

        A provider raising an exception is recorded and skipped; it never
        aborts the whole search.
        """
        all_results: list[SearchResult] = []
        failed: list[str] = []
        for provider in self.providers:
            try:
                all_results.extend(await provider.search(query, limit=limit))
            except Exception:
                failed.append(provider.name)
        results = sort_by_relevance(query, dedup(all_results))
        return results[:limit], failed
