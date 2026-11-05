"""Audio-content matching for files the bot has actually seen.

Why this exists (the real cause found in the link path): Shazam/AudD
only know catalog tracks, and the channel engine only matches *text*
in channel posts. An AI song that lives only as a file in a Telegram
channel therefore has no path at all: the Instagram link gives no audio
(the page does not advertise a public media file), the catalog says
not_found, and the channel engine is never even run on a link because
there is no query text. This module closes the file side of that gap.

What it does, and what it honestly does NOT do:
- A file the user sends or forwards (including a forwarded channel
  audio file) is indexed by content: full SHA-256 plus fixed-size
  64 KiB chunk hashes from the start of the file.
- A later upload matches if it is byte-identical OR shares >= 2 chunk
  hashes with a stored file. That covers an identical re-upload and a
  shortened / trimmed copy whose prefix is preserved.
- It does NOT claim to match a speed-changed, heavily re-encoded or
  noisy remix by bytes alone: those cases are handled by the Shazam /
  AudD audio fingerprint (content-based, tried on several windows),
  and a byte match is never passed off as a catalog identification.
  A match here is labelled "channel/file match", not "identified".
- Private channels are never read. A channel file only enters the
  index when a user forwards / sends it to the bot; the bot cannot
  scrape channels it was not given.
"""
from __future__ import annotations

import hashlib
import json

CHUNK = 65536
MAX_CHUNKS = 16
MIN_SHARED_CHUNKS = 2


def chunk_hashes(data: bytes) -> list[str]:
    """Fixed-size chunk hashes from the start of the file."""
    out: list[str] = []
    for i in range(0, min(len(data), CHUNK * MAX_CHUNKS), CHUNK):
        out.append(hashlib.sha256(data[i:i + CHUNK]).hexdigest())
    return out


def fingerprint(data: bytes) -> dict:
    return {
        "sha256": hashlib.sha256(data or b"").hexdigest(),
        "chunk_hashes": chunk_hashes(data or b""),
        "size": len(data or b""),
    }


def find_match(stored: list[dict], data: bytes) -> dict | None:
    """Return the best stored record matching `data`, or None.

    Exact full hash wins. Otherwise >= MIN_SHARED_CHUNKS shared chunk
    hashes win (shortened copy). An unrelated file shares ~0 chunks,
    so it never matches. Never returns a near match on one chunk alone.
    """
    if not data or not stored:
        return None
    fp = fingerprint(data)
    chunk_set = set(fp["chunk_hashes"])
    best, best_shared = None, 0
    for rec in stored:
        if rec.get("sha256") and rec["sha256"] == fp["sha256"]:
            return rec
        try:
            stored_chunks = set(json.loads(rec.get("chunk_hashes") or "[]"))
        except ValueError:
            stored_chunks = set()
        shared = len(chunk_set & stored_chunks)
        if shared > best_shared:
            best, best_shared = rec, shared
    if best is not None and best_shared >= MIN_SHARED_CHUNKS:
        return best
    return None
