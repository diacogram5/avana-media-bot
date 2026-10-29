"""Sandbox-only benchmark for the identify pipeline (BEFORE changes).

Uses explicit httpx proxy mounts + trust_env=False because this
sandbox's no_proxy list breaks httpx env parsing. Production code is
NOT touched by this shim.
"""
import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

_PX = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")


def _patch_httpx_for_sandbox():
    if not _PX:
        return
    _real = httpx.AsyncClient

    class SandboxClient(_real):
        def __init__(self, *a, **k):
            k.setdefault("trust_env", False)
            mounts = dict(k.pop("mounts", {}) or {})
            mounts.setdefault("http://", httpx.AsyncHTTPTransport(proxy=_PX))
            mounts.setdefault("https://", httpx.AsyncHTTPTransport(proxy=_PX))
            super().__init__(*a, mounts=mounts, **k)

    httpx.AsyncClient = SandboxClient


_patch_httpx_for_sandbox()

# Count + time every raw HTTP fetch during resolve.
_FETCH_LOG = []


def _count_fetches():
    from app.services import instagram as ig

    orig = ig._get

    async def wrapped(client, url, headers):
        t = time.time()
        out = await orig(client, url, headers)
        _FETCH_LOG.append({"url": url, "ms": int((time.time() - t) * 1000),
                           "chars": len(out)})
        return out

    ig._get = wrapped


STAGES = []


def _capture_stage_events():
    from app import logging_config as lc

    logger = logging.getLogger("bench")
    orig = lc.log_event

    def wrapped(lg, event, **fields):
        if event == "identify_stage":
            STAGES.append(fields)
        return orig(lg, event, **fields)

    lc.log_event = wrapped
    # rebind already-imported module loggers' log_event refs
    import app.bot.handlers as h

    h.log_event = wrapped


async def main():
    _count_fetches()
    _capture_stage_events()
    from app.services.instagram import resolve_instagram_media
    from app.services.shazam_id import analyze_audio

    url = "https://www.instagram.com/reel/DexfQJaoNrR/"

    # --- 1. resolve once ---
    _FETCH_LOG.clear()
    t = time.time()
    audio_url, video_url, page_text, strategy = await resolve_instagram_media(url)
    resolve_ms = int((time.time() - t) * 1000)
    print(json.dumps({"resolve_once_ms": resolve_ms,
                      "strategy": strategy,
                      "fetches": [(f["url"].split("/")[-2][:40] if "/" in f["url"] else f["url"][:40], f["ms"]) for f in _FETCH_LOG],
                      "total_fetch_ms": sum(f["ms"] for f in _FETCH_LOG)},
                     ensure_ascii=False))

    # --- 2. resolve twice (current link flow: _send_link_video + _identify_link_audio) ---
    _FETCH_LOG.clear()
    t = time.time()
    await resolve_instagram_media(url)
    await resolve_instagram_media(url)
    print(json.dumps({"resolve_twice_ms": int((time.time() - t) * 1000),
                      "page_fetches": len(_FETCH_LOG)}, ensure_ascii=False))

    if not audio_url:
        print("NO AUDIO URL — cannot continue")
        return
    # --- 3. audio download ---
    t = time.time()
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.get(audio_url, headers={"User-Agent": "Mozilla/5.0",
                                                 "Referer": "https://www.instagram.com/"})
        r.raise_for_status()
        data = r.content
    print(json.dumps({"audio_download_ms": int((time.time() - t) * 1000),
                      "audio_bytes": len(data)}, ensure_ascii=False))
    open("/tmp/final-b/scripts/bench_audio.mp4", "wb").write(data)

    # --- 4. validate ---
    t = time.time()
    analysis = analyze_audio(data)
    print(json.dumps({"validate_ms": int((time.time() - t) * 1000),
                      **analysis}, ensure_ascii=False))

    # --- 5. full _identify_audio (validate + shazam lookup) ---
    from app.bot import handlers as h
    from app.config import Config
    cfg = Config()

    async def _noop(*a, **k):
        pass

    h.callback = None
    STAGES.clear()
    t = time.time()
    res = await h._identify_audio(data, "clip.mp4", cfg)
    total = int((time.time() - t) * 1000)
    print(json.dumps({"identify_total_ms": total, "status": res.status,
                      "title": res.title, "artist": res.artist,
                      "detail": res.detail}, ensure_ascii=False))
    for s in STAGES:
        print("STAGE", json.dumps(s, ensure_ascii=False, default=str))


asyncio.run(main())
