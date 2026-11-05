"""Hourly clip feed for the Iranian story-clip archive.

Sources (verified 2026-11-05, real fetches — do NOT re-verify from scratch):
  1. Telegram @ehsaspaak («کلیپ عاشقانه آهنگ متن», 43.6K subs): the public
     page https://t.me/s/ehsaspaak (browser UA, no login) lists video posts
     with public CDN MP4 links (cdn*.telesco.pe). ~4-5 new videos/day.
     WARNING: the channel also posts text poems and gambling/betting ads —
     text posts have no <video> (auto-skipped) and ad posts are filtered
     by a keyword blocklist; anything left still goes to the moderation
     queue (never auto-accepted).
  2. Aparat public v1 API (no key, no login):
     GET https://www.aparat.com/api/fa/v1/video/video/show/videohash/{uid}
     returns JSON:API with title, duration, owner_username, category,
     can_download and file_link (direct MP4). The payload also embeds
     ~50 related-video UIDs (included[].type == 'Video') — that graph is
     the bootstrap stock. NOTE: no verified endpoint lists a channel's
     LATEST uploads, so there is no Aparat "latest" call; later runs
     re-walk the related graph of the seed (bounded) and rely on exact
     dedup. yt-dlp's Aparat extractor is BROKEN — never use it here.

Design:
- Intake downloads ONE file at a time (<=20MB), ffprobe-confirms an
  audio stream and a 10-60s duration, then inserts status='pending'
  with categorized_by='feed'. NEVER auto-accept: every clip goes to the
  existing moderation queue; queue-time analysis (Shazam + lyrics +
  suggestion) is unchanged.
- THE file_id PROBLEM: feed clips come from the web, so the archive row
  starts with file_id='' and keeps the public source_url. Delivery is
  solved in two stages (see story_archive.py):
    * preferred: approval-time processing downloads from source_url and
      uploads the badged final ONCE to the private storage channel
      (SARCH_STORAGE_CHAT_ID set) -> processed_file_id.
    * fallback (no storage channel): the clip is approved raw by design
      and deliver_feed_first() applies the user badge ONCE at first
      delivery, then stores the resulting file_id as processed_file_id
      (later deliveries are instant). If the badge fails, the raw URL
      is sent and the file_id is captured instead.
  The queue preview also captures a file_id: send_video by public URL
  returns a message whose video.file_id is stored on the row.
- Hourly job: asyncio loop, single-flight, ALL network/download work
  in an executor — NEVER blocks the event loop (the disabled
  daily_indexer froze the bot exactly this way). try/except around
  everything; the job must never crash the bot.
- No new env vars, no new paid services, no changes to the Instagram
  downloader, Snaplytics/Kabo, song search or remix.

DB: story_archive gains two additive columns (source_url, feed_key)
and a small feed_state key/value table — same migration pattern as
Phase 1 (backup snapshot first, additive idempotent ALTERs).
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from . import story_archive as _sarch
from ..logging_config import get_logger

_log = get_logger("avana.clip_feed")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TG_CHANNEL = "ehsaspaak"
TG_PAGE_URL = f"https://t.me/s/{TG_CHANNEL}"
TG_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
         "AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/120.0.0.0 Safari/537.36")
TG_MAX_PER_RUN = 10            # max Telegram clips intaken per cycle

APARAT_SHOW_URL = ("https://www.aparat.com/api/fa/v1/video/video/"
                   "show/videohash/{uid}")
APARAT_SEED_UIDS = ["jtk01l2"]  # verified seed (owner @faezehradmanesh)
AP_BOOTSTRAP_SHOW_CALLS = 40    # max Aparat show calls on the first run
AP_BOOTSTRAP_NEW = 20           # max Aparat clips accepted on the first run
AP_RUN_SHOW_CALLS = 8           # max Aparat show calls on later runs
AP_RUN_NEW = 5                  # max Aparat clips accepted on later runs

MIN_DURATION_SEC = 10
MAX_DURATION_SEC = 60
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024   # Bot API download limit
HTTP_TIMEOUT_SEC = 25

FEED_INTERVAL_SEC = 3600        # hourly
FEED_FIRST_DELAY_SEC = 120      # let polling come up before the first run

# Betting/gambling ad markers seen on @ehsaspaak. A video post whose
# caption matches is skipped at intake (the moderation queue stays the
# real filter — this only keeps obvious ads out of it).
_AD_BLOCKLIST = (
    "شرط‌بندی", "شرطبندی", "سایت شرط", "بونوس", "شارژ کن",
    "کازینو", "پیش‌بینی فوتبال", "موجودی بگیر",
)

_FA_NORM = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه"})


def _norm_fa(s: str) -> str:
    return (s or "").translate(_FA_NORM)


def _is_ad_text(text: str) -> bool:
    t = _norm_fa(text or "")
    return any(kw in t for kw in _AD_BLOCKLIST)


def is_persian_title(title: str) -> bool:
    """True when the title has at least 3 Persian/Arabic-script letters."""
    return len(re.findall(r"[\u0600-\u06FF]", title or "")) >= 3


# ---------------------------------------------------------------------------
# Pure parsers (sandbox-testable, no I/O)
# ---------------------------------------------------------------------------
def parse_duration(text: str) -> int | None:
    """'0:44' -> 44, '1:05:30' -> 3930, garbage -> None."""
    if not text:
        return None
    parts = (text or "").strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if not 1 <= len(nums) <= 3:
        return None
    total = 0
    for n in nums:
        total = total * 60 + n
    return total if total > 0 else None


_POST_RE = re.compile(
    r'<div class="tgme_widget_message[^>]*?data-post="ehsaspaak/(\d+)"')
_VIDEO_RE = re.compile(r'<video[^>]+src="((?:https?|file)://[^"]+\.mp4[^"]*)"')
_DUR_RE = re.compile(
    r'<time class="message_video_duration[^"]*">([^<]+)</time>')
_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_post_text(raw: str) -> str:
    txt = _TAG_RE.sub(" ", raw or "")
    txt = _html.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def parse_tg_page(html: str) -> list[dict]:
    """Parse a t.me/s/ehsaspaak page.

    Returns one dict per post that carries a DIRECT <video src=...mp4>
    (posts without a direct URL — text posts and 'not supported'
    placeholders — are skipped). Betting-ad posts are filtered out.
    Each dict: {post_id, url, duration (int|None), text}.
    """
    out = []
    if not html:
        return out
    marks = list(_POST_RE.finditer(html))
    for i, m in enumerate(marks):
        start = m.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        block = html[start:end]
        vm = _VIDEO_RE.search(block)
        if not vm:
            continue  # no direct video URL in this post
        tm = re.search(r"ehsaspaak/(\d+)", m.group(0))
        post_id = int(tm.group(1)) if tm else int(m.group(1))
        dm = _DUR_RE.search(block)
        duration = parse_duration(dm.group(1)) if dm else None
        tx = _TEXT_RE.search(block)
        text = _clean_post_text(tx.group(1)) if tx else ""
        if _is_ad_text(text):
            continue  # gambling/betting ad with a video: never intaken
        out.append({"post_id": post_id, "url": vm.group(1),
                    "duration": duration, "text": text})
    # dedup by post_id (a video tag can repeat inside one post block)
    seen = set()
    uniq = []
    for p in out:
        if p["post_id"] in seen:
            continue
        seen.add(p["post_id"])
        uniq.append(p)
    return uniq


def parse_aparat_show(payload: dict) -> dict | None:
    """Normalize one Aparat show-endpoint payload (JSON:API shape)."""
    try:
        attrs = (payload or {}).get("data", {}).get("attributes", {}) or {}
        uid = attrs.get("uid") or ""
        if not uid:
            return None
        cat = attrs.get("category") or {}
        dur = attrs.get("duration")
        try:
            dur = int(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur = None
        return {
            "uid": str(uid),
            "title": attrs.get("title") or "",
            "duration": dur,
            "owner": attrs.get("owner_username") or "",
            "category": cat.get("name") or "",
            "category_en": cat.get("name_en") or "",
            "can_download": bool(attrs.get("can_download")),
            "file_link": attrs.get("file_link") or "",
        }
    except Exception:
        return None


def parse_aparat_related(payload: dict) -> list[dict]:
    """Related-video UIDs embedded in a show payload (included[].Video)."""
    out = []
    try:
        for it in (payload or {}).get("included", []) or []:
            if it.get("type") != "Video":
                continue
            a = it.get("attributes") or {}
            uid = a.get("uid")
            if not uid:
                continue
            try:
                dur = int(a.get("duration")) if a.get("duration") is not None else None
            except (TypeError, ValueError):
                dur = None
            out.append({"uid": str(uid), "title": a.get("title") or "",
                        "duration": dur})
    except Exception:
        return []
    return out


def _aparat_accept(info: dict) -> bool:
    """Full gate for an Aparat video before intake."""
    if not info or not info.get("file_link"):
        return False
    if not is_persian_title(info.get("title") or ""):
        return False
    dur = info.get("duration")
    if dur is None or not (MIN_DURATION_SEC <= dur <= MAX_DURATION_SEC):
        return False
    if (info.get("category") or "").strip() not in ("موسیقی",):
        if (info.get("category_en") or "").strip().lower() != "music":
            return False
    if not info.get("can_download"):
        return False
    return True


def _aparat_candidate(rel: dict) -> bool:
    """Cheap pre-filter for a related UID (before spending a show call)."""
    if not rel.get("uid"):
        return False
    if not is_persian_title(rel.get("title") or ""):
        return False
    dur = rel.get("duration")
    if dur is not None and not (MIN_DURATION_SEC <= dur <= MAX_DURATION_SEC):
        return False
    return True


# ---------------------------------------------------------------------------
# Sync I/O (always called from an executor — never on the event loop)
# ---------------------------------------------------------------------------
def _http_get(url: str, timeout: int = HTTP_TIMEOUT_SEC) -> bytes | None:
    """GET with a browser UA. None on any failure. Never raises."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": TG_UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if getattr(r, "status", 200) != 200:
                return None
            return r.read()
    except Exception as e:
        _log.debug("clip_feed http_get failed %s: %s", url[:80], e)
        return None


def _fetch_tg_page() -> str | None:
    data = _http_get(TG_PAGE_URL)
    if not data:
        return None
    try:
        return data.decode("utf-8", "replace")
    except Exception:
        return None


def _fetch_aparat_show(uid: str) -> dict | None:
    data = _http_get(APARAT_SHOW_URL.format(uid=urllib.parse.quote(uid)))
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except Exception:
        return None


def download_url_to_file(url: str, dest: str,
                         max_bytes: int = MAX_DOWNLOAD_BYTES
                         ) -> tuple[str | None, int | str]:
    """Stream a URL to dest with a hard size cap.

    Returns (dest, size) on success, (None, reason) on failure.
    reason: 'too_large' | 'http' | 'timeout' | 'error'.
    Never raises. file:// URLs work (used by the sandbox tests).
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": TG_UA})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
            total = 0
            length = r.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) > max_bytes:
                        return (None, "too_large")
                except ValueError:
                    pass
            with open(dest, "wb") as fh:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        try:
                            os.remove(dest)
                        except OSError:
                            pass
                        return (None, "too_large")
                    fh.write(chunk)
            return (dest, total)
    except urllib.error.HTTPError:
        return (None, "http")
    except (TimeoutError, urllib.error.URLError) as e:
        if isinstance(e, urllib.error.URLError) and "timed out" in str(e).lower():
            return (None, "timeout")
        return (None, "http")
    except Exception as e:
        _log.debug("clip_feed download failed %s: %s", url[:80], e)
        return (None, "error")


def probe_video(path: str) -> dict | None:
    """ffprobe a local file. None on failure. Never raises."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type,codec_name,width,height",
             "-show_entries", "format=duration,size",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout or "{}")
        fmt = data.get("format", {}) or {}
        width = height = 0
        has_audio = False
        for st in data.get("streams", []) or []:
            if st.get("codec_type") == "audio":
                has_audio = True
            if st.get("codec_type") == "video":
                width = int(st.get("width") or 0)
                height = int(st.get("height") or 0)
        try:
            dur = float(fmt.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0
        try:
            size = int(fmt.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return {"duration": dur, "width": width, "height": height,
                "has_audio": has_audio, "size": size}
    except Exception as e:
        _log.debug("clip_feed probe failed %s: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# Feed state (key/value in Turso, via the story_archive connection)
# ---------------------------------------------------------------------------
def feed_get(key: str, default: str = "") -> str:
    conn = _sarch._get_conn()
    if not conn:
        return default
    try:
        cur = conn.execute(
            "SELECT value FROM feed_state WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else default
    except Exception:
        return default


def feed_set(key: str, value: str) -> bool:
    conn = _sarch._get_conn()
    if not conn:
        return False
    try:
        conn.execute(
            "INSERT OR REPLACE INTO feed_state (key, value, updated_at)"
            " VALUES (?,?,?)", (key, str(value), int(time.time())))
        conn.commit()
        return True
    except Exception:
        return False


def feed_key_seen(feed_key: str) -> bool:
    """True when a clip with this feed key is already stored (any status)."""
    conn = _sarch._get_conn()
    if not conn:
        return False
    try:
        cur = conn.execute(
            "SELECT 1 FROM story_archive WHERE feed_key=?", (feed_key,))
        return cur.fetchone() is not None
    except Exception:
        return False


def refresh_pending_tg_urls(posts: list[dict]) -> int:
    """Refresh source_url for Telegram feed clips still on the page
    (their CDN tokens can go stale) and not yet finally processed.
    Zero extra HTTP — uses the already-fetched page. Returns how many
    rows were updated."""
    if not posts:
        return 0
    conn = _sarch._get_conn()
    if not conn:
        return 0
    try:
        fresh = {p["post_id"]: p["url"] for p in posts if p.get("url")}
        cur = conn.execute(
            "SELECT id, feed_key, source_url FROM story_archive"
            " WHERE status IN ('pending','active') AND feed_key LIKE 'tg:%'"
            " AND (processed_file_id IS NULL OR processed_file_id='')")
        n = 0
        for clip_id, feed_key, old_url in cur.fetchall():
            try:
                pid = int((feed_key or "")[3:])
            except (TypeError, ValueError):
                continue
            new_url = fresh.get(pid)
            if new_url and new_url != old_url:
                conn.execute(
                    "UPDATE story_archive SET source_url=? WHERE id=?",
                    (new_url, clip_id))
                n += 1
        if n:
            conn.commit()
        return n
    except Exception as e:
        _log.debug("clip_feed refresh_pending_tg_urls failed: %s", e)
        return 0


def get_feed_status() -> dict:
    """Admin-panel status lines. Never raises."""
    try:
        last_run = int(feed_get("last_run_at", "0") or 0)
    except (TypeError, ValueError):
        last_run = 0
    try:
        last_new = int(feed_get("last_new", "0") or 0)
    except (TypeError, ValueError):
        last_new = 0
    try:
        last_seen = int(feed_get("tg_last_seen", "0") or 0)
    except (TypeError, ValueError):
        last_seen = 0
    pending_feed = 0
    conn = _sarch._get_conn()
    if conn:
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM story_archive"
                " WHERE status='pending' AND feed_key != ''")
            pending_feed = cur.fetchone()[0]
        except Exception:
            pending_feed = 0
    return {
        "last_run_at": last_run,
        "last_new": last_new,
        "last_error": feed_get("last_error", ""),
        "last_run_kind": feed_get("last_run_kind", ""),
        "last_seen_tg": last_seen,
        "pending_feed": pending_feed,
    }


# ---------------------------------------------------------------------------
# Intake (shared by both sources)
# ---------------------------------------------------------------------------
async def intake_feed_item(*, feed_key: str, source_url: str, title: str,
                           duration_hint: int | None = None,
                           source_username: str = ""
                           ) -> tuple[bool, str]:
    """Download, validate and archive ONE feed clip as status='pending'.

    Returns (True, 'ok') or (False, reason). reason: 'duplicate' |
    'too_large' | 'http' | 'timeout' | 'probe_failed' | 'no_audio' |
    'too_short' | 'too_long' | 'db' | 'bad_input'. Never raises.
    The clip is NEVER auto-accepted — the moderation queue decides.
    """
    import shutil

    if not feed_key or not source_url:
        return (False, "bad_input")
    if _sarch.is_duplicate("feed:" + feed_key) or feed_key_seen(feed_key):
        return (False, "duplicate")
    tmpd = tempfile.mkdtemp(prefix="feed_in_")
    try:
        dest = os.path.join(tmpd, "clip.mp4")
        loop = asyncio.get_running_loop()
        path, res = await loop.run_in_executor(
            None, download_url_to_file, source_url, dest, MAX_DOWNLOAD_BYTES)
        if not path:
            return (False, res if isinstance(res, str) else "download_failed")
        size = int(res) if isinstance(res, int) else 0
        info = await loop.run_in_executor(None, probe_video, dest)
        if not info:
            return (False, "probe_failed")
        if not info.get("has_audio"):
            return (False, "no_audio")
        dur = info.get("duration") or duration_hint or 0
        if dur < MIN_DURATION_SEC:
            return (False, "too_short")
        if dur > MAX_DURATION_SEC:
            return (False, "too_long")
        ok, why = _sarch.archive_clip(
            file_unique_id="feed:" + feed_key, file_id="",
            category="", subcategory="", mode="resend",
            source_username=source_username,
            orig_caption=(title or "")[:500],
            duration=int(dur), width=info.get("width") or 0,
            height=info.get("height") or 0,
            status="pending", file_size=size,
            categorized_by="feed", source_url=source_url, feed_key=feed_key)
        return (ok, why)
    except Exception as e:
        _log.warning("clip_feed intake %s failed: %s", feed_key, e)
        return (False, "db")
    finally:
        try:
            shutil.rmtree(tmpd, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Source cycles (async wrappers; sync I/O stays in the executor)
# ---------------------------------------------------------------------------
async def run_telegram_cycle(*, tg_fetcher=None) -> dict:
    """One Telegram check: new video posts since last_seen -> intake.

    Never raises. Returns {new, skipped, error}.
    """
    out = {"new": 0, "skipped": 0, "error": None}
    try:
        loop = asyncio.get_running_loop()
        fetch = tg_fetcher or _fetch_tg_page
        html = await loop.run_in_executor(None, fetch)
        if not html:
            out["error"] = "tg_fetch_failed"
            return out
        posts = parse_tg_page(html)
        refresh_pending_tg_urls(posts)
        try:
            last_seen = int(feed_get("tg_last_seen", "0") or 0)
        except (TypeError, ValueError):
            last_seen = 0
        new_posts = [p for p in posts if p["post_id"] > last_seen]
        for p in new_posts[:TG_MAX_PER_RUN]:
            ok, why = await intake_feed_item(
                feed_key=f"tg:{p['post_id']}", source_url=p["url"],
                title=p.get("text") or "", duration_hint=p.get("duration"),
                source_username=TG_CHANNEL)
            if ok:
                out["new"] += 1
            else:
                out["skipped"] += 1
                _log.info("clip_feed tg post %s skipped: %s", p["post_id"], why)
        if posts:
            top = max(p["post_id"] for p in posts)
            if top > last_seen:
                feed_set("tg_last_seen", str(top))
        return out
    except Exception as e:
        _log.warning("clip_feed telegram cycle failed: %s", e)
        out["error"] = "tg_cycle_failed"
        return out


async def run_aparat_cycle(*, bootstrap: bool = False,
                           aparat_fetcher=None) -> dict:
    """One Aparat check: walk the related-UID graph from the seed.

    Bootstrap (first run) does a bounded BFS for immediate stock;
    later runs re-walk the seed's current related list (bounded).
    Never raises. Returns {new, skipped, calls, error}.
    """
    out = {"new": 0, "skipped": 0, "calls": 0, "error": None}
    try:
        loop = asyncio.get_running_loop()
        fetch = aparat_fetcher or _fetch_aparat_show
        max_calls = AP_BOOTSTRAP_SHOW_CALLS if bootstrap else AP_RUN_SHOW_CALLS
        max_new = AP_BOOTSTRAP_NEW if bootstrap else AP_RUN_NEW
        queue = list(APARAT_SEED_UIDS)
        seen_uids: set[str] = set()
        while queue and out["calls"] < max_calls and out["new"] < max_new:
            uid = queue.pop(0)
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            payload = await loop.run_in_executor(None, fetch, uid)
            out["calls"] += 1
            if not payload:
                continue
            for rel in parse_aparat_related(payload):
                if (rel["uid"] not in seen_uids
                        and _aparat_candidate(rel)):
                    queue.append(rel["uid"])
            info = parse_aparat_show(payload)
            if not info:
                continue
            if not _aparat_accept(info):
                continue
            ok, why = await intake_feed_item(
                feed_key=f"ap:{info['uid']}", source_url=info["file_link"],
                title=info["title"], duration_hint=info["duration"],
                source_username=info["owner"])
            if ok:
                out["new"] += 1
            else:
                out["skipped"] += 1
                _log.info("clip_feed aparat %s skipped: %s", info["uid"], why)
        return out
    except Exception as e:
        _log.warning("clip_feed aparat cycle failed: %s", e)
        out["error"] = "ap_cycle_failed"
        return out


# ---------------------------------------------------------------------------
# Whole cycle + hourly loop
# ---------------------------------------------------------------------------
_FEED_LOCK = None


def _feed_lock():
    global _FEED_LOCK
    if _FEED_LOCK is None:
        _FEED_LOCK = asyncio.Lock()
    return _FEED_LOCK


async def run_feed_cycle(*, bootstrap: bool = False,
                         tg_fetcher=None, aparat_fetcher=None) -> dict:
    """One full feed cycle (Telegram + Aparat). Single-flight: a second
    concurrent call returns {'skipped': True}. Never raises."""
    lock = _feed_lock()
    if lock.locked():
        return {"skipped": True, "tg_new": 0, "ap_new": 0, "error": None}
    async with lock:
        t0 = time.time()
        _log.info("clip_feed cycle start (bootstrap=%s)", bootstrap)
        stats = {"skipped": False, "tg_new": 0, "ap_new": 0,
                 "tg_error": None, "ap_error": None, "error": None}
        try:
            tg = await run_telegram_cycle(tg_fetcher=tg_fetcher)
            stats["tg_new"] = tg.get("new", 0)
            stats["tg_error"] = tg.get("error")
            ap = await run_aparat_cycle(bootstrap=bootstrap,
                                        aparat_fetcher=aparat_fetcher)
            stats["ap_new"] = ap.get("new", 0)
            stats["ap_error"] = ap.get("error")
            errs = [e for e in (tg.get("error"), ap.get("error")) if e]
            stats["error"] = ";".join(errs) if errs else None
        except Exception as e:
            _log.warning("clip_feed cycle failed: %s", e)
            stats["error"] = "cycle_failed"
        try:
            feed_set("last_run_at", str(int(time.time())))
            feed_set("last_new", str(stats["tg_new"] + stats["ap_new"]))
            feed_set("last_error", stats["error"] or "")
            feed_set("last_run_kind",
                     "bootstrap" if bootstrap else "incremental")
        except Exception:
            pass
        _log.info("clip_feed cycle done in %.1fs: tg_new=%d ap_new=%d "
                  "error=%s", time.time() - t0, stats["tg_new"],
                  stats["ap_new"], stats["error"])
        return stats


async def clip_feed_loop(interval_sec: int = FEED_INTERVAL_SEC,
                         first_delay_sec: int = FEED_FIRST_DELAY_SEC) -> None:
    """Hourly feed loop for bot startup. NEVER dies: every iteration is
    wrapped in try/except and the loop keeps sleeping. The first run is
    a bounded bootstrap so the archive stocks immediately."""
    await asyncio.sleep(first_delay_sec)
    first = True
    while True:
        try:
            await run_feed_cycle(bootstrap=first)
        except Exception as e:  # noqa: BLE001 - the loop itself never dies
            _log.warning("clip_feed loop iteration failed: %s", e)
        first = False
        await asyncio.sleep(interval_sec)
