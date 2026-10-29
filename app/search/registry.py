"""Extensible source registry for all search capabilities.

A source is a plain record::

    {"name", "kind", "fetch", "priority", "timeout", "status", "enabled"}

`kind` is one of:
    info      text/metadata search results (NEVER a file)
    identify  audio fingerprinting / content matching
    file      full downloadable audio files (gated by the caller)
    similar   similar-artist / similar-song recommendations
    lyrics    lyrics text (+ translation)

Rules (enforced by design, not convention):
  - a failing source NEVER kills the whole search: every fetch runs in
    its own try/except with a per-source timeout, the error is logged,
    the failure is recorded by name, and the next source by priority runs.
  - kinds are separate namespaces. Text/info-only sources are never
    presented as file sources — a `file` lookup only touches `file`
    sources; `info` sources only return metadata.
  - no cap on the number of sources; adding one needs no core change —
    just a `register()` call (or the `@source` decorator).

Sync fetch callables are supported and wrapped; async ones are awaited.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("avano.sources")

KINDS = ("info", "identify", "file", "similar", "lyrics")

# Provenance of a registration: "proven" (live probe returned real
# results), "unverified" (registered on spec/API docs, not yet probed
# live), "dead" (probed and failing — kept for the audit trail, not run).
STATUS_PROVEN = "proven"
STATUS_UNVERIFIED = "unverified"
STATUS_DEAD = "dead"


@dataclass
class Source:
    name: str
    kind: str
    fetch: Callable[..., Any]
    priority: int = 100
    timeout: float = 15.0
    enabled: bool = True
    status: str = STATUS_UNVERIFIED
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown source kind: {self.kind!r} (expected one of {KINDS})")
        if not callable(self.fetch):
            raise TypeError(f"source {self.name!r}: fetch must be callable")


_REGISTRY: dict[str, Source] = {}


def register(name: str, kind: str, fetch: Callable[..., Any], *,
             priority: int = 100, timeout: float = 15.0,
             enabled: bool = True, status: str = STATUS_UNVERIFIED,
             note: str = "") -> Source:
    """Register (or replace) a source. This is the whole API for adding a
    new source — no core changes needed anywhere else."""
    src = Source(name=name, kind=kind, fetch=fetch, priority=priority,
                 timeout=timeout, enabled=enabled, status=status, note=note)
    _REGISTRY[name] = src
    return src


def source(name: str, kind: str, **kwargs):
    """Decorator form: @source("name", "info", priority=10)."""
    def deco(fn):
        register(name, kind, fn, **kwargs)
        return fn
    return deco


def unregister(name: str) -> bool:
    return _REGISTRY.pop(name, None) is not None


def get(name: str) -> Source | None:
    return _REGISTRY.get(name)


def set_status(name: str, status: str, note: str = "") -> None:
    src = _REGISTRY.get(name)
    if src is None:
        raise KeyError(name)
    src.status = status
    if note:
        src.note = note


def sources(kind: str | None = None, *, include_disabled: bool = False) -> list[Source]:
    """All sources (or one kind), lowest priority first. Dead sources are
    never returned — they stay registered only as an audit trail."""
    out = [s for s in _REGISTRY.values()
           if (kind is None or s.kind == kind)
           and (include_disabled or s.enabled)
           and s.status != STATUS_DEAD]
    return sorted(out, key=lambda s: (s.priority, s.name))


async def _call(src: Source, *args, **kwargs) -> Any:
    if inspect.iscoroutinefunction(src.fetch):
        coro = src.fetch(*args, **kwargs)
    else:
        loop = asyncio.get_running_loop()
        coro = loop.run_in_executor(None, lambda: src.fetch(*args, **kwargs))
    return await asyncio.wait_for(coro, timeout=src.timeout)


async def fetch_all(kind: str, *args, **kwargs) -> tuple[dict[str, Any], list[str]]:
    """Run every enabled source of `kind` in priority order.

    Returns (results_by_name, failed_names). A source raising (or timing
    out) is logged, recorded in failed_names, and the next source runs —
    a failing source never kills the search.
    """
    results: dict[str, Any] = {}
    failed: list[str] = []
    for src in sources(kind):
        try:
            results[src.name] = await _call(src, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — isolation is the point
            log.warning("source %r (%s) failed: %s", src.name, src.kind, exc)
            failed.append(src.name)
    return results, failed


async def collect(kind: str, *args, **kwargs) -> tuple[list, list[str]]:
    """fetch_all() for list-returning kinds (info, similar): flattens the
    per-source result lists, preserving priority order."""
    results, failed = await fetch_all(kind, *args, **kwargs)
    items: list = []
    for name in [s.name for s in sources(kind)]:
        val = results.get(name)
        if isinstance(val, list):
            items.extend(val)
    return items, failed


async def first_hit(kind: str, *args, **kwargs) -> tuple[Any | None, str | None, list[str]]:
    """First non-empty result in priority order. For file/identify/lyrics
    kinds where the first success wins. Returns (result, source_name, failed)."""
    failed: list[str] = []
    for src in sources(kind):
        try:
            val = await _call(src, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — isolation is the point
            log.warning("source %r (%s) failed: %s", src.name, src.kind, exc)
            failed.append(src.name)
            continue
        if val:
            return val, src.name, failed
    return None, None, failed


def audit() -> list[dict]:
    """Full registry snapshot for reporting: name -> kind -> priority ->
    status. Includes dead sources (they are the rejection audit trail)."""
    return [
        {"name": s.name, "kind": s.kind, "priority": s.priority,
         "status": s.status, "enabled": s.enabled, "note": s.note}
        for s in sorted(_REGISTRY.values(), key=lambda s: (s.kind, s.priority, s.name))
    ]
