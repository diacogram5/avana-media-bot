"""Telegram-native smart downloader (callback prefix ``dl:``).

Honest, queue-based downloading:
- A link is validated with the same rules as :func:`app.media.process_link`.
- Format options are REAL per source: a direct file URL gets a real
  download (HEAD-probed); YouTube/TikTok get their official-player note
  (no fabricated file); Instagram runs the anonymous multi-strategy
  resolver and offers the real file when found, otherwise a
  signal-gated error line (age-gated / private-deleted / empty shell /
  timeout / network); Aparat tries the public page for a real media
  file and otherwise falls back to the official player.
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
from ..db import user_lang_or_default
from ..media import process_link
from ..bot.logic import menu_escape
from ..security import validate_link
from ..services.link_media import extract_media_url, extract_video_qualities

# ---------------------------------------------------------------------------
# Spec-exact strings (port-spec/web-ui-spec.md, sections 4 and 11).
# Single-sourced from app/i18n.py: the module constants below are the fa
# (Persian) values so existing references keep working; every user-facing
# render path uses t("dl_*", lang) with the per-user language instead.
# ---------------------------------------------------------------------------
from ..i18n import t

TITLE = t("dl_title")
EXPLAINER = t("dl_explainer")
PROMPT_SEND_LINK = t("dl_prompt_send_link")
QUEUE_EMPTY = t("dl_queue_empty")
PICK_FORMAT = t("dl_pick_format")
INVALID_LINK = t("dl_not_processable")
UNAVAILABLE = t("dl_unavailable")
# Distinct, signal-gated Instagram failure lines (link→download audit,
# 2026-10-29): each is only shown when its real signal fired, never as
# a guess.
AGE_GATED = t("dl_age_gated")
EMPTY_SHELL = t("dl_empty_shell")
FETCH_TIMEOUT = t("dl_fetch_timeout")
FETCH_NETWORK = t("dl_fetch_network")
ENQUEUE_FAILED = t("dl_queue_failed")
LINK_FAILED = t("dl_process_failed")
TOO_BIG = t("dl_too_large")
RETRY_LATER = t("dl_temp_error_retry")
DELETED = t("dl_removed_from_queue")
EXITED = t("dl_exited")
FILE_LABEL = t("dl_download_file")
YOUTUBE_FORMAT_LABEL = t("dl_play_youtube")
TIKTOK_FORMAT_LABEL = t("dl_play_tiktok")
INSTAGRAM_FORMAT_LABEL = t("dl_play_instagram")
APARAT_FORMAT_LABEL = t("dl_play_aparat")
YOUTUBE_NOTE = t("dl_youtube_no_direct")
TIKTOK_NOTE = t("dl_tiktok_no_direct")

NOT_A_URL = t("dl_not_a_link")
LINK_NOT_VALID = t("dl_link_invalid")
SCHEME_NOT_ALLOWED = t("dl_only_http")
PRIVATE_BLOCKED = t("dl_private_address")
DOMAIN_NOT_ALLOWED = t("dl_domain_unsupported")

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

# (Removed: the old code->fa-text map now lives in download_error_text(),
# which resolves per-language via i18n. The fa constants above stay for
# backwards-compatible references.)


# fa label dicts stay byte-identical (tests pin them); the *_KEY maps give
# the i18n key for each label so render paths can localize per user.
_STAGE_KEY = {
    "queued": "dl_status_queued", "preparing": "dl_status_preparing",
    "fetching": "dl_status_fetching", "saving": "dl_status_saving",
    "done": "dl_status_done", "cached": "dl_status_cached",
    "player": "dl_ready_player", "retrying": "dl_temp_error_retry",
    "failed": "dl_failed",
}
_QSTATUS_KEY = {
    "ready": "dl_ready", "ready_cached": "dl_ready_cached",
    "failed": "dl_failed", "working": "dl_processing", "queued": "dl_queued",
}
_CTYPE_KEY = {
    "video": "dl_type_video", "audio": "dl_type_audio",
    "photo": "dl_type_photo", "post": "dl_type_post",
    "unknown": "dl_type_unknown",
}
_SOURCE_KEY = {
    "direct": "dl_source_direct", "youtube": "plat_youtube",
    "tiktok": "plat_tiktok", "instagram": "plat_instagram",
    "aparat": "plat_aparat", "unknown": "dl_source_unknown",
}


def _lbl(keymap: dict, fa_table: dict, code: str, lang: str) -> str:
    if lang != "fa" and code in keymap:
        return t(keymap[code], lang)
    return fa_table.get(code, code)


def download_error_text(code: str, lang: str = "fa") -> str:
    """User-facing line for a detect_formats error code; never a traceback."""
    key = {"unavailable": "dl_unavailable", "age_gated": "dl_age_gated",
           "empty_shell": "dl_empty_shell", "timeout": "dl_fetch_timeout",
           "network_error": "dl_fetch_network"}.get(code or "")
    return t(key, lang) if key else t("dl_process_failed", lang)


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


def validation_line(reason: str, lang: str = "fa") -> str:
    """Section-4 validation message for a ``validate_link`` reason."""
    if reason in ("empty_or_too_long", "unparsable", "no_host",
                  "userinfo_not_allowed", "dns_failed"):
        return t("dl_not_a_link", lang)
    if reason == "scheme_not_allowed":
        return t("dl_only_http", lang)
    if reason in ("private_host_blocked", "private_ip_blocked",
                  "resolves_to_private_ip"):
        return t("dl_private_address", lang)
    if reason == "domain_not_allowed":
        return t("dl_domain_unsupported", lang)
    return t("dl_link_invalid", lang)


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
    # Separate audio-stream URL (Instagram DASH serves audio apart from
    # video); "" when not applicable. Lets the downloader mux both.
    audio_url: str = ""
    # "" | "unavailable" | "age_gated" | "empty_shell" | "timeout" | "network_error"
    error: str = ""


async def _detect_instagram_formats(url: str) -> FormatResult:
    """Instagram formats via the anonymous multi-strategy resolver.

    The SAME resolver the plain-link message path uses (page -> embed ->
    captioned embed -> crawler DASH): its (audio_url, video_url) output
    becomes this stage's file_url, so the downloader finally offers the
    real file instead of only the official player. When the resolver
    finds nothing, the miss is classified by real signals — oEmbed
    gating markers, oEmbed 404, transport timeout/network — and an
    honest HTTP-200-with-zero-media shell gets its own line.
    """
    from ..services.instagram import check_instagram_gating, resolve_instagram_media
    try:
        audio_url, video_url, _page_text, _strategy = \
            await resolve_instagram_media(url)
    except Exception:
        audio_url = video_url = None
    if video_url:
        return FormatResult(
            formats=[{"id": "file", "label": FILE_LABEL}],
            content_type="video",
            file_url=video_url,
            # Keep the separate DASH audio stream: the video URL alone
            # is a SILENT file; the downloader muxes both (see run_job).
            audio_url=audio_url or "",
        )
    if audio_url:
        return FormatResult(
            formats=[{"id": "file", "label": FILE_LABEL}],
            content_type="audio",
            file_url=audio_url,
        )
    try:
        gating = await check_instagram_gating(url)
    except Exception:
        gating = None
    if gating == "age_gated":
        return FormatResult(formats=[], error="age_gated")
    if gating == "private_or_deleted":
        return FormatResult(formats=[], error="unavailable")
    if gating == "timeout":
        return FormatResult(formats=[], error="timeout")
    if gating == "network_error":
        return FormatResult(formats=[], error="network_error")
    return FormatResult(formats=[], error="empty_shell")


async def detect_formats(url: str, source: str, lang: str = "fa") -> FormatResult:
    """Real, honest format options for a source. No fabricated formats.

    - direct: HEAD probe (15s); audio/video content-type -> file download.
    - youtube/tiktok: official player only (platforms give no public file).
    - instagram: the anonymous multi-strategy resolver (page -> embed ->
      captioned embed -> crawler DASH); a real media file -> file
      download, otherwise a signal-gated error line (age-gated /
      private-deleted / empty shell / timeout / network) — never a file.
    - aparat: fetch the public page; a real advertised media file ->
      file download, otherwise the official player; an unreachable
      page -> "unavailable" (private/deleted), never a file.
    """
    if source == "youtube":
        return FormatResult(
            formats=[{"id": "yt_player",
                      "label": t("dl_play_youtube", lang)}],
            note=t("dl_youtube_no_direct", lang),
            content_type="post",
        )
    if source == "tiktok":
        return FormatResult(
            formats=[{"id": "tt_player",
                      "label": t("dl_play_tiktok", lang)}],
            note=t("dl_tiktok_no_direct", lang),
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
    if source == "instagram":
        return await _detect_instagram_formats(url)
    if source == "aparat":
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
                label = q["hint"] or t("dl_format_quality", lang).format(n=i + 1)
                if size:
                    label += f" · ≈{_human_mb(size, lang)}"
                formats.append({"id": f"q{i}", "label": f"🎬 {label}",
                                "url": q["url"]})
            formats.append({"id": "qbest", "label": t("dl_best_quality", lang),
                            "url": qualities[0]["url"]})
            return FormatResult(formats=formats, content_type="video")
        media_url = extract_media_url(html)
        if media_url:
            return FormatResult(
                formats=[{"id": "file", "label": t("dl_download_file", lang)}],
                content_type="video",
                file_url=media_url,
            )
        label = (t("dl_play_instagram", lang) if source == "instagram"
                 else t("dl_play_aparat", lang))
        return FormatResult(
            formats=[{"id": f"{'ig' if source == 'instagram' else 'ap'}_player",
                      "label": label}],
            content_type="post",
        )
    return FormatResult(formats=[], error="")


def _human_mb(size_bytes: int, lang: str = "fa") -> str:
    mb = size_bytes / (1024 * 1024)
    if mb >= 1:
        return t("dl_human_mb", lang).format(mb=mb)
    return t("dl_human_kb", lang).format(kb=size_bytes // 1024)


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


def render_job(job: dict, lang: str = "fa") -> str:
    """Job message body: title + source/type + ``{stageLabel} · {N}٪``."""
    stage = _lbl(_STAGE_KEY, STAGE_LABELS, job.get("stage", ""), lang)
    if not stage:
        stage = job.get("stage", "")
    source = _lbl(_SOURCE_KEY, SOURCE_LABELS, job.get("source", ""), lang)
    ctype = _lbl(_CTYPE_KEY, CONTENT_TYPE_LABELS,
                 job.get("content_type", ""), lang)
    return t("dl_job_body", lang).format(
        title=t("dl_title", lang), source=source, ctype=ctype,
        stage=stage, progress=int(job.get("progress", 0)))


def render_queue(jobs: list[dict], lang: str = "fa") -> str:
    if not jobs:
        return t("dl_queue_empty", lang)
    lines = [t("dl_queue_title", lang).format(title=t("dl_title", lang))]
    for i, job in enumerate(jobs, 1):
        status = _lbl(_QSTATUS_KEY, QUEUE_STATUS_LABELS,
                      job.get("status", ""), lang)
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


def source_note(source: str, lang: str = "fa") -> str:
    """The official-player note for sources that give no public file."""
    if source == "youtube":
        return t("dl_youtube_no_direct", lang)
    if source == "tiktok":
        return t("dl_tiktok_no_direct", lang)
    return ""


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------


def job_keyboard(job_id: int, url: str, lang: str = "fa") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("dl_download_file", lang),
                              callback_data=f"dl:go:{job_id}")],
        [InlineKeyboardButton(text=t("dl_source_original", lang), url=url)],
        [InlineKeyboardButton(text=t("dl_retry", lang),
                              callback_data=f"dl:retry:{job_id}")],
        [InlineKeyboardButton(text=t("dl_remove_from_queue", lang),
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
    lang = user_lang_or_default(conn, job.get("user_id"))
    text = render_job(job, lang)
    if extra:
        text += f"\n{extra}"
    try:
        await message.edit_text(
            text, reply_markup=job_keyboard(job_id, job["url"], lang))
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


async def _send_file(message: types.Message, job: dict, data: bytes,
                   lang: str = "fa") -> str:
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
        sent = await send_music(message, data, name.rsplit(".", 1)[0],
                                lang=lang)
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

            # Instagram DASH: video and audio are separate streams; mux both
            # so the downloaded file is not silent. Falls back to the plain
            # download when there is no separate audio URL.
            _audio_url = job.get("audio_url") or ""
            if _audio_url and job.get("content_type") == "video":
                from ..services.instagram import fetch_merged_video
                data, _ = await fetch_merged_video(
                    download_url, _audio_url,
                    max_mb=min(config.max_file_mb, 50))
                if not data:
                    raise _TransientError(RETRY_LATER)
            else:
                data = await _download_bytes(download_url, limit, _prog)
            update_job(conn, job_id, stage="saving", progress=90)
            await _refresh_status(message, conn, job_id)
            job = get_job(conn, job_id)
            lang = user_lang_or_default(conn, job.get("user_id"))
            file_id = await _send_file(message, job, data, lang)
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
    lang = user_lang_or_default(conn, job.get("user_id"))
    status_msg = await message.answer(
        render_job(job, lang),
        reply_markup=job_keyboard(job["id"], job["url"], lang))
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
    async def dl_open(callback: types.CallbackQuery, conn: sqlite3.Connection,
                      state: FSMContext):
        await callback.answer()
        await state.set_state(DlStates.await_url)
        lang = user_lang_or_default(conn, callback.from_user.id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t("pro_main_menu", lang), callback_data="menu:main"),
            InlineKeyboardButton(text=t("m_cancel_btn", lang), callback_data="flow:cancel")]])
        await callback.message.answer(
            f"{t('dl_title', lang)}\n\n{t('dl_explainer', lang)}\n\n"
            f"{t('dl_prompt_send_link', lang)}", reply_markup=kb)

    @fr.message(DlStates.await_url, Command("cancel"))
    async def dl_cancel(message: types.Message, conn: sqlite3.Connection,
                        state: FSMContext):
        await state.clear()
        lang = user_lang_or_default(conn, message.from_user.id)
        await message.answer(t("dl_exited", lang))

    @fr.message(DlStates.await_url, F.text)
    @menu_escape
    async def dl_receive_url(message: types.Message, conn: sqlite3.Connection,
                             config: Config, state: FSMContext):
        lang = user_lang_or_default(conn, message.from_user.id)
        text = (message.text or "").strip()
        url = _extract_url(text)
        validation = validate_link(url or text)
        # Reuse the link pipeline's rules; detect_source only adds the
        # spec-mandated TikTok exception on top.
        outcome = process_link(url or text)
        source = detect_source(url or text, validation)
        if not url or (not outcome.ok and source != "tiktok"):
            line = validation_line(validation.reason, lang)
            await message.answer(f"{t('dl_not_processable', lang)}\n{line}")
            return
        norm = normalize_url(url)
        cached = find_cached(conn, norm)
        if cached:
            update_job(conn, cached["id"], status="ready_cached",
                       stage="cached", progress=100)
            await state.clear()
            await message.answer(t("dl_cached_done", lang).format(
                title=t("dl_title", lang),
                cached=t("dl_status_cached", lang)))
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
                await message.answer(t("dl_process_failed", lang))
            return
        result = await detect_formats(url, source, lang)
        if result.error:
            await message.answer(download_error_text(result.error, lang))
            return
        if not result.formats:
            await message.answer(
                f"{t('dl_not_processable', lang)}\n{t('dl_link_invalid', lang)}")
            return
        job_id = create_job(conn, message.from_user.id, norm, source,
                            result.content_type, result.formats)
        if not job_id:
            await message.answer(t("dl_queue_failed", lang))
            return
        if result.file_url:
            update_job(conn, job_id, result_url=result.file_url,
                       audio_url=result.audio_url or "")
        job = get_job(conn, job_id)
        if len(result.formats) > 1:
            await state.clear()
            await message.answer(
                f"{t('dl_title', lang)}\n{t('dl_pick_format', lang)}",
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
    async def dl_receive_non_text(message: types.Message,
                                  conn: sqlite3.Connection):
        lang = user_lang_or_default(conn, message.from_user.id)
        await message.answer(t("dl_prompt_send_link", lang))

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
        lang = user_lang_or_default(conn, callback.from_user.id)
        note = source_note(job["source"], lang)
        await callback.message.edit_text(
            render_job(job, lang),
            reply_markup=job_keyboard(job_id, job["url"], lang))
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
        lang = user_lang_or_default(conn, callback.from_user.id)
        status = job.get("status")
        if status == "working":
            await callback.answer(t("dl_still_processing", lang))
            return
        if status in ("ready", "ready_cached"):
            await callback.answer(_lbl(_QSTATUS_KEY, QUEUE_STATUS_LABELS,
                                       status, lang))
            return
        if status != "queued":
            await callback.answer(t("dl_retry_hint", lang))
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
        lang = user_lang_or_default(conn, callback.from_user.id)
        try:
            await callback.message.edit_text(t("dl_removed_from_queue", lang))
        except Exception:
            pass

    @fr.callback_query(F.data == "dl:list")
    async def dl_list(callback: types.CallbackQuery, conn: sqlite3.Connection):
        await callback.answer()
        lang = user_lang_or_default(conn, callback.from_user.id)
        jobs = list_jobs(conn, callback.from_user.id)
        await callback.message.answer(render_queue(jobs, lang))

    router.include_router(fr)
