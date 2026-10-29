"""Sandbox-only AFTER benchmark (same real input as bench_speed.py).

Shim: explicit httpx proxy mounts + trust_env=False (sandbox quirk only).
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

_FETCH_LOG = []
STAGES = []
TRANSCODE_CALLS = []


def _instrument():
    from app.services import instagram as ig
    from app.services import shazam_id as sid
    from app import logging_config as lc

    orig_get = ig._get

    async def wrapped(client, url, headers):
        t = time.time()
        out = await orig_get(client, url, headers)
        _FETCH_LOG.append({"ms": int((time.time() - t) * 1000)})
        return out

    ig._get = wrapped

    orig_tc = sid.transcode_to_wav

    def counted(data, max_seconds=90):
        TRANSCODE_CALLS.append(len(data))
        return orig_tc(data, max_seconds=max_seconds)

    sid.transcode_to_wav = counted

    import app.bot.handlers as h
    orig_le = lc.log_event

    def wrapped_le(lg, event, **fields):
        if event == "identify_stage":
            STAGES.append(fields)
        return orig_le(lg, event, **fields)

    lc.log_event = wrapped_le
    h.log_event = wrapped_le


async def main():
    _instrument()
    from app.services.instagram import resolve_instagram_media
    from app.bot import handlers as h
    from app.config import Config
    from app import db as dbmod

    url = "https://www.instagram.com/reel/DexfQJaoNrR/"
    cfg = Config()
    conn = dbmod.init_db("/tmp/final-b/scripts/bench.db")

    # --- 1. single resolve (new link flow) ---
    _FETCH_LOG.clear()
    t = time.time()
    resolved = await resolve_instagram_media(url)
    audio_url = resolved[0]
    print(json.dumps({
        "AFTER_resolve_once_ms": int((time.time() - t) * 1000),
        "strategy": resolved[3], "page_fetches": len(_FETCH_LOG)},
        ensure_ascii=False))

    # --- 2. _identify_audio on the SAME 123002-byte reel audio ---
    data = open("/tmp/final-b/scripts/bench_audio.mp4", "rb").read()
    assert len(data) == 123002
    TRANSCODE_CALLS.clear()
    STAGES.clear()
    t = time.time()
    res = await h._identify_audio(data, "clip.mp4", cfg, conn)
    print(json.dumps({
        "AFTER_identify_123KB_ms": int((time.time() - t) * 1000),
        "transcode_calls": len(TRANSCODE_CALLS),
        "status": res.status}, ensure_ascii=False))
    for s in STAGES:
        if s.get("stage") in ("cache_check", "validate", "input_prepared",
                              "lookup_response", "result_sent"):
            print("  STAGE", json.dumps(s, ensure_ascii=False, default=str))

    # --- 3. multi-window path on a 485KB valid AAC (>200KB -> 3 windows) ---
    tone = open("/tmp/final-b/scripts/bench_tone.mp4", "rb").read()
    TRANSCODE_CALLS.clear()
    STAGES.clear()
    t = time.time()
    res = await h._identify_audio(tone, "clip.mp4", cfg, conn)
    print(json.dumps({
        "AFTER_identify_485KB_ms": int((time.time() - t) * 1000),
        "transcode_calls": len(TRANSCODE_CALLS),
        "status": res.status}, ensure_ascii=False))
    for s in STAGES:
        if s.get("stage") in ("validate", "input_prepared", "lookup_response"):
            print("  STAGE", json.dumps(s, ensure_ascii=False, default=str))

    # --- 4. hash-cache hit: seed an identified result, re-identify ---
    import hashlib
    hh = hashlib.sha256(data).hexdigest()
    dbmod.set_identify_cache(conn, f"hash:{hh}", "identified",
                             "Dooset Daram", "Mehdi Ahmadvand")
    TRANSCODE_CALLS.clear()
    STAGES.clear()
    t = time.time()
    res = await h._identify_audio(data, "clip.mp4", cfg, conn)
    print(json.dumps({
        "AFTER_cache_hit_ms": int((time.time() - t) * 1000),
        "transcode_calls": len(TRANSCODE_CALLS),
        "status": res.status, "title": res.title}, ensure_ascii=False))

    # --- 5. url-key hit in _identify_link_audio: no network at all ---
    dbmod.set_identify_cache(conn, f"url:{audio_url}", "identified",
                             "Dooset Daram", "Mehdi Ahmadvand")

    real_client = httpx.AsyncClient

    class NoNet(real_client):
        async def get(self, *a, **k):
            raise AssertionError("network touched on url-cache hit")

    httpx.AsyncClient = NoNet
    from types import SimpleNamespace
    outcome = SimpleNamespace(kind="instagram", official_url=url, ok=True)
    t = time.time()
    link_res = await h._identify_link_audio(outcome, cfg, conn,
                                            resolved=resolved)
    print(json.dumps({
        "AFTER_link_url_hit_ms": int((time.time() - t) * 1000),
        "status": link_res[0], "title": link_res[1]}, ensure_ascii=False))
    conn.close()


asyncio.run(main())
