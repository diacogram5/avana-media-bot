"""Per-request timing instrumentation — LOGS ONLY.

Every helper here is a thin wrapper around ``log_event`` and can never
raise: a logging failure must never break the request it measures.
Nothing in this module changes control flow, config, tokens or any
feature — it only records how long things took.

Events emitted:
  ``link_stage`` / ``identify_stage`` — one line per request stage:
      req_id, stage, ms (this stage), total_ms (since request start).
      The ``total`` stage also carries uptime_seconds, cpu_count,
      max_rss_kb and the per-request Turso totals.
  ``turso_query`` — one line per Turso query in a request:
      req_id, query (table.operation), ms.
  ``subprocess`` — one line per ffmpeg / ffprobe / fpcalc / yt-dlp call:
      req_id (when known), tool, ms, argv (first args, URLs redacted).
  ``process_start`` — once at startup: started_at, cpu_count.
"""
from __future__ import annotations

import os
import resource
import subprocess
import time
import uuid

from .logging_config import log_event

# Captured at import time: this module is imported by handlers.py at
# process start, so it is the process start for practical purposes.
PROCESS_START = time.time()

_TURSO_STATS: dict[str, dict] = {}
_TURSO_STATS_MAX = 500


def new_req_id() -> str:
    """Short opaque id grouping one request's stage lines."""
    return uuid.uuid4().hex[:8]


def uptime_seconds() -> float:
    """Seconds since this process started (cold-start detection)."""
    return time.time() - PROCESS_START


def cpu_count() -> int:
    try:
        return os.cpu_count() or 0
    except Exception:
        return 0


def max_rss_kb() -> int:
    """Peak resident memory of this process (Linux: kilobytes)."""
    try:
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:
        return 0


def rss_kb() -> int:
    """Current resident memory of this process (Linux: kilobytes).

    STEP 2 (2026-11-08): the high-water mark (max_rss_kb) never drops,
    so per-stage RAM needs the live VmRSS from /proc/self/status.
    Stdlib only, never raises.
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return 0


def log_stage(logger, event: str, req_id, t_req_start, stage: str,
              t_stage_start=None, **extra) -> None:
    """One structured stage line: ms for this stage + total since start."""
    try:
        now = time.perf_counter()
        total_ms = (int((now - t_req_start) * 1000)
                    if t_req_start is not None else None)
        ms = (int((now - t_stage_start) * 1000)
              if t_stage_start is not None else total_ms)
        fields = {"stage": stage, "ms": ms, "rss_kb": rss_kb()}
        if req_id is not None:
            fields["req_id"] = req_id
        if total_ms is not None:
            fields["total_ms"] = total_ms
        fields.update(extra)
        log_event(logger, event, **fields)
    except Exception:
        pass


def log_total(logger, event: str, req_id, t_req_start, status: str,
              headline_total: str | None = None, **extra) -> None:
    """The final line of a request: total ms + uptime + resources + Turso.

    ``headline_total`` names one extra field carrying the same total
    (e.g. "link_to_video_ms" / "click_to_identify_ms") so the 24h
    analysis can read the headline numbers directly.
    """
    try:
        count, turso_ms = turso_summary(req_id)
        now = time.perf_counter()
        total_ms = (int((now - t_req_start) * 1000)
                    if t_req_start is not None else 0)
        fields = {"req_id": req_id, "stage": "total",
                  "ms": total_ms, "total_ms": total_ms, "status": status,
                  "uptime_seconds": round(uptime_seconds(), 1),
                  "cpu_count": cpu_count(), "max_rss_kb": max_rss_kb(),
                  "rss_kb": rss_kb(),
                  "turso_queries": count, "turso_total_ms": turso_ms}
        if headline_total:
            fields[headline_total] = total_ms
        fields.update(extra)
        log_event(logger, event, **fields)
    except Exception:
        pass


def note_turso(req_id, query: str, ms: float, logger=None) -> None:
    """Record one Turso query: per-request totals + one turso_query line.

    Calls without a request id (background tasks, admin flows) only
    update nothing and log nothing — the request paths are what matter.
    """
    try:
        if not req_id:
            return
        st = _TURSO_STATS.get(req_id)
        if st is None:
            if len(_TURSO_STATS) >= _TURSO_STATS_MAX:
                _TURSO_STATS.pop(next(iter(_TURSO_STATS)))
            st = _TURSO_STATS[req_id] = {"count": 0, "total_ms": 0}
        st["count"] += 1
        st["total_ms"] += int(ms)
        if logger is not None:
            log_event(logger, "turso_query", req_id=req_id, query=query,
                      ms=int(ms))
    except Exception:
        pass


def turso_summary(req_id):
    """(query count, total ms) for a request; pops the entry."""
    try:
        st = _TURSO_STATS.pop(req_id, None) if req_id else None
    except Exception:
        st = None
    if not st:
        return 0, 0
    return st["count"], st["total_ms"]


def _safe_argv(cmd) -> list:
    """First args for identification — never a URL, token or key."""
    out = []
    for arg in list(cmd or [])[:4]:
        s = str(arg)
        low = s.lower()
        if "://" in s or "token" in low or "key" in low or "secret" in low:
            out.append("[redacted]")
        else:
            out.append(s[:40])
    return out


def log_subprocess(logger, req_id, tool: str, ms: float, **extra) -> None:
    """One subprocess line: tool + wall-clock ms (+ optional detail)."""
    try:
        fields = {"tool": tool, "ms": int(ms)}
        if req_id is not None:
            fields["req_id"] = req_id
        fields.update(extra)
        log_event(logger, "subprocess", **fields)
    except Exception:
        pass


def run(cmd, *, req_id=None, logger=None, stage=None, **kwargs):
    """``subprocess.run`` with a wall-clock timing line.

    Same arguments, same return value, same exceptions — only an extra
    ``subprocess`` log line (logged even when the call raises, via
    finally, so timeouts are visible too). ``stage`` gives the call a
    distinct stage name in the log line (e.g. "ffmpeg_transcode" vs
    "ffmpeg_mux") so the two ffmpeg steps are separable in the logs.
    """
    t0 = time.perf_counter()
    try:
        return subprocess.run(cmd, **kwargs)
    finally:
        try:
            ms = int((time.perf_counter() - t0) * 1000)
            tool = os.path.basename(str(cmd[0])) if cmd else "?"
            if logger is not None:
                extra = {"argv": _safe_argv(cmd)}
                if stage is not None:
                    extra["stage"] = stage
                log_subprocess(logger, req_id, tool, ms, **extra)
        except Exception:
            pass
