"""Modular source adapters for full-song file search.

Each adapter is independent: one failing never blocks the others.
The orchestrator runs them in parallel and returns the first valid file.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol

from ...logging_config import get_logger, log_event

log = get_logger("search_adapters")


@dataclass
class FileResult:
    """A validated full-song file ready to send."""
    data: bytes
    ext: str
    duration_sec: float
    source: str          # adapter name, e.g. "wordpress:musicsweb.ir"
    bitrate_kbps: int = 0
    title: str = ""
    artist: str = ""


@dataclass
class AdapterStats:
    """Per-adapter performance stats (for the audit)."""
    name: str
    calls: int = 0
    hits: int = 0
    errors: int = 0
    total_ms: int = 0

    @property
    def hit_rate(self) -> float:
        return self.hits / self.calls if self.calls else 0.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0


class SourceAdapter(Protocol):
    """Interface every file-source adapter implements."""
    name: str
    timeout_s: int

    async def search(self, artist: str, title: str) -> FileResult | None:
        """Return a validated file or None. Never raises."""
        ...


# Global stats registry (in-memory; reset on restart)
_stats: dict[str, AdapterStats] = {}


def get_stats(name: str) -> AdapterStats:
    if name not in _stats:
        _stats[name] = AdapterStats(name=name)
    return _stats[name]


def all_stats() -> list[AdapterStats]:
    return list(_stats.values())


async def _run_one(adapter: SourceAdapter, artist: str, title: str,
                   ) -> FileResult | None:
    """Run one adapter with timeout and stats. Never raises."""
    st = get_stats(adapter.name)
    st.calls += 1
    t0 = time.time()
    try:
        res = await asyncio.wait_for(
            adapter.search(artist, title), timeout=adapter.timeout_s)
        ms = int((time.time() - t0) * 1000)
        st.total_ms += ms
        if res:
            st.hits += 1
            log_event(log, "adapter", stage="hit", adapter=adapter.name,
                      ms=ms, bytes=len(res.data))
        else:
            log_event(log, "adapter", stage="miss", adapter=adapter.name,
                      ms=ms)
        return res
    except asyncio.TimeoutError:
        st.errors += 1
        log_event(log, "adapter", stage="timeout", adapter=adapter.name,
                  timeout_s=adapter.timeout_s)
        return None
    except Exception as exc:
        st.errors += 1
        log_event(log, "adapter", stage="error", adapter=adapter.name,
                  error=f"{type(exc).__name__}: {str(exc)[:60]}")
        return None


async def _score_result(res: FileResult, artist: str, title: str) -> float:
    """Score a file by how well it matches the requested song.
    Accuracy outranks speed: a slower exact match beats a fast near-miss."""
    from ...bot.logic import token_overlap_ratio, identified_version_tokens
    if not res:
        return 0.0
    # Use the file's REAL metadata (not the request) for scoring
    ft = (res.title or "").lower()
    fa = (res.artist or "").lower()
    wt = title.strip().lower()
    wa = artist.strip().lower()

    title_score = token_overlap_ratio(wt, ft)
    artist_score = token_overlap_ratio(wa, fa) if wa else 0.5

    # Version must match (remix vs original etc.)
    want_ver = set(identified_version_tokens(title))
    got_ver = set(identified_version_tokens(res.title or ""))
    version_ok = (want_ver == got_ver)

    score = title_score * 0.5 + artist_score * 0.4
    if not version_ok:
        score -= 0.3
    # Prefer higher real bitrate as tiebreaker (honest quality)
    score += min(res.bitrate_kbps / 1000, 0.1)
    return score


async def search_parallel(adapters: list[SourceAdapter],
                          artist: str, title: str,
                          first_win: bool = False) -> FileResult | None:
    """Run adapters in parallel and pick the BEST match, not the fastest.

    Accuracy outranks speed: all adapters run concurrently, then results
    are scored by real title/artist/version matching. The highest-scoring
    valid file wins. first_win=True is kept for backward compat but
    defaults to False — a fast wrong file is worse than a slow right one.
    """
    if not adapters:
        return None
    if first_win:
        # Legacy: return as soon as any adapter delivers.
        # WARNING: fastest ≠ most accurate. Prefer first_win=False.
        pending = {asyncio.create_task(_run_one(a, artist, title))
                   for a in adapters}
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    res = t.result()
                    if res:
                        for p in pending:
                            p.cancel()
                        return res
            return None
        finally:
            for p in pending:
                p.cancel()
    else:
        # Accuracy-first: wait for all, score each, pick best match.
        results = await asyncio.gather(
            *[_run_one(a, artist, title) for a in adapters],
            return_exceptions=True)
        valid = [r for r in results if isinstance(r, FileResult)]
        if not valid:
            return None
        scored = [(_score_result_sync(r, artist, title), r) for r in valid]
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best = scored[0]
        log_event(log, "adapter", stage="best_pick",
                  adapter=best.source, score=round(best_score, 2),
                  candidates=len(valid))
        return best


def _score_result_sync(res: FileResult, artist: str, title: str) -> float:
    """Sync wrapper for scoring (called after gather completes)."""
    from ...bot.logic import token_overlap_ratio, identified_version_tokens
    ft = (res.title or "").lower()
    fa = (res.artist or "").lower()
    wt = title.strip().lower()
    wa = artist.strip().lower()
    title_score = token_overlap_ratio(wt, ft)
    artist_score = token_overlap_ratio(wa, fa) if wa else 0.5
    want_ver = set(identified_version_tokens(title))
    got_ver = set(identified_version_tokens(res.title or ""))
    score = title_score * 0.5 + artist_score * 0.4
    if want_ver != got_ver:
        score -= 0.3
    score += min(res.bitrate_kbps / 1000, 0.1)
    return score
