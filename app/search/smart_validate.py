"""Smart-path file validation (2026-11-09 free-only smart search).

The NORMAL full-song path (``verify_fullsong_file``) is UNCHANGED: it
bracket-strips "(Remix)" before comparing, so a 7:23 original and a
5:41 remix look identical to it, and it has no expected-duration
comparison — measured on the four Taziyane fixtures it ACCEPTS the
wrong 7:23 files and REJECTS the right 5:41 file. This module is the
smart path's own pipeline, used ONLY by the retry button's smart
search:

  tags (brand-suffix strip + transliteration + version tokens
  compared BEFORE bracket-stripping)
  -> expected-duration gate (|candidate - identified| <= 10 s)
  -> optional fingerprint (fpcalc vs the clip, bar >= 0.90)

Statuses: "match" | "conditional" | "mismatch" | "unverifiable".
"conditional" = tags + duration pass but the version token is absent
from the file's own tags; the caller may accept it ONLY when the
version token appears in the source URL/filename (the source's own
label) — never on tags alone. Fingerprint is an ADVISORY confirm,
never a hard gate: a noisy clip (speech overlay, speed change) can
score below the bar on the right file, and tags + duration already
separate the known traps (341 s vs 443 s); the similarity is logged
and recorded in the outcome.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

_FFPROBE_TIMEOUT_SEC = 15
_DURATION_TOLERANCE_SEC = 10.0
_FINGERPRINT_BAR = 0.90

# Site-brand words that appear in file titles as packaging, never as
# song identity (extends logic._PACKAGING_STOPWORDS, which the shared
# skeleton matcher already drops). "from" introduces a brand suffix
# ("... From abanmusics.com"); "com"/"ir"/"net"/"org" are TLD
# leftovers of a stamped domain.
_BRAND_TOKENS = frozenset({
    "from", "abanmusics", "melonmusic", "musicviral", "fazremix",
    "javanmelody", "musicsweb", "minevisam", "musicsfa", "ezmusic",
    "mokhtalefmusic", "gisomusic", "irmp3", "com", "net", "org",
    "download",
})


def _canon_token(tok: str) -> str:
    """Canonical form so Persian/Finglish spellings of one word meet.

    Applied to the skeleton (Persian chars already Latinised by
    logic._skeleton_token): "x" -> "ks" (remix == ریمیکس "rimiks"),
    "iy" -> "i" (taziyane == taziane), "e" -> "i" (remix == rimiks),
    one trailing "h" stripped (tazianeh/tazianh == taziane), one
    trailing "i" stripped (the "e" that became "i" at word end).
    Checked: taziyane / tazianeh / tazianh -> "tazian";
    remix / rimiks -> "rimiks"; tataloo / amir unchanged.
    """
    t = (tok or "").lower()
    t = t.replace("x", "ks")
    t = t.replace("iy", "i")
    t = t.replace("e", "i")
    if len(t) > 2 and t.endswith("h"):
        t = t[:-1]
    if len(t) > 2 and t.endswith("i"):
        t = t[:-1]
    return t


def smart_tokens(text: str) -> list[str]:
    """Distinctive canon tokens of `text`, brand packaging removed."""
    from ..bot.logic import _distinctive_tokens
    out = []
    for tok in _distinctive_tokens(text or ""):
        if tok in _BRAND_TOKENS:
            continue
        c = _canon_token(tok)
        if len(c) >= 2:
            out.append(c)
    return out


def _strip_bracketed(text: str) -> str:
    """Remove (...) / [...] suffixes (same regex as logic._strip_brackets)."""
    return re.sub(r"[\(\[].*?[\)\]]", " ", text or "")


def smart_title_matches(wanted_title: str, file_title: str) -> bool:
    """Core-title equality after bracket + brand stripping + canon.

    No extra tokens allowed (the codebase's strict card rule): a file
    titled "Taziyane (Remix) [Amir Tataloo] From abanmusics.com"
    matches "Taziyane (Remix)" — brackets and the brand suffix are
    packaging, not identity. Version words are NOT compared here;
    they are checked separately (see carries_version_tokens).
    """
    want = set(smart_tokens(_strip_bracketed(wanted_title)))
    got = set(smart_tokens(_strip_bracketed(file_title)))
    if not want:
        return False
    return want == got


def smart_artist_matches(wanted_artist: str, file_artist: str) -> bool:
    """True when every wanted artist token is in the file's artist.

    A missing artist tag is a missing signal, not a contradiction
    (same rule as verify_fullsong_file); a present-but-different
    artist still blocks.
    """
    want = set(smart_tokens(wanted_artist or ""))
    if not want:
        return True
    got = set(smart_tokens(file_artist or ""))
    if not got:
        return True
    return want <= got


def duration_gate_ok(file_duration: float,
                     expected_duration: float | None,
                     tolerance: float = _DURATION_TOLERANCE_SEC) -> bool:
    """Hard gate: |candidate - identified| <= tolerance (default 10 s).

    Separates the 5:41 remix (341 s) from the 7:23 original (443 s)
    by 102 s. Skipped (True) when the expected duration is unknown
    — the catalog lookup found nothing to compare against.
    """
    if not expected_duration:
        return True
    try:
        return abs(float(file_duration or 0) - float(expected_duration)) <= tolerance
    except Exception:
        return False


def validate_smart_file(wanted_title: str, wanted_artist: str,
                        file_title: str, file_artist: str,
                        file_duration_sec: float,
                        expected_duration_sec: float | None = None,
                        source_url: str = "") -> dict:
    """Validate one candidate file against the identify result.

    Gate order (first failure decides the reason):
      1. no title tag -> "unverifiable"
      2. core title mismatch -> "mismatch" / "title_mismatch"
      3. artist mismatch -> "mismatch" / "artist_mismatch"
      4. duration gate -> "mismatch" / "duration_mismatch"
      5. version tokens in the file's own tags -> "match"
         else -> "conditional" / "version_not_in_tags"
    Returns {"status", "detail", "duration_diff", "version_in_tags",
    "version_in_source", "file_title", "file_artist",
    "duration_sec"}. Never raises.
    """
    from ..bot.logic import carries_version_tokens, identified_version_tokens
    file_title = (file_title or "").strip()
    file_artist = (file_artist or "").strip()
    try:
        dur = float(file_duration_sec or 0)
    except Exception:
        dur = 0.0
    expected = None
    try:
        expected = float(expected_duration_sec) if expected_duration_sec else None
    except Exception:
        expected = None
    diff = (abs(dur - expected) if expected else None)
    version_tokens = identified_version_tokens(wanted_title or "")
    version_in_tags = (not version_tokens
                       or carries_version_tokens(wanted_title or "", file_title))
    version_in_source = False
    if version_tokens and source_url:
        # Percent-encoded filenames ("%28Remix-320%29") tokenise as
        # "28remix" — decode first so the version token is visible.
        from urllib.parse import unquote
        hay = set(smart_tokens(unquote(source_url)))
        version_in_source = all(_canon_token(t) in hay for t in version_tokens)
    base = {"file_title": file_title, "file_artist": file_artist,
            "duration_sec": dur, "duration_diff": diff,
            "version_tokens": version_tokens,
            "version_in_tags": bool(version_in_tags),
            "version_in_source": bool(version_in_source)}
    if not file_title:
        return {**base, "status": "unverifiable", "detail": "no_title_tag"}
    if not smart_title_matches(wanted_title or "", file_title):
        return {**base, "status": "mismatch", "detail": "title_mismatch"}
    if not smart_artist_matches(wanted_artist or "", file_artist):
        return {**base, "status": "mismatch", "detail": "artist_mismatch"}
    if expected and not duration_gate_ok(dur, expected):
        return {**base, "status": "mismatch", "detail": "duration_mismatch"}
    if version_in_tags:
        return {**base, "status": "match", "detail": "match"}
    return {**base, "status": "conditional",
            "detail": "version_not_in_tags"}


def conditional_acceptable(validation: dict, source_url: str = "") -> bool:
    """A "conditional" file is acceptable ONLY when the version token
    appears in the source URL/filename actually being offered (the
    source's own label) AND the duration gate passed. The URL is
    re-checked here — a stored flag from another computation never
    suffices. Tags alone never suffice. Never raises.
    """
    if not isinstance(validation, dict):
        return False
    if validation.get("status") != "conditional":
        return False
    if validation.get("duration_diff") is None:
        return False
    if validation.get("duration_diff", 9999) > _DURATION_TOLERANCE_SEC:
        return False
    if not source_url:
        return False
    tokens = validation.get("version_tokens") or []
    if not tokens:
        return False
    from urllib.parse import unquote
    hay = set(smart_tokens(unquote(source_url)))
    return all(_canon_token(t) in hay for t in tokens)


def probe_file_identity_from_path(path: str) -> tuple[str, str, float]:
    """(title, artist, duration_sec) from a file on disk, via ffprobe.

    Same ffprobe query as fullsong_verify.probe_file_identity, but
    reads a path instead of bytes (the smart path stages candidates
    on disk, never in RAM). Never raises: any failure returns
    ("", "", 0.0).
    """
    if not path or not os.path.isfile(path):
        return "", "", 0.0
    try:
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


def _fp_bit_similarity(a: list[int], b: list[int]) -> float:
    """Fraction of equal bits between two equal-length int arrays."""
    if not a:
        return 0.0
    tot = 0
    for x, y in zip(a, b):
        tot += 32 - bin((x ^ y) & 0xFFFFFFFF).count("1")
    return tot / (32 * len(a))


def fpclip_similarity(clip_items: list[int],
                      candidate_items: list[int]) -> float:
    """Best sliding-window similarity of a short clip fingerprint
    inside a full-song fingerprint (0.0..1.0). The clip is an excerpt,
    so it aligns at the excerpt's position in the song. Returns 0.0
    when either side is empty. Pure Python; the numpy fast path in
    exclusive_bank must return identical numbers.
    """
    query = list(clip_items or [])
    stored = list(candidate_items or [])
    n = len(query)
    if n == 0 or len(stored) < n:
        return 0.0
    best = -1.0
    for off in range(0, len(stored) - n + 1):
        sim = _fp_bit_similarity(query, stored[off:off + n])
        if sim > best:
            best = sim
    return max(0.0, best)


def fingerprint_file_items(path: str,
                           max_seconds: int = 120) -> list[int] | None:
    """Chromaprint integer fingerprint of an audio file, or None.

    Runs the vendored fpcalc (same binary the exclusive bank uses).
    Never raises; None means "unavailable", not "different". The
    import is lazy so this module stays importable without the bot
    package's fingerprint machinery.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        from ..bot.exclusive_bank import fingerprint_file
        _dur, items = fingerprint_file(path, max_seconds=max_seconds)
        return list(items) if items else None
    except Exception:
        return None
