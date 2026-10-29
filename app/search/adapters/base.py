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


async def search_parallel(adapters: list[SourceAdapter],
                          artist: str, title: str,
                          first_win: bool = True) -> FileResult | None:
    """Run adapters in parallel. If first_win, return the first valid file
    without waiting for the rest. Otherwise wait for all and pick best."""
    if not adapters:
        return None
    if first_win:
        # Return as soon as any adapter delivers a valid file.
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
        results = await asyncio.gather(
            *[_run_one(a, artist, title) for a in adapters],
            return_exceptions=True)
        valid = [r for r in results if isinstance(r, FileResult)]
        if not valid:
            return None
        # Highest real bitrate wins (honest quality, no upscaling).
        valid.sort(key=lambda r: r.bitrate_kbps, reverse=True)
        return valid[0]
