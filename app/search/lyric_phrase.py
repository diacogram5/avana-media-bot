"""Lyric-phrase search: accurate, never a nearest guess.

Pipeline (cheap first, stop on exact verification):
1. Catalog variants of the phrase (smart variants).
2. Persian lyric-site search (musics-fa) for phrase variants; a post
   title is only a candidate, never a result.
3. A candidate is returned ONLY if its fetched lyrics actually contain
   the phrase (overlap >= 0.60, see lyrics.lyric_overlap) AND the
   catalog confirms the same title+artist.
If no candidate verifies, the result is empty — the caller must say
so briefly and offer similar-song search, never a nearest song.

All network callables are injectable so tests run offline; the live
pipeline uses the defaults below.
"""
from __future__ import annotations

import html
import re

import httpx

from . import SearchResult
from .lyrics import lyric_overlap, lyric_variants


def parse_site_titles(page_html: str) -> list[str]:
    titles = []
    for m in re.finditer(r"<h2[^>]*>.*?<a[^>]*>(.*?)</a>", page_html or "", re.S):
        t = re.sub(r"<[^>]+>", "", html.unescape(m.group(1))).strip()
        if t:
            titles.append(t)
    return titles


async def default_site_search(phrase: str) -> list[str]:
    """Search musics-fa (WordPress search, post content includes lyrics)."""
    out: list[str] = []
    for variant in lyric_variants(phrase)[:3]:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                resp = await c.get("https://musics-fa.com/", params={"s": variant},
                                   headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
                if resp.status_code != 200:
                    continue
                for t in parse_site_titles(resp.text):
                    if t not in out:
                        out.append(t)
        except Exception:
            continue
    return out


def _candidate_titles_from_post(post_title: str) -> list[str]:
    t = post_title
    for prefix in ("دانلود آهنگ ", "دانلود ریمیکس ", "دانلود "):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    return [t] if t else []


async def lyric_phrase_search(phrase: str, catalog_search, lyric_fetcher,
                              site_search=None) -> list[SearchResult]:
    """Return verified catalog results for a lyric phrase, or [].

    catalog_search(query) -> list[SearchResult]
    lyric_fetcher(title, artist) -> lyrics str | None
    site_search(phrase) -> list[str] post titles
    """
    if not (phrase or "").strip():
        return []
    site_search = site_search or default_site_search
    seen: set[str] = set()
    candidates: list[SearchResult] = []

    async def _add(results):
        for r in results or []:
            key = f"{r.title.lower()}|{r.artist.lower()}"
            if key not in seen:
                seen.add(key)
                candidates.append(r)

    # 1. Catalog variants of the phrase itself.
    for variant in lyric_variants(phrase):
        try:
            await _add(await catalog_search(variant))
        except Exception:
            continue
    # 2. Site posts -> catalog lookup of the post title.
    try:
        posts = await site_search(phrase)
    except Exception:
        posts = []
    for post in posts[:8]:
        for cand_title in _candidate_titles_from_post(post):
            try:
                await _add(await catalog_search(cand_title))
            except Exception:
                continue
    # 3. Verify: lyrics must actually contain the phrase.
    verified: list[SearchResult] = []
    for cand in candidates[:30]:
        try:
            lyrics = await lyric_fetcher(cand.title, cand.artist)
        except Exception:
            lyrics = None
        if lyrics and lyric_overlap(phrase, lyrics) >= 0.60:
            verified.append(cand)
    return verified
