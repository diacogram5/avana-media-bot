"""Full-song file identity verification (2026-11-09 mismatch fix).

A download site can LABEL a file with the wanted song while the audio
inside is a different song (seen live 2026-11-08 23:54: identify said
"ATM" / "Tiem & Salii", the sent file was "Bu Üräyin Dərdi Sənsən" /
"Canım Xəstə"). Two holes made it possible:

1. ``FileResult.title``/``artist`` echo the SEARCH QUERY (persian_music
   returns ``title=title, artist=artist``), and the orchestrator's score
   compares the query against itself — so a persian_music hit always
   scores a perfect match, whatever the file really is.
2. ``_title_match`` in persian_music always returns True, and the
   msbmusic scrape path returns the FIRST mp3 on a search page whatever
   song it is (measured: a "Fereshteh" search returned Poori's song and
   a maddahi).

The ONLY trustworthy identity is the file's OWN metadata tags (ID3 /
format tags), read with ffprobe from the downloaded bytes. Measured
2026-11-09 on real site downloads: 6/6 files carry usable title+artist
tags (plus duration) — so requiring tags does not block legit sends
from these sources. Site-brand stamps in tags ("[Msbmusic.IR]",
"موزیکدل", "- Download1Music.IR") are packaging stopwords in logic.py.

Rules (caller decides the fallback; this module never sends):
- tags present and matching the identify result -> "match" (send).
- tags present but a different song -> "mismatch" (never send).
- no title tag -> "unverifiable" (never send the file).
- a missing ARTIST tag alone does not block a title match (logged as
  "artist_tag_missing"): an absent tag is a missing signal, not a
  contradiction. A present-but-different artist still blocks.

Matching reuses ``logic.identified_card_acceptable`` (the codebase's
strict card rule) on bracket-stripped titles, so "(Remix)"-style
suffixes do not affect the verdict. Fingerprinting is explicitly NOT
used here (user order 2026-11-09): tag-based check only.

Caveat (honest limit): if a file's OWN tags are wrong the same way its
page label is, this check cannot catch it — only audio fingerprinting
could, and that is out of scope for this fix.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

_FFPROBE_TIMEOUT_SEC = 15


def probe_file_identity(data: bytes, ext: str = "mp3") -> tuple[str, str, float]:
    """(title, artist, duration_sec) from the file's OWN tags.

    Writes the bytes to a temp file and reads format tags + duration
    with ffprobe. Works for mp3 (ID3) and m4a. Never raises: any failure
    returns ("", "", 0.0) so the caller treats the file as unverifiable.
    """
    if not data:
        return "", "", 0.0
    suffix = "." + (ext or "mp3").lstrip(".").lower()
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, data)
        os.close(fd)
        fd = None
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format_tags=title,artist:format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=_FFPROBE_TIMEOUT_SEC)
        fmt = json.loads(r.stdout or "{}").get("format") or {}
        tags = fmt.get("tags") or {}
        lower = {str(k).lower(): v for k, v in tags.items()}
        try:
            duration = float(fmt.get("duration") or 0)
        except Exception:
            duration = 0.0
        return (str(lower.get("title") or "").strip(),
                str(lower.get("artist") or "").strip(),
                duration)
    except Exception:
        return "", "", 0.0
    finally:
        try:
            if fd is not None:
                os.close(fd)
        except Exception:
            pass
        try:
            os.unlink(path)
        except Exception:
            pass


def _strip_bracketed(text: str) -> str:
    """Remove (...) / [...] suffixes (same regex as logic._strip_brackets)."""
    return re.sub(r"[\(\[].*?[\)\]]", " ", text or "")


def file_identity_matches(wanted_title: str, wanted_artist: str,
                          file_title: str, file_artist: str) -> bool:
    """True when the file's own title/artist ARE the identify result.

    Bracket-insensitive on both sides (a "(Remix)" tag never decides the
    verdict); Persian/Arabic char normalization and site-brand stopwords
    come from logic.py's skeleton matching.
    """
    from ..bot.logic import identified_card_acceptable
    return identified_card_acceptable(
        _strip_bracketed(wanted_title), wanted_artist,
        _strip_bracketed(file_title), file_artist, "")


def verify_fullsong_file(data: bytes, ext: str,
                         wanted_title: str, wanted_artist: str) -> dict:
    """Verify downloaded bytes against the identify result.

    Returns {"status", "file_title", "file_artist", "duration_sec",
    "detail"} with status "match" | "mismatch" | "unverifiable".
    Never raises.
    """
    file_title, file_artist, duration = probe_file_identity(data, ext)
    if not file_title:
        return {"status": "unverifiable", "file_title": file_title,
                "file_artist": file_artist, "duration_sec": duration,
                "detail": "no_title_tag"}
    if not file_artist:
        # Title matches but the artist tag is absent: check the title
        # alone (an absent tag cannot contradict the title).
        from ..bot.logic import identified_card_acceptable
        ok = identified_card_acceptable(
            _strip_bracketed(wanted_title), "",
            _strip_bracketed(file_title), "", "")
        return {"status": "match" if ok else "mismatch",
                "file_title": file_title, "file_artist": file_artist,
                "duration_sec": duration,
                "detail": "artist_tag_missing" if ok else "title_mismatch"}
    ok = file_identity_matches(wanted_title, wanted_artist,
                               file_title, file_artist)
    return {"status": "match" if ok else "mismatch",
            "file_title": file_title, "file_artist": file_artist,
            "duration_sec": duration,
            "detail": "match" if ok else "title_artist_mismatch"}
