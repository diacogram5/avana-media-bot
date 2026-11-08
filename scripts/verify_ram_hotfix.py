"""Sandbox tests for the RAM hotfix (full-song cache caps + LYRICS_CACHE cap).
No network, no Telegram, no real Turso.

Cases:
  H1  _FULLSONG_CACHE_MAX == 2 and _FULLSONG_CACHE_TTL == 600.
  H2  3 distinct songs cached -> only the 2 newest remain (oldest evicted).
  H3  Expired entry (ts older than TTL) is a miss -> re-fetches via adapters.
  H4  fullsong _CACHE_MAX == 1; second put evicts the first.
  H5  LYRICS_CACHE_MAX == 200; 201st put evicts the oldest, len stays 200.
  H6  No direct LYRICS_CACHE[key] = assignment left outside _lyrics_cache_put.

Run: python3 scripts/verify_ram_hotfix.py ; exit 0 = all PASS.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Strip proxy env vars (sandbox httpx gotcha).
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))


# Reuses verify_speedfix's fake-aiogram install + real module imports
# (handlers, adapters, turso_cache). Import-safe: its main() is __main__-guarded.
import verify_speedfix as vs  # noqa: E402

adapters_mod = vs.adapters_mod
h = vs.h

import app.search.fullsong as fullsong_mod  # noqa: E402
from app.search.adapters.base import FileResult  # noqa: E402


def _reset_fullsong_cache() -> None:
    adapters_mod._FULLSONG_CACHE.clear()
    adapters_mod._FULLSONG_CACHE_ORDER.clear()
    adapters_mod._FULLSONG_CACHE_TS.clear()


async def main() -> None:
    # H1: constants.
    check("H1 cap 2 + TTL 600",
          adapters_mod._FULLSONG_CACHE_MAX == 2
          and adapters_mod._FULLSONG_CACHE_TTL == 600,
          f"MAX={adapters_mod._FULLSONG_CACHE_MAX} TTL={adapters_mod._FULLSONG_CACHE_TTL}")

    # H2/H3: stub search_parallel to return a distinct FileResult per call.
    calls = {"n": 0}

    async def _fake_search_parallel(adapters, artist, title, first_win=False):
        calls["n"] += 1
        return FileResult(data=b"x" * 100, ext="mp3", duration_sec=200.0,
                          source="fake", url=None, size=100)

    orig_search = adapters_mod.search_parallel
    adapters_mod.search_parallel = _fake_search_parallel
    try:
        _reset_fullsong_cache()
        await adapters_mod.find_full_song_file_v2("A1", "T1")
        await adapters_mod.find_full_song_file_v2("A2", "T2")
        await adapters_mod.find_full_song_file_v2("A3", "T3")
        k1 = adapters_mod._fullsong_cache_key("A1", "T1")
        k2 = adapters_mod._fullsong_cache_key("A2", "T2")
        k3 = adapters_mod._fullsong_cache_key("A3", "T3")
        check("H2 cap 2 evicts oldest",
              len(adapters_mod._FULLSONG_CACHE) == 2
              and k1 not in adapters_mod._FULLSONG_CACHE
              and k2 in adapters_mod._FULLSONG_CACHE
              and k3 in adapters_mod._FULLSONG_CACHE,
              f"keys={sorted(adapters_mod._FULLSONG_CACHE)}")

        # H3: backdate k2 past the TTL -> next lookup must re-fetch.
        calls["n"] = 0
        adapters_mod._FULLSONG_CACHE_TS[k2] = time.time() - 601
        res = await adapters_mod.find_full_song_file_v2("A2", "T2")
        check("H3 expired entry re-fetches",
              calls["n"] == 1 and res is not None,
              f"adapter calls={calls['n']} res={bool(res)}")
    finally:
        adapters_mod.search_parallel = orig_search
        _reset_fullsong_cache()

    # H4: fullsong _CACHE max 1.
    fullsong_mod._CACHE.clear()
    check("H4 fullsong _CACHE_MAX == 1", fullsong_mod._CACHE_MAX == 1,
          f"MAX={fullsong_mod._CACHE_MAX}")
    fullsong_mod._cache_put("k1", b"a", "mp3", 100.0, "s1")
    fullsong_mod._cache_put("k2", b"b", "mp3", 100.0, "s2")
    check("H4 second put evicts first",
          len(fullsong_mod._CACHE) == 1 and "k1" not in fullsong_mod._CACHE
          and "k2" in fullsong_mod._CACHE,
          f"keys={sorted(fullsong_mod._CACHE)}")
    got = fullsong_mod._cache_get("k2")
    check("H4 _cache_get returns stored row",
          got is not None and got[0] == b"b" and got[1] == "mp3",
          f"got={got}")

    # H5: LYRICS_CACHE cap 200, FIFO.
    h.LYRICS_CACHE.clear()
    check("H5 LYRICS_CACHE_MAX == 200", h.LYRICS_CACHE_MAX == 200,
          f"MAX={h.LYRICS_CACHE_MAX}")
    for i in range(200):
        h._lyrics_cache_put(f"k{i}", ("t", "a", "l"))
    h._lyrics_cache_put("k200", ("t", "a", "l"))
    check("H5 201st put evicts oldest, len stays 200",
          len(h.LYRICS_CACHE) == 200 and "k0" not in h.LYRICS_CACHE
          and "k200" in h.LYRICS_CACHE,
          f"len={len(h.LYRICS_CACHE)} k0={'k0' in h.LYRICS_CACHE}")
    h._lyrics_cache_put("k1", ("t2", "a2", "l2"))
    check("H5 re-put of existing key does not evict",
          len(h.LYRICS_CACHE) == 200 and h.LYRICS_CACHE["k1"][0] == "t2",
          f"len={len(h.LYRICS_CACHE)}")
    h.LYRICS_CACHE.clear()

    # H6: static — no direct LYRICS_CACHE[key] = left in handlers.py.
    src = (ROOT / "app" / "bot" / "handlers.py").read_text()
    assigns = [ln for ln in src.splitlines()
               if "LYRICS_CACHE[" in ln and "=" in ln
               and "LYRICS_CACHE_MAX" not in ln]
    check("H6 no direct LYRICS_CACHE assignment left",
          all("_lyrics_cache_put" in ln or "LYRICS_CACHE[key] = value" in ln
              for ln in assigns) and len(assigns) == 1,
          f"assignment lines={assigns}")


if __name__ == "__main__":
    asyncio.run(main())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
