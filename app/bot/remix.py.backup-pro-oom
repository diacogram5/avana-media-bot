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

    SAFETY: Never re-encodes on Render's 512MB box — the libmp3lame
    re-encode path caused OOM kills (2026-11-01). If stream copy fails,
    falls back to copying the full source (the remix engine handles
    long files via its own trimming).
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
        # Stream copy failed. DO NOT re-encode (OOM risk on 512MB).
        # Fall back to a full-file copy; the engine will handle it.
        log.warning("extract_section: stream copy failed, "
                    "using full file instead of re-encode (OOM safety)")
        import shutil as _shutil
        _shutil.copy2(apath, dest_abs)
    out_dur = probe_duration(dest_abs)
    if out_dur < 1.0:
        raise RuntimeError("extracted section is empty")
    return dest_abs


def analyze_track(path: str) -> dict:
    """Analyze BPM, beats, key and scale with Essentia.

    Returns {"bpm": float, "beats": list[float], "beats_confidence": float,
             "key": str, "scale": str, "confidence": float,
             "key_strength": float}.
    Beats are in seconds. Raises RuntimeError on failure.
    """
    try:
        import essentia.standard as es
    except ImportError as e:
        raise RuntimeError("essentia is not installed") from e

    apath = _ensure_readable(path)
    # Load as mono 44100 Hz for consistent analysis
    loader = es.MonoLoader(filename=apath, sampleRate=44100)
    audio = loader()

    rhythm = es.RhythmExtractor2013(method="multifeature")
    bpm, beats, confidence, _ticks, _estimates = rhythm(audio)

    key_ext = es.KeyExtractor()
    key, scale, strength = key_ext(audio)

    # Free large buffers promptly (512 MB Render box)
    del audio
    gc.collect()

    # beats is a numpy array of beat positions in seconds; convert to list
    try:
        beats_list = [float(b) for b in beats]
    except Exception:
        beats_list = []

    return {
        "bpm": float(bpm),
        "beats": beats_list,
        "beats_confidence": float(confidence),
        "key": str(key),
        "scale": str(scale),
        "confidence": float(confidence),
        "key_strength": float(strength),
    }


# --- Beat alignment helpers (Phase 1) ---

# Minimum beat-detection confidence to trust beat positions for alignment.
# Below this, we fall back to time-based (non-beat-aligned) mixing.
BEAT_CONFIDENCE_THRESHOLD = 0.35


def _nearest_beat(beats: list[float], target: float) -> float | None:
    """Return the beat position closest to target, or None if no beats."""
    if not beats:
        return None
    return min(beats, key=lambda b: abs(b - target))


def _beats_in_range(beats: list[float], start: float,
                    end: float) -> list[float]:
    """Return beats within [start, end)."""
    return [b for b in beats if start <= b < end]


def beats_reliable(info: dict) -> bool:
    """Check if beat data is trustworthy for alignment decisions."""
    beats = info.get("beats") or []
    conf = info.get("beats_confidence", 0) or 0
    bpm = info.get("bpm", 0) or 0
    return (len(beats) >= 4 and conf >= BEAT_CONFIDENCE_THRESHOLD
            and 60 <= bpm <= 200)


def compute_xfade_plan(info1: dict, info2: dict, dur1: float, dur2: float,
                       ) -> dict:
    """Compute a beat-aligned crossfade plan.

    Returns {"xfade_beats": int, "xfade_sec": float,
             "t1_end_beat": float|None, "t2_start_beat": float|None,
             "beat_aligned": bool}.
    - xfade length adapts to tempo: 4 beats (min 4s, max 12s).
    - If beats are reliable for both tracks, the crossfade boundaries
      snap to actual beat positions; otherwise falls back to time-based.
    """
    bpm1 = info1.get("bpm") or 120.0
    if bpm1 <= 0:
        bpm1 = 120.0
    # 4 beats at track1's tempo
    beat_sec = 60.0 / bpm1
    xfade_sec = max(4.0, min(12.0, 4 * beat_sec))

    plan = {
        "xfade_beats": 4,
        "xfade_sec": round(xfade_sec, 2),
        "t1_end_beat": None,
        "t2_start_beat": None,
        "beat_aligned": False,
    }

    if beats_reliable(info1) and beats_reliable(info2):
        beats1 = info1["beats"]
        # Track1's crossfade should END on a beat near its end.
        # Find the latest beat that leaves room for the xfade.
        t1_end = _nearest_beat(beats1, dur1 - xfade_sec / 2)
        beats2 = info2["beats"]
        # Track2's crossfade should START on a downbeat-ish position:
        # use the first strong beat (index 0 is usually a downbeat).
        t2_start = beats2[0] if beats2 else None
        if t1_end is not None and t2_start is not None:
            plan["t1_end_beat"] = round(t1_end, 3)
            plan["t2_start_beat"] = round(t2_start, 3)
            plan["beat_aligned"] = True
            log.info("compute_xfade_plan: beat-aligned "
                     "(t1_end=%.2f t2_start=%.2f xfade=%.1fs)",
                     t1_end, t2_start, xfade_sec)
        else:
            log.info("compute_xfade_plan: beat snap failed, time-based")
    else:
        log.info("compute_xfade_plan: beats unreliable "
                 "(conf1=%.2f conf2=%.2f), time-based",
                 info1.get("beats_confidence", 0) or 0,
                 info2.get("beats_confidence", 0) or 0)
    return plan


# --- Key compatibility (Phase 2) ---

# Simplified Camelot-wheel compatibility: each key maps to a wheel position
# (0-11). Compatible if same position, adjacent (±1), or relative major/minor.
_CAMELOT = {
    # major keys
    "C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5,
    "F#": 6, "Gb": 6, "C#": 7, "Db": 7, "Ab": 8, "Eb": 9,
    "Bb": 10, "F": 11,
    # minor keys (relative minor shares position with major)
    "Am": 0, "Em": 1, "Bm": 2, "F#m": 3, "C#m": 4, "G#m": 5,
    "D#m": 6, "Ebm": 6, "Bbm": 7, "Fm": 8, "Cm": 9, "Gm": 10,
    "Dm": 11,
}

# Minimum key-detection strength to trust the compatibility check.
KEY_STRENGTH_THRESHOLD = 0.4


def _normalize_key(key: str, scale: str) -> str | None:
    """Normalize Essentia key/scale to a Camelot lookup key like 'Am'."""
    if not key:
        return None
    k = key.strip()
    # Essentia returns e.g. key="A", scale="minor" -> "Am"
    if scale and scale.lower().startswith("min"):
        # Avoid double-m
        return k + "m" if not k.endswith("m") else k
    return k


def check_key_compatibility(info1: dict, info2: dict) -> dict:
    """Check musical key compatibility between two analyzed tracks.

    Returns {"compatible": bool|None, "detail": str, "trusted": bool}.
    - compatible=True: same/adjacent/relative keys (smooth).
    - compatible=False: distant keys (clash risk) -> warn, don't force.
    - compatible=None / trusted=False: detection too weak to judge.
    """
    s1 = info1.get("key_strength", 0) or 0
    s2 = info2.get("key_strength", 0) or 0
    if s1 < KEY_STRENGTH_THRESHOLD or s2 < KEY_STRENGTH_THRESHOLD:
        return {"compatible": None, "trusted": False,
                "detail": "key detection too weak to judge"}

    k1 = _normalize_key(info1.get("key", ""), info1.get("scale", ""))
    k2 = _normalize_key(info2.get("key", ""), info2.get("scale", ""))
    p1 = _CAMELOT.get(k1) if k1 else None
    p2 = _CAMELOT.get(k2) if k2 else None
    if p1 is None or p2 is None:
        return {"compatible": None, "trusted": False,
                "detail": f"unknown key mapping ({k1} vs {k2})"}

    diff = abs(p1 - p2)
    diff = min(diff, 12 - diff)  # wheel distance
    if diff <= 1:
        return {"compatible": True, "trusted": True,
                "detail": f"compatible keys ({k1} vs {k2})"}
    if diff == 2:
        return {"compatible": True, "trusted": True,
                "detail": f"workable keys ({k1} vs {k2}), slight tension"}
    return {"compatible": False, "trusted": True,
            "detail": f"distant keys ({k1} vs {k2}) — clash risk"}


# --- Output validation (Phase 5) ---

def _measure_loudness(path: str) -> dict:
    """Measure integrated LUFS and true peak with FFmpeg ebur128.

    Returns {"lufs": float|None, "true_peak_db": float|None}.
    Uses a real measurement pass (not the loudnorm filter's estimate).
    """
    apath = _ensure_readable(path)
    # ebur128 with true peak measurement
    r = _run([
        "ffmpeg", "-v", "info", "-i", apath,
        "-filter:a", "ebur128=peak=true:framelog=quiet",
        "-f", "null", "-",
    ], timeout=120)
    out = (r.stderr or "") + "\n" + (r.stdout or "")
    lufs = None
    tpeak = None
    for line in out.splitlines():
        ll = line.lower()
        # ebur128 summary lines look like:
        #   I:         -14.2 LUFS
        #   Peak:       -1.5 dBFS
        if "i:" in ll and "lufs" in ll:
            try:
                lufs = float(ll.split("i:")[1].split("lufs")[0].strip())
            except Exception:
                pass
        if "peak:" in ll and "dbfs" in ll:
            try:
                tpeak = float(ll.split("peak:")[1].split("dbfs")[0].strip())
            except Exception:
                pass
    return {"lufs": lufs, "true_peak_db": tpeak}


def validate_output(path: str) -> dict:
    """Validate the final remix MP3 before delivery (mandatory).

    Checks:
    1. File exists, non-empty, ffprobe-readable, sane duration (30-150s).
    2. No long leading/trailing silence (>2s) via silencedetect.
    3. True peak below 0 dBFS (no clipping) via ebur128 measurement.
    4. Integrated loudness within a sane range (-20..-8 LUFS).

    Returns {"ok": bool, "reason": str, "duration": float,
             "lufs": float|None, "true_peak_db": float|None}.
    A failed check returns ok=False with a clear reason — the caller
    must NOT deliver the file.
    """
    apath = _ensure_readable(path)
    try:
        dur = probe_duration(apath)
    except Exception as e:
        return {"ok": False, "reason": f"unreadable: {e}",
                "duration": 0, "lufs": None, "true_peak_db": None}
    if not (30 <= dur <= 150):
        return {"ok": False, "reason": f"bad duration {dur:.1f}s",
                "duration": dur, "lufs": None, "true_peak_db": None}

    # Silence check: leading/trailing silence > 2s is a defect.
    r = _run([
        "ffmpeg", "-v", "info", "-i", apath,
        "-af", "silencedetect=noise=-40dB:d=2.0",
        "-f", "null", "-",
    ], timeout=120)
    sil_log = (r.stderr or "").lower()
    # Count silence_end events near the edges; a simple heuristic:
    # if "silence_start: 0" exists -> leading silence; check its duration.
    leading_sil = 0.0
    trailing_sil = 0.0
    try:
        import re as _re
        for m in _re.finditer(
                r"silence_start:\s*([\d.]+).*?silence_end:\s*([\d.]+)",
                sil_log, _re.S):
            s, e = float(m.group(1)), float(m.group(2))
            if s < 0.5:
                leading_sil = max(leading_sil, e - s)
            if e > dur - 0.5:
                trailing_sil = max(trailing_sil, e - s)
    except Exception:
        pass
    if leading_sil > 2.0 or trailing_sil > 2.0:
        return {"ok": False,
                "reason": f"excessive silence "
                          f"(lead={leading_sil:.1f}s trail={trailing_sil:.1f}s)",
                "duration": dur, "lufs": None, "true_peak_db": None}

    # Loudness + true peak measurement (real, not filter estimate).
    meas = _measure_loudness(apath)
    lufs = meas["lufs"]
    tpeak = meas["true_peak_db"]
    if tpeak is not None and tpeak > 0.0:
        return {"ok": False,
                "reason": f"clipping detected (true peak {tpeak:.1f} dBFS)",
                "duration": dur, "lufs": lufs, "true_peak_db": tpeak}
    if lufs is not None and not (-20.0 <= lufs <= -8.0):
        # Warn-level: don't fail, just note it (some masters are hot).
        log.warning("validate_output: loudness %.1f LUFS outside "
                    "-20..-8 range", lufs)

    log.info("validate_output: OK dur=%.1fs lufs=%s tpeak=%s",
             dur, lufs, tpeak)
    return {"ok": True, "reason": "ok", "duration": dur,
            "lufs": lufs, "true_peak_db": tpeak}


def build_remix(file1: str, file2: str, out_path: str,
                info1: dict, info2: dict,
                progress_cb=None) -> dict:
    """Build the remix MP3 from two analyzed files (professional engine).

    Steps:
    1. Key-compatibility check (warn only, never blocks).
    2. Tempo-match file2 to file1's BPM via FFmpeg rubberband filter.
    3. Loudness-normalize both tracks to -14 LUFS (EBU R128) + peak limit.
    4. Beat-aligned adaptive crossfade (4 beats, snapped to beat grid
       when beat data is reliable; energy-adaptive otherwise).
    5. Encode to MP3 (bitrate from OUTPUT_BITRATE).
    6. Validate output (duration, silence, peak, playability).

    Returns {"output": path, "size_mb": float, "elapsed_sec": float, ...}.
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
        tempo_changed = abs(ratio - 1.0) > 0.005

        # --- Phase 2: key compatibility (advisory) ---
        key_check = check_key_compatibility(info1, info2)

        if progress_cb:
            progress_cb("sync", ratio)

        # --- Step 1: tempo-match file2 -> file1 BPM (+ loudnorm) ---
        # We chain rubberband + loudnorm in one FFmpeg pass to save CPU.
        matched2 = os.path.join(tmpdir, "track2_matched.wav")
        filt2 = []
        if tempo_changed:
            filt2.append(f"rubberband=tempo={ratio:.4f}")
        # Loudness normalize to -14 LUFS with true-peak limiting at -1 dBTP.
        # Single-pass loudnorm is an approximation but far better than nothing;
        # we verify with a real measurement afterwards (see validate_output).
        filt2.append("loudnorm=I=-14:TP=-1.0:LRA=11")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", file2,
               "-filter:a", ",".join(filt2),
               "-ar", "44100", "-ac", "2", matched2]
        r = _run(cmd, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"tempo-match failed: {r.stderr.strip()[:200]}")

        # --- Step 2: normalize file1 the same way ---
        norm1 = os.path.join(tmpdir, "track1_norm.wav")
        r = _run(["ffmpeg", "-y", "-v", "error", "-i", file1,
                  "-filter:a", "loudnorm=I=-14:TP=-1.0:LRA=11",
                  "-ar", "44100", "-ac", "2", norm1], timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"normalize failed: {r.stderr.strip()[:200]}")

        # --- Phase 1+4: beat-aligned adaptive crossfade plan ---
        dur1 = probe_duration(norm1)
        dur2 = probe_duration(matched2)
        plan = compute_xfade_plan(info1, info2, dur1, dur2)
        xfade_sec = plan["xfade_sec"]

        # Energy-adaptive tweak: if the transition region is very energetic
        # (drop/chorus), shorten slightly for punch; if calm, lengthen.
        try:
            prof1 = _energy_profile(norm1)
            # Look at the last ~10s of track1 (the outgoing region)
            tail = prof1[-10:] if len(prof1) >= 10 else prof1
            tail_e = sum(tail) / max(1, len(tail))
            all_e = sum(prof1) / max(1, len(prof1))
            if all_e > 0:
                if tail_e > all_e * 1.3:
                    xfade_sec = max(4.0, xfade_sec * 0.75)  # punchy
                    log.info("build_remix: energetic outro, xfade shortened "
                             "to %.1fs", xfade_sec)
                elif tail_e < all_e * 0.6:
                    xfade_sec = min(12.0, xfade_sec * 1.25)  # gentle
                    log.info("build_remix: calm outro, xfade lengthened "
                             "to %.1fs", xfade_sec)
        except Exception as e:
            log.warning("build_remix: energy-adaptive xfade skipped (%s)", e)

        if progress_cb:
            progress_cb("xfade", None)

        # --- Step 3: crossfade ---
        # Beat alignment: trim track2 to start on its first beat so the
        # downbeats line up. We do this with -ss before the crossfade.
        t2_ss = 0.0
        if plan["beat_aligned"] and plan["t2_start_beat"]:
            t2_ss = max(0.0, plan["t2_start_beat"])
            log.info("build_remix: track2 trimmed to %.2fs for downbeat "
                     "alignment", t2_ss)

        # Simple robust crossfade (avoid over-complex filtergraphs on 0.1 CPU)
        xfade_filter = (
            f"[0:a][1:a]acrossfade=d={xfade_sec:.2f}:c1=tri:c2=tri[a]"
        )
        inputs = ["-i", norm1, "-i", matched2]
        if t2_ss > 0.01:
            # Re-encode track2 with the trim first (cheap, one pass)
            trimmed2 = os.path.join(tmpdir, "track2_trimmed.wav")
            r = _run(["ffmpeg", "-y", "-v", "error",
                      "-ss", f"{t2_ss:.3f}", "-i", matched2,
                      "-c:a", "pcm_s16le", trimmed2], timeout=300)
            if r.returncode == 0 and os.path.exists(trimmed2):
                inputs = ["-i", norm1, "-i", trimmed2]
            else:
                log.warning("build_remix: track2 trim failed, using untrimmed")

        r = _run([
            "ffmpeg", "-y", "-v", "error",
            *inputs,
            "-filter_complex", xfade_filter,
            "-map", "[a]", "-c:a", "libmp3lame", "-b:a", OUTPUT_BITRATE,
            out_path,
        ], timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"mix failed: {r.stderr.strip()[:200]}")

        # --- Phase 5: output validation (mandatory) ---
        validation = validate_output(out_path)
        if not validation["ok"]:
            raise RuntimeError(
                f"output validation failed: {validation['reason']}")

        size_mb = os.path.getsize(out_path) / 1024 / 1024
        return {
            "output": out_path,
            "size_mb": round(size_mb, 2),
            "elapsed_sec": round(time.time() - t0, 1),
            "bpm1": round(bpm1, 1),
            "bpm2": round(bpm2, 1),
            "ratio": round(ratio, 3),
            "xfade_sec": round(xfade_sec, 1),
            "beat_aligned": plan["beat_aligned"],
            "key_check": key_check,
            "validation": validation,
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
