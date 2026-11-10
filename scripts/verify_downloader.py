"""Sandbox tests for the downloader URL normalizer (fix round 5, F-12).

``app/features/downloader.py::normalize_url`` used to keep the query
string and never collapsed reel spellings, so the same reel pasted
with different tracking params (``?igshid=...``, ``?utm_source=...``)
or as ``/p/`` vs ``/reel/`` missed the ``download_jobs`` cache and
re-downloaded. It now delegates to the single fix-3 normalizer in
``app/bot/file_cache.py``, so the ``dl:`` flow shares keys with the
main link flow.

Cases:
  D1  The same reel with different tracking params, ``www.`` vs bare
      host, and ``/p/`` vs ``/reel/`` vs ``/reels/`` vs ``/tv/``
      spellings normalizes to one key.
  D2  Different reels still get different keys.
  D3  The downloader normalizer agrees exactly with
      ``file_cache._normalize_url`` and ``turso_cache._normalize_url``
      on every spelling in D1/D2 (one implementation, three callers).
  D4  Non-Instagram URLs: the query string is dropped, different
      paths still differ, and odd/empty input never raises.

No network, no Telegram, no real Turso. The fake aiogram harness from
scripts/verify_speedfix.py is reused only so the downloader module
imports.

Run: python3 scripts/verify_downloader.py ; exit 0 = all PASS.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Strip proxy env vars (sandbox httpx gotcha).
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)

import verify_speedfix as vs  # noqa: E402  (installs fake aiogram)

from app.features import downloader as dl  # noqa: E402
from app.bot import file_cache as fc  # noqa: E402
from app.bot import turso_cache as tc  # noqa: E402

check = vs.check
RESULTS = vs.RESULTS


def main() -> int:
    _u1 = "https://www.instagram.com/reel/ABC123/?igshid=xyz&utm_source=ig"
    _u2 = "https://instagram.com/p/ABC123"
    _u3 = "https://instagram.com/reel/ABC123/"
    _u4 = "https://instagram.com/reels/ABC123?igshid=other"
    _u5 = "https://instagram.com/tv/ABC123"
    _other = "https://instagram.com/reel/ZZZ999/"
    _spellings = [_u1, _u2, _u3, _u4, _u5]

    # ---- D1: one reel, many spellings -> one key ------------------------
    try:
        _norms = [dl.normalize_url(u) for u in _spellings]
        check("D1a same reel converges across tracking params / host / path spelling",
              len(set(_norms)) == 1, f"norms={_norms}")
        check("D1b the canonical form drops the query string",
              "?" not in _norms[0] and "igshid" not in _norms[0]
              and "utm_source" not in _norms[0], f"norm={_norms[0]!r}")
        check("D1c the canonical form is the /reel/ spelling, www stripped",
              _norms[0] == "https://instagram.com/reel/ABC123",
              f"norm={_norms[0]!r}")
    except Exception as exc:
        check("D1 same reel converges", False, f"{type(exc).__name__}: {exc}")

    # ---- D2: different reels still differ -------------------------------
    try:
        check("D2a a different reel gets a different normalized URL",
              dl.normalize_url(_other) != dl.normalize_url(_u1),
              f"{dl.normalize_url(_other)!r} vs {dl.normalize_url(_u1)!r}")
        check("D2b a different reel gets a different cache key",
              dl.normalize_url(_other) != dl.normalize_url(_u3))
    except Exception as exc:
        check("D2 different reels differ", False, f"{type(exc).__name__}: {exc}")

    # ---- D3: one implementation, three callers --------------------------
    try:
        _agree = all(
            dl.normalize_url(u) == fc._normalize_url(u) == tc._normalize_url(u)
            for u in _spellings + [_other])
        check("D3 downloader agrees with file_cache and turso_cache normalizers",
              _agree)
    except Exception as exc:
        check("D3 normalizers agree", False, f"{type(exc).__name__}: {exc}")

    # ---- D4: non-Instagram URLs and odd input ---------------------------
    try:
        check("D4a a direct-file URL drops its query string",
              dl.normalize_url("https://cdn.example.com/song.mp3?token=abc")
              == "https://cdn.example.com/song.mp3")
        check("D4b different paths still differ",
              dl.normalize_url("https://cdn.example.com/a.mp3")
              != dl.normalize_url("https://cdn.example.com/b.mp3"))
        check("D4c empty input never raises",
              dl.normalize_url("") == "")
        check("D4d whitespace-only input never raises",
              dl.normalize_url("   ") == "")
    except Exception as exc:
        check("D4 non-Instagram URLs and odd input", False,
              f"{type(exc).__name__}: {exc}")

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
