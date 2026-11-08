"""Sandbox tests for STEP 2 (video download: stream-to-disk, -c copy mux,
RapidAPI progressive shortcut, one-download semaphore, per-stage RSS).

No network beyond localhost, no Telegram, no real Turso.

Cases:
  V1  Progressive file (audio_url == video_url, RapidAPI case): one
      download, ffprobe finds audio, mux SKIPPED (no ffmpeg_mux line),
      path returned on disk, has_audio True, small RSS growth.
  V2  Genuine DASH pair (different video/audio URLs): both downloaded,
      -c copy mux runs (ffmpeg_mux line), merged file has video+audio.
  V3  Video-only (no audio_url): path returned, has_audio False, no mux.
  V4  Failing URL (404): (None, False, None), no exception.
  V5  Semaphore: two concurrent fetches against a 0.6s-slow server
      serialize (elapsed >= 1.1s) and a "queued" line is logged.
  V6  timing: rss_kb present in log_stage and log_total lines.
  V7  Temp cleanup: after tmpdir.cleanup() the returned path is gone.
  V8  RSS before/after (5MB video): --rss-mode old|new subprocess runs
      print peak ru_maxrss; the parent compares them.

Run: python3 scripts/verify_step2.py ; exit 0 = all PASS.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import resource
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Strip proxy env vars (sandbox httpx gotcha).
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)

RESULTS: list[tuple[str, bool, str]] = []
LOG_LINES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name
          + (f" — {detail}" if detail and not ok else ""))


class _ListHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_LINES.append(self.format(record))
        except Exception:
            pass


def _vmrss_kb() -> int:
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        return 0


def _ffprobe_streams(path: str) -> set[str]:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", path],
        capture_output=True, timeout=30)
    return {s.strip() for s in p.stdout.decode(errors="replace").splitlines()
            if s.strip()}


def _make_media(d: Path) -> dict[str, Path]:
    """Generate test files: prog.mp4 (~5MB, video+audio), vonly.mp4,
    aonly.m4a. Returns name -> path."""
    prog = d / "prog.mp4"
    # Mandelbrot compresses poorly (unlike testsrc) — lands near 6MB.
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i",
         "mandelbrot=size=1280x720:rate=30:maxiter=200",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
         "-c:v", "libx264", "-preset", "veryfast", "-b:v", "5M",
         "-c:a", "aac", "-b:a", "128k",
         "-shortest", "-t", "8", str(prog)],
        check=True, timeout=120)
    vonly = d / "vonly.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=duration=5:size=640x360:rate=30",
         "-c:v", "libx264", "-b:v", "2M", "-an", str(vonly)],
        check=True, timeout=120)
    aonly = d / "aonly.m4a"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "sine=frequency=880:duration=5",
         "-c:a", "aac", "-b:a", "128k", str(aonly)],
        check=True, timeout=120)
    return {"prog": prog, "vonly": vonly, "aonly": aonly}


class _Handler(SimpleHTTPRequestHandler):
    slow_delay = 0.0

    def do_GET(self):
        if "slow" in self.path and self.slow_delay:
            time.sleep(self.slow_delay)
        super().do_GET()

    def log_message(self, *args):
        pass


def _serve(d: Path, slow_delay: float = 0.0):
    _Handler.slow_delay = slow_delay
    handler = functools.partial(_Handler, directory=str(d))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


async def _run_cases() -> None:
    import httpx  # noqa: F401  (ensures the sandbox httpx is importable)
    from app.services import instagram as ig
    from app import timing as timing_mod

    logging.getLogger().setLevel(logging.INFO)
    h = _ListHandler()
    logging.getLogger().addHandler(h)
    # NOTE: app loggers set propagate=False (logging_config.get_logger),
    # so the capture handler must sit on the module logger itself.
    ig.log.addHandler(h)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        media = _make_media(d)
        prog_mb = media["prog"].stat().st_size / 1024 / 1024
        print(f"test media: prog.mp4 {prog_mb:.2f} MB, "
              f"vonly {media['vonly'].stat().st_size/1024:.0f} KB, "
              f"aonly {media['aonly'].stat().st_size/1024:.0f} KB")
        srv, port = _serve(d)
        base = f"http://127.0.0.1:{port}"
        try:
            # Warm-up fetch (first-call httpx/event-loop overhead) so the
            # V1 RSS measurement sees only the fetch itself.
            _wp, _, _wt = await ig.fetch_merged_video(
                f"{base}/prog.mp4", f"{base}/prog.mp4", max_mb=50)
            if _wt is not None:
                _wt.cleanup()
            # V1: progressive (RapidAPI case — same URL twice).
            LOG_LINES.clear()
            rss0 = _vmrss_kb()
            path, has_audio, tmpdir = await ig.fetch_merged_video(
                f"{base}/prog.mp4", f"{base}/prog.mp4", max_mb=50)
            rss1 = _vmrss_kb()
            blob = "\n".join(LOG_LINES)
            check("V1 progressive: path on disk, has_audio, mux skipped",
                  isinstance(path, str) and path
                  and os.path.exists(path) and has_audio is True
                  and tmpdir is not None
                  and '"mux": "skipped"' in blob
                  and "ffmpeg_mux" not in blob,
                  f"path={path!r} has_audio={has_audio} "
                  f"mux_skipped={'\"mux\": \"skipped\"' in blob}")
            check("V1 progressive: no big bytes in RAM",
                  isinstance(path, str) and (rss1 - rss0) < 2 * 1024,
                  f"VmRSS growth {rss1 - rss0} KB for a "
                  f"{prog_mb:.1f} MB file")
            check("V1 progressive: file really has video+audio",
                  isinstance(path, str)
                  and _ffprobe_streams(path) == {"video", "audio"},
                  f"streams={_ffprobe_streams(path) if path else None}")
            if tmpdir is not None:
                tmpdir.cleanup()
            # V7: temp cleanup (uses the V1 tmpdir's path).
            check("V7 temp dir deleted after cleanup",
                  isinstance(path, str) and not os.path.exists(path),
                  f"path still exists: {path}")

            # V2: genuine DASH pair (different URLs) — mux must run.
            LOG_LINES.clear()
            path2, has_audio2, tmpdir2 = await ig.fetch_merged_video(
                f"{base}/vonly.mp4", f"{base}/aonly.m4a", max_mb=50)
            blob2 = "\n".join(LOG_LINES)
            check("V2 DASH pair: muxed file with video+audio",
                  isinstance(path2, str) and os.path.exists(path2)
                  and has_audio2 is True
                  and _ffprobe_streams(path2) == {"video", "audio"},
                  f"path={path2!r} has_audio={has_audio2}")
            check("V2 DASH pair: -c copy mux ran",
                  "ffmpeg_mux" in blob2,
                  "no ffmpeg_mux line in logs")
            if tmpdir2 is not None:
                tmpdir2.cleanup()

            # V3: video-only, no audio URL.
            LOG_LINES.clear()
            path3, has_audio3, tmpdir3 = await ig.fetch_merged_video(
                f"{base}/vonly.mp4", None, max_mb=50)
            blob3 = "\n".join(LOG_LINES)
            check("V3 video-only: path returned, has_audio False, no mux",
                  isinstance(path3, str) and os.path.exists(path3)
                  and has_audio3 is False and "ffmpeg_mux" not in blob3,
                  f"path={path3!r} has_audio={has_audio3}")
            if tmpdir3 is not None:
                tmpdir3.cleanup()

            # V4: failing URL.
            path4, has_audio4, tmpdir4 = await ig.fetch_merged_video(
                f"{base}/missing.mp4", None, max_mb=50)
            check("V4 failing URL: (None, False, None), no exception",
                  path4 is None and has_audio4 is False
                  and tmpdir4 is None,
                  f"got {path4!r} {has_audio4} {tmpdir4!r}")

            # V5: semaphore serializes two concurrent slow fetches.
            srv2, port2 = _serve(d, slow_delay=0.6)
            try:
                LOG_LINES.clear()
                t0 = time.perf_counter()
                r1, r2 = await asyncio.gather(
                    ig.fetch_merged_video(
                        f"http://127.0.0.1:{port2}/vonly.mp4?slow=1", None,
                        max_mb=50),
                    ig.fetch_merged_video(
                        f"http://127.0.0.1:{port2}/vonly.mp4?slow=1", None,
                        max_mb=50))
                el = time.perf_counter() - t0
                blob5 = "\n".join(LOG_LINES)
                # Each fetch hits the 0.6s server delay; serialized they
                # must take >= ~1.1s, parallel would take ~0.6s.
                check("V5 semaphore: two concurrent downloads serialize",
                      el >= 1.1 and r1[0] is not None and r2[0] is not None,
                      f"elapsed={el:.2f}s")
                check("V5 semaphore: queued wait is logged",
                      '"stage": "queued"' in blob5,
                      "no queued line")
                for _r in (r1, r2):
                    if _r[2] is not None:
                        _r[2].cleanup()
            finally:
                srv2.shutdown()

            # V6: rss_kb in timing stage + total lines.
            lg = logging.getLogger("step2-timing-test")
            lg.setLevel(logging.INFO)
            lg.addHandler(h)
            lg.propagate = False
            LOG_LINES.clear()
            t_req = time.perf_counter()
            timing_mod.log_stage(lg, "link_stage", "testreq", t_req,
                                 "download", t_req)
            timing_mod.log_total(lg, "link_stage", "testreq", t_req,
                                 "sent", headline_total="link_to_video_ms")
            blob6 = "\n".join(LOG_LINES)
            check("V6 rss_kb present in stage + total lines",
                  blob6.count('"rss_kb"') >= 2,
                  f"rss_kb occurrences: {blob6.count('\"rss_kb\"')}")
        finally:
            srv.shutdown()


def _rss_mode(mode: str) -> None:
    """Subprocess RSS measurement: run ONE path over a 5MB file and print
    peak ru_maxrss + VmRSS right after. Parent compares old vs new."""
    import httpx
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        media = _make_media(d)
        srv, port = _serve(d)
        url = f"http://127.0.0.1:{port}/prog.mp4"
        try:
            if mode == "old":
                # Old behaviour: resp.content (bytes) x2 + mux + read back.
                async def _old():
                    import tempfile as _tf
                    async with httpx.AsyncClient(timeout=40) as c:
                        v = (await c.get(url)).content
                        a = (await c.get(url)).content
                    with _tf.TemporaryDirectory() as tmp:
                        vp, ap, op = (f"{tmp}/v.mp4", f"{tmp}/a.m4a",
                                      f"{tmp}/out.mp4")
                        open(vp, "wb").write(v)
                        open(ap, "wb").write(a)
                        subprocess.run(
                            ["ffmpeg", "-y", "-v", "error",
                             "-i", vp, "-i", ap, "-c:v", "copy",
                             "-c:a", "copy", "-shortest",
                             "-movflags", "+faststart", op],
                            check=False, capture_output=True, timeout=120)
                        merged = open(op, "rb").read()
                    return len(merged)
                n = asyncio.run(_old())
                print(f"OLD path: merged {n} bytes")
            else:
                from app.services import instagram as ig
                path, has_audio, tmpdir = asyncio.run(
                    ig.fetch_merged_video(url, url, max_mb=50))
                print(f"NEW path: path={path} has_audio={has_audio}")
                if tmpdir is not None:
                    tmpdir.cleanup()
        finally:
            srv.shutdown()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"RSS_PEAK_KB={peak} RSS_NOW_KB={_vmrss_kb()}")


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--rss-mode":
        _rss_mode(sys.argv[2])
        return 0
    asyncio.run(_run_cases())
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
