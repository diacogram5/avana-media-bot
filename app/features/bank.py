"""Superior-AI bank: two-stage song identification.

The normal search runs first (elsewhere); when it cannot identify a song
with confidence, a ``bank:search:{token}`` button is shown for the same
request. Clicking it searches the *same audio sample* against this
bank's files by real audio content (spectral-landmark fingerprints from
``app.search.landmark``), never by filename or caption.

Bank sources are restricted to exactly two kinds: public Telegram
channels registered by the manager, and registered superior-AI song
sites (only suno.com is technically active). Manually added direct
files were removed and never re-enter the bank.

Audience rules:
- Channel/source names NEVER appear in public messages.
- Every failure sends ONE short Persian line; an extraction/search
  error is never reported as "not found".
- Finished outcomes are stored on the sample row and served from the
  store on repeat clicks; the bank is never searched twice for the
  same sample.

``app.search.landmark`` is imported lazily inside functions so a
missing/broken landmark module degrades to an honest decode error,
never a fabricated fingerprint.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
import sqlite3
import time
import urllib.parse

import httpx
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..bot.audio_send import send_song_audio
from ..bot.logic import menu_escape
from ..config import Config
from ..db import user_lang_or_default
from ..i18n import t

# --------------------------------------------------------------------------
# Constants / strings (exact Persian wording from the web-ui spec)
# --------------------------------------------------------------------------
BANK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
SAMPLE_DATA_CAP = 3 * 1024 * 1024       # user upload bytes fingerprinted directly
DOWNLOAD_CAP = 15 * 1024 * 1024        # any single download
DOWNLOAD_TIMEOUT = 30
INDEX_TIMEOUT = 20
CHANNEL_CAP = 5                        # max registered channels
SOURCE_KIND_CAP = 10                   # max sources per kind (generic add)
CHANNEL_FILES_PER_RUN = 10             # max new files per channel per index run
SEARCHING_STALE_SEC = 3 * 60
MIN_LANDMARKS = 20

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,}$")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$")

# Public bank strings (single-sourced from app/i18n.py; the constants are
# the fa values so existing references keep working).
MSG_FOUND = t("bank_found")
MSG_SEARCHING = t("bank_searching")
MSG_NO_SAMPLE = t("bank_sample_missing")
MSG_IN_PROGRESS = t("bank_search_running")
MSG_EMPTY = t("bank_no_files")
MSG_EXTRACT_ERR = t("bank_extract_failed")
MSG_CLIENT_ERR = t("bank_search_failed")
LABEL_BANK = t("bank_panel_title")
LABEL_SUNO = t("bank_suno_file")
BTN_DOWNLOAD = t("bank_download_song")
BTN_SOURCE_POST = t("bank_post_source")

# Admin strings (exact copies from web-ui-spec.md section 16 / web-logic-spec.md section 5)
# Labels are i18n keys now (usernames never change); fa renders the exact
# spec Persian via t().
SUGGESTED_BANK_CHANNELS = [
    ("AiMusical", "bank_suggest_ai_songs"),
    ("ghadimy_ai", "bank_suggest_remix_ai"),
    ("radiojavan", "bank_suggest_tg_music"),
    ("esssi_ai", "bank_suggest_ai_made"),
    ("PersianPopAi", "bank_suggest_ai_remix"),
    ("icyremix", "bank_suggest_tg_remix"),
    ("djsoroushmiix", "bank_suggest_tg_remix"),
]


def _suggested_label(label_key: str, lang: str) -> str:
    return t(label_key, lang)
STATUS_INDEXABLE = t("bank_status_indexable")
STATUS_BLOCKED = t("bank_status_blocked")
STATUS_UNCHECKED = t("bank_status_unchecked")

MSG_BAD_USERNAME = t("bank_bad_username")
MSG_BAD_DOMAIN = t("bank_bad_domain")
MSG_DUP_CHANNEL = t("bank_dup_channel")
MSG_DUP_SOURCE = t("bank_dup_source")
MSG_DUP_IDENTIFIER = t("bank_dup_identifier")
MSG_CHANNEL_CAP = t("bank_channel_cap")
MSG_KIND_CAP = t("bank_kind_cap")
MSG_CHANNEL_ADDED = t("bank_channel_added")
MSG_SOURCE_ADDED = t("bank_source_added")
MSG_SOURCE_DELETED = t("bank_source_deleted")
MSG_SOURCE_EDITED = t("bank_source_edited")
MSG_CONFIRM_DELETE = t("cmn_confirm_delete")
MSG_NO_CHANNEL_SOURCE = t("bank_no_channel_source")
MSG_INDEX_ADDED = t("bank_index_added_n")
MSG_INDEX_NONE_NEW = t("bank_index_none_new")
MSG_INDEX_NOTHING = t("bank_index_nothing")
MSG_INDEX_BLOCKED = t("bank_index_blocked")
MSG_DL_MISSING = t("bank_dl_missing")

PANEL_TITLE = t("bank_panel_title")
PANEL_DESC = t("bank_panel_desc")
PANEL_EMPTY = t("bank_empty")
PANEL_GUIDE = t("bank_how_to_register")


# --------------------------------------------------------------------------
# Lazy landmark access
# --------------------------------------------------------------------------
def _landmark():
    """Return the landmark module, or None when it cannot be imported."""
    try:
        from ..search import landmark
        return landmark
    except ImportError:
        return None


def _fingerprint(data: bytes) -> dict | None:
    lm = _landmark()
    if lm is None:
        return None
    try:
        return lm.fingerprint_bytes(data or b"")
    except Exception:
        return None


def _pack(landmarks: list[int]) -> str:
    lm = _landmark()
    if lm is None:
        raise RuntimeError("landmark module unavailable")
    return lm.pack_landmarks(landmarks or [])


def _unpack(chroma: str) -> list[int]:
    lm = _landmark()
    if lm is None:
        return []
    try:
        return lm.unpack_landmarks(chroma or "")
    except Exception:
        return []


def _similarity(sample_flat: list[int], bank_chroma: str) -> dict | None:
    lm = _landmark()
    if lm is None:
        return None
    bank_flat = _unpack(bank_chroma)
    if len(bank_flat) < 40:
        return None
    try:
        return lm.landmark_similarity(
            {"landmarks": sample_flat, "landmark_count": len(sample_flat) // 2},
            {"landmarks": bank_flat, "landmark_count": len(bank_flat) // 2},
        )
    except Exception:
        return None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
class _TooLarge(Exception):
    pass


def _now() -> int:
    return int(time.time())


def _download_bytes(url: str, max_bytes: int = DOWNLOAD_CAP,
                    timeout: int = DOWNLOAD_TIMEOUT) -> bytes:
    with httpx.stream("GET", url, headers={"User-Agent": BANK_UA},
                      timeout=timeout, follow_redirects=True) as r:
        r.raise_for_status()
        buf = bytearray()
        for chunk in r.iter_bytes(65536):
            buf += chunk
            if len(buf) > max_bytes:
                raise _TooLarge()
    return bytes(buf)


def _norm_channel(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = re.sub(r"^(www\.)?t\.me/", "", s, flags=re.I)
    s = s.strip().lstrip("@").rstrip("/")
    return s


def _norm_domain(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = s.split("/")[0].strip()
    return s


def _is_owner(uid: int | None, config: Config) -> bool:
    return config.owner_id is not None and uid == config.owner_id


def _fresh_conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _run_in_conn(db_path: str, func, *args):
    """Run a blocking sync bank function with its own DB connection.

    A separate connection is used (never the shared event-loop one),
    so this is safe inside asyncio.to_thread.
    """
    c = _fresh_conn(db_path)
    try:
        return func(*args, c)
    finally:
        c.close()


# --------------------------------------------------------------------------
# DB layer (functions taking conn)
# --------------------------------------------------------------------------
def list_bank_sources(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM bank_sources ORDER BY sort_order, id").fetchall()
    return [dict(r) for r in rows]


def _source_count(conn, kind: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM bank_sources WHERE kind=?", (kind,)).fetchone()
    return int(row[0]) if row else 0


def add_bank_source(conn, kind: str, identifier: str,
                    label: str = "", lang: str = "fa") -> dict:
    """Unified add; returns {"ok": bool, "message": str, "id": int|None}."""
    kind = "site" if kind == "site" else "channel"
    if kind == "channel":
        ident = _norm_channel(identifier)
        if not USERNAME_RE.match(ident):
            return {"ok": False, "message": t("bank_bad_username", lang), "id": None}
        dup_key, cap_key, ok_key = ("bank_dup_channel", "bank_channel_cap",
                                    "bank_channel_added")
    else:
        ident = _norm_domain(identifier)
        if not DOMAIN_RE.match(ident) or ".." in ident:
            return {"ok": False, "message": t("bank_bad_domain", lang), "id": None}
        dup_key, cap_key, ok_key = ("bank_dup_source", None, "bank_source_added")
    exists = conn.execute(
        "SELECT id FROM bank_sources WHERE kind=? AND lower(identifier)=lower(?)",
        (kind, ident)).fetchone()
    if exists:
        return {"ok": False, "message": t(dup_key, lang), "id": None}
    if cap_key and _source_count(conn, "channel") >= CHANNEL_CAP:
        return {"ok": False, "message": t(cap_key, lang), "id": None}
    if _source_count(conn, kind) >= SOURCE_KIND_CAP:
        return {"ok": False, "message": t("bank_kind_cap", lang), "id": None}
    row = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM bank_sources WHERE kind=?",
                       (kind,)).fetchone()
    sort_order = (int(row[0]) if row else -1) + 1
    cur = conn.execute(
        "INSERT INTO bank_sources (kind, identifier, label, enabled, sort_order)"
        " VALUES (?,?,?,?,?)",
        (kind, ident, label or "", 1, sort_order))
    conn.commit()
    return {"ok": True, "message": t(ok_key, lang), "id": cur.lastrowid}


def add_bank_channel(conn, channel: str, lang: str = "fa") -> dict:
    """Legacy-shaped entry point: adds a Telegram channel source."""
    return add_bank_source(conn, "channel", channel, lang=lang)


def update_bank_source(conn, row_id: int, identifier: str,
                       label: str | None = None, lang: str = "fa") -> dict:
    row = conn.execute("SELECT * FROM bank_sources WHERE id=?",
                       (row_id,)).fetchone()
    if not row:
        return {"ok": False, "message": t("bank_search_failed", lang)}
    kind = row["kind"]
    if kind == "channel":
        ident = _norm_channel(identifier)
        if not USERNAME_RE.match(ident):
            return {"ok": False, "message": t("bank_bad_username", lang)}
    else:
        ident = _norm_domain(identifier)
        if not DOMAIN_RE.match(ident) or ".." in ident:
            return {"ok": False, "message": t("bank_bad_domain", lang)}
    dup = conn.execute(
        "SELECT id FROM bank_sources WHERE kind=? AND lower(identifier)=lower(?) AND id!=?",
        (kind, ident, row_id)).fetchone()
    if dup:
        return {"ok": False, "message": t("bank_dup_identifier", lang)}
    if label is None:
        label = row["label"]
    conn.execute("UPDATE bank_sources SET identifier=?, label=? WHERE id=?",
                 (ident, label, row_id))
    conn.commit()
    return {"ok": True, "message": t("bank_source_edited", lang)}


def delete_bank_source(conn, row_id: int, lang: str = "fa") -> dict:
    conn.execute("DELETE FROM bank_sources WHERE id=?", (row_id,))
    conn.commit()
    # Indexed files stay listed (individually deletable) but are no
    # longer searched once their source is gone/disabled.
    return {"ok": True, "message": t("bank_source_deleted", lang)}


def toggle_bank_source(conn, row_id: int) -> int | None:
    row = conn.execute("SELECT enabled FROM bank_sources WHERE id=?",
                       (row_id,)).fetchone()
    if not row:
        return None
    new = 0 if row["enabled"] else 1
    conn.execute("UPDATE bank_sources SET enabled=? WHERE id=?", (new, row_id))
    conn.commit()
    return new


def move_bank_source(conn, row_id: int, direction: str) -> bool:
    """Swap sort_order with the neighbour of the same kind. Returns True on move."""
    row = conn.execute("SELECT * FROM bank_sources WHERE id=?",
                       (row_id,)).fetchone()
    if not row:
        return False
    if direction == "up":
        cmp_op, order = "<", "DESC"
    else:
        cmp_op, order = ">", "ASC"
    other = conn.execute(
        f"SELECT * FROM bank_sources WHERE kind=? AND sort_order {cmp_op} ?"
        f" ORDER BY sort_order {order}, id {order} LIMIT 1",
        (row["kind"], row["sort_order"])).fetchone()
    if not other:
        # Fall back to neighbour by id when sort_orders collide.
        other = conn.execute(
            f"SELECT * FROM bank_sources WHERE kind=? AND id {cmp_op} ?"
            f" ORDER BY id {order} LIMIT 1",
            (row["kind"], row["id"])).fetchone()
        if not other:
            return False
    conn.execute("UPDATE bank_sources SET sort_order=? WHERE id=?",
                 (other["sort_order"], row["id"]))
    conn.execute("UPDATE bank_sources SET sort_order=? WHERE id=?",
                 (row["sort_order"], other["id"]))
    conn.commit()
    return True


def list_bank_items(conn, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM audio_bank ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def delete_bank_item(conn, item_id: int) -> bool:
    cur = conn.execute("DELETE FROM audio_bank WHERE id=?", (item_id,))
    conn.commit()
    return cur.rowcount > 0


def bank_item_count(conn) -> int:
    row = conn.execute("SELECT COUNT(*) FROM audio_bank").fetchone()
    return int(row[0]) if row else 0


def get_bank_sample(conn, token: str) -> dict | None:
    row = conn.execute("SELECT * FROM bank_samples WHERE token=?",
                       (token,)).fetchone()
    return dict(row) if row else None


def set_sample_state(conn, token: str, state: str,
                     outcome: dict | None = None) -> None:
    conn.execute(
        "UPDATE bank_samples SET state=?, result_json=?, updated_at=? WHERE token=?",
        (state, json.dumps(outcome or {}, ensure_ascii=False), _now(), token))
    conn.commit()


def get_channel_status(conn, channel: str) -> dict | None:
    row = conn.execute("SELECT * FROM bank_channel_status WHERE channel=?",
                       (channel,)).fetchone()
    return dict(row) if row else None


def set_channel_status(conn, channel: str, status: str,
                       seen_posts: int = 0, audio_files: int = 0,
                       indexed_count: int = 0,
                       has_fingerprint: int = 0) -> None:
    conn.execute(
        """INSERT INTO bank_channel_status
           (channel, status, seen_posts, audio_files, indexed_count, has_fingerprint)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(channel) DO UPDATE SET
             status=excluded.status, seen_posts=excluded.seen_posts,
             audio_files=excluded.audio_files,
             indexed_count=excluded.indexed_count,
             has_fingerprint=excluded.has_fingerprint""",
        (channel, status, seen_posts, audio_files, indexed_count, has_fingerprint))
    conn.commit()


# --------------------------------------------------------------------------
# Sample arming (called by the search flows when normal search missed)
# --------------------------------------------------------------------------
def arm_sample(kind: str, seed: str, data: bytes | None, url: str,
               clue: str, conn) -> str | None:
    """Fingerprint an audio sample and store/re-arm its bank_samples row.

    Returns the deterministic token (sha256(seed)[:40]) on success, or
    None when no usable fingerprint exists — the caller then shows the
    honest extraction-error line and never a fake bank button.
    """
    payload: bytes | None = None
    if data and len(data) <= SAMPLE_DATA_CAP:
        payload = data
    elif url:
        try:
            payload = _download_bytes(url)
        except Exception:
            return None
    if not payload:
        return None
    fp = _fingerprint(payload)
    if (fp is None or fp.get("status") != "ok"
            or (fp.get("landmark_count") or 0) < MIN_LANDMARKS):
        return None
    try:
        chroma = _pack(fp.get("landmarks") or [])
    except Exception:
        return None
    token = hashlib.sha256((seed or "").encode("utf-8")).hexdigest()[:40]
    now = _now()
    conn.execute(
        """INSERT INTO bank_samples
           (token, kind, chroma, clue, state, result_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(token) DO UPDATE SET
             kind=excluded.kind, chroma=excluded.chroma, clue=excluded.clue,
             state='ready', result_json='', updated_at=excluded.updated_at""",
        (token, kind or "", chroma, (clue or "")[:300], "ready", "",
         now, now))
    conn.commit()
    return token


# --------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------
_POST_SPLIT_RE = re.compile(r'data-post="([^"]+)"')
_AUDIO_URL_RE = re.compile(
    r'href="(https?://[^"\s]+\.(?:mp3|m4a|ogg|wav|flac)[^"\s]*)"', re.I)
_TEXT_RE = re.compile(r'tgme_widget_message_text[^>]*>(.*?)</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _iter_post_bodies(page_html: str):
    """Yield (post_id, body) pairs, reusing channel.py's data-post split pattern."""
    blocks = _POST_SPLIT_RE.split(page_html or "")
    for i in range(1, len(blocks) - 1, 2):
        yield blocks[i], blocks[i + 1]


def _post_audio_urls(body: str) -> list[str]:
    urls = []
    for u in _AUDIO_URL_RE.findall(body or ""):
        u = u.replace("&amp;", "&")
        if u not in urls:
            urls.append(u)
    return urls


def _post_text(body: str) -> str:
    m = _TEXT_RE.search(body or "")
    raw = m.group(1) if m else ""
    text = _TAG_RE.sub(" ", raw)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_preview(channel: str, timeout: int = INDEX_TIMEOUT) -> str:
    resp = httpx.get(f"https://t.me/s/{channel}",
                     headers={"User-Agent": BANK_UA},
                     timeout=timeout, follow_redirects=True)
    if resp.status_code != 200:
        return ""
    return resp.text or ""


def _channel_indexed_count(conn, channel: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM audio_bank WHERE source='channel' AND lower(channel)=lower(?)",
        (channel,)).fetchone()
    return int(row[0]) if row else 0


def index_channels(conn) -> dict:
    """Grow the bank from enabled channel sources.

    Returns {"added": int, "total": int, "blocked": [channels]}.
    Blocked channels are recorded in bank_channel_status for the admin
    panel only — never leaked into public messages.
    """
    sources = [s for s in list_bank_sources(conn)
               if s["kind"] == "channel" and s["enabled"]]
    added = 0
    blocked: list[str] = []
    for src in sources:
        channel = src["identifier"]
        try:
            page = _fetch_preview(channel)
        except Exception:
            page = ""
        if not page or 'data-post="' not in page:
            blocked.append(channel)
            set_channel_status(conn, channel, "blocked")
            continue
        seen_posts = 0
        audio_files = 0
        new_this_channel = 0
        for post_id, body in _iter_post_bodies(page):
            seen_posts += 1
            post_url = f"https://t.me/{channel}/{post_id}"
            exists = conn.execute(
                "SELECT id FROM audio_bank WHERE post_url=?",
                (post_url,)).fetchone()
            if exists:
                continue
            urls = _post_audio_urls(body)
            audio_files += len(urls)
            if not urls or new_this_channel >= CHANNEL_FILES_PER_RUN:
                continue
            audio_url = urls[0]
            try:
                data = _download_bytes(audio_url)
            except Exception:
                continue
            fp = _fingerprint(data)
            if (fp is None or fp.get("status") != "ok"
                    or (fp.get("landmark_count") or 0) < MIN_LANDMARKS):
                continue
            try:
                chroma = _pack(fp.get("landmarks") or [])
            except Exception:
                continue
            title = _post_text(body).split("\n")[0][:120]
            conn.execute(
                """INSERT INTO audio_bank
                   (source, channel, title, artist, chroma, file_url, post_url, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ("channel", channel, title, "", chroma, audio_url,
                 post_url, _now()))
            conn.commit()
            added += 1
            new_this_channel += 1
        indexed = _channel_indexed_count(conn, channel)
        set_channel_status(conn, channel, "indexable", seen_posts,
                           audio_files, indexed, 1 if indexed else 0)
    total = bank_item_count(conn)
    return {"added": added, "total": total, "blocked": blocked}


def index_suno(conn, clue: str) -> dict:
    """Best-effort Suno indexing for this sample's clue. Silent skips."""
    row = conn.execute(
        "SELECT * FROM bank_sources WHERE kind='site' AND lower(identifier)='suno.com'"
        " AND enabled=1").fetchone()
    if not row:
        return {"added": 0}
    clue = (clue or "").strip()
    if len(clue) < 2:
        return {"added": 0}
    added = 0
    try:
        q = urllib.parse.quote(f"site:suno.com/song {clue}")
        resp = httpx.get(f"https://html.duckduckgo.com/html/?q={q}",
                         headers={"User-Agent": BANK_UA},
                         timeout=INDEX_TIMEOUT, follow_redirects=True)
        uuids = []
        for u in re.findall(r"suno\.com/song/([0-9a-fA-F-]{36})",
                            resp.text or ""):
            if u not in uuids:
                uuids.append(u)
        for uuid in uuids[:5]:
            try:
                clip = httpx.get(
                    f"https://studio-api.prod.suno.com/api/clip/{uuid}",
                    headers={"User-Agent": BANK_UA},
                    timeout=INDEX_TIMEOUT, follow_redirects=True).json()
                audio_url = (clip or {}).get("audio_url") or ""
                title = str((clip or {}).get("title") or "")[:160]
                if not audio_url:
                    continue
                exists = conn.execute(
                    "SELECT id FROM audio_bank WHERE file_url=? OR post_url=?",
                    (audio_url,
                     f"https://suno.com/song/{uuid}")).fetchone()
                if exists:
                    continue
                data = _download_bytes(audio_url)
                fp = _fingerprint(data)
                if (fp is None or fp.get("status") != "ok"
                        or (fp.get("landmark_count") or 0) < MIN_LANDMARKS):
                    continue
                chroma = _pack(fp.get("landmarks") or [])
                conn.execute(
                    """INSERT INTO audio_bank
                       (source, channel, title, artist, chroma, file_url, post_url, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    ("suno", "", title, "", chroma, audio_url,
                     f"https://suno.com/song/{uuid}", _now()))
                conn.commit()
                added += 1
            except Exception:
                continue
    except Exception:
        pass
    return {"added": added}


# --------------------------------------------------------------------------
# Bank search
# --------------------------------------------------------------------------
def _usable_rows(conn) -> list[dict]:
    sources = list_bank_sources(conn)
    enabled_channels = {s["identifier"].lower() for s in sources
                        if s["kind"] == "channel" and s["enabled"]}
    suno_on = any(s["kind"] == "site" and s["identifier"].lower() == "suno.com"
                  and s["enabled"] for s in sources)
    rows = conn.execute("SELECT * FROM audio_bank").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("source") == "channel" and (d.get("channel") or "").lower() in enabled_channels:
            out.append(d)
        elif d.get("source") == "suno" and suno_on:
            out.append(d)
        # Legacy/other sources are listed nowhere and never searched.
    return out


def _outcome(status: str, key: str, lang: str, hits=None,
             bank_count: int = 0, **params) -> dict:
    """A bank-search outcome: fa text for storage/back-compat plus the
    message key + params so any UI language can re-render it later.
    Stored rows (result_json) keep working: missing keys fall back to the
    stored fa text."""
    return {"status": status, "message": t(key, "fa").format(**params),
            "message_key": key, "message_params": params,
            "hits": hits or [], "bank_count": bank_count}


def _localize_outcome(outcome: dict, lang: str) -> dict:
    """Re-render a stored/fresh outcome's message in the user's language.
    fa (or a stored row without a message key) is returned unchanged."""
    if lang == "fa" or not isinstance(outcome, dict):
        return outcome
    key = outcome.get("message_key")
    if not key:
        return outcome
    out = dict(outcome)
    try:
        out["message"] = t(key, lang).format(
            **(outcome.get("message_params") or {}))
    except Exception:
        pass
    return out


def _sanitize_stored(outcome: dict, lang: str = "fa") -> dict:
    """Any stored error mentioning a channel is replaced by the generic line."""
    if (outcome.get("status") == "error"
            and ("@" in str(outcome.get("message", ""))
                 or "قابل مشاهده نیست" in str(outcome.get("message", "")))):
        outcome = _outcome("error", "bank_no_files", lang,
                           bank_count=outcome.get("bank_count", 0))
    return outcome


def search_bank(token: str, conn, lang: str = "fa") -> dict:
    """Run (or serve) the second-stage bank search.

    Returns {"status": "found"|"not_found"|"error", "message": str,
             "hits": [...], "bank_count": int}.
    Public messages never carry channel names. ``lang`` only selects the
    UI-chrome wording; the search itself never branches on it.
    """
    token = (token or "")[:64]
    sample = get_bank_sample(conn, token)
    if not sample:
        return _localize_outcome(
            _outcome("error", "bank_sample_missing", lang), lang)
    state = sample.get("state") or ""
    if state in ("found", "not_found", "error") and sample.get("result_json"):
        try:
            return _localize_outcome(
                _sanitize_stored(json.loads(sample["result_json"]), lang), lang)
        except (ValueError, TypeError):
            pass  # corrupted store -> run fresh below
    if state == "searching":
        updated = sample.get("updated_at") or 0
        if _now() - updated < SEARCHING_STALE_SEC:
            return _localize_outcome(
                _outcome("error", "bank_search_running", lang), lang)
        # stale -> run fresh
    sample_flat = _unpack(sample.get("chroma") or "")
    if len(sample_flat) < 40:
        outcome = _outcome("error", "bank_extract_failed", lang)
        set_sample_state(conn, token, "error", outcome)
        return _localize_outcome(outcome, lang)
    try:
        set_sample_state(conn, token, "searching")
        # Grow the index first, from the ONLY two allowed sources.
        try:
            index_channels(conn)
        except Exception:
            pass
        try:
            index_suno(conn, sample.get("clue") or "")
        except Exception:
            pass
        usable = _usable_rows(conn)
        bank_count = len(usable)
        if bank_count == 0:
            outcome = _outcome("error", "bank_no_files", lang,
                               bank_count=bank_count)
            set_sample_state(conn, token, "error", outcome)
            return _localize_outcome(outcome, lang)
        best = None
        best_votes = -1
        for r in usable:
            sim = _similarity(sample_flat, r.get("chroma") or "")
            if not sim:
                continue
            if sim.get("votes", 0) > best_votes:
                best_votes = sim["votes"]
                best = (r, sim)
        if best is not None and best[1].get("match"):
            r, sim = best
            score = float(sim.get("ratio", 0.0))
            pct = int(round(score * 100))
            hit = {
                "id": r["id"],
                "title": r.get("title") or "",
                "artist": r.get("artist") or "",
                "source": r.get("source") or "",
                "channel": "",
                "post_url": r.get("post_url") or "",
                "file_url": r.get("file_url") or "",
                "score": min(1.0, max(0.0, score)),
                "pct": pct,
            }
            outcome = _outcome("found", "bank_found", lang, hits=[hit],
                               bank_count=bank_count)
            set_sample_state(conn, token, "found", outcome)
            return _localize_outcome(outcome, lang)
        outcome = _outcome("not_found", "bank_search_not_found", lang,
                           bank_count=bank_count, count=bank_count)
        set_sample_state(conn, token, "not_found", outcome)
        return _localize_outcome(outcome, lang)
    except Exception:
        outcome = _outcome("error", "bank_search_failed", lang)
        try:
            set_sample_state(conn, token, "error", outcome)
        except Exception:
            pass
        return _localize_outcome(outcome, lang)


def bank_search_button(token: str, lang: str = "fa") -> dict:
    """Data the main handlers need to render the second-search button."""
    return {"text": t("m_bank_search_btn", lang),
            "callback_data": f"bank:search:{token}"}


# --------------------------------------------------------------------------
# Admin panel rendering
# --------------------------------------------------------------------------
def _source_label(s: dict, lang: str = "fa") -> str:
    kind = (t("bank_source_channel", lang) if s["kind"] == "channel"
            else t("bank_source_site", lang))
    label = f'{kind} {s["identifier"]}'
    if s.get("label"):
        label += f' ({s["label"]})'
    return label


def _source_indexed(conn, s: dict) -> int:
    if s["kind"] == "channel":
        return _channel_indexed_count(conn, s["identifier"])
    if s["identifier"].lower() == "suno.com":
        row = conn.execute(
            "SELECT COUNT(*) FROM audio_bank WHERE source='suno'").fetchone()
        return int(row[0]) if row else 0
    return 0


def render_bank_panel(conn, suggest_status: dict | None = None,
                      lang: str = "fa") -> tuple[str, InlineKeyboardMarkup]:
    """Admin bank panel: (text, keyboard). Channel names appear here only."""
    sources = list_bank_sources(conn)
    channels = [s for s in sources if s["kind"] == "channel"]
    sites = [s for s in sources if s["kind"] == "site"]
    total = bank_item_count(conn)

    lines = [f"🏛 {t('bank_panel_title', lang)}", "", t("bank_panel_desc", lang), ""]
    if not sources:
        lines += [t("bank_empty", lang), "", t("bank_how_to_register", lang)]

    kb: list[list[InlineKeyboardButton]] = []

    def _toggle_row(s):
        return [
            InlineKeyboardButton(
                text=t("adm_disabled", lang) if s["enabled"] else t("adm_active", lang),
                callback_data=f"bank:toggle:{s['id']}"),
            InlineKeyboardButton(text=t("adm_up", lang), callback_data=f"bank:up:{s['id']}"),
            InlineKeyboardButton(text=t("adm_down", lang), callback_data=f"bank:down:{s['id']}"),
            InlineKeyboardButton(text=t("adm_edit", lang), callback_data=f"bank:edit:{s['id']}"),
            InlineKeyboardButton(text=t("adm_delete", lang), callback_data=f"bank:del:{s['id']}"),
        ]

    if channels:
        lines += ["", t("bank_panel_channels", lang)]
        for s in channels:
            n = _source_indexed(conn, s)
            st = get_channel_status(conn, s["identifier"]) or {}
            status_txt = {"indexable": t("bank_status_indexable", lang),
                          "blocked": t("bank_status_blocked", lang)}.get(
                st.get("status") or "", t("bank_status_unchecked", lang))
            en = (t("bank_active_in_search", lang) if s["enabled"]
                  else t("bank_inactive_in_search", lang))
            lines.append(t("bank_source_row", lang).format(
                label=_source_label(s, lang), status=status_txt, n=n, en=en))
            if st:
                fp = (t("bank_fp_recorded", lang) if st.get("has_fingerprint")
                      else t("bank_fp_missing", lang))
                lines.append(t("bank_source_stats", lang).format(
                    seen=st.get("seen_posts", 0),
                    audio=st.get("audio_files", 0),
                    indexed=st.get("indexed_count", 0), fp=fp))
            kb.append(_toggle_row(s))

    if sites:
        lines += ["", t("bank_panel_sites", lang)]
        for s in sites:
            n = _source_indexed(conn, s)
            supported = (t("bank_supported", lang)
                         if s["identifier"].lower() == "suno.com"
                         else t("bank_unsupported", lang))
            en = (t("bank_active_in_search", lang) if s["enabled"]
                  else t("bank_inactive_in_search", lang))
            lines.append(t("bank_source_row", lang).format(
                label=_source_label(s, lang), status=supported, n=n, en=en))
            kb.append(_toggle_row(s))

    kb.append([InlineKeyboardButton(text=t("bank_add_source", lang),
                                   callback_data="bank:add")])
    kb.append([InlineKeyboardButton(text=t("bank_index_channels", lang),
                                   callback_data="bank:index")])

    # Suggested channels (admin only)
    suggest_status = suggest_status or {}
    lines += ["", t("bank_suggest_channels", lang)]
    registered = {s["identifier"].lower() for s in channels}
    for ch, label_key in SUGGESTED_BANK_CHANNELS:
        label = _suggested_label(label_key, lang)
        if ch.lower() in registered:
            lines.append(t("bank_suggest_row", lang).format(label=label, ch=ch))
            continue
        st = suggest_status.get(ch)
        if st in (STATUS_INDEXABLE, t("bank_status_indexable", lang)):
            st_txt = t("bank_status_indexable", lang)
        elif st in (STATUS_BLOCKED, t("bank_status_blocked", lang)):
            st_txt = t("bank_status_blocked", lang)
        else:
            st_txt = t("bank_status_unchecked", lang)
        lines.append(t("bank_suggest_row_status", lang).format(
            label=label, ch=ch, st=st_txt))
        kb.append([InlineKeyboardButton(
            text=t("bank_add_suggest_btn", lang).format(ch=ch),
            callback_data=f"bank:sugadd:{ch}")])

    # Indexed files
    lines += ["", t("bank_indexed_files", lang).format(total=total)]
    for item in list_bank_items(conn, 20):
        if item["source"] == "suno":
            src = t("bank_suno_label", lang)
        else:
            src = (t("bank_file_channel", lang).format(channel=item["channel"])
                   if item.get("channel") else t("bank_panel_title", lang))
        title = (item.get("title") or t("bank_no_title", lang))[:60]
        lines.append(f'• {title} — {src}')
        kb.append([InlineKeyboardButton(
            text=t("bank_del_file_btn", lang).format(title=title[:30]),
            callback_data=f"bank:delitem:{item['id']}")])

    kb.append([InlineKeyboardButton(text=t("bank_close", lang),
                                   callback_data="bank:close")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


def probe_suggested() -> dict:
    """Live t.me/s probe for the 7 suggested channels (admin panel only)."""
    out = {}
    for ch, _label in SUGGESTED_BANK_CHANNELS:
        try:
            page = _fetch_preview(ch, timeout=8)
            out[ch] = STATUS_INDEXABLE if (page and 'data-post="' in page) else STATUS_BLOCKED
        except Exception:
            out[ch] = STATUS_BLOCKED
    return out


def admin_index_message(result: dict, lang: str = "fa") -> str:
    """Admin-worded index report; blocked channel names appear here only."""
    if not result.get("has_channels"):
        return t("bank_no_channel_source", lang)
    blocked = result.get("blocked") or []
    total = result.get("total", 0)
    added = result.get("added", 0)
    if blocked and total == 0:
        chs = "، ".join("@" + c for c in blocked)
        return t("bank_index_blocked", lang).format(chs=chs)
    parts = []
    if added > 0:
        parts.append(t("bank_index_added_n", lang).format(n=added))
    elif total > 0:
        parts.append(t("bank_index_none_new", lang))
    else:
        parts.append(t("bank_index_nothing", lang))
    if blocked:
        parts.append(t("bank_index_invisible", lang).format(
            chs="، ".join("@" + c for c in blocked)))
    return "\n".join(parts)


# --------------------------------------------------------------------------
# FSM
# --------------------------------------------------------------------------
class BankStates(StatesGroup):
    add_identifier = State(state="bank:add_identifier")
    edit_identifier = State(state="bank:edit_identifier")


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
def _found_card_text(outcome: dict, lang: str = "fa") -> str:
    hit = outcome["hits"][0]
    # Song title/artist are USER CONTENT: never translated, only the chrome.
    title = (hit.get("title") or t("bank_no_title", lang)).strip()
    artist = (hit.get("artist") or "").strip()
    line1 = f"{title} – {artist}" if artist else title
    src_label = (t("bank_suno_label", lang) if hit.get("source") == "suno"
                 else t("bank_panel_title", lang))
    return t("bank_found_card", lang).format(
        found=t("bank_found", lang), line1=line1, src_label=src_label,
        pct=hit.get("pct", 0))


def _found_card_kb(outcome: dict, lang: str = "fa") -> InlineKeyboardMarkup:
    hit = outcome["hits"][0]
    post_url = hit.get("post_url") or hit.get("file_url") or ""
    row = [InlineKeyboardButton(text=t("bank_download_song", lang),
                               callback_data=f"bank:dl:{hit['id']}")]
    if post_url:
        row.append(InlineKeyboardButton(text=t("bank_post_source", lang),
                                       url=post_url))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def register(router: Router) -> None:
    fr = Router()

    # -- public: second search -------------------------------------------
    @fr.callback_query(F.data.startswith("bank:search:"))
    async def _bank_search(callback: CallbackQuery, conn: sqlite3.Connection,
                           config: Config):
        token = callback.data.split(":", 2)[2]
        lang = user_lang_or_default(conn, callback.from_user.id)
        await callback.answer(t("bank_searching", lang))
        try:
            outcome = await asyncio.to_thread(
                _run_in_conn, config.db_path,
                functools.partial(search_bank, lang=lang), token)
        except Exception:
            await callback.message.answer(t("bank_search_failed", lang))
            return
        status = outcome.get("status")
        if status == "found":
            await callback.message.answer(_found_card_text(outcome, lang),
                                          reply_markup=_found_card_kb(outcome, lang))
        else:
            await callback.message.answer(
                outcome.get("message") or t("bank_search_failed", lang))

    @fr.callback_query(F.data.startswith("bank:dl:"))
    async def _bank_dl(callback: CallbackQuery, conn: sqlite3.Connection,
                       config: Config):
        await callback.answer()
        lang = user_lang_or_default(conn, callback.from_user.id)
        try:
            item_id = int(callback.data.split(":", 2)[2])
        except (ValueError, IndexError):
            await callback.message.answer(t("bank_search_failed", lang))
            return
        row = conn.execute("SELECT * FROM audio_bank WHERE id=?",
                           (item_id,)).fetchone()
        if not row or not row["file_url"]:
            await callback.message.answer(t("bank_dl_missing", lang))
            return
        d = dict(row)
        caption = (d.get("title") or "").strip()
        if d.get("artist"):
            caption = f"{caption} – {d['artist']}".strip(" –")
        try:
            sent = await send_song_audio(
                callback.message, d["file_url"], d.get("title") or "",
                d.get("artist") or "", caption=caption or None)
            if sent is None:
                raise RuntimeError("send failed")
        except Exception:
            await callback.message.answer(t("bank_search_failed", lang))

    # -- admin panel ------------------------------------------------------
    async def _send_panel(message: Message, config: Config, user_id: int,
                          edit: bool = False):
        _lc = _fresh_conn(config.db_path)
        try:
            lang = user_lang_or_default(_lc, user_id)
        finally:
            _lc.close()
        statuses = await asyncio.to_thread(probe_suggested)

        def _build(db_path):
            c = _fresh_conn(db_path)
            try:
                return render_bank_panel(c, statuses, lang)
            finally:
                c.close()

        text, kb = await asyncio.to_thread(_build, config.db_path)
        if edit:
            try:
                await message.edit_text(text, reply_markup=kb)
            except Exception:
                await message.answer(text, reply_markup=kb)
        else:
            await message.answer(text, reply_markup=kb)

    async def _owner_only(callback: CallbackQuery, config: Config) -> bool:
        if not _is_owner(getattr(callback.from_user, "id", None), config):
            await callback.answer()
            return False
        return True

    @fr.callback_query(F.data == "bank:panel")
    async def _bank_panel(callback: CallbackQuery, conn: sqlite3.Connection,
                          config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        await _send_panel(callback.message, config, callback.from_user.id, edit=True)

    @fr.callback_query(F.data == "bank:cancel")
    async def _bank_cancel(callback: CallbackQuery, conn: sqlite3.Connection,
                           config: Config, state: FSMContext):
        """Cancel a bank add/edit prompt: clear the stale state and land
        back on the bank panel. Both awaiting prompts offer this."""
        if not await _owner_only(callback, config):
            return
        await state.clear()
        await callback.answer()
        await _send_panel(callback.message, config, callback.from_user.id, edit=True)

    @fr.callback_query(F.data == "bank:close")
    async def _bank_close(callback: CallbackQuery, conn: sqlite3.Connection,
                          config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass

    @fr.callback_query(F.data == "bank:index")
    async def _bank_index(callback: CallbackQuery, conn: sqlite3.Connection,
                          config: Config):
        if not await _owner_only(callback, config):
            return
        lang = user_lang_or_default(conn, callback.from_user.id)
        await callback.answer(t("bank_indexing", lang))

        def _run(db_path):
            c = _fresh_conn(db_path)
            try:
                sources = [s for s in list_bank_sources(c)
                           if s["kind"] == "channel" and s["enabled"]]
                result = index_channels(c)
                result["has_channels"] = bool(sources)
                return result
            finally:
                c.close()

        try:
            result = await asyncio.to_thread(_run, config.db_path)
            msg = admin_index_message(result, lang)
        except Exception:
            msg = t("bank_search_failed", lang)
        try:
            await callback.message.answer(msg)
        except Exception:
            pass

    @fr.callback_query(F.data == "bank:add")
    async def _bank_add(callback: CallbackQuery, conn: sqlite3.Connection,
                        config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        lang = user_lang_or_default(conn, callback.from_user.id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t("bank_kind_channel", lang),
                                 callback_data="bank:addkind:channel"),
            InlineKeyboardButton(text=t("bank_kind_site", lang),
                                 callback_data="bank:addkind:site"),
        ]])
        await callback.message.answer(t("bank_add_choose_kind", lang), reply_markup=kb)

    @fr.callback_query(F.data.startswith("bank:addkind:"))
    async def _bank_addkind(callback: CallbackQuery, conn: sqlite3.Connection,
                            config: Config, state: FSMContext):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        kind = callback.data.split(":", 2)[2]
        if kind not in ("channel", "site"):
            return
        await state.set_state(BankStates.add_identifier)
        await state.update_data(kind=kind)
        # In-bot cancel: without it the owner waits in add_identifier with
        # no visible way back (and a menu-button tap would be eaten as the
        # identifier — see @menu_escape below).
        lang = user_lang_or_default(conn, callback.from_user.id)
        _cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t("adm_cancel", lang), callback_data="bank:cancel")]])
        if kind == "channel":
            await callback.message.answer(
                t("bank_add_channel_prompt", lang),
                reply_markup=_cancel_kb)
        else:
            await callback.message.answer(
                t("bank_add_site_prompt", lang),
                reply_markup=_cancel_kb)

    @fr.message(BankStates.add_identifier, F.text)
    @menu_escape
    async def _bank_add_identifier(message: Message, conn: sqlite3.Connection,
                                   config: Config, state: FSMContext):
        if not _is_owner(getattr(message.from_user, "id", None), config):
            return
        data = await state.get_data()
        await state.clear()
        lang = user_lang_or_default(conn, message.from_user.id)
        res = add_bank_source(conn, data.get("kind") or "channel",
                              message.text or "", lang=lang)
        await message.answer(res["message"])

    @fr.callback_query(F.data.startswith("bank:sugadd:"))
    async def _bank_sugadd(callback: CallbackQuery, conn: sqlite3.Connection,
                           config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        channel = callback.data.split(":", 2)[2]
        lang = user_lang_or_default(conn, callback.from_user.id)
        res = add_bank_channel(conn, channel, lang=lang)
        await callback.message.answer(res["message"])

    @fr.callback_query(F.data.startswith("bank:toggle:"))
    async def _bank_toggle(callback: CallbackQuery, conn: sqlite3.Connection,
                           config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            toggle_bank_source(conn, int(callback.data.split(":", 2)[2]))
        except (ValueError, IndexError):
            pass
        await _send_panel(callback.message, config, callback.from_user.id, edit=True)

    @fr.callback_query(F.data.startswith("bank:up:"))
    async def _bank_up(callback: CallbackQuery, conn: sqlite3.Connection,
                       config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            move_bank_source(conn, int(callback.data.split(":", 2)[2]), "up")
        except (ValueError, IndexError):
            pass
        await _send_panel(callback.message, config, callback.from_user.id, edit=True)

    @fr.callback_query(F.data.startswith("bank:down:"))
    async def _bank_down(callback: CallbackQuery, conn: sqlite3.Connection,
                         config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            move_bank_source(conn, int(callback.data.split(":", 2)[2]), "down")
        except (ValueError, IndexError):
            pass
        await _send_panel(callback.message, config, callback.from_user.id, edit=True)

    @fr.callback_query(F.data.startswith("bank:del:"))
    async def _bank_del(callback: CallbackQuery, conn: sqlite3.Connection,
                        config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            row_id = int(callback.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return
        lang = user_lang_or_default(conn, callback.from_user.id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t("cmn_confirm_delete", lang),
                                 callback_data=f"bank:delconfirm:{row_id}"),
            InlineKeyboardButton(text=t("adm_cancel", lang), callback_data="bank:panel"),
        ]])
        await callback.message.answer(t("bank_del_confirm_q", lang), reply_markup=kb)

    @fr.callback_query(F.data.startswith("bank:delconfirm:"))
    async def _bank_delconfirm(callback: CallbackQuery, conn: sqlite3.Connection,
                               config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        lang = user_lang_or_default(conn, callback.from_user.id)
        try:
            res = delete_bank_source(conn, int(callback.data.split(":", 2)[2]), lang=lang)
            await callback.message.answer(res["message"])
        except (ValueError, IndexError):
            pass
        await _send_panel(callback.message, config, callback.from_user.id, edit=True)

    @fr.callback_query(F.data.startswith("bank:edit:"))
    async def _bank_edit(callback: CallbackQuery, conn: sqlite3.Connection,
                         config: Config, state: FSMContext):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            row_id = int(callback.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return
        row = conn.execute("SELECT * FROM bank_sources WHERE id=?",
                           (row_id,)).fetchone()
        if not row:
            return
        await state.set_state(BankStates.edit_identifier)
        await state.update_data(row_id=row_id)
        lang = user_lang_or_default(conn, callback.from_user.id)
        kind = (t("bank_edit_username", lang) if row["kind"] == "channel"
                else t("bank_edit_domain", lang))
        await callback.message.answer(
            t("bank_edit_new_kind", lang).format(kind=kind),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=t("adm_cancel", lang), callback_data="bank:cancel")]]))

    @fr.message(BankStates.edit_identifier, F.text)
    @menu_escape
    async def _bank_edit_identifier(message: Message, conn: sqlite3.Connection,
                                    config: Config, state: FSMContext):
        if not _is_owner(getattr(message.from_user, "id", None), config):
            return
        data = await state.get_data()
        await state.clear()
        lang = user_lang_or_default(conn, message.from_user.id)
        res = update_bank_source(conn, int(data.get("row_id") or 0),
                                 message.text or "", lang=lang)
        await message.answer(res["message"])

    @fr.callback_query(F.data.startswith("bank:delitem:"))
    async def _bank_delitem(callback: CallbackQuery, conn: sqlite3.Connection,
                            config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        try:
            item_id = int(callback.data.split(":", 2)[2])
        except (ValueError, IndexError):
            return
        lang = user_lang_or_default(conn, callback.from_user.id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t("cmn_confirm_delete", lang),
                                 callback_data=f"bank:delitemconfirm:{item_id}"),
            InlineKeyboardButton(text=t("adm_cancel", lang), callback_data="bank:panel"),
        ]])
        await callback.message.answer(t("bank_del_file_confirm_q", lang), reply_markup=kb)

    @fr.callback_query(F.data.startswith("bank:delitemconfirm:"))
    async def _bank_delitemconfirm(callback: CallbackQuery,
                                   conn: sqlite3.Connection, config: Config):
        if not await _owner_only(callback, config):
            return
        await callback.answer()
        lang = user_lang_or_default(conn, callback.from_user.id)
        try:
            ok = delete_bank_item(conn, int(callback.data.split(":", 2)[2]))
            await callback.message.answer(
                t("bank_file_deleted", lang) if ok else t("bank_search_failed", lang))
        except (ValueError, IndexError):
            pass
        await _send_panel(callback.message, config, callback.from_user.id, edit=True)

    router.include_router(fr)
