"""Smart Remix engine (BETA - admin only).

A self-contained module for creating DJ-style remixes from 2 user-sent
audio files. Uses Essentia for BPM/key analysis and FFmpeg (with the
built-in rubberband filter) for tempo-matching and crossfading.

Safety limits (hard-coded for the beta):
- Exactly 2 files per request.
- Max 60 seconds per file.
- Only one remix job at a time (global asyncio lock).
- Temp files are always cleaned up.
- The module never touches tokens, admin IDs, or other bot settings.

The feature can be disabled entirely via REMIX_BETA_ENABLED = False.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import os
import shutil
import subprocess
import tempfile
import time

log = logging.getLogger("avana.remix")

# Master switch for the beta feature. Set to False to disable completely.
REMIX_BETA_ENABLED = True

# Beta limits
MAX_FILES = 2
MAX_DURATION_SEC = 60
MAX_FILE_MB = 15
OUTPUT_BITRATE = "192k"
CROSSFADE_SEC = 8

# Global lock: only one remix job at a time.
_remix_lock = asyncio.Lock()


def is_enabled() -> bool:
    return REMIX_BETA_ENABLED


def is_locked() -> bool:
    return _remix_lock.locked()


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output. Raises on non-zero exit."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )


def probe_duration(path: str) -> float:
    """Return audio duration in seconds via ffprobe. Raises on failure."""
    r = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ], timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip()[:200]}")
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise RuntimeError("ffprobe returned invalid duration")


def analyze_track(path: str) -> dict:
    """Analyze BPM, key and scale with Essentia.

    Returns {"bpm": float, "key": str, "scale": str, "confidence": float}.
    Raises RuntimeError on failure.
    """
    try:
        import essentia.standard as es
    except ImportError as e:
        raise RuntimeError("essentia is not installed") from e

    # Load as mono 44100 Hz for consistent analysis
    loader = es.MonoLoader(filename=path, sampleRate=44100)
    audio = loader()

    rhythm = es.RhythmExtractor2013(method="multifeature")
    bpm, _beats, confidence, _ticks, _estimates = rhythm(audio)

    key_ext = es.KeyExtractor()
    key, scale, strength = key_ext(audio)

    # Free large buffers promptly (512 MB Render box)
    del audio
    gc.collect()

    return {
        "bpm": float(bpm),
        "key": str(key),
        "scale": str(scale),
        "confidence": float(confidence),
        "key_strength": float(strength),
    }


def build_remix(file1: str, file2: str, out_path: str,
                info1: dict, info2: dict,
                progress_cb=None) -> dict:
    """Build the remix MP3 from two analyzed files.

    Steps:
    1. Tempo-match file2 to file1's BPM via FFmpeg rubberband filter.
    2. Crossfade the two tracks with an 8-second overlap.
    3. Encode to MP3 192k.

    Returns {"output": path, "size_mb": float, "elapsed_sec": float}.
    """
    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="remix_")
    try:
        bpm1 = info1["bpm"] or 120.0
        bpm2 = info2["bpm"] or 120.0
        if bpm1 <= 0:
            bpm1 = 120.0
        if bpm2 <= 0:
            bpm2 = 120.0
        ratio = bpm1 / bpm2
        # Clamp to sane time-stretch range to avoid artifacts
        ratio = max(0.8, min(1.25, ratio))

        if progress_cb:
            progress_cb("sync", ratio)

        # Step 1: tempo-match file2 -> file1 BPM
        matched2 = os.path.join(tmpdir, "track2_matched.wav")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", file2]
        if abs(ratio - 1.0) > 0.005:
            cmd += ["-filter:a", f"rubberband=tempo={ratio:.4f}"]
        cmd += ["-ar", "44100", "-ac", "2", matched2]
        r = _run(cmd, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"tempo-match failed: {r.stderr.strip()[:200]}")

        # Also normalize file1 to the same sample format
        norm1 = os.path.join(tmpdir, "track1_norm.wav")
        r = _run(["ffmpeg", "-y", "-v", "error", "-i", file1,
                  "-ar", "44100", "-ac", "2", norm1], timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"normalize failed: {r.stderr.strip()[:200]}")

        if progress_cb:
            progress_cb("xfade", None)

        # Step 2: crossfade with overlap
        r = _run([
            "ffmpeg", "-y", "-v", "error",
            "-i", norm1, "-i", matched2,
            "-filter_complex",
            f"[0:a][1:a]acrossfade=d={CROSSFADE_SEC}:c1=tri:c2=tri[a]",
            "-map", "[a]", "-c:a", "libmp3lame", "-b:a", OUTPUT_BITRATE,
            out_path,
        ], timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"mix failed: {r.stderr.strip()[:200]}")

        size_mb = os.path.getsize(out_path) / 1024 / 1024
        return {
            "output": out_path,
            "size_mb": round(size_mb, 2),
            "elapsed_sec": round(time.time() - t0, 1),
            "bpm1": round(bpm1, 1),
            "bpm2": round(bpm2, 1),
            "ratio": round(ratio, 3),
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        gc.collect()


async def run_remix_job(file1: str, file2: str, out_path: str,
                        status_cb=None) -> dict:
    """Full remix job: validate -> analyze -> mix. Runs under the global
    lock so only one job executes at a time. Heavy work runs in a thread
    so the bot's event loop stays responsive.

    status_cb(stage: str, detail) is called with stages:
    "validate" | "analyze1" | "analyze2" | "sync" | "xfade" | "done" | "error"
    """
    if not is_enabled():
        raise RuntimeError("remix beta is disabled")

    async with _remix_lock:
        loop = asyncio.get_running_loop()
        t_start = time.time()

        def _sync_progress(stage, detail):
            if status_cb:
                try:
                    status_cb(stage, detail)
                except Exception:
                    pass

        try:
            # --- validate ---
            _sync_progress("validate", None)
            for p in (file1, file2):
                if not os.path.exists(p):
                    raise RuntimeError("input file missing")
                if os.path.getsize(p) / 1024 / 1024 > MAX_FILE_MB:
                    raise RuntimeError(
                        f"file too large (max {MAX_FILE_MB} MB)")
            dur1 = await loop.run_in_executor(None, probe_duration, file1)
            dur2 = await loop.run_in_executor(None, probe_duration, file2)
            if dur1 > MAX_DURATION_SEC or dur2 > MAX_DURATION_SEC:
                raise RuntimeError(
                    f"each file must be under {MAX_DURATION_SEC}s "
                    f"(got {dur1:.0f}s / {dur2:.0f}s)")

            # --- analyze ---
            _sync_progress("analyze1", None)
            info1 = await loop.run_in_executor(None, analyze_track, file1)
            _sync_progress("analyze2", info1)
            info2 = await loop.run_in_executor(None, analyze_track, file2)

            # --- mix ---
            _sync_progress("mix", (info1, info2))
            result = await loop.run_in_executor(
                None, build_remix, file1, file2, out_path,
                info1, info2, _sync_progress)

            result["total_sec"] = round(time.time() - t_start, 1)
            result["info1"] = info1
            result["info2"] = info2
            _sync_progress("done", result)
            return result
        except Exception as e:
            _sync_progress("error", str(e))
            raise
