"""Parallel orchestrator for full-song file search (v2 architecture).

Replaces the sequential find_full_song_file with modular adapters run in
parallel. First valid file wins — no waiting for slow sources.
"""

from __future__ import annotations

from .base import FileResult, search_parallel
from .radiojavan import RadioJavanAdapter
from .wordpress import WordPressAdapter
from .youtube import YouTubeAdapter


def _adapters() -> list:
    """All file-source adapters, fastest/most-reliable first."""
    return [
        RadioJavanAdapter(),  # Persian music, direct downloads, ~3-10s
        WordPressAdapter(),   # Iranian catalogs, real MP3, ~4s
        # YouTube removed 2026-10-30: "Sign in to confirm you're not a bot"
        # = complete IP block from Render. Wasted 3-4s per query.
    ]


async def find_full_song_file_v2(artist: str, title: str) -> dict | None:
    """Return {"data","ext","duration_sec","source"} or None.

    Same contract as find_full_song_file, but via parallel adapters.
    """
    res: FileResult | None = await search_parallel(
        _adapters(), artist, title, first_win=False)
    if not res:
        return None
    return {"data": res.data, "ext": res.ext,
            "duration_sec": res.duration_sec, "source": res.source}
