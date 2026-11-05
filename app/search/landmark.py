"""Shazam-style spectral-landmark audio fingerprint (pure Python, stdlib only).

A faithful port of the web bot's local fingerprint used for the
superior-AI bank search:
- ``fftMagnitudes`` / ``computeLandmarks`` from
  ``~/workspace/ts-spaces/space/server/src/privileged.ts``
- ``encodeLandmarks`` / ``landmarkSimilarity`` and the acceptance bar
  (280 votes, 0.13 aligned ratio) from
  ``~/workspace/ts-spaces/space/server/src/actions.ts``

How it works: STFT frames (2048 samples, 1024 hop) are reduced to 40
log-spaced bands (250-4000 Hz). Per frame, a band is a peak when it
stands out from the frame's median band energy (>= median*3, floor
1e-6) and is a local maximum against the previous/next frame in the
same band. Each peak is paired with up to 3 later peaks (dt in
[3, 28] frames); each pair becomes a hash of
(quantized freq 1, quantized freq 2, dt in 2-frame buckets) anchored
at the first peak's frame.

The same recording — full, excerpted, re-encoded, slightly
tempo-shifted — reproduces many identical hashes aligned at ONE time
offset; a different recording yields only scattered random
collisions. Measured on real files in the web bot's environment: a
14 s excerpt vs its full song aligned 2167 votes, a +8% tempo variant
342, unrelated songs (even same-composer, same-key ones) at most 198.
The bar (280 votes AND aligned ratio 0.13) sits between those worlds.

Honesty rules, same as the web:
- Decoding needs ffmpeg (arbitrary bytes) or a plain WAV (parsed
  directly). No ffmpeg and not a WAV -> an honest "decode
  unavailable" error, never a fake fingerprint.
- < 1 s of audio, silence, or no usable peaks -> status "no_audio",
  never a match.
- Decode failure -> status "error", never "ok".

Stdlib only (math, struct, wave not needed here, subprocess, shutil,
base64, json). Importing this module never runs ffmpeg.
"""
from __future__ import annotations

import base64
import json
import math
import shutil
import struct
import subprocess

TARGET_RATE = 16000
FFT_SIZE = 2048
HOP = 1024
LO_HZ = 250.0
HI_HZ = 4000.0
N_BANDS = 40
MAX_LANDMARK_PAIRS = 12000
PEAK_SILENCE_FLOOR = 0.01

# Acceptance bar for landmark_similarity, measured on real files
# (see module docstring): at least 280 landmark hashes aligned at one
# time offset AND an aligned ratio of 0.13.
BANK_MATCH_MIN_VOTES = 280
BANK_MATCH_MIN_SCORE = 0.13


# --------------------------------------------------------------------------
# FFT (exact port of fftMagnitudes: iterative radix-2, float64)
# --------------------------------------------------------------------------
def _fft_magnitudes(re: list[float], im: list[float]) -> list[float]:
    n = len(re)
    # Bit-reversal permutation.
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            re[i], re[j] = re[j], re[i]
            im[i], im[j] = im[j], im[i]
    length = 2
    while length <= n:
        ang = (-2.0 * math.pi) / length
        wr = math.cos(ang)
        wi = math.sin(ang)
        half = length >> 1
        for i in range(0, n, length):
            cwr, cwi = 1.0, 0.0
            for k in range(half):
                ur = re[i + k]
                ui = im[i + k]
                vr = re[i + k + half]
                vi = im[i + k + half]
                xr = vr * cwr - vi * cwi
                xi = vr * cwi + vi * cwr
                re[i + k] = ur + xr
                im[i + k] = ui + xi
                re[i + k + half] = ur - xr
                im[i + k + half] = ui - xi
                nwr = cwr * wr - cwi * wi
                cwi = cwr * wi + cwi * wr
                cwr = nwr
        length <<= 1
    return [math.hypot(re[i], im[i]) for i in range(n // 2)]


def _js_round(x: float) -> int:
    """JS Math.round semantics (round half up), for bit-exact ports."""
    return int(math.floor(x + 0.5))


def _qfreq(f: float) -> int:
    return max(
        0,
        min(
            31,
            _js_round((24.0 * math.log2(f / LO_HZ)) / math.log2(HI_HZ / LO_HZ)),
        ),
    )


# --------------------------------------------------------------------------
# Decoding: arbitrary bytes -> mono PCM @ 16 kHz
# --------------------------------------------------------------------------
def _looks_like_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WAVE"


def _resample_linear(pcm: list[float], src_rate: int, dst_rate: int) -> list[float]:
    if src_rate == dst_rate or not pcm:
        return pcm
    n_src = len(pcm)
    n_dst = int(round(n_src / src_rate * dst_rate))
    out: list[float] = []
    for i in range(n_dst):
        t = i * src_rate / dst_rate
        i0 = int(t)
        frac = t - i0
        i1 = i0 + 1 if i0 + 1 < n_src else n_src - 1
        out.append(pcm[i0] * (1.0 - frac) + pcm[i1] * frac)
    return out


def _wav_to_pcm(data: bytes, sample_rate: int) -> tuple[list[float], int]:
    """Parse a 16-bit PCM WAV (mono/stereo, any rate) without ffmpeg."""
    pos = 12
    audio_format = channels = rate = bits = None
    data_start = data_len = None
    size = len(data)
    while pos + 8 <= size:
        chunk_id = data[pos:pos + 4]
        (chunk_size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        body = pos + 8
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise ValueError("wav fmt chunk too short")
            audio_format, channels, rate = struct.unpack("<HHI", data[body:body + 8])
            (bits,) = struct.unpack("<H", data[body + 14:body + 16])
        elif chunk_id == b"data":
            data_start, data_len = body, chunk_size
        pos = body + chunk_size + (chunk_size & 1)
    if audio_format not in (1, 0xFFFE):
        raise ValueError("wav is not PCM (format %r)" % (audio_format,))
    if bits != 16:
        raise ValueError("wav is not 16-bit (bits=%r)" % (bits,))
    if channels not in (1, 2):
        raise ValueError("wav channel count not supported: %r" % (channels,))
    if data_start is None or not rate:
        raise ValueError("wav missing data chunk or rate")
    data_end = min(data_start + data_len, size)
    n_frames = (data_end - data_start) // (2 * channels)
    if n_frames <= 0:
        raise ValueError("wav has no audio frames")
    raw = data[data_start:data_start + n_frames * 2 * channels]
    ints = struct.unpack("<%dh" % (n_frames * channels), raw)
    if channels == 1:
        pcm = [v / 32768.0 for v in ints]
    else:
        pcm = [
            (ints[2 * i] + ints[2 * i + 1]) / 2.0 / 32768.0
            for i in range(n_frames)
        ]
    return _resample_linear(pcm, rate, sample_rate), sample_rate


def decode_to_pcm(data: bytes, sample_rate: int = TARGET_RATE) -> tuple[list[float], int] | None:
    """Decode arbitrary audio bytes to mono PCM floats in [-1, 1].

    Returns ``(pcm, sample_rate)`` or ``None`` when decoding is not
    possible: ffmpeg is missing (and the bytes are not a plain WAV),
    or decoding failed. Never raises on bad input; never fabricates
    audio.
    """
    if not data:
        return None
    if _looks_like_wav(data):
        try:
            return _wav_to_pcm(data, sample_rate)
        except (ValueError, struct.error):
            pass  # fall through to ffmpeg, which may handle odd WAVs
    if shutil.which("ffmpeg") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0", "-ac", "1", "-ar", str(sample_rate),
                "-f", "s16le", "pipe:1",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    raw = proc.stdout
    n = len(raw) // 2
    if n <= 0:
        return None
    ints = struct.unpack("<%dh" % n, raw[: 2 * n])
    return [v / 32768.0 for v in ints], sample_rate


# --------------------------------------------------------------------------
# Landmark computation (exact port of computeLandmarks)
# --------------------------------------------------------------------------
def compute_landmarks(
    pcm: list[float], sample_rate: int = TARGET_RATE
) -> dict:
    """Compute spectral landmarks. Returns ``{"landmarks": [...],
    "landmark_count": n, "duration_sec": float}``.

    ``landmarks`` is a flat list of ``[hash, anchorFrame, ...]`` pairs,
    exactly like the web bot. Only the first 90 seconds are
    fingerprinted (``pcm[:16000*90]``), as in the web.
    """
    pcm = list(pcm[: TARGET_RATE * 90])
    duration_sec = len(pcm) / sample_rate if sample_rate else 0.0

    edges = [
        LO_HZ * (HI_HZ / LO_HZ) ** (i / N_BANDS) for i in range(N_BANDS + 1)
    ]
    win = [
        0.5 * (1.0 - math.cos((2.0 * math.pi * i) / (FFT_SIZE - 1)))
        for i in range(FFT_SIZE)
    ]

    n_frames = max(0, (len(pcm) - FFT_SIZE) // HOP + 1)
    band_mag = [0.0] * (n_frames * N_BANDS)
    band_freq = [0.0] * (n_frames * N_BANDS)
    re = [0.0] * FFT_SIZE
    im = [0.0] * FFT_SIZE

    for fr in range(n_frames):
        start = fr * HOP
        for i in range(FFT_SIZE):
            re[i] = pcm[start + i] * win[i]
            im[i] = 0.0
        mags = _fft_magnitudes(re, im)
        for band in range(N_BANDS):
            f0 = edges[band]
            f1 = edges[band + 1]
            b0 = max(1, math.floor((f0 * FFT_SIZE) / sample_rate))
            b1 = min(FFT_SIZE // 2 - 1, math.ceil((f1 * FFT_SIZE) / sample_rate))
            best = 0.0
            best_bin = b0
            for b in range(b0, b1 + 1):
                m = mags[b]
                if m > best:
                    best = m
                    best_bin = b
            band_mag[fr * N_BANDS + band] = best
            band_freq[fr * N_BANDS + band] = (best_bin * sample_rate) / FFT_SIZE

    # Peaks: band magnitude >= max(median of frame bands * 3, 1e-6)
    # AND a local maximum vs previous/next frame in the same band.
    peaks: list[tuple[int, float]] = []  # (frame, freq)
    for fr in range(n_frames):
        row = band_mag[fr * N_BANDS:(fr + 1) * N_BANDS]
        median = sorted(row)[len(row) // 2] if row else 0.0
        thresh = max(median * 3.0, 1e-6)
        for band in range(N_BANDS):
            m = band_mag[fr * N_BANDS + band]
            if m < thresh:
                continue
            prev = band_mag[(fr - 1) * N_BANDS + band] if fr > 0 else 0.0
            nxt = (
                band_mag[(fr + 1) * N_BANDS + band]
                if fr < n_frames - 1
                else 0.0
            )
            if m < prev or m < nxt:
                continue
            peaks.append((fr, band_freq[fr * N_BANDS + band]))

    # Pair each peak with up to 3 later peaks, dt in [3, 28] frames.
    # dt is stored halved (2-frame buckets): a small tempo drift then
    # shifts pair distances by less than one bucket.
    landmarks: list[int] = []
    for i, (fa, freq_a) in enumerate(peaks):
        paired = 0
        for j in range(i + 1, len(peaks)):
            fb, freq_b = peaks[j]
            dt = fb - fa
            if dt < 3:
                continue
            if dt > 28:
                break
            h = _qfreq(freq_a) | (_qfreq(freq_b) << 6) | ((dt >> 1) << 12)
            landmarks.append(h)
            landmarks.append(fa)
            paired += 1
            if paired >= 3:
                break

    # Cap at MAX_LANDMARK_PAIRS pairs with stride thinning, exactly
    # like the web.
    n_pairs = len(landmarks) // 2
    if n_pairs > MAX_LANDMARK_PAIRS:
        stride = math.ceil(n_pairs / MAX_LANDMARK_PAIRS)
        thinned: list[int] = []
        for i in range(0, n_pairs, stride):
            thinned.append(landmarks[2 * i])
            thinned.append(landmarks[2 * i + 1])
        landmarks = thinned
        n_pairs = len(landmarks) // 2

    return {
        "landmarks": landmarks,
        "landmark_count": n_pairs,
        "duration_sec": duration_sec,
    }


# --------------------------------------------------------------------------
# fingerprint_bytes: bytes -> status dict
# --------------------------------------------------------------------------
def fingerprint_bytes(data: bytes) -> dict:
    """Fingerprint audio bytes. Returns ``{"status", "landmarks",
    "landmark_count", "duration_sec", "message"}``.

    - ``"ok"``: landmarks hold the flat ``[hash, anchorFrame, ...]``
      pairs.
    - ``"no_audio"``: decoded to < 1 s of audio, silence, or no usable
      spectral peaks.
    - ``"error"``: could not decode (ffmpeg missing or decode failed).
    Landmarks are empty unless status is ``"ok"``.
    """
    decoded = decode_to_pcm(data or b"")
    if decoded is None:
        if shutil.which("ffmpeg") is None and not _looks_like_wav(data or b""):
            message = "decode unavailable: ffmpeg is not installed on this host"
        else:
            message = "could not decode audio bytes"
        return {
            "status": "error",
            "landmarks": [],
            "landmark_count": 0,
            "duration_sec": 0.0,
            "message": message,
        }
    pcm, sr = decoded
    duration_sec = round(len(pcm) / sr, 1) if sr else 0.0
    if duration_sec < 1.0:
        return {
            "status": "no_audio",
            "landmarks": [],
            "landmark_count": 0,
            "duration_sec": duration_sec,
            "message": "audio shorter than 1 second",
        }
    if max((abs(v) for v in pcm), default=0.0) < PEAK_SILENCE_FLOOR:
        return {
            "status": "no_audio",
            "landmarks": [],
            "landmark_count": 0,
            "duration_sec": duration_sec,
            "message": "audio is silent",
        }
    r = compute_landmarks(pcm, sr)
    if len(r["landmarks"]) < 40:
        return {
            "status": "no_audio",
            "landmarks": [],
            "landmark_count": 0,
            "duration_sec": round(r["duration_sec"], 1),
            "message": "no usable audio landmarks",
        }
    return {
        "status": "ok",
        "landmarks": r["landmarks"],
        "landmark_count": r["landmark_count"],
        "duration_sec": round(r["duration_sec"], 1),
        "message": "",
    }


# --------------------------------------------------------------------------
# Similarity (exact port of landmarkSimilarity + acceptance bar)
# --------------------------------------------------------------------------
def landmark_similarity(fp1: dict, fp2: dict) -> dict:
    """Match two fingerprints by actual audio content.

    Counts landmark hashes of fp1 that appear in fp2 at ONE shared
    time offset (bucketed ±1 frame): ``bucket = round((tb - tq) / 2)``.
    ``ratio`` is the web's exact definition: best bucket votes divided
    by ``max(1, min(count1, count2))``. ``match`` is True only when
    votes >= 280 AND ratio >= 0.13.
    """
    q_flat = fp1.get("landmarks") or []
    b_flat = fp2.get("landmarks") or []
    q_count = fp1.get("landmark_count") or len(q_flat) // 2
    b_count = fp2.get("landmark_count") or len(b_flat) // 2

    by_hash: dict[int, list[int]] = {}
    for i in range(0, len(b_flat) - 1, 2):
        by_hash.setdefault(b_flat[i], []).append(b_flat[i + 1])

    votes: dict[int, int] = {}
    best = 0
    for i in range(0, len(q_flat) - 1, 2):
        times = by_hash.get(q_flat[i])
        if not times:
            continue
        tq = q_flat[i + 1]
        for tb in times:
            bucket = _js_round((tb - tq) / 2.0)
            v = votes.get(bucket, 0) + 1
            votes[bucket] = v
            if v > best:
                best = v

    denom = max(1, min(q_count, b_count))
    ratio = best / denom
    return {
        "votes": best,
        "ratio": ratio,
        "match": best >= BANK_MATCH_MIN_VOTES and ratio >= BANK_MATCH_MIN_SCORE,
    }


# --------------------------------------------------------------------------
# pack / unpack (matches the web's encodeLandmarks / parseLandmarks wire
# format: compact JSON {"v":2,"b64": base64 of little-endian uint32
# pairs, hash in the low 18 bits, anchor frame above it}).
# --------------------------------------------------------------------------
def pack_landmarks(landmarks: list[int]) -> str:
    """Pack flat ``[hash, anchorFrame, ...]`` pairs into the web's
    compact JSON form (``{"v":2,"b64":...}``)."""
    n = len(landmarks) // 2
    buf = bytearray(n * 4)
    for i in range(n):
        packed = (landmarks[2 * i] & 0x3FFFF) | (landmarks[2 * i + 1] << 18)
        struct.pack_into("<I", buf, i * 4, packed)
    b64 = base64.b64encode(bytes(buf)).decode("ascii")
    return json.dumps({"v": 2, "b64": b64}, separators=(",", ":"))


def unpack_landmarks(s: str) -> list[int]:
    """Inverse of :func:`pack_landmarks`. Returns ``[]`` on malformed
    input (the web's ``parseLandmarks`` additionally rejects
    fingerprints shorter than 40 entries as too short to be real; this
    inverse keeps short-but-valid packs so round-trips are lossless)."""
    try:
        p = json.loads(s)
    except (ValueError, TypeError):
        return []
    if not isinstance(p, dict) or p.get("v") != 2:
        return []
    b64 = p.get("b64")
    if not isinstance(b64, str) or not b64:
        return []
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, base64.binascii.Error):
        return []
    if len(raw) == 0 or len(raw) % 4 != 0:
        return []
    flat: list[int] = []
    for i in range(len(raw) // 4):
        (packed,) = struct.unpack_from("<I", raw, i * 4)
        flat.append(packed & 0x3FFFF)
        flat.append(packed >> 18)
    return flat
