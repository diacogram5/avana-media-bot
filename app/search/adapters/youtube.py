"""YouTube adapter (via yt-dlp).

Universal coverage but slow (~15-25s) and typically 128k. Tries up to 5
search candidates; skips videos that block datacenter IPs.
"""

from __future__ import annotations

import asyncio

from ..youtube_full import find_youtube_audio
from .base import FileResult, SourceAdapter


class YouTubeAdapter(SourceAdapter):
    name = "youtube"
    timeout_s = 60

    async def search(self, artist: str, title: str) -> FileResult | None:
        res = await find_youtube_audio(artist, title)
        if not res:
            return None
        return FileResult(
            data=res["data"], ext=res["ext"],
            duration_sec=res["duration_sec"], source="youtube",
            bitrate_kbps=128,  # YouTube bestaudio is typically ~128k AAC
            title=title, artist=artist)
