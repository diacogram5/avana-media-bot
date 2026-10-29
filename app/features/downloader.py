"""Telegram-native smart downloader (callback prefix ``dl:``).

Honest, queue-based downloading:
- A link is validated with the same rules as :func:`app.media.process_link`.
- Format options are REAL per source: a direct file URL gets a real
  download (HEAD-probed); YouTube/TikTok get their official-player note
  (no fabricated file); Instagram/Aparat try the public page for a real
  media file and otherwise fall back to the official player.
- At most 2 jobs run concurrently (module-level ``asyncio.Semaphore(2)``).
- The same normalized URL reuses a cached ``file_id``
  (stage ``آماده است (از کش)``).
- Every failure is one short Persian line; never a traceback.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from ..config import Config
from ..media import process_link
from ..bot.logic import menu_escape
from ..security import validate_link
from ..services.link_media import extract_media_url, extract_video_qualities

# ---------------------------------------------------------------------------
# Spec-exact strings (port-spec/web-ui-spec.md, sections 4 and 11)
# ---------------------------------------------------------------------------

TITLE = "دانلودر هوشمند"
EXPLAINER = (
    "لینک اینستاگرام، یوتیوب، تیک‌تاک، آپارات یا فایل مستقیم را بفرستید؛ "
    "نوع محتوا و گزینه‌های واقعی آن خودکار تشخیص داده می‌شود. "
    "پردازش صفی است (هم‌زمان حداکثر دو کار) و فایل تکراری از کش داده می‌شود. "
    "فقط محتوای عمومی و قانونی؛ محتوای خصوصی یا پولی دور زده نمی‌شود."
)
PROMPT_SEND_LINK = "یک لینک بفرستید."
QUEUE_EMPTY = "صف خالی است؛ یک لینک بفرستید تا اینجا به صف اضافه شود."
PICK_FORMAT = "برای شروع پردازش، یک فرمت را انتخاب کنید."
INVALID_LINK = "این لینک قابل پردازش نیست."
UNAVAILABLE = "فایل این لینک در دسترس نیست؛ ممکن است پست خصوصی یا حذف شده باشد."
ENQUEUE_FAILED = "ثبت در صف دانلود نشد؛ دوباره تلاش کنید."
LINK_FAILED = "لینک پردازش نشد؛ دوباره تلاش کنید."
TOO_BIG = "فایل برای ارسال از طریق تلگرام بزرگ است."
RETRY_LATER = "خطای موقت؛ تلاش دوباره"
DELETED = "از صف دانلود حذف شد."
EXITED = "از دانلودر خارج شدید."
FILE_LABEL = "دانلود فایل"
YOUTUBE_FORMAT_LABEL = "پخش با پلیر رسمی یوتیوب"
TIKTOK_FORMAT_LABEL = "پخش با پلیر رسمی تیک‌تاک"
INSTAGRAM_FORMAT_LABEL = "پخش با پلیر رسمی اینستاگرام"
APARAT_FORMAT_LABEL = "پخش با پلیر رسمی آپارات"
YOUTUBE_NOTE = "یوتیوب فایل مستقیم عمومی نمی‌دهد؛ دانلود فایل از این منبع ممکن نیست"
TIKTOK_NOTE = "تیک‌تاک فایل مستقیم عمومی نمی‌دهد؛ دانلود فایل از این منبع ممکن نیست"

NOT_A_URL = (
    "این متن شبیه لینک نیست. یک نشانی کامل (https://…) بفرستید "
    "یا با /cancel از این بخش خارج شوید."
)
LINK_NOT_VALID = "لینک معتبر نیست؛ یک نشانی کامل با https:// بفرستید."
SCHEME_NOT_ALLOWED = "فقط لینک‌های http و https پشتیبانی می‌شوند."
PRIVATE_BLOCKED = "این نشانی داخلی یا خصوصی است و به دلایل امنیتی پردازش نمی‌شود."
DOMAIN_NOT_ALLOWED = (
    "این دامنه در فهرست منابع پشتیبانی‌شده نیست. منابع فعلی: یوتیوب، "
    "اینستاگرام، تیک‌تاک، آپارات و لینک مستقیم فایل صوتی/تصویری."
)

STAGE_LABELS = {
    "queued": "در صف پردازش",
    "preparing": "در حال آماده‌سازی",
    "fetching": "در حال دریافت فایل از منبع",
    "saving": "در حال ذخیره فایل",
    "done": "دانلود کامل شد",
    "cached": "آماده است (از کش)",
    "player": "آماده پخش با پلیر رسمی",
    "retrying": "خطای موقت؛ تلاش دوباره",
    "failed": "ناموفق",
}

QUEUE_STATUS_LABELS = {
    "ready": "آماده",
    "ready_cached": "آماده · از کش",
    "failed": "ناموفق",
    "working": "در حال پردازش",
    "queued": "در صف",
}

CONTENT_TYPE_LABELS = {
    "video": "ویدیو",
    "audio": "صوتی",
    "photo": "عکس",
    "post": "پست",
    "unknown": "نامشخص",
}

SOURCE_LABELS = {
    "direct": "فایل مستقیم",
    "youtube": "یوتیوب",
    "tiktok": "تیک‌تاک",
    "instagram": "اینستاگرام",
    "aparat": "آپارات",
    "unknown": "منبع نامشخص",
}

PLAYER_FORMATS = {"yt_player", "tt_player", "ig_player", "ap_player"}

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# At most two downloads run at the same time (spec section 11).
_SEMAPHORE = asyncio.Semaphore(2)

BRIEF = {
    "enqueue": ENQUEUE_FAILED,
    "link": LINK_FAILED,
}


class DlStates(StatesGroup):
    # group_name="dl" so the full FSM state is exactly "dl:await_url"
    # (aiogram otherwise prefixes the StatesGroup class name).
    await_url = State(state="await_url", group_name="dl")


class _TransientError(Exception):
    """A failure worth retrying (network blip, 5xx, rate limit)."""


class _PermanentError(Exception):
    """A failure retrying will not fix (gone, too big, forbidden)."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def validation_line(reason: str) -> str:
    """Section-4 validation message for a ``validate_link`` reason."""
    if reason in ("empty_or_too_long", "unparsable", "no_host",
                  "userinfo_not_allowed", "dns_failed"):
        return NOT_A_URL
    if reason == "scheme_not_allowed":
        return SCHEME_NOT_ALLOWED
    if reason in ("private_host_blocked", "private_ip_blocked",
                  "resolves_to_private_ip"):
        return PRIVATE_BLOCKED
    if reason == "domain_not_allowed":
        return DOMAIN_NOT_ALLOWED
    return LINK_NOT_VALID


def normalize_url(url: str) -> str:
    """Canonical form used for cache lookups: lowercase host, no junk."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    if not host:
        return url
    netloc = host if not parts.port else f"{host}:{parts.port}"
    path = parts.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    out = f"{parts.scheme.lower()}://{netloc}{path}"
    if parts.query:
        out += "?" + parts.query
    return out


def detect_source(url: str, validation=None) -> str:
    """Source key for a URL, using process_link-equivalent rules.

    TikTok is an explicitly supported downloader source per the web UI
    spec even though the base domain allowlist predates it, so a public
    ``tiktok.com`` host that passed every other security check is
    accepted here. Nothing else is relaxed.
    """
    validation = validation if validation is not None else validate_link(url)
    host = (validation.host or "").lower()
    if validation.ok:
        kind = validation.kind
        if kind == "direct_file":
            return "direct"
        if kind == "tiktok":
            return "tiktok"
        if "youtube" in host or host == "youtu.be":
            return "youtube"
        if "instagram" in host:
            return "instagram"
        if "aparat" in host:
            return "aparat"
        return "unknown"
    if validation.reason == "domain_not_allowed" and (
        host == "tiktok.com" or host.endswith(".tiktok.com")
    ):
        return "tiktok"
    return "invalid"


def _content_key(content_type: str) -> str:
    ct = (content_type or "").lower()
    if ct.startswith("video/"):
        return "video"
    if ct.startswith("audio/"):
        return "audio"
    if ct.startswith("image/"):
        return "photo"
    return "unknown"


@dataclass
class FormatResult:
    formats: list
    note: str = ""
    content_type: str = "unknown"
    file_url: str = ""
    error: str = ""  # "" | "unavailable"


async def detect_formats(url: str, source: str) -> FormatResult:
    """Real, honest format options for a source. No fabricated formats.

    - direct: HEAD probe (15s); audio/video content-type -> file download.
    - youtube/tiktok: official player only (platforms give no public file).
    - instagram/aparat: fetch the public page; a real advertised media
      file -> file download, otherwise the official player; an
      unreachable page -> "unavailable" (private/deleted), never a file.
    """
    if source == "youtube":
        return FormatResult(
            formats=[{"id": "yt_player", "label": YOUTUBE_FORMAT_LABEL}],
            note=YOUTUBE_NOTE,
            content_type="post",
        )
    if source == "tiktok":
        return FormatResult(
            formats=[{"id": "tt_player", "label": TIKTOK_FORMAT_LABEL}],
            note=TIKTOK_NOTE,
            content_type="post",
        )
    if source == "direct":
        head = await _head_media(url)
        if head is None:
            return FormatResult(formats=[], error="")
        ckey = _content_key(head.headers.get("content-type", ""))
        if ckey in ("video", "audio"):
            return FormatResult(
                formats=[{"id": "file", "label": FILE_LABEL}],
                content_type=ckey,
                file_url=url,
            )
        return FormatResult(formats=[], content_type=ckey)
    if source in ("instagram", "aparat"):
        html = await _fetch_page(url)
        if html is None:
            return FormatResult(formats=[], error="unavailable")
        qualities = extract_video_qualities(html)
        if len(qualities) >= 2:
            # Genuinely multiple qualities: the user picks BEFORE any
            # download. Options come only from the page's real advertised
            # files — never fabricated. Sizes are HEAD-probed where known.
            def _res(q):
                m = re.search(r"(\d+)", q.get("hint") or "")
                return int(m.group(1)) if m else 0
            qualities.sort(key=_res, reverse=True)
            formats = []
            for i, q in enumerate(qualities):
                size = await _probe_size(q["url"])
                label = q["hint"] or f"کیفیت {i + 1}"
                if size:
                    label += f" · ≈{_human_mb(size)}"
                formats.append({"id": f"q{i}", "label": f"🎬 {label}",
                                "url": q["url"]})
            formats.append({"id": "qbest", "label": "⭐ بالاترین کیفیت موجود",
                            "url": qualities[0]["url"]})
            return FormatResult(formats=formats, content_type="video")
        media_url = extract_media_url(html)
        if media_url:
            return FormatResult(
                formats=[{"id": "file", "label": FILE_LABEL}],
                content_type="video",
                file_url=media_url,
            )
        label = INSTAGRAM_FORMAT_LABEL if source == "instagram" else APARAT_FORMAT_LABEL
        return FormatResult(
            formats=[{"id": f"{'ig' if source == 'instagram' else 'ap'}_player",
                      "label": label}],
            content_type="post",
        )
    return FormatResult(formats=[], error="")


def _human_mb(size_bytes: int) -> str:
    mb = size_bytes / (1024 * 1024)
    return f"{mb:.0f} مگابایت" if mb >= 1 else f"{size_bytes // 1024} کیلوبایت"


async def _probe_size(url: str) -> int | None:
    """HEAD-probed content length, or None when unknown."""
    resp = await _head_media(url)
    if resp is None:
        return None
    cl = resp.headers.get("content-length")
    return int(cl) if cl and cl.isdigit() else None


async def _head_media(url: str):
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.head(url, headers={"User-Agent": _UA})
    except Exception:
        return None
    if resp.status_code >= 400:
        return None
    return resp


async def _fetch_page(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": _UA})
    except Exception:
        return None
    if resp.status_code >= 400:
        return None
    return resp.text


def render_job(job: dict) -> str:
    """Job message body: title + source/type + ``{stageLabel} · {N}٪``."""
    stage = STAGE_LABELS.get(job.get("stage", ""), job.get("stage", ""))
    source = SOURCE_LABELS.get(job.get("source", ""), job.get("source", ""))
    ctype = CONTENT_TYPE_LABELS.get(job.get("content_type", ""), "نامشخص")
    return f"{TITLE}\n{source} · {ctype}\n{stage} · {int(job.get('progress', 0))}٪"


def render_queue(jobs: list[dict]) -> str:
    if not jobs:
        return QUEUE_EMPTY
    lines = [f"{TITLE} — صف دانلود"]
    for i, job in enumerate(jobs, 1):
        status = QUEUE_STATUS_LABELS.get(job.get("status", ""), job.get("status", ""))
        short = job.get("url", "")
        if len(short) > 42:
            short = short[:41] + "…"
        lines.append(f"{i}. {short} — {status}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DB helpers (take conn; feature-local per the integration contract)
# ---------------------------------------------------------------------------

_JOB_COLUMNS = (
    "id", "user_id", "url", "source", "content_type", "status", "stage",
    "progress", "formats_json", "chosen_format", "result_file_id",
    "result_url", "error", "created_at", "updated_at",
)


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    return {col: row[col] for col in _JOB_COLUMNS}


def create_job(conn: sqlite3.Connection, user_id: int, url: str, source: str,
               content_type: str, formats: list, note: str = "") -> int | None:
    now = int(time.time())
    try:
        cur = conn.execute(
            """INSERT INTO download_jobs
               (user_id, url, source, content_type, status, stage, progress,
                formats_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user_id, url, source, content_type, "queued", "queued", 0,
             json.dumps(formats, ensure_ascii=False), now, now),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error:
        return None


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM download_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_dict(row)


def update_job(conn: sqlite3.Connection, job_id: int, **fields) -> None:
    fields["updated_at"] = int(time.time())
    sets = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE download_jobs SET {sets} WHERE id = ?",
        (*fields.values(), job_id),
    )
    conn.commit()


def delete_job(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("DELETE FROM download_jobs WHERE id = ?", (job_id,))
    conn.commit()


def list_jobs(conn: sqlite3.Connection, user_id: int, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM download_jobs WHERE user_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def find_cached(conn: sqlite3.Connection, url: str) -> dict | None:
    """A finished job for the same normalized URL with a stored file_id."""
    row = conn.execute(
        """SELECT * FROM download_jobs
           WHERE url = ? AND status IN ('ready', 'ready_cached')
             AND result_file_id != ''
           ORDER BY updated_at DESC, id DESC LIMIT 1""",
        (normalize_url(url),),
    ).fetchone()
    return _row_to_dict(row)


def retry_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """Reset a failed job back to queued. Returns False if not failed."""
    job = get_job(conn, job_id)
    if not job or job["status"] != "failed":
        return False
    update_job(conn, job_id, status="queued", stage="queued", progress=0,
               error="", result_file_id="", result_url=job.get("result_url", ""))
    return True


def source_note(source: str) -> str:
    """The official-player note for sources that give no public file."""
    if source == "youtube":
        return YOUTUBE_NOTE
    if source == "tiktok":
        return TIKTOK_NOTE
    return ""


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------


def job_keyboard(job_id: int, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="دانلود فایل",
                              callback_data=f"dl:go:{job_id}")],
        [InlineKeyboardButton(text="منبع اصلی", url=url)],
        [InlineKeyboardButton(text="تلاش دوباره",
                              callback_data=f"dl:retry:{job_id}")],
        [InlineKeyboardButton(text="حذف از صف دانلود",
                              callback_data=f"dl:del:{job_id}")],
    ])


def format_keyboard(job_id: int, formats: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f["label"],
                              callback_data=f"dl:fmt:{job_id}:{f['id']}")]
        for f in formats
    ])


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def _refresh_status(message: types.Message, conn: sqlite3.Connection,
                          job_id: int, extra: str = "") -> None:
    job = get_job(conn, job_id)
    if not job:
        return
    text = render_job(job)
    if extra:
        text += f"\n{extra}"
    try:
        await message.edit_text(text, reply_markup=job_keyboard(job_id, job["url"]))
    except Exception:
        pass


async def _download_bytes(url: str, limit_bytes: int,
                          on_progress=None) -> bytes:
    buf = bytearray()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        ) as client:
            async with client.stream("GET", url,
                                     headers={"User-Agent": _UA}) as resp:
                status = resp.status_code
                if status in (401, 403, 404, 410):
                    raise _PermanentError(UNAVAILABLE)
                if status >= 400:
                    raise _TransientError(RETRY_LATER)
                total = resp.headers.get("content-length")
                total = int(total) if total and total.isdigit() else None
                if total is not None and total > limit_bytes:
                    raise _PermanentError(TOO_BIG)
                async for chunk in resp.aiter_bytes(65536):
                    buf += chunk
                    if len(buf) > limit_bytes:
                        raise _PermanentError(TOO_BIG)
                    if on_progress:
                        await on_progress(len(buf), total)
    except (_TransientError, _PermanentError):
        raise
    except Exception:
        raise _TransientError(RETRY_LATER)
    return bytes(buf)


def _filename(url: str, content_type: str) -> str:
    try:
        base = urlsplit(url).path.rsplit("/", 1)[-1] or "file"
    except ValueError:
        base = "file"
    if "." not in base:
        ext = {"video": ".mp4", "audio": ".mp3", "photo": ".jpg"}.get(
            content_type, ".bin")
        base += ext
    return base[:120]


async def _send_file(message: types.Message, job: dict, data: bytes) -> str:
    """Send the downloaded bytes via the unified media pipeline; return
    the Telegram file_id for caching."""
    from ..bot.media_send import send_music, send_video
    ctype = job.get("content_type", "unknown")
    name = _filename(job.get("result_url") or job.get("url", ""), ctype)
    if ctype == "video":
        sent = await send_video(
            message, BufferedInputFile(data, filename=name))
        if sent is None:
            raise _TransientError(RETRY_LATER)
        return sent.video.file_id
    if ctype == "audio":
        # The pipeline delivers a real MP3 with honest specs; the title
        # is unknown here so the URL basename seeds the filename.
        sent = await send_music(message, data, name.rsplit(".", 1)[0])
        if sent is None:
            raise _TransientError(RETRY_LATER)
        return sent.audio.file_id
    tg_file = BufferedInputFile(data, filename=name)
    if ctype == "photo":
        sent = await message.answer_photo(tg_file)
        return sent.photo[-1].file_id
    sent = await message.answer_document(tg_file)
    return sent.document.file_id


async def run_job(message: types.Message, conn: sqlite3.Connection,
                  config: Config, job_id: int) -> None:
    """Execute one download job; the SAME message is edited for progress."""
    job = get_job(conn, job_id)
    if not job or job.get("status") != "queued":
        return
    async with _SEMAPHORE:
        job = get_job(conn, job_id)
        if not job or job.get("status") != "queued":
            return
        update_job(conn, job_id, status="working", stage="preparing",
                   progress=5, error="")
        await _refresh_status(message, conn, job_id)
        try:
            formats = json.loads(job.get("formats_json") or "[]")
            chosen = next((f for f in formats
                           if f.get("id") == job.get("chosen_format")), None)
            if not chosen:
                raise _PermanentError(LINK_FAILED)
            if chosen["id"] in PLAYER_FORMATS:
                update_job(conn, job_id, status="ready", stage="player",
                           progress=100)
                await _refresh_status(message, conn, job_id)
                return
            download_url = job.get("result_url") or job.get("url")
            limit = min(config.max_file_mb, 50) * 1024 * 1024
            update_job(conn, job_id, stage="fetching", progress=10)
            await _refresh_status(message, conn, job_id)
            last = {"pct": 10}

            async def _prog(done: int, total: int | None) -> None:
                if not total:
                    return
                pct = 10 + min(80, int(80 * done / total))
                if pct - last["pct"] >= 15:
                    last["pct"] = pct
                    update_job(conn, job_id, progress=pct)
                    await _refresh_status(message, conn, job_id)

            data = await _download_bytes(download_url, limit, _prog)
            update_job(conn, job_id, stage="saving", progress=90)
            await _refresh_status(message, conn, job_id)
            job = get_job(conn, job_id)
            file_id = await _send_file(message, job, data)
            update_job(conn, job_id, status="ready", stage="done",
                       progress=100, result_file_id=file_id)
            await _refresh_status(message, conn, job_id)
        except _TransientError as exc:
            update_job(conn, job_id, status="failed", stage="retrying",
                       error=str(exc))
            await _refresh_status(message, conn, job_id)
        except _PermanentError as exc:
            update_job(conn, job_id, status="failed", stage="failed",
                       error=str(exc))
            await _refresh_status(message, conn, job_id)
        except Exception:
            update_job(conn, job_id, status="failed", stage="retrying",
                       error=RETRY_LATER)
            await _refresh_status(message, conn, job_id)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _extract_url(text: str) -> str:
    for token in (text or "").split():
        low = token.lower()
        if low.startswith("http://") or low.startswith("https://"):
            return token.strip().strip(".,);]}>\"'")
    return ""


async def _start_job_from_message(message: types.Message,
                                  conn: sqlite3.Connection,
                                  config: Config, job: dict, note: str) -> None:
    status_msg = await message.answer(
        render_job(job), reply_markup=job_keyboard(job["id"], job["url"]))
    if note:
        await message.answer(note)
    asyncio.create_task(run_job(status_msg, conn, config, job["id"]))


def pick_format(conn: sqlite3.Connection, job_id: int, user_id: int,
                fmt_id: str) -> dict | None:
    """Record the user's format/quality choice. Returns the updated job,
    or None when the choice is invalid (unknown job/format, wrong user).

    The chosen quality's own file URL becomes the download source —
    exactly what the user picked, nothing else.
    """
    job = get_job(conn, job_id)
    if not job or job["user_id"] != user_id:
        return None
    formats = json.loads(job.get("formats_json") or "[]")
    chosen = next((f for f in formats if f.get("id") == fmt_id), None)
    if not chosen:
        return None
    update_job(conn, job_id, chosen_format=fmt_id,
               result_url=chosen.get("url") or job.get("result_url") or "")
    return get_job(conn, job_id)


def register(router: Router) -> None:
    fr = Router()

    @fr.callback_query(F.data == "dl:open")
    async def dl_open(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.set_state(DlStates.await_url)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="menu:main")]])
        await callback.message.answer(
            f"{TITLE}\n\n{EXPLAINER}\n\n{PROMPT_SEND_LINK}", reply_markup=kb)

    @fr.message(DlStates.await_url, Command("cancel"))
    async def dl_cancel(message: types.Message, state: FSMContext):
        await state.clear()
        await message.answer(EXITED)

    @fr.message(DlStates.await_url, F.text)
    @menu_escape
    async def dl_receive_url(message: types.Message, conn: sqlite3.Connection,
                             config: Config, state: FSMContext):
        text = (message.text or "").strip()
        url = _extract_url(text)
        validation = validate_link(url or text)
        # Reuse the link pipeline's rules; detect_source only adds the
        # spec-mandated TikTok exception on top.
        outcome = process_link(url or text)
        source = detect_source(url or text, validation)
        if not url or (not outcome.ok and source != "tiktok"):
            line = validation_line(validation.reason)
            await message.answer(f"{INVALID_LINK}\n{line}")
            return
        norm = normalize_url(url)
        cached = find_cached(conn, norm)
        if cached:
            update_job(conn, cached["id"], status="ready_cached",
                       stage="cached", progress=100)
            await state.clear()
            await message.answer(f"{TITLE}\n{STAGE_LABELS['cached']} · 100٪")
            try:
                ctype = cached.get("content_type", "unknown")
                tg_file = cached["result_file_id"]
                if ctype == "video":
                    await message.answer_video(tg_file)
                elif ctype == "audio":
                    await message.answer_audio(tg_file)
                elif ctype == "photo":
                    await message.answer_photo(tg_file)
                else:
                    await message.answer_document(tg_file)
            except Exception:
                await message.answer(BRIEF["link"])
            return
        result = await detect_formats(url, source)
        if result.error == "unavailable":
            await message.answer(UNAVAILABLE)
            return
        if not result.formats:
            await message.answer(f"{INVALID_LINK}\n{LINK_NOT_VALID}")
            return
        job_id = create_job(conn, message.from_user.id, norm, source,
                            result.content_type, result.formats)
        if not job_id:
            await message.answer(BRIEF["enqueue"])
            return
        if result.file_url:
            update_job(conn, job_id, result_url=result.file_url)
        job = get_job(conn, job_id)
        if len(result.formats) > 1:
            await state.clear()
            await message.answer(
                f"{TITLE}\n{PICK_FORMAT}",
                reply_markup=format_keyboard(job_id, result.formats),
            )
            if result.note:
                await message.answer(result.note)
            return
        update_job(conn, job_id, chosen_format=result.formats[0]["id"])
        job = get_job(conn, job_id)
        await state.clear()
        await _start_job_from_message(message, conn, config, job, result.note)

    @fr.message(DlStates.await_url)
    async def dl_receive_non_text(message: types.Message):
        await message.answer(PROMPT_SEND_LINK)

    @fr.callback_query(F.data.startswith("dl:fmt:"))
    async def dl_pick_format(callback: types.CallbackQuery,
                             conn: sqlite3.Connection, config: Config):
        await callback.answer()
        try:
            _, _, job_id_s, fmt_id = callback.data.split(":", 3)
            job_id = int(job_id_s)
        except (ValueError, AttributeError):
            return
        job = pick_format(conn, job_id, callback.from_user.id, fmt_id)
        if job is None:
            return
        note = source_note(job["source"])
        await callback.message.edit_text(
            render_job(job), reply_markup=job_keyboard(job_id, job["url"]))
        if note:
            await callback.message.answer(note)
        asyncio.create_task(run_job(callback.message, conn, config, job_id))

    @fr.callback_query(F.data.startswith("dl:go:"))
    async def dl_go(callback: types.CallbackQuery, conn: sqlite3.Connection,
                    config: Config):
        try:
            job_id = int(callback.data.split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer()
            return
        job = get_job(conn, job_id)
        if not job or job["user_id"] != callback.from_user.id:
            await callback.answer()
            return
        status = job.get("status")
        if status == "working":
            await callback.answer("در حال پردازش است.")
            return
        if status in ("ready", "ready_cached"):
            await callback.answer(QUEUE_STATUS_LABELS.get(status, ""))
            return
        if status != "queued":
            await callback.answer("برای تلاش دوباره، «تلاش دوباره» را بزنید.")
            return
        await callback.answer()
        asyncio.create_task(run_job(callback.message, conn, config, job_id))

    @fr.callback_query(F.data.startswith("dl:retry:"))
    async def dl_retry(callback: types.CallbackQuery, conn: sqlite3.Connection,
                       config: Config):
        try:
            job_id = int(callback.data.split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer()
            return
        job = get_job(conn, job_id)
        if not job or job["user_id"] != callback.from_user.id:
            await callback.answer()
            return
        if not retry_job(conn, job_id):
            await callback.answer()
            return
        await callback.answer()
        await _refresh_status(callback.message, conn, job_id)
        asyncio.create_task(run_job(callback.message, conn, config, job_id))

    @fr.callback_query(F.data.startswith("dl:del:"))
    async def dl_delete(callback: types.CallbackQuery, conn: sqlite3.Connection):
        try:
            job_id = int(callback.data.split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.answer()
            return
        job = get_job(conn, job_id)
        if not job or job["user_id"] != callback.from_user.id:
            await callback.answer()
            return
        delete_job(conn, job_id)
        await callback.answer()
        try:
            await callback.message.edit_text(DELETED)
        except Exception:
            pass

    @fr.callback_query(F.data == "dl:list")
    async def dl_list(callback: types.CallbackQuery, conn: sqlite3.Connection):
        await callback.answer()
        jobs = list_jobs(conn, callback.from_user.id)
        await callback.message.answer(render_queue(jobs))

    router.include_router(fr)
