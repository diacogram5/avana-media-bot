"""Smart catalog search: full/partial name, artist, or a lyric line.

Strategy (all against the same legal iTunes catalog, previews only):
1. the exact query
2. if the query looks like a lyric line (>3 words), its first 4 words
3. the query with filler words stripped
Results are labelled "search results", NEVER "identified" — only an
audio-fingerprint match may use that word. If no variant matches, the
caller must report "not found", never return an unrelated nearest song
as if it were the requested one.
"""
from __future__ import annotations

from . import SearchEngine, SearchResult

_FILLERS = {"آهنگ", "ترانه", "موزیک", "song", "music"}


def search_variants(query: str) -> list[str]:
    q = " ".join((query or "").split())
    variants = [q] if q else []
    words = q.split()
    if len(words) > 3:
        short = " ".join(words[:4])
        if short not in variants:
            variants.append(short)
    stripped = " ".join(w for w in words if w.lower() not in _FILLERS)
    if stripped and stripped not in variants:
        variants.append(stripped)
    return variants


async def smart_search(engine: SearchEngine, query: str,
                       limit: int = 10) -> tuple[list[SearchResult], str]:
    """Return (results, matched_variant). Empty results => ('', no match)."""
    for variant in search_variants(query):
        results, _failed = await engine.search(variant, limit=limit)
        if results:
            return results, variant
    return [], ""
