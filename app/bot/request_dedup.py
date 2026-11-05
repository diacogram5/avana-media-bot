"""Concurrent request deduplication (Requirement #6).

If multiple users request the same content simultaneously, only perform
the expensive operation (download, transcoding) once, and share the result.

Uses asyncio locks keyed by content identifier. Each lock is held only
during the expensive operation, then released. Waiting coroutines get
the cached result.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict

# In-memory locks for in-flight requests
# Key: content identifier (e.g., "song:artist|title" or "url:https://...")
# Value: (lock, result, timestamp)
_inflight: Dict[str, tuple[asyncio.Lock, object, float]] = {}

# How long to keep completed results for late joiners (seconds)
_RESULT_TTL = 30

def _cleanup():
    """Remove stale entries."""
    now = time.time()
    stale = [k for k, (_, _, ts) in _inflight.items() if now - ts > _RESULT_TTL]
    for k in stale:
        _inflight.pop(k, None)

async def deduplicated(key: str, coro_factory):
    """Execute coro_factory() deduplicated by key.
    
    If another coroutine is already processing the same key, wait for it
    and return its result instead of doing duplicate work.
    
    Args:
        key: Unique identifier for the operation
        coro_factory: Zero-arg async callable that performs the work
        
    Returns:
        The result of coro_factory(), either computed or shared.
    """
    _cleanup()
    
    # Check if already in-flight
    if key in _inflight:
        lock, result, _ = _inflight[key]
        # Wait for the in-flight operation to complete
        async with lock:
            # Re-check after acquiring lock (result should be set)
            if key in _inflight:
                _, result, _ = _inflight[key]
                return result
        # If not in _inflight anymore, fall through to compute
    
    # Create new entry
    lock = asyncio.Lock()
    _inflight[key] = (lock, None, time.time())
    
    async with lock:
        try:
            result = await coro_factory()
            # Store result for late joiners
            _inflight[key] = (lock, result, time.time())
            return result
        except Exception:
            # Remove failed entry so retries can proceed
            _inflight.pop(key, None)
            raise
    # Note: entry remains in _inflight for _RESULT_TTL for late joiners,
    # _cleanup() will remove it eventually.
