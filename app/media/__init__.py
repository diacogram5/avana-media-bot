"""Link processing + media sending rules.

Policy, enforced in code (not just in docs):
- Only allowlisted domains or direct audio/video file URLs (see security.py).
- Private/internal IPs and SSRF targets are blocked before any fetch.
- No DRM bypass, no paywall/auth bypass, no private/protected content.
- If a source cannot be legally or technically fetched, return the
  official source link + a clear explanation — never a fake file.
- Telegram limits are respected: max file size (default 50 MB, Telegram
  Bot API limit for uploads is also 50 MB) and a concurrency semaphore.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

from ..security import validate_link

TELEGRAM_BOT_UPLOAD_LIMIT_MB = 50


@dataclass
class LinkOutcome:
    ok: bool
    message: str
    official_url: str = ""
    kind: str = ""
    title: str = ""
    downloadable: bool = False
    direct_url: str = ""


def process_link(url: str, max_file_mb: int = 50) -> LinkOutcome:
    """Classify a link. This stage does NOT download protected content.

    - YouTube/Instagram/Aparat: metadata fetch requires the platform's
      official API / public page; without it we return the official link.
    - Direct file URL: marked downloadable only in principle; the actual
      download (in sender.py) re-validates, checks size, and cleans up.
    """
    validation = validate_link(url)
    if not validation.ok:
        if validation.reason in ("private_host_blocked", "private_ip_blocked", "resolves_to_private_ip"):
            return LinkOutcome(False, "link_blocked", url, validation.kind)
        return LinkOutcome(False, "link_invalid", url, validation.kind)
    if validation.kind == "direct_file":
        return LinkOutcome(
            True, "direct_file", url, "direct_file", downloadable=True, direct_url=url
        )
    # Platform links: official link is always the safe, lawful fallback.
    return LinkOutcome(
        True, "link_not_downloadable", url, validation.kind,
        downloadable=False,
    )


def within_telegram_limit(size_bytes: int | None, max_file_mb: int = 50) -> bool:
    if size_bytes is None:
        return True  # unknown size: caller must check again after download
    limit = min(max_file_mb, TELEGRAM_BOT_UPLOAD_LIMIT_MB) * 1024 * 1024
    return size_bytes <= limit


def cleanup_temp_files(directory: str, older_than_seconds: int = 3600) -> int:
    """Delete temp files older than the retention window. Returns count."""
    removed = 0
    if not os.path.isdir(directory):
        return 0
    now = time.time()
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and now - os.path.getmtime(path) > older_than_seconds:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


class DownloadLimiter:
    """Async semaphore wrapper so concurrent downloads stay bounded."""

    def __init__(self, max_concurrent: int = 2):
        self._sem = asyncio.Semaphore(max(1, max_concurrent))

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc):
        self._sem.release()
        return False
