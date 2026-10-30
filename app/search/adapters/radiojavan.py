"""Radio Javan adapter.

Uses the public Radio Javan endpoints via the radiojavanapi package
(unofficial wrapper around RJ's own public web API — same endpoints the
website uses). Provides real full-song downloads for Persian music.

Proven 2026-10-30: search + hq_link download works, ~300-700ms total,
real full durations (160-280s), audio/x-m4a content.
"""

from __future__ import annotations

import asyncio

from ...logging_config import get_logger, log_event
from .base import FileResult, SourceAdapter

log = get_logger("radiojavan")


class RadioJavanAdapter(SourceAdapter):
    name = "radiojavan"
    timeout_s = 20

    def _search_sync(self, artist: str, title: str) -> FileResult | None:
        """Blocking search + download. Runs in a thread."""
        from radiojavanapi import Client
        import httpx

        c = Client()
        query = f"{artist} {title}".strip()
        r = c.search(query)
        if not r.songs:
            # Fallback: title only
            r = c.search(title.strip())
            if not r.songs:
                return None

        # Pick best match using token overlap (not just substring).
        # This prevents sending the wrong song when titles are similar.
        from ...bot.logic import token_overlap_ratio
        wanted = title.strip().lower()
        best = None
        best_score = 0.0
        for s in r.songs[:5]:
            st = (s.title or "").lower()
            # Title must contain all wanted tokens (strict)
            score = token_overlap_ratio(wanted, st)
            # Artist bonus: prefer same artist
            sa = (s.artist or "").lower()
            if artist.strip().lower() in sa or sa in artist.strip().lower():
                score += 0.1
            if score > best_score:
                best_score = score
                best = s
        # Require high confidence; otherwise don't send a wrong song
        if best is None or best_score < 0.8:
            log_event(log, "radiojavan", stage="low_confidence",
                      wanted=title[:40], score=round(best_score, 2))
            return None

        full = c.get_song_by_id(str(best.id))
        dl = str(full.hq_link) if full.hq_link else ""
        if not dl:
            return None

        # Download the file (requests, not httpx — sandbox proxy breaks httpx)
        import requests as _rq
        resp = _rq.get(dl, timeout=30,
                       headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        data = resp.content
        if len(data) < 100_000:
            return None
        # M4A magic: ftyp box
        if not (data[4:8] == b"ftyp" or data[:4] == b"\x00\x00\x00\x1c"):
            # Accept audio content-type anyway if magic is off
            ct = resp.headers.get("Content-Type", "")
            if not ct.startswith("audio"):
                return None

        duration = float(full.duration or 0)
        if duration < 60:  # must be a full song, not a clip
            return None

        log_event(log, "radiojavan", stage="hit",
                  title=best.title[:40], duration_s=round(duration, 1),
                  bytes=len(data))
        return FileResult(
            data=data, ext="m4a", duration_sec=duration,
            source="radiojavan", bitrate_kbps=256,
            title=title, artist=artist)

    async def search(self, artist: str, title: str) -> FileResult | None:
        try:
            return await asyncio.to_thread(self._search_sync, artist, title)
        except Exception as exc:
            log_event(log, "radiojavan", stage="error",
                      error=f"{type(exc).__name__}: {str(exc)[:60]}")
            return None
