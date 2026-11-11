"""Sandbox tests for the Render sources probe (stage 5, 2026-11-11).

``app/services/sources_probe.py`` answers, from Render's own network,
which Persian full-version sites serve verified files and which ID
engines answer. It is TEMPORARY (removed after the probe runs) and must
never log a key. The sandbox has fake DNS and no shazamio, so every
network call here is faked with a canned ``urllib.request.urlopen``
and ``ffprobe_duration`` is stubbed — these tests prove the verdict
logic and the no-key-leak rule, NOT Render reachability (the live
probe proves that).

Cases:
  S1  ffprobe_duration on a nonexistent local file fails fast, no raise.
  S2  probe_expected_duration: Deezer hit -> (341.0, "deezer");
      Deezer empty -> (None, "deezer_no_match").
  S3  probe_persian_site MAIN: WP post with an mp3, ffprobe 341s,
      expected 341s.
  S4  probe_persian_site NEITHER on a duration mismatch (ffprobe 443s).
  S5  probe_persian_site CANDIDATE when the expected duration is unknown.
  S6  probe_persian_site NEITHER when wp-json is unreachable.
  S7  probe_shazamio -> UNPROVEN (shazamio is absent in the sandbox).
  S8  probe_rapidapi_shazam: no key -> UNPROVEN; fake key + 403 ->
      UNPROVEN "not_subscribed"; fake key + 200 Shazam body -> MAIN,
      and the fake key never appears in the result.
  S9  probe_audd: no token -> UNPROVEN; fake token + error JSON ->
      NEITHER with error_code 900 verbatim, fake token never in result.
  S10 probe_acoustid / probe_audiotag without a key -> UNPROVEN with
      reachability recorded (fake 400 / fake 200).
  S11 run_probe end-to-end (1 song, faked network, stubbed ffprobe,
      fake keys set): 16 site results, summary MAIN 16, identification
      records the ffmpeg slice failure on fake mp3 bytes with zero
      engine calls, and NO fake key value appears in the JSON dump.
  S12 webpanel carries the /probe-sources route (lock + worker exist).

No network, no Telegram, no real Turso. The fake aiogram harness from
scripts/verify_speedfix.py is reused only so app.webpanel imports.

Run: python3 scripts/verify_sources_probe.py ; exit 0 = all PASS.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Strip proxy env vars (sandbox httpx gotcha).
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)

import verify_speedfix as vs  # noqa: E402  (installs fake aiogram)

from app.services import sources_probe as sp  # noqa: E402
import app.webpanel as wp  # noqa: E402

check = vs.check
RESULTS = vs.RESULTS

TAZIYANE = {"title": "Taziyane (Remix)", "artist": "Amir Tataloo",
            "expected_s": 341.0}
WP_POST = json.dumps([{
    "title": {"rendered": "Taziyane (Remix) - Amir Tataloo"},
    "content": {"rendered":
                '<p><a href="https://dl.example.com/taziyane-remix.mp3">dl</a></p>'},
    "link": "https://javanmelody.ir/post/10959"},
]).encode("utf-8")
DEEZER_HIT = json.dumps({"data": [{"duration": 341,
                                   "title": "Taziyane (Remix)"}]}).encode("utf-8")
DEEZER_EMPTY = json.dumps({"data": []}).encode("utf-8")
SHAZAM_BODY = json.dumps({"track": {"title": "Taziyane (Remix)",
                                    "subtitle": "Amir Tataloo"}}).encode("utf-8")
AUDD_ERR = json.dumps({"status": "error",
                       "error": {"error_code": 900,
                                 "error_message": "test quota"}}).encode("utf-8")

FAKE_KEYS = {
    "RAPIDAPI_KEY": "FAKE-RAPIDAPI-KEY-111",
    "AUDD_API_TOKEN": "FAKE-AUDD-TOKEN-222",
    "ACOUSTID_API_KEY": "FAKE-ACOUSTID-KEY-333",
    "AUDIOTAG_API_KEY": "FAKE-AUDIOTAG-KEY-444",
}


class FakeResp:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body
        self.headers = {}

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """Canned urlopen. Routes by URL substring; tests adjust `mode`."""

    def __init__(self):
        self.mode = {}
        self.fail_wp_json = False

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "api.acoustid.org" in url:
            return FakeResp(400, b'{"error": "missing fingerprint"}')
        if "audiotag.info/api" in url:
            return FakeResp(200, b'{"status": "error"}')
        if "shazam.p.rapidapi.com" in url:
            mode = self.mode.get("rapidapi", "403")
            if mode == "403":
                return FakeResp(403, b'{"message": "not subscribed"}')
            return FakeResp(200, SHAZAM_BODY)
        if "api.audd.io" in url:
            return FakeResp(200, AUDD_ERR)
        if "api.deezer.com/search" in url:
            return FakeResp(200, self.mode.get("deezer", DEEZER_HIT))
        if "/wp-json/" in url and "posts?search=" in url:
            return FakeResp(200, WP_POST)
        if url.rstrip("/").endswith("/wp-json"):
            if self.fail_wp_json:
                raise urllib.error.URLError("fake dns")
            return FakeResp(200, b"{}")
        if url.lower().endswith(".mp3"):
            return FakeResp(200, b"FAKE-MP3-BYTES")
        return FakeResp(200, b"{}")


def _patch_net(fake):
    real = urllib.request.urlopen
    urllib.request.urlopen = fake
    return real


def _patch_ffprobe(canned_s):
    real = sp.ffprobe_duration
    sp.ffprobe_duration = lambda url, timeout=30: {
        "ok": True, "duration_s": canned_s, "ms": 1, "stderr": ""}
    return real


def _set_fake_keys():
    old = {}
    for k, v in FAKE_KEYS.items():
        old[k] = os.environ.get(k)
        os.environ[k] = v
    return old


def _restore_keys(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def main() -> int:
    # S1: ffprobe on a nonexistent local file fails fast, no raise.
    d = sp.ffprobe_duration("file:///nonexistent-probe-file.mp3", timeout=10)
    check("S1 ffprobe missing file -> ok False", d.get("ok") is False)
    check("S1 ffprobe missing file -> no duration",
          d.get("duration_s") is None)

    # S2: expected duration from Deezer.
    fake = FakeNet()
    real_urlopen = _patch_net(fake)
    try:
        dur, src = sp.probe_expected_duration(TAZIYANE, timeout=5)
        check("S2 Deezer hit -> 341.0/deezer",
              dur == 341.0 and src == "deezer")
        fake.mode["deezer"] = DEEZER_EMPTY
        dur, src = sp.probe_expected_duration(TAZIYANE, timeout=5)
        check("S2 Deezer empty -> None/deezer_no_match",
              dur is None and src == "deezer_no_match")
    finally:
        urllib.request.urlopen = real_urlopen

    # S3-S6: Persian WP site verdicts.
    fake = FakeNet()
    real_urlopen = _patch_net(fake)
    try:
        real_fp = _patch_ffprobe(341.0)
        try:
            r = sp.probe_persian_site("javanmelody.ir", TAZIYANE, timeout=5)
            check("S3 WP mp3 + duration match -> MAIN",
                  r["verdict"] == "MAIN" and r["reason"] == "duration match")
            check("S3 mp3 extracted",
                  r["checks"]["mp3_extractable"]["pass"] is True
                  and r["checks"]["mp3_extractable"]["mp3s"])
        finally:
            sp.ffprobe_duration = real_fp
        real_fp = _patch_ffprobe(443.0)
        try:
            r = sp.probe_persian_site("javanmelody.ir", TAZIYANE, timeout=5)
            check("S4 duration mismatch -> NEITHER",
                  r["verdict"] == "NEITHER"
                  and r["reason"] == "duration mismatch")
        finally:
            sp.ffprobe_duration = real_fp
        real_fp = _patch_ffprobe(341.0)
        try:
            no_exp = dict(TAZIYANE, expected_s=None)
            r = sp.probe_persian_site("javanmelody.ir", no_exp, timeout=5)
            check("S5 unknown expected duration -> CANDIDATE",
                  r["verdict"] == "CANDIDATE")
        finally:
            sp.ffprobe_duration = real_fp
        fake.fail_wp_json = True
        r = sp.probe_persian_site("javanmelody.ir", TAZIYANE, timeout=5)
        check("S6 wp-json unreachable -> NEITHER",
              r["verdict"] == "NEITHER"
              and r["reason"] == "wp-json not reachable")
    finally:
        urllib.request.urlopen = real_urlopen

    # S7: shazamio is absent in the sandbox.
    r = sp.probe_shazamio(b"\x00" * 100)
    check("S7 shazamio absent -> UNPROVEN",
          r["verdict"] == "UNPROVEN"
          and r["reason"] == "shazamio not installed")

    # S8: RapidAPI Shazam.
    old = _set_fake_keys()
    fake = FakeNet()
    real_urlopen = _patch_net(fake)
    try:
        os.environ.pop("RAPIDAPI_KEY", None)
        r = sp.probe_rapidapi_shazam(b"\x00" * 100)
        check("S8 no key -> UNPROVEN",
              r["verdict"] == "UNPROVEN" and r["reason"] == "RAPIDAPI_KEY not set")
        os.environ["RAPIDAPI_KEY"] = FAKE_KEYS["RAPIDAPI_KEY"]
        fake.mode["rapidapi"] = "403"
        r = sp.probe_rapidapi_shazam(b"\x00" * 100)
        check("S8 403 -> UNPROVEN not_subscribed",
              r["verdict"] == "UNPROVEN" and r["reason"] == "not_subscribed")
        fake.mode["rapidapi"] = "200"
        r = sp.probe_rapidapi_shazam(b"\x00" * 100)
        check("S8 200 Shazam body -> MAIN with title/artist",
              r["verdict"] == "MAIN" and r["title"] == "Taziyane (Remix)"
              and r["artist"] == "Amir Tataloo")
        check("S8 fake key never in result",
              FAKE_KEYS["RAPIDAPI_KEY"] not in json.dumps(r))
    finally:
        urllib.request.urlopen = real_urlopen
        _restore_keys(old)

    # S9: AudD error object recorded verbatim, token never in result.
    old = _set_fake_keys()
    fake = FakeNet()
    real_urlopen = _patch_net(fake)
    try:
        os.environ.pop("AUDD_API_TOKEN", None)
        r = sp.probe_audd("/nonexistent-slice.mp3")
        check("S9 no token -> UNPROVEN",
              r["verdict"] == "UNPROVEN" and r["reason"] == "AUDD_API_TOKEN not set")
        os.environ["AUDD_API_TOKEN"] = FAKE_KEYS["AUDD_API_TOKEN"]
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"FAKE-SLICE")
            slice_path = f.name
        try:
            r = sp.probe_audd(slice_path)
        finally:
            os.unlink(slice_path)
        check("S9 error JSON -> NEITHER, error_code 900 verbatim",
              r["verdict"] == "NEITHER" and r.get("error_code") == 900
              and r.get("error_message") == "test quota")
        check("S9 fake token never in result",
              FAKE_KEYS["AUDD_API_TOKEN"] not in json.dumps(r))
    finally:
        urllib.request.urlopen = real_urlopen
        _restore_keys(old)

    # S10: AcoustID / AudioTag without keys -> UNPROVEN, reachable recorded.
    fake = FakeNet()
    real_urlopen = _patch_net(fake)
    old = _set_fake_keys()
    try:
        for k in ("ACOUSTID_API_KEY", "AUDIOTAG_API_KEY"):
            os.environ.pop(k, None)
        r = sp.probe_acoustid("/nonexistent-slice.mp3")
        check("S10 acoustid no key -> UNPROVEN, reachable (400)",
              r["verdict"] == "UNPROVEN" and r.get("reachable") is True
              and r.get("reach_status") == 400)
        r = sp.probe_audiotag("/nonexistent-slice.mp3")
        check("S10 audiotag no key -> UNPROVEN, reachable",
              r["verdict"] == "UNPROVEN" and r.get("reachable") is True)
    finally:
        urllib.request.urlopen = real_urlopen
        _restore_keys(old)

    # S11: run_probe end-to-end, 1 song, faked network, fake keys set.
    fake = FakeNet()
    real_urlopen = _patch_net(fake)
    old = _set_fake_keys()
    real_fp = _patch_ffprobe(341.0)
    try:
        results = sp.run_probe(song_titles=["Taziyane (Remix)"], timeout=5)
        sites = results["songs"][0]["results"]
        wp_sites = [r for r in sites if r.get("kind") == "persian_wp"]
        check("S11 16 Persian WP site results", len(wp_sites) == 16)
        check("S11 all 16 WP sites MAIN",
              all(r["verdict"] == "MAIN" for r in wp_sites))
        check("S11 summary MAIN 16",
              results["summary"].get("MAIN") == 16)
        ident = results["identification"].get("Taziyane (Remix)", {})
        check("S11 slice fails on fake mp3 bytes -> slice_error, no engines",
              ident.get("audio", {}).get("slice_error")
              == "ffmpeg 15s slice failed"
              and ident.get("engines") == [])
        dump = json.dumps(results, ensure_ascii=False)
        check("S11 no fake key value in the JSON dump",
              all(v not in dump for v in FAKE_KEYS.values()))
    finally:
        urllib.request.urlopen = real_urlopen
        sp.ffprobe_duration = real_fp
        _restore_keys(old)

    # S12: webpanel carries the /probe-sources route.
    import threading
    check("S12 webpanel has probe lock + worker",
          isinstance(wp._SOURCES_PROBE_LOCK, type(threading.Lock()))
          and callable(wp._run_sources_probe_async))
    src = Path(wp.__file__).read_text(encoding="utf-8")
    check("S12 webpanel source contains /probe-sources route",
          '"/probe-sources"' in src and "run_probe" in src)

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
