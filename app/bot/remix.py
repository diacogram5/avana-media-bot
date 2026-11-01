"""Smart Remix engine (BETA - admin only).

A self-contained module for creating DJ-style remixes from 2 user-sent
audio files. Uses Essentia for BPM/key analysis and FFmpeg (with the
built-in rubberband filter) for tempo-matching and crossfading.

How it works (v2 - full songs supported):
1. User sends full songs (no manual trimming needed).
2. Each song is validated (exists, size, duration within safety limits).
3. If a song is longer than 60s, the engine automatically selects the
   best 30-60s section: highest-energy continuous window, avoiding
   fade-in/out regions, with boundaries nudged to quiet points so we
   don't cut mid-word or mid-beat. Falls back to a safe middle section
   if analysis fails.
4. The selected sections go through the standard engine:
   Essentia BPM/key -> FFmpeg rubberband tempo-match -> 8s crossfade
   -> MP3 192k.

Safety limits (hard-coded for the beta):
- Exactly 2 files per request.
- Max 10 minutes / 50 MB per input file (server safety).
- Only one remix job at a time (global asyncio lock).
- Temp files are cleaned up only after processing + delivery.
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

# Beta limits (v2: full songs accepted, server-safe caps)
MAX_FILES = 2
MAX_INPUT_DURATION_SEC = 600   # 10 minutes per input file (hard safety cap)
MAX_INPUT_FILE_MB = 50         # 50 MB per input file (hard safety cap)
OUTPUT_BITRATE = "192k"
CROSSFADE_SEC = 8

# Auto section-selection for long inputs
EXTRACT_TARGET_SEC = 45.0      # target length of the auto-selected section
EXTRACT_MIN_SEC = 30.0         # never extract less than this
EXTRACT_MAX_SEC = 60.0         # inputs at/under this are used as-is
# Fraction of the track edges to skip (fade-in/out, silence, spoken intro)
EDGE_SKIP_FRACTION = 0.05
# Boundary nudge range (seconds) to find a quiet cut point
BOUNDARY_NUDGE_SEC = 2.0

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


def _ensure_readable(path: str) -> str:
    """Validate that an audio file exists and is readable.

    Returns the absolute path. Raises RuntimeError with a clear message
    if the file is missing, empty, or unreadable. This guard runs BEFORE
    any ffprobe/ffmpeg call so we never hit 'No such file or directory'
    from the subprocess.
    """
    apath = os.path.abspath(path)
    if not os.path.exists(apath):
        raise RuntimeError(f"input file not found on disk: {apath}")
    if not os.path.isfile(apath):
        raise RuntimeError(f"input path is not a file: {apath}")
    try:
        size = os.path.getsize(apath)
    except OSError as e:
        raise RuntimeError(f"cannot access input file: {e}") from e
    if size <= 0:
        raise RuntimeError("input file is empty (0 bytes)")
    if not os.access(apath, os.R_OK):
        raise RuntimeError(f"input file is not readable: {apath}")
    return apath


def probe_duration(path: str) -> float:
    """Return audio duration in seconds via ffprobe. Raises on failure."""
    apath = _ensure_readable(path)
    r = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", apath,
    ], timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip()[:200]}")
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise RuntimeError("ffprobe returned invalid duration")


def _energy_profile(path: str, window_sec: float = 1.0,
                    sample_rate: int = 16000) -> list[float]:
    """Compute per-window RMS energy of an audio file.

    Loads the file at a low sample rate (fast, low RAM) and returns the
    RMS energy of each `window_sec`-long window. Used to find the most
    energetic (musically dense) part of a long song.

    Tries Essentia first; falls back to FFmpeg raw-PCM decode + numpy
    if Essentia is unavailable, so section selection still works.
    """
    apath = _ensure_readable(path)
    try:
        import numpy as np
    except ImportError as e:
        raise RuntimeError("numpy is not installed") from e

    audio = None
    try:
        import essentia.standard as es
        loader = es.MonoLoader(filename=apath, sampleRate=sample_rate)
        audio = loader()
    except ImportError:
        # Fallback: decode with FFmpeg to raw float32 PCM.
        log.info("_energy_profile: essentia unavailable, using ffmpeg decode")
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", apath,
             "-ar", str(sample_rate), "-ac", "1",
             "-f", "f32le", "-acodec", "pcm_f32le", "-"],
            capture_output=True, timeout=300)
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError(
                f"ffmpeg decode failed: {r.stderr.decode()[:200]}")
        audio = np.frombuffer(r.stdout, dtype=np.float32).copy()
        del r
        gc.collect()
    except Exception as e:
        raise RuntimeError(f"audio load failed: {e}") from e

    try:
        if audio.size == 0:
            raise RuntimeError("audio decoded to 0 samples")
        win = max(1, int(sample_rate * window_sec))
        n_win = int((audio.size + win - 1) // win)
        profile: list[float] = []
        for i in range(n_win):
            chunk = audio[i * win:(i + 1) * win]
            if chunk.size == 0:
                profile.append(0.0)
            else:
                profile.append(float(np.sqrt(np.mean(chunk ** 2))))
        return profile
    finally:
        del audio
        gc.collect()


def select_best_section(path: str,
                        target_sec: float = EXTRACT_TARGET_SEC
                        ) -> tuple[float, float]:
    """Pick the best `target_sec`-long section of an audio file.

    Returns (start_sec, duration_sec).

    Strategy:
    1. Files at/under EXTRACT_MAX_SEC are used whole (no cut).
    2. Otherwise: compute the per-second energy profile, skip the
       fade-in/out edges, and take the highest-mean-energy continuous
       window — the densest musical part (usually chorus/drop).
    3. Nudge both boundaries to a local energy minimum within
       ±BOUNDARY_NUDGE_SEC so we don't cut in the middle of a loud
       transient, vocal word, or beat.
    4. If analysis fails for any reason, fall back to a safe continuous
       middle section (never random, never empty).

    The original file is never modified; the caller extracts the
    returned window into a temp file.
    """
    apath = _ensure_readable(path)
    duration = probe_duration(apath)

    # Short enough: use the whole file, no extraction needed.
    if duration <= EXTRACT_MAX_SEC:
        return (0.0, duration)

    target = min(target_sec, EXTRACT_MAX_SEC)
    target = max(target, EXTRACT_MIN_SEC)
    if duration <= target:
        return (0.0, duration)

    def _middle_fallback() -> tuple[float, float]:
        start = max(0.0, (duration - target) / 2.0)
        return (start, target)

    try:
        profile = _energy_profile(apath)
    except Exception as e:
        log.warning("select_best_section: energy profile failed (%s), "
                    "using middle fallback", e)
        return _middle_fallback()

    if not profile or max(profile) <= 0:
        return _middle_fallback()

    win_sec = 1.0  # must match _energy_profile default
    n = len(profile)
    target_wins = max(1, int(round(target / win_sec)))

    # Searchable range: skip edges (fade-in/out, silence, spoken intro).
    lo = int(n * EDGE_SKIP_FRACTION)
    hi = n - int(n * EDGE_SKIP_FRACTION) - target_wins
    if hi <= lo:
        lo, hi = 0, max(0, n - target_wins)
    if hi <= lo:
        return _middle_fallback()

    # Sliding-window mean energy; pick the max.
    best_i, best_e = lo, -1.0
    # Prefix sums for O(n) window means.
    prefix = [0.0]
    for v in profile:
        prefix.append(prefix[-1] + v)
    for i in range(lo, hi + 1):
        mean_e = (prefix[i + target_wins] - prefix[i]) / target_wins
        if mean_e > best_e:
            best_e, best_i = mean_e, i

    # Nudge boundaries to local energy minima so cuts land in quieter
    # moments (between phrases/beats) rather than mid-word/mid-hit.
    nudge_wins = max(1, int(round(BOUNDARY_NUDGE_SEC / win_sec)))

    def _nudge(idx: int) -> int:
        s = max(0, idx - nudge_wins)
        e = min(n - 1, idx + nudge_wins)
        return min(range(s, e + 1), key=lambda j: profile[j])

    start_i = _nudge(best_i)
    end_i = _nudge(best_i + target_wins)
    # Keep the window length sane after nudging.
    if end_i - start_i < int(EXTRACT_MIN_SEC / win_sec):
        start_i, end_i = best_i, best_i + target_wins
    start_i = max(0, min(start_i, n - 1))
    end_i = max(start_i + 1, min(end_i, n))

    start_sec = start_i * win_sec
    dur_sec = (end_i - start_i) * win_sec
    # Clamp inside the real duration.
    if start_sec + dur_sec > duration:
        dur_sec = duration - start_sec
    if dur_sec < EXTRACT_MIN_SEC:
        return _middle_fallback()
    log.info("select_best_section: %.1fs -> start=%.1fs len=%.1fs "
             "(energy-guided)", duration, start_sec, dur_sec)
    return (start_sec, dur_sec)


def extract_section(src_path: str, dest_path: str,
                    start_sec: float, duration_sec: float) -> str:
    """Extract [start_sec, start_sec+duration_sec) into dest_path.

    Uses stream copy (no re-encode) for speed, then validates the
    result exists and has a sane duration. The source file is untouched.
    Returns dest_path.
    """
    apath = _ensure_readable(src_path)
    dest_abs = os.path.abspath(dest_path)
    # Guard against degenerate ranges.
    start_sec = max(0.0, start_sec)
    duration_sec = max(1.0, duration_sec)

    r = _run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{start_sec:.2f}",
        "-i", apath,
        "-t", f"{duration_sec:.2f}",
        "-c", "copy",
        dest_abs,
    ], timeout=120)
    if r.returncode != 0 or not os.path.exists(dest_abs):
        # Stream copy can fail on some containers; retry with re-encode.
        log.warning("extract_section: stream copy failed, retrying "
                    "with re-encode")
        r = _run([
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{start_sec:.2f}",
            "-i", apath,
            "-t", f"{duration_sec:.2f}",
            "-c:a", "libmp3lame", "-b:a", "128k",
            dest_abs,
        ], timeout=300)
    if r.returncode != 0:
        raise RuntimeError(
            f"section extract failed: {r.stderr.strip()[:200]}")
    out_dur = probe_duration(dest_abs)
    if out_dur < 1.0:
        raise RuntimeError("extracted section is empty")
    return dest_abs


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
    """Full remix job: validate -> auto-section -> analyze -> mix.

    Runs under the global lock so only one job executes at a time.
    Heavy work runs in a thread so the bot's event loop stays responsive.

    Long inputs (>60s) are automatically trimmed to their best
    30-60s section (energy-guided, beat-aware boundaries); the user's
    original files are never modified — extraction goes to temp files.

    status_cb(stage: str, detail) is called with stages:
    "validate" | "extract1" | "extract2" | "analyze1" | "analyze2" |
    "sync" | "xfade" | "done" | "error"
    """
    if not is_enabled():
        raise RuntimeError("remix beta is disabled")

    async with _remix_lock:
        loop = asyncio.get_running_loop()
        t_start = time.time()
        tmpdir = tempfile.mkdtemp(prefix="remix_job_")

        def _sync_progress(stage, detail):
            if status_cb:
                try:
                    status_cb(stage, detail)
                except Exception:
                    pass

        # Paths actually fed to the engine (originals or extracted).
        work1, work2 = file1, file2
        # (start_sec, used_sec) per input for the final report.
        section1: tuple[float, float] | None = None
        section2: tuple[float, float] | None = None

        try:
            # --- validate ---
            _sync_progress("validate", None)
            for p in (file1, file2):
                _ensure_readable(p)  # exists, non-empty, readable
                if os.path.getsize(p) / 1024 / 1024 > MAX_INPUT_FILE_MB:
                    raise RuntimeError(
                        f"file too large (max {MAX_INPUT_FILE_MB} MB)")
            dur1 = await loop.run_in_executor(None, probe_duration, file1)
            dur2 = await loop.run_in_executor(None, probe_duration, file2)
            for d in (dur1, dur2):
                if d > MAX_INPUT_DURATION_SEC:
                    raise RuntimeError(
                        f"file too long (max {MAX_INPUT_DURATION_SEC // 60} "
                        f"minutes, got {d:.0f}s)")
                if d < 5:
                    raise RuntimeError(
                        f"file too short ({d:.0f}s) for a remix")

            # --- auto section-select for long inputs ---
            async def _maybe_extract(src: str, dur: float, tag: str,
                                     stage: str):
                if dur <= EXTRACT_MAX_SEC:
                    return src, None
                _sync_progress(stage, {"duration": dur})
                start, length = await loop.run_in_executor(
                    None, select_best_section, src)
                dest = os.path.join(tmpdir, f"{tag}_section.mp3")
                await loop.run_in_executor(
                    None, extract_section, src, dest, start, length)
                return dest, (start, length)

            work1, section1 = await _maybe_extract(
                file1, dur1, "track1", "extract1")
            work2, section2 = await _maybe_extract(
                file2, dur2, "track2", "extract2")

            # --- analyze ---
            _sync_progress("analyze1", None)
            info1 = await loop.run_in_executor(None, analyze_track, work1)
            _sync_progress("analyze2", info1)
            info2 = await loop.run_in_executor(None, analyze_track, work2)

            # --- mix ---
            _sync_progress("mix", (info1, info2))
            result = await loop.run_in_executor(
                None, build_remix, work1, work2, out_path,
                info1, info2, _sync_progress)

            result["total_sec"] = round(time.time() - t_start, 1)
            result["info1"] = info1
            result["info2"] = info2
            if section1:
                result["section1"] = {
                    "start": round(section1[0], 1),
                    "len": round(section1[1], 1),
                    "orig_dur": round(dur1, 1),
                }
            if section2:
                result["section2"] = {
                    "start": round(section2[0], 1),
                    "len": round(section2[1], 1),
                    "orig_dur": round(dur2, 1),
                }
            _sync_progress("done", result)
            return result
        except Exception as e:
            _sync_progress("error", str(e))
            raise
        finally:
            # Job-scoped temp dir (extracted sections) is cleaned here.
            # The caller's tmpdir (originals + final MP3) is cleaned by
            # the caller after delivery.
            shutil.rmtree(tmpdir, ignore_errors=True)
            gc.collect()
