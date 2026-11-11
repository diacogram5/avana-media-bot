#!/usr/bin/env python3
# TEMPORARY — Render sources probe (stage 5, 2026-11-11). NOT a feature:
# it answers, from Render's own network, which Persian full-version
# sites serve verified files and which ID engines answer. Remove this
# module AND the /probe-sources route in app/webpanel.py once the probe
# has run and the verdicts are recorded. No key is ever logged.
"""Render probe for free music sources + identification engines — AvanoMediaBot.

STANDALONE tool. No dependency on the bot code. No hardcoded keys:
keys only from env (JAMENDO_CLIENT_ID, AUDIUS_API_KEY, LASTFM_API_KEY,
RAPIDAPI_KEY, AUDD_API_TOKEN, ACOUSTID_API_KEY, AUDIOTAG_API_KEY).
Run ON Render (real network) — sandbox DNS is fake, so sandbox results
prove nothing. Output: JSON on stdout. Exit 0 always (per-check verdicts
carry the result); exit 2 on harness error.

Usage:
    python3 probe_sources.py [--song Taziyane] [--all] [--timeout 10]

Test songs (expected durations, seconds):
    Taziyane (Remix) / Amir Tataloo -> 341
    Sob Zohr Shab / Ho3ein -> 196.8
    Avatar / Reza Pishro -> 332
    Joft 6 / HesamTiem -> 225.5
    Saf-o Sadeh / Mehdi Ahmadvand -> unknown (expected duration is
        resolved from Deezer/iTunes inside the probe when possible;
        the duration check is skipped when it stays unknown)

Sections:
    A. Persian WordPress sites (per song): wp-json 200 -> search hit ->
       mp3 extractable -> ffprobe duration vs expected (or CANDIDATE
       when the expected duration is unknown but the mp3 probes OK).
    B. Identification engines (once, on a real Taziyane mp3 downloaded
       during section A): shazamio (the live path), RapidAPI Shazam
       detect (same Shazam DB, different egress), AudD status (ONE
       call — answers the quota-exhaustion question), AcoustID
       reachability (key required for a real lookup), AudioTag
       reachability (free API, personal key required for a lookup).
    C. Metadata validators (Deezer, iTunes, MusicBrainz) + full-file
       APIs (Jamendo, Audius, Internet Archive, Last.fm if keyed).

Verdicts: MAIN (proven usable) / CANDIDATE (reachable + file OK, one
check unverified) / METADATA (title/duration only, never a file) /
NEITHER (blocked, wrong, or login/paywalled, with reason) / UNPROVEN
(could not test — usually a missing key).
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error

UA = {"User-Agent": "AvanoMediaBot-Probe/1.0 (architecture evaluation; contact: admin)"}
TEST_SONGS = [
    {"title": "Taziyane (Remix)", "artist": "Amir Tataloo", "expected_s": 341},
    {"title": "Sob Zohr Shab", "artist": "Ho3ein", "expected_s": 196.8},
    {"title": "Avatar", "artist": "Reza Pishro", "expected_s": 332},
    {"title": "Joft 6", "artist": "HesamTiem", "expected_s": 225.5},
    {"title": "Saf-o Sadeh", "artist": "Mehdi Ahmadvand", "expected_s": None},
    # Reel 2, 2026-11-11 13:22 test: Shazam-only (AudD not_found in bg,
    # wrong name in Q4 middle slice). The genuine single-source case.
    {"title": "Beni Biraz Sevseydin", "artist": "Nilay IŞIK", "expected_s": None},
]
# Songs the identification-engine section runs on (needs a downloadable
# mp3 per song; one AudD call per song, so the list stays short).
ID_ENGINE_SONGS = ["Taziyane (Remix)", "Saf-o Sadeh", "Beni Biraz Sevseydin"]
PERSIAN_SITES = [
    "nex1music.com", "musicdel.ir", "pop-music.ir", "musics-fa.com",
    "upmusics.com", "nicmusic.net", "rozmusic.com", "musico.ir",
    "musicpusic.com", "mokhtalefmusic.com",
    # Added 2026-11-11 from Similarweb competitor lists (unprobed):
    "tajmusics.com", "musicsweb.ir", "sevilmusics.com", "songsara.net",
    "behmelody.in", "music-irani.com",
]
MP3_RE = re.compile(r'https?://[^\s"\'<>]+\.mp3(?:\?[^\s"\'<>]*)?', re.I)


def http_get(url, timeout=10, headers=None):
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers=headers or UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(1_000_000)
            return {"ok": True, "status": r.status,
                    "ms": round((time.monotonic() - t0) * 1000),
                    "body": body, "headers": dict(r.headers)}
    except urllib.error.HTTPError as e:
        try:
            body = e.read(1_000_000)
        except Exception:
            body = b""
        return {"ok": False, "status": e.code,
                "ms": round((time.monotonic() - t0) * 1000),
                "body": body, "error": "http_error"}
    except Exception as e:  # timeout / DNS / connection
        return {"ok": False, "status": None,
                "ms": round((time.monotonic() - t0) * 1000),
                "body": b"", "error": type(e).__name__ + ": " + str(e)[:200]}


def http_post_raw(url, data: bytes, headers, timeout=20):
    """POST raw bytes; returns {ok, status, ms, body, error}."""
    t0 = time.monotonic()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(1_000_000)
            return {"ok": True, "status": r.status,
                    "ms": round((time.monotonic() - t0) * 1000),
                    "body": body, "headers": dict(r.headers)}
    except urllib.error.HTTPError as e:
        try:
            body = e.read(1_000_000)
        except Exception:
            body = b""
        return {"ok": False, "status": e.code,
                "ms": round((time.monotonic() - t0) * 1000),
                "body": body, "error": "http_error"}
    except Exception as e:
        return {"ok": False, "status": None,
                "ms": round((time.monotonic() - t0) * 1000),
                "body": b"", "error": type(e).__name__ + ": " + str(e)[:200]}


def ffprobe_duration(url, timeout=30):
    """Duration of a remote mp3 via ffprobe (no full download)."""
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", url],
            capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "").strip()
        return {"ok": p.returncode == 0 and bool(out),
                "duration_s": float(out) if out else None,
                "ms": round((time.monotonic() - t0) * 1000),
                "stderr": (p.stderr or "")[:200]}
    except Exception as e:
        return {"ok": False, "duration_s": None,
                "ms": round((time.monotonic() - t0) * 1000),
                "error": type(e).__name__ + ": " + str(e)[:200]}


def probe_expected_duration(song, timeout):
    """Resolve an unknown expected duration from Deezer search.

    Returns (duration_s or None, source string). Only used when the
    test song has no known expected duration (Saf-o Sadeh).
    """
    q = urllib.parse.quote(f"{song['artist']} {song['title']}")
    r = http_get(f"https://api.deezer.com/search?q={q}", timeout)
    if not r["ok"]:
        return None, "deezer_unreachable"
    try:
        data = json.loads(r["body"])
        items = data.get("data") or []
        for it in items:
            dur = it.get("duration")
            if dur:
                return float(dur), "deezer"
    except Exception:
        pass
    return None, "deezer_no_match"


def probe_persian_site(site, song, timeout):
    """Checklist (a): wp-json 200 -> search hit -> mp3 extractable -> ffprobe duration."""
    checks, base = {}, f"https://{site}"
    r = http_get(base + "/wp-json/", timeout)
    checks["wp_json_200"] = {"pass": r["ok"] and r["status"] == 200,
                             "status": r["status"], "ms": r["ms"],
                             "error": r.get("error")}
    if not checks["wp_json_200"]["pass"]:
        return {"source": site, "kind": "persian_wp", "checks": checks,
                "verdict": "NEITHER", "reason": "wp-json not reachable"}
    q = urllib.parse.quote(song["title"])
    r = http_get(base + f"/wp-json/wp/v2/posts?search={q}&per_page=5", timeout)
    found = False
    mp3s = []
    if r["ok"] and r["status"] == 200:
        try:
            posts = json.loads(r["body"])
            for post in posts if isinstance(posts, list) else []:
                text = json.dumps(post, ensure_ascii=False)
                mp3s += MP3_RE.findall(text)
                if song["title"].split("(")[0].strip().lower() in text.lower():
                    found = True
        except Exception as e:
            checks["search_parse_error"] = str(e)[:200]
    mp3s = sorted(set(mp3s))
    checks["search_hit"] = {"pass": found, "status": r["status"], "ms": r["ms"]}
    checks["mp3_extractable"] = {"pass": bool(mp3s), "mp3s": mp3s[:3],
                                 "count": len(mp3s)}
    exp = song.get("expected_s")
    if not mp3s:
        return {"source": site, "kind": "persian_wp", "checks": checks,
                "verdict": "NEITHER", "reason": "no mp3 in search results"}
    d = ffprobe_duration(mp3s[0], timeout=30)
    checks["ffprobe_duration"] = {**d, "expected_s": exp}
    if exp is None:
        checks["ffprobe_duration"]["within_10s"] = None
        return {"source": site, "kind": "persian_wp", "checks": checks,
                "verdict": "CANDIDATE",
                "reason": "mp3 probes OK, expected duration unknown"}
    match = bool(d["duration_s"]) and abs(d["duration_s"] - exp) <= 10
    checks["ffprobe_duration"]["within_10s"] = match
    verdict = "MAIN" if match else "NEITHER"
    reason = "duration match" if match else "duration mismatch"
    return {"source": site, "kind": "persian_wp", "checks": checks,
            "verdict": verdict, "reason": reason}


def probe_metadata_api(name, url, timeout, duration_path=None):
    r = http_get(url, timeout)
    out = {"source": name, "kind": "metadata", "checks": {
        "http": {"pass": r["ok"], "status": r["status"], "ms": r["ms"],
                 "error": r.get("error")}}}
    if r["ok"]:
        try:
            data = json.loads(r["body"])
            out["checks"]["json_parse"] = {"pass": True}
            if isinstance(data, dict) and "error" in data:
                out["checks"]["api_error"] = {"pass": False, "error": data["error"]}
                out.update(verdict="NEITHER", reason="api returned error object")
                return out
            out["checks"]["has_results"] = {"pass": bool(data)}
        except Exception as e:
            out["checks"]["json_parse"] = {"pass": False, "error": str(e)[:200]}
    out.update(verdict="METADATA" if r["ok"] else "NEITHER",
               reason="answers" if r["ok"] else "unreachable")
    return out


# --- Section B: identification engines (needs a real local mp3) ----------

def download_mp3(url, timeout=60, max_bytes=12 * 1024 * 1024):
    """Download one mp3 to a temp file. Returns (path or None, info)."""
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None, {"ok": False, "error": "too_large",
                          "ms": round((time.monotonic() - t0) * 1000)}
        fd, path = tempfile.mkstemp(suffix=".mp3")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path, {"ok": True, "status": 200, "bytes": len(data),
                      "ms": round((time.monotonic() - t0) * 1000)}
    except Exception as e:
        return None, {"ok": False,
                      "error": type(e).__name__ + ": " + str(e)[:200],
                      "ms": round((time.monotonic() - t0) * 1000)}


def ffmpeg_slice(src, start_s, dur_s, dst, extra=None):
    """Cut a slice with ffmpeg. Returns True on success."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(start_s),
           "-t", str(dur_s), "-i", src] + (extra or []) + [dst]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0
    except Exception:
        return False


def _parse_shazam_track(body: bytes):
    """Parse a Shazam detect response (same shape shazamio returns):
    {track: {title, subtitle}}. Returns (title, artist) or (None, None)."""
    try:
        data = json.loads(body)
    except Exception:
        return None, None
    track = data.get("track") or {}
    title = (track.get("title") or "").strip() or None
    artist = (track.get("subtitle") or "").strip() or None
    return title, artist


def probe_shazamio(data: bytes, timeout=35):
    """The live path: shazamio recognize on real audio bytes."""
    try:
        from shazamio import Shazam
    except Exception:
        return {"engine": "shazamio", "verdict": "UNPROVEN",
                "reason": "shazamio not installed"}

    async def _run():
        shazam = Shazam()
        try:
            return await asyncio.wait_for(shazam.recognize(data), timeout=30)
        finally:
            try:
                await shazam.close()
            except Exception:
                pass

    t0 = time.monotonic()
    try:
        payload = asyncio.run(_run())
    except Exception as e:
        return {"engine": "shazamio", "verdict": "NEITHER",
                "reason": "lookup error",
                "error": type(e).__name__ + ": " + str(e)[:200],
                "ms": round((time.monotonic() - t0) * 1000)}
    title, artist = _parse_shazam_track(
        json.dumps(payload or {}).encode("utf-8"))
    return {"engine": "shazamio",
            "verdict": "MAIN" if title else "NEITHER",
            "reason": "matched" if title else "no match",
            "title": title, "artist": artist,
            "ms": round((time.monotonic() - t0) * 1000)}


def probe_rapidapi_shazam(wav_bytes: bytes, timeout=20):
    """Same Shazam DB, different egress: apidojo Shazam via RapidAPI.

    POST raw 3-5s 44.1kHz WAV to /songs/v2/detect (fallback /songs/detect).
    403 means the account is not subscribed to the Shazam listing —
    a user action (subscribe to the BASIC free plan on RapidAPI). If
    that BASIC plan requires a card on file, the no-payment order
    blocks this path; the probe reports the HTTP facts only.
    """
    key = os.environ.get("RAPIDAPI_KEY")
    if not key:
        return {"engine": "rapidapi_shazam", "verdict": "UNPROVEN",
                "reason": "RAPIDAPI_KEY not set"}
    headers = {"content-type": "text/plain",
               "X-RapidAPI-Key": key,
               "X-RapidAPI-Host": "shazam.p.rapidapi.com"}
    for path in ("/songs/v2/detect", "/songs/detect"):
        r = http_post_raw("https://shazam.p.rapidapi.com" + path,
                          wav_bytes, headers, timeout)
        if r["status"] == 404:
            continue
        if r["status"] == 403:
            return {"engine": "rapidapi_shazam", "verdict": "UNPROVEN",
                    "reason": "not_subscribed",
                    "endpoint": path, "status": 403,
                    "note": "account not subscribed to the Shazam listing; "
                            "user action: subscribe to the BASIC free plan",
                    "ms": r["ms"]}
        if not r["ok"]:
            return {"engine": "rapidapi_shazam", "verdict": "NEITHER",
                    "reason": "request failed", "endpoint": path,
                    "status": r["status"], "error": r.get("error"),
                    "ms": r["ms"]}
        title, artist = _parse_shazam_track(r["body"])
        return {"engine": "rapidapi_shazam",
                "verdict": "MAIN" if title else "NEITHER",
                "reason": "matched" if title else "no match",
                "endpoint": path, "title": title, "artist": artist,
                "ms": r["ms"]}
    return {"engine": "rapidapi_shazam", "verdict": "NEITHER",
            "reason": "both detect endpoints 404"}


def probe_audd(slice_path, timeout=30):
    """ONE AudD call on a real slice. Answers the quota question:
    identified/not_found = token alive; an error object with an
    error_code (e.g. quota) is recorded verbatim. Never logs the token.
    """
    token = os.environ.get("AUDD_API_TOKEN")
    if not token:
        return {"engine": "audd", "verdict": "UNPROVEN",
                "reason": "AUDD_API_TOKEN not set"}
    try:
        with open(slice_path, "rb") as f:
            audio = f.read()
    except Exception as e:
        return {"engine": "audd", "verdict": "UNPROVEN",
                "reason": "slice unreadable", "error": str(e)[:200]}
    boundary = "----AvanoProbe" + str(int(time.time()))
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="api_token"\r\n\r\n{token}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="return"\r\n\r\napple_music,spotify\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="slice.mp3"\r\n'
        f"Content-Type: audio/mpeg\r\n\r\n"
    ).encode("utf-8") + audio + f"\r\n--{boundary}--\r\n".encode("utf-8")
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    r = http_post_raw("https://api.audd.io/", body, headers, timeout)
    out = {"engine": "audd", "ms": r["ms"], "status": r["status"]}
    if not r["ok"]:
        out.update(verdict="NEITHER", reason="request failed",
                   error=r.get("error"))
        return out
    try:
        payload = json.loads(r["body"])
    except Exception:
        out.update(verdict="NEITHER", reason="unparseable response")
        return out
    if isinstance(payload, dict) and payload.get("status") == "error":
        err = payload.get("error") or {}
        out.update(verdict="NEITHER", reason="api error",
                   error_code=err.get("error_code"),
                   error_message=err.get("error_message"))
        return out
    result = (payload or {}).get("result") or {}
    title = (result.get("title") or "").strip() or None
    artist = (result.get("artist") or "").strip() or None
    out.update(verdict="MAIN" if title else "NEITHER",
               reason="matched" if title else "no match",
               title=title, artist=artist)
    return out


def _fpcalc_path():
    for cand in (shutil.which("fpcalc"), "./app/bin/fpcalc",
                 "/opt/render/project/src/app/bin/fpcalc"):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def probe_acoustid(slice_path, timeout=15):
    """AcoustID: (a) reachability without a key (expect a 400 error
    JSON — proves the host answers from Render); (b) a real lookup
    only when ACOUSTID_API_KEY is set (free registration at
    acoustid.org; the user sets it in Render env — never in chat).
    The docs example key is NEVER used here.
    """
    r = http_get("https://api.acoustid.org/v2/lookup", timeout)
    reachable = r["status"] in (400, 401, 403)
    key = os.environ.get("ACOUSTID_API_KEY")
    if not key:
        return {"engine": "acoustid", "verdict": "UNPROVEN",
                "reason": "ACOUSTID_API_KEY not set",
                "reachable": reachable, "reach_status": r["status"],
                "note": "free key: register at acoustid.org, set in Render env",
                "ms": r["ms"]}
    fpcalc = _fpcalc_path()
    if not fpcalc:
        return {"engine": "acoustid", "verdict": "UNPROVEN",
                "reason": "fpcalc unavailable", "reachable": reachable}
    try:
        p = subprocess.run([fpcalc, slice_path], capture_output=True,
                           text=True, timeout=30)
        fp = dur = None
        for line in (p.stdout or "").splitlines():
            if line.startswith("FINGERPRINT="):
                fp = line.split("=", 1)[1].strip()
            elif line.startswith("DURATION="):
                dur = line.split("=", 1)[1].strip()
    except Exception as e:
        return {"engine": "acoustid", "verdict": "NEITHER",
                "reason": "fpcalc failed", "error": str(e)[:200]}
    if not fp or not dur:
        return {"engine": "acoustid", "verdict": "NEITHER",
                "reason": "no fingerprint from fpcalc"}
    q = urllib.parse.urlencode({
        "client": key, "meta": "recordings", "duration": dur,
        "fingerprint": fp})
    r = http_get("https://api.acoustid.org/v2/lookup?" + q, timeout)
    out = {"engine": "acoustid", "ms": r["ms"], "status": r["status"]}
    if not r["ok"]:
        out.update(verdict="NEITHER", reason="lookup failed",
                   error=r.get("error"))
        return out
    try:
        data = json.loads(r["body"])
    except Exception:
        out.update(verdict="NEITHER", reason="unparseable response")
        return out
    results = (data or {}).get("results") or []
    best = None
    for res in results:
        if res.get("score", 0) >= 0.8:
            recs = res.get("recordings") or []
            if recs:
                best = (res, recs[0])
                break
    if not best:
        out.update(verdict="NEITHER", reason="no match >= 0.8")
        return out
    res, rec = best
    artists = rec.get("artists") or []
    out.update(verdict="MAIN", reason="matched",
               score=res.get("score"), title=rec.get("title"),
               artist=", ".join(a.get("name") for a in artists if a.get("name")))
    return out


def probe_audiotag(slice_path, timeout=30):
    """AudioTag: free API, personal key from a free account
    (register at audiotag.info/apisection; key in Render env as
    AUDIOTAG_API_KEY — never in chat). Without a key: reachability
    check only (POST without apikey -> an error JSON proves the host
    answers from Render). With a key: one `identify` upload of the
    15s slice; the raw response shape is recorded (the API doc is
    registered-users-only, so the probe documents what the API
    actually returns instead of assuming a shape).
    """
    boundary = "----AvanoProbe" + str(int(time.time()))
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    body_no_key = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="action"\r\n\r\nidentify\r\n'
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    r = http_post_raw("https://audiotag.info/api", body_no_key, headers, timeout)
    reachable = r["status"] is not None
    key = os.environ.get("AUDIOTAG_API_KEY")
    if not key:
        return {"engine": "audiotag", "verdict": "UNPROVEN",
                "reason": "AUDIOTAG_API_KEY not set",
                "reachable": reachable, "reach_status": r["status"],
                "note": "free key: free account at audiotag.info/apisection, "
                        "set in Render env",
                "ms": r["ms"]}
    try:
        with open(slice_path, "rb") as f:
            audio = f.read()
    except Exception as e:
        return {"engine": "audiotag", "verdict": "UNPROVEN",
                "reason": "slice unreadable", "error": str(e)[:200],
                "reachable": reachable}
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="apikey"\r\n\r\n{key}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="action"\r\n\r\nidentify\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="slice.mp3"\r\n'
        f"Content-Type: audio/mpeg\r\n\r\n"
    ).encode("utf-8") + audio + f"\r\n--{boundary}--\r\n".encode("utf-8")
    r = http_post_raw("https://audiotag.info/api", body, headers, timeout)
    out = {"engine": "audiotag", "ms": r["ms"], "status": r["status"],
           "reachable": reachable}
    if not r["ok"]:
        out.update(verdict="NEITHER", reason="request failed",
                   error=r.get("error"))
        return out
    out["raw_snippet"] = r["body"][:1000].decode("utf-8", "ignore")
    try:
        payload = json.loads(r["body"])
    except Exception:
        out.update(verdict="NEITHER", reason="unparseable response")
        return out
    title = artist = None
    res = payload.get("result")
    if isinstance(res, list) and res:
        first = res[0] or {}
        title = (first.get("title") or "").strip() or None
        artist = (first.get("artist") or "").strip() or None
    elif isinstance(res, dict):
        title = (res.get("title") or "").strip() or None
        artist = (res.get("artist") or "").strip() or None
    out.update(verdict="MAIN" if title else "NEITHER",
               reason="matched" if title else "no match",
               title=title, artist=artist)
    return out


def run_identification_engines(song, mp3_urls, timeout=10):
    """Section B: download one real mp3, slice it, run all engines.

    mp3_urls: list of mp3 URLs found for this song (preferred order).
    Returns a dict; never raises, never logs a key.
    """
    out = {"song": song["title"], "audio": None, "engines": []}
    local = None
    info = None
    for url in (mp3_urls or [])[:3]:
        local, info = download_mp3(url, timeout=60)
        if local:
            out["audio"] = {"url_host": urllib.parse.urlparse(url).netloc,
                            "bytes": info.get("bytes"), "ms": info.get("ms")}
            break
    if not local:
        out["audio"] = {"error": "no mp3 downloadable", "detail": info}
        return out
    try:
        dur = None
        try:
            d = ffprobe_duration("file://" + local, timeout=20)
            dur = d.get("duration_s")
        except Exception:
            dur = None
        start = max(0, int((dur or 180) / 2) - 7)
        tmp = tempfile.gettempdir()
        slice_path = os.path.join(tmp, "probe_slice.mp3")
        seg_path = os.path.join(tmp, "probe_seg.wav")
        if not ffmpeg_slice(local, start, 15, slice_path):
            out["audio"]["slice_error"] = "ffmpeg 15s slice failed"
            return out
        with open(slice_path, "rb") as f:
            slice_bytes = f.read()
        out["engines"].append(probe_shazamio(slice_bytes))
        if ffmpeg_slice(local, start, 4, seg_path,
                        extra=["-ar", "44100", "-ac", "1"]):
            with open(seg_path, "rb") as f:
                seg_bytes = f.read()
            out["engines"].append(probe_rapidapi_shazam(seg_bytes))
        else:
            out["engines"].append(
                {"engine": "rapidapi_shazam", "verdict": "UNPROVEN",
                 "reason": "ffmpeg 4s wav segment failed"})
        out["engines"].append(probe_audd(slice_path))
        out["engines"].append(probe_acoustid(slice_path))
        out["engines"].append(probe_audiotag(slice_path))
    finally:
        try:
            os.unlink(local)
        except Exception:
            pass
    return out


def run_probe(song_titles=None, timeout=10):
    """Full probe for the selected songs + the identification engines.

    song_titles: None = all TEST_SONGS. Returns the results dict.
    """
    songs = [s for s in TEST_SONGS
             if song_titles is None or s["title"] in song_titles]
    results = {"songs": [], "identification": {}, "summary": {}}
    mp3s_by_song = {}
    for song in songs:
        song = dict(song)
        entry = {"song": song, "results": []}
        if song.get("expected_s") is None:
            dur, src = probe_expected_duration(song, timeout)
            song["expected_s"] = dur
            entry["expected_duration_source"] = src
        for site in PERSIAN_SITES:
            res = probe_persian_site(site, song, timeout)
            entry["results"].append(res)
            if res["verdict"] in ("MAIN", "CANDIDATE"):
                mp3s = (res.get("checks", {}).get("mp3_extractable", {})
                        .get("mp3s") or [])
                if mp3s:
                    mp3s_by_song.setdefault(song["title"], []).extend(mp3s)
        q = urllib.parse.quote(f"{song['artist']} {song['title']}")
        entry["results"].append(probe_metadata_api(
            "deezer", f"https://api.deezer.com/search?q={q}", timeout))
        entry["results"].append(probe_metadata_api(
            "itunes",
            f"https://itunes.apple.com/search?term={q}&media=music&entity=song&limit=3",
            timeout))
        entry["results"].append(probe_metadata_api(
            "musicbrainz",
            "https://musicbrainz.org/ws/2/recording/?query="
            + urllib.parse.quote(f"recording:{song['title']} AND artist:{song['artist']}")
            + "&fmt=json", timeout))
        results["songs"].append(entry)

    # Full-file APIs (once, on the first song).
    first = songs[0] if songs else TEST_SONGS[0]
    q = urllib.parse.quote(f"{first['artist']} {first['title']}")
    extra = []
    jamendo_key = os.environ.get("JAMENDO_CLIENT_ID")
    if jamendo_key:
        extra.append(probe_metadata_api(
            "jamendo",
            f"https://api.jamendo.com/v3.0/tracks/?client_id={jamendo_key}"
            f"&format=json&name={urllib.parse.quote(first['title'])}", timeout))
    else:
        extra.append({"source": "jamendo", "kind": "full_file_api",
                      "verdict": "UNPROVEN",
                      "reason": "JAMENDO_CLIENT_ID not set"})
    audius_key = os.environ.get("AUDIUS_API_KEY")
    extra.append(probe_metadata_api(
        "audius_keyless",
        f"https://discoveryprovider.audius.co/v1/tracks/search?query={q}"
        f"&app_name=AvanoProbe", timeout))
    if audius_key:
        extra.append(probe_metadata_api(
            "audius_keyed",
            f"https://api.audius.co/v1/tracks/search?query={q}"
            f"&app_name=AvanoProbe&api_key={audius_key}", timeout))
    extra.append(probe_metadata_api(
        "internet_archive",
        "https://archive.org/advancedsearch.php?q="
        + urllib.parse.quote(f"title:({first['title']}) AND mediatype:audio")
        + "&fl[]=identifier&fl[]=title&fl[]=creator&rows=5&output=json", timeout))
    lastfm_key = os.environ.get("LASTFM_API_KEY")
    if lastfm_key:
        extra.append(probe_metadata_api(
            "lastfm",
            "https://ws.audioscrobbler.com/2.0/?method=track.getInfo"
            f"&api_key={lastfm_key}&artist={urllib.parse.quote(first['artist'])}"
            f"&track={urllib.parse.quote(first['title'])}&format=json", timeout))
    results["songs"][0]["results"].extend(extra)

    # Section B: identification engines on a real mp3 per song
    # (Taziyane + Saf-o Sadeh + the Turkish single-source case).
    # Prefer javanmelody / melonmusic / musicdel hosts first.
    def _pref(u):
        h = urllib.parse.urlparse(u).netloc
        for i, k in enumerate(("javanmelody", "melonmusic", "musicdel")):
            if k in h:
                return i
        return 9

    song_by_title = {s["title"]: s for s in songs}
    for title in ID_ENGINE_SONGS:
        urls = mp3s_by_song.get(title)
        if not urls or title not in song_by_title:
            continue
        urls = sorted(set(urls), key=_pref)
        results["identification"][title] = run_identification_engines(
            song_by_title[title], urls, timeout)
    if not results["identification"]:
        results["identification"] = {
            "note": "no downloadable mp3 found for any ID-engine song — "
                    "identification engines not tested"}

    summary = {}
    for entry in results["songs"]:
        for r in entry["results"]:
            summary.setdefault(r["verdict"], 0)
            summary[r["verdict"]] += 1
    for _title, ident in results["identification"].items():
        if isinstance(ident, dict):
            for e in ident.get("engines", []):
                summary.setdefault("id:" + e["verdict"], 0)
                summary["id:" + e["verdict"]] += 1
    results["summary"] = summary
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--song", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--timeout", type=int, default=10)
    args = ap.parse_args()
    if args.all or args.song is None:
        titles = None if args.all else ["Taziyane (Remix)"]
    else:
        titles = [args.song]
    print(json.dumps(run_probe(titles, args.timeout),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
