"""Song identification from audio via a Shazam signature — no token needed.

This is the method the browser test version proved on the user's exact
reel: the audio of the video is signed and the signature is looked up
against Shazam's catalogue. Three consecutive calls on that reel returned
"Dooset Daram – Mehdi Ahmadvand".

Honesty rules, same as the AudD module:
- No match -> status "not_found". A catalog search result is never
  labelled "identified" here.
- Library missing or lookup error -> status "error". Nothing is invented.
- This uses Shazam's unofficial (reverse-engineered) endpoint via the
  `shazamio` library, so it can be rate-limited or blocked under heavy
  use; AudD (with a token) remains the paid fallback in the handlers.

The `shazamio` import is lazy on purpose: the package still imports and
its tests still run on a machine without that optional dependency; in
that case identification via this path returns "error" instead of a match.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ShazamIdResult:
    status: str  # identified | not_found | error
    title: str = ""
    artist: str = ""
    raw: dict | None = None


def parse_shazam_payload(payload: dict) -> ShazamIdResult:
    """Parse a Shazam v2 recognise response.

    A match looks like {"matches": [...], "track": {"title": ...,
    "subtitle": ...}} where subtitle is the artist. Anything without a
    track title is "not_found" — never a guessed nearest song.
    """
    if not isinstance(payload, dict):
        return ShazamIdResult(status="not_found")
    track = payload.get("track") or {}
    if not isinstance(track, dict):
        return ShazamIdResult(status="not_found")
    title = (track.get("title") or "").strip()
    artist = (track.get("subtitle") or "").strip()
    if not title:
        return ShazamIdResult(status="not_found")
    return ShazamIdResult(status="identified", title=title, artist=artist, raw=track)


async def identify_bytes_shazam(data: bytes) -> ShazamIdResult:
    """Identify raw audio/video bytes via a Shazam signature.

    `data` is the Telegram file bytes or a downloaded media file; the
    library builds the signature from the audio inside it. No token or
    key is required.
    """
    if not data:
        return ShazamIdResult(status="error")
    try:
        from shazamio import Shazam
    except ImportError:
        return ShazamIdResult(status="error")
    try:
        shazam = Shazam()
        try:
            payload = await shazam.recognize(data)
        finally:
            try:
                await shazam.close()
            except Exception:
                pass
        return parse_shazam_payload(payload)
    except Exception:
        return ShazamIdResult(status="error")


async def identify_bytes_shazam_multi(data: bytes) -> ShazamIdResult:
    """Try Shazam on several windows of the same file, stop on a match.

    Rationale: a clip with speech over the start, a remix intro, or a
    shortened copy may fail on the full file but match on a later
    window. Slicing container bytes can fail to decode — that only
    yields "error", never a false match, because only an identified
    result (track title from Shazam) is accepted. Bounded to 3 attempts
    so one file cannot burn unbounded lookup time.
    """
    if not data:
        return ShazamIdResult(status="error")
    windows = [data]
    if len(data) > 200_000:
        windows.append(data[len(data) // 3:])
        windows.append(data[2 * len(data) // 3:])
    last = ShazamIdResult(status="error")
    for window in windows:
        result = await identify_bytes_shazam(window)
        if result.status == "identified":
            return result
        last = result
    return last


async def identify_file_shazam(path: str) -> ShazamIdResult:
    """Identify an audio/video file on disk via a Shazam signature."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return ShazamIdResult(status="error")
    return await identify_bytes_shazam(data)
