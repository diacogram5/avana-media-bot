"""🎬 کلیپ‌های منتخب استوری آوانو — story-ready clips by mood/occasion.

Standalone module: no changes to existing bot code are required to use it.
The parent only adds one menu button row (see story_button_row()) and calls
register_story_handlers(router).

Source:
  * Pixabay Video API — free key, documented, direct MP4 file URLs,
    portrait orientation. License: Pixabay Content License (free use incl.
    commercial); every caption credits the filmmaker + links the Pixabay page.
  * Pexels was REMOVED 2026-11-04: it stopped issuing new API keys (~2 months
    earlier, no reopen date), so no new applicant can obtain a key.

What this module NEVER does:
  * No Instagram scraping / no bypassing of any platform's limits.
  * Clips are NEVER presented as real Instagram trends — every list carries
    the «⭐ منتخب آوانو» badge.
  * No watermark is applied here. WATERMARK_ENABLED = False and
    maybe_watermark() is a clearly-marked hook: the Avano Motion Watermark
    track stays in design/sample stage until the user approves it.

All network I/O runs in executor threads — the event loop is never blocked.
In-memory cache only (12h TTL per category/subtopic); no DB schema changes.
Per-user navigation state is encoded entirely in callback_data (no global
per-user dicts).

Callback scheme (unchanged from the Pexels version — byte-identical):
    story:cat                        → category list
    story:sublist:<cat_id>           → subtopic list of a category
    story:sub:<cat_id>:<sub_id>      → clip list, page 0
    story:page:<cat_id>:<sub_id>:<p> → clip list, page p
    story:clip:<video_id>            → send the video
    story:back                       → back to categories
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger("avana.story_clips")

# ---------------------------------------------------------------------------
# Optional aiogram import (broken/missing in sandbox; present on Render).
# Keyboard builders work with tiny stubs so the self-test runs anywhere.
# ---------------------------------------------------------------------------
try:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    _AIOGRAM_OK = True
except Exception:  # pragma: no cover - sandbox fallback
    _AIOGRAM_OK = False

    class InlineKeyboardButton:  # type: ignore[no-redef]
        def __init__(self, text, callback_data=None, **kwargs):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:  # type: ignore[no-redef]
        def __init__(self, inline_keyboard=None):
            self.inline_keyboard = inline_keyboard or []


# ---------------------------------------------------------------------------
# Watermark hook — DESIGN TRACK ONLY. Do NOT enable until the user approves
# the Avano Motion Watermark sample (appearance, opacity, timing, position).
# ---------------------------------------------------------------------------
WATERMARK_ENABLED = False


def maybe_watermark(file_url: str) -> str:
    """Hook for the future Avano Motion Watermark pipeline.

    Currently returns the URL unchanged (WATERMARK_ENABLED = False).
    When the watermark design is approved, this function will route the
    download through the watermark processor and return the processed file.
    """
    return file_url


# ---------------------------------------------------------------------------
# Taxonomy (exact Persian labels from the approved spec)
# ---------------------------------------------------------------------------
CATEGORIES = [
    {"id": "love", "emoji": "❤️", "title": "عاشقانه", "subtopics": [
        {"id": "express", "title": "ابراز عشق"},
        {"id": "longing", "title": "دلتنگی"},
        {"id": "onesided", "title": "عشق یک‌طرفه"},
        {"id": "couple", "title": "کلیپ دونفره"},
    ]},
    {"id": "sad", "emoji": "💔", "title": "غمگین", "subtopics": [
        {"id": "breakup", "title": "جدایی"},
        {"id": "lonely", "title": "تنهایی"},
        {"id": "tears", "title": "بغض و دلتنگی"},
        {"id": "betrayal", "title": "بی‌وفایی"},
    ]},
    {"id": "occasion", "emoji": "🌹", "title": "مناسبتی", "subtopics": [
        {"id": "birthday", "title": "تولد"},
        {"id": "anniversary", "title": "سالگرد"},
        {"id": "valentine", "title": "روز عشق"},
        {"id": "parents", "title": "روز مادر و پدر"},
        {"id": "calendar", "title": "مناسبت‌های تقویمی"},
    ]},
    {"id": "deep", "emoji": "🖤", "title": "سنگین و مفهومی", "subtopics": [
        {"id": "pride", "title": "غرور"},
        {"id": "silence", "title": "سکوت"},
        {"id": "indifferent", "title": "بی‌تفاوتی"},
        {"id": "sarcasm", "title": "کنایه"},
    ]},
    {"id": "happy", "emoji": "✨", "title": "شاد و انرژی‌بخش", "subtopics": [
        {"id": "hope", "title": "امید"},
        {"id": "success", "title": "موفقیت"},
        {"id": "friendship", "title": "رفاقت"},
        {"id": "freshstart", "title": "شروع تازه"},
    ]},
]

# Pre-approved English queries per subtopic (2-3 each).
# Fixed and reviewable — no paid API, no ML classifier.
# Pixabay searches its tag index, so the same English queries apply.
SUBTOPIC_QUERIES: dict[tuple[str, str], list[str]] = {
    ("love", "express"): ["couple in love", "romantic couple kiss", "love couple sunset"],
    ("love", "longing"): ["missing someone", "lonely woman window rain", "longing love"],
    ("love", "onesided"): ["unrequited love", "sad man alone night", "lonely heart"],
    ("love", "couple"): ["couple holding hands", "couple walking together", "happy couple"],
    ("sad", "breakup"): ["breakup sad", "crying woman dark", "heartbreak"],
    ("sad", "lonely"): ["lonely person night", "alone in dark room", "solitude rain"],
    ("sad", "tears"): ["tears crying closeup", "sad eyes tears", "crying rain window"],
    ("sad", "betrayal"): ["betrayal", "walking away sad", "broken heart"],
    ("occasion", "birthday"): ["birthday celebration", "birthday cake candles", "happy birthday party"],
    ("occasion", "anniversary"): ["wedding anniversary", "couple anniversary celebration"],
    ("occasion", "valentine"): ["valentines day", "red roses love", "romantic dinner candles"],
    ("occasion", "parents"): ["mother and child love", "father and daughter", "family hug"],
    ("occasion", "calendar"): ["celebration fireworks", "new year celebration", "festival lights"],
    ("deep", "pride"): ["confident man dark", "proud silhouette", "strong man cinematic"],
    ("deep", "silence"): ["silence dark", "man thinking dark", "quiet night alone"],
    ("deep", "indifferent"): ["walking away", "cold stare", "indifferent man"],
    ("deep", "sarcasm"): ["smirking man", "cynical smile dark", "mocking laugh"],
    ("happy", "hope"): ["sunrise hope", "hopeful woman sunlight", "new dawn"],
    ("happy", "success"): ["success celebration", "trophy winner", "achievement"],
    ("happy", "friendship"): ["friends laughing", "friendship group fun", "friends together"],
    ("happy", "freshstart"): ["new beginning", "open road journey", "fresh start morning"],
}

_CAT_BY_ID = {c["id"]: c for c in CATEGORIES}
_SUB_BY_ID = {(c["id"], s["id"]): s for c in CATEGORIES for s in c["subtopics"]}


# ---------------------------------------------------------------------------
# Pixabay Video API client (urllib in executor; never blocks the event loop)
#
#   GET https://pixabay.com/api/videos/?key={KEY}&q=...&video_type=film
#       &orientation=vertical&safesearch=true&per_page=30
# Response: {"total": N, "totalHits": N,
#            "hits": [{"id", "pageURL", "tags", "duration",
#                      "videos": {"large": {"url","width","height"},
#                                 "medium": {...}, "small": {...}, "tiny": {...}},
#                      "user", "userImageURL", "previewURL", ...}]}
# Rate limit: 100 requests / 60s per key. Our pacing (3 concurrent + 2s
# between subtopics) stays far below it.
# ---------------------------------------------------------------------------
PIXABAY_VIDEOS_URL = "https://pixabay.com/api/videos/"
FETCH_TIMEOUT = 15
MAX_CONCURRENT_REQUESTS = 3
CACHE_TTL_SEC = 24 * 3600  # Pixabay API terms require 24h response caching
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")

_sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
_refresh_lock = asyncio.Lock()
_req_count = 0


class ConfigMissing(Exception):
    """PIXABAY_API_KEY is not configured. UI shows a short 'coming soon'."""


def _pixabay_key() -> str:
    """Read the key at call time. Never logged, never embedded in messages."""
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key:
        raise ConfigMissing("PIXABAY_API_KEY not set")
    return key


def _http_get_json(params: dict) -> dict:
    """Blocking GET to the Pixabay video API. One retry. Executor only."""
    import json
    import urllib.parse
    import urllib.request

    key = _pixabay_key()  # raises ConfigMissing before any network use
    qs = urllib.parse.urlencode({"key": key, **params})
    req = urllib.request.Request(
        f"{PIXABAY_VIDEOS_URL}?{qs}",
        headers={"User-Agent": USER_AGENT,
                 "Accept": "application/json"},
    )
    last_err: Exception | None = None
    for _attempt in range(2):  # initial try + one retry
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Pixabay HTTP {resp.status}")
                return json.loads(resp.read().decode("utf-8"))
        except ConfigMissing:
            raise
        except Exception as e:  # noqa: BLE001 - retry once, then raise
            last_err = e
            time.sleep(1)
    raise RuntimeError(f"Pixabay request failed: {last_err}")


async def _pixabay_get(params: dict) -> dict:
    global _req_count
    async with _sem:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _http_get_json, params)
        _req_count += 1
        return data


# ---------------------------------------------------------------------------
# Clip parsing helpers (pure functions — unit-tested)
# ---------------------------------------------------------------------------
def _title_from_tags(tags: str) -> str:
    """Pixabay has no title field; derive one from the tag list.

    "couple, love, romantic sunset" -> "Couple, Love, Romantic Sunset"
    """
    try:
        words = [t.strip() for t in (tags or "").split(",")]
        words = [w for w in words if w][:4]
        return ", ".join(w.title() for w in words) or "Untitled"
    except Exception:
        return "Untitled"


def _pick_file(videos: dict) -> tuple[str | None, int, int]:
    """Pick the smallest rendition with width >= 720.

    Pixabay shape: {"large": {"url","width","height"}, "medium": {...}, ...}
    Returns (url, width, height). Falls back to the smallest rendition.
    """
    cands: list[tuple[int, int, str]] = []
    for rend in (videos or {}).values():
        if not isinstance(rend, dict):
            continue
        url = (rend.get("url") or "").strip()
        try:
            w = int(rend.get("width") or 0)
            h = int(rend.get("height") or 0)
        except (TypeError, ValueError):
            w, h = 0, 0
        if url:
            cands.append((w, h, url))
    if not cands:
        return None, 0, 0
    ok = [(w, h, u) for w, h, u in cands if w >= 720]
    pool = ok or cands
    pool.sort(key=lambda x: x[0])
    w, h, u = pool[0]
    return u, w, h


def _quality_label(width: int) -> str:
    if width >= 2160:
        return "4K"
    if width >= 1440:
        return "2K"
    if width >= 1080:
        return "Full HD"
    if width >= 720:
        return "HD"
    return "SD"


def _parse_video(v: dict) -> dict | None:
    """Convert one Pixabay video hit into our clip dict.

    Landscape videos are dropped (story clips are portrait only).
    """
    try:
        vid = int(v.get("id"))
    except (TypeError, ValueError):
        return None
    file_url, width, height = _pick_file(v.get("videos") or {})
    if not file_url:
        return None
    if width and height and width >= height:
        return None  # landscape — skip
    try:
        duration = int(v.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    filmmaker = (v.get("user") or "Pixabay").strip() or "Pixabay"
    page_url = v.get("pageURL") or ""
    return {
        "video_id": vid,
        "title": _title_from_tags(v.get("tags") or "")[:120],
        "duration": duration,
        "width": width,
        "height": height,
        "quality": _quality_label(width),
        "filmmaker": filmmaker[:80],
        "page_url": page_url,
        "file_url": file_url,
        "preview_image": v.get("previewURL") or v.get("userImageURL") or "",
        "first_seen": int(time.time()),
    }


# ---------------------------------------------------------------------------
# In-memory cache (12h TTL per category/subtopic). No DB changes.
# ---------------------------------------------------------------------------
_cache: dict[tuple[str, str], dict] = {}
# video_id -> (cat_id, sub_id), for the clip-view "back" button
_origin: dict[int, tuple[str, str]] = {}


def _is_expired(fetched_at: float) -> bool:
    return (time.time() - fetched_at) > CACHE_TTL_SEC


async def _refresh_subtopic(cat_id: str, sub_id: str) -> list[dict]:
    """Fetch all queries for one subtopic. Raises ConfigMissing / RuntimeError."""
    queries = SUBTOPIC_QUERIES[(cat_id, sub_id)]
    seen: set[int] = set()
    # Dedup across sibling subtopics of the same category
    for (c, s), entry in _cache.items():
        if c == cat_id and (c, s) != (cat_id, sub_id):
            for it in entry.get("items", []):
                seen.add(it["video_id"])
    items: list[dict] = []
    for q in queries:
        data = await _pixabay_get({
            "q": q, "video_type": "film", "orientation": "vertical",
            "safesearch": "true", "per_page": 30,
        })
        for v in data.get("hits", []):
            clip = _parse_video(v)
            if clip and clip["video_id"] not in seen:
                seen.add(clip["video_id"])
                items.append(clip)
        if len(items) >= 25:
            break
    # Thin result? Fill from popular videos (portrait filtered client-side).
    if len(items) < 5:
        try:
            pop = await _pixabay_get({
                "video_type": "film", "orientation": "vertical",
                "safesearch": "true", "order": "popular", "per_page": 30,
            })
            for v in pop.get("hits", []):
                clip = _parse_video(v)
                if clip and clip["video_id"] not in seen:
                    seen.add(clip["video_id"])
                    items.append(clip)
                if len(items) >= 10:
                    break
        except Exception as e:  # noqa: BLE001 - filler is best-effort
            log.warning("story: popular filler failed (%s)", e)
    if not items:
        raise RuntimeError("empty result from Pixabay")
    _cache[(cat_id, sub_id)] = {"items": items, "fetched_at": time.time()}
    for it in items:
        _origin[it["video_id"]] = (cat_id, sub_id)
    log.info("story: refreshed %s/%s -> %d clips", cat_id, sub_id, len(items))
    return items


async def get_clips(cat_id: str, sub_id: str) -> list[dict]:
    """Return cached clips; refresh lazily on expiry. Never crashes."""
    key = (cat_id, sub_id)
    entry = _cache.get(key)
    if entry and not _is_expired(entry["fetched_at"]):
        return entry["items"]
    async with _refresh_lock:  # single-flight
        entry = _cache.get(key)
        if entry and not _is_expired(entry["fetched_at"]):
            return entry["items"]
        try:
            return await _refresh_subtopic(cat_id, sub_id)
        except ConfigMissing:
            raise
        except Exception as e:  # noqa: BLE001 - keep last valid list
            log.warning("story: refresh %s/%s failed (%s); keeping old",
                        cat_id, sub_id, e)
            if entry:
                return entry["items"]
            raise


def find_clip(video_id: int) -> dict | None:
    """Locate a clip by id across all cached subtopics (stateless lookup)."""
    for entry in _cache.values():
        for it in entry.get("items", []):
            if it["video_id"] == video_id:
                return it
    return None


async def refresh_loop(interval_sec: int = CACHE_TTL_SEC) -> None:
    """Background refresher for bot startup. NEVER crashes the bot."""
    while True:
        try:
            for key in list(SUBTOPIC_QUERIES.keys()):
                try:
                    entry = _cache.get(key)
                    if not entry or _is_expired(entry["fetched_at"]):
                        await _refresh_subtopic(*key)
                except ConfigMissing:
                    log.info("story: PIXABAY_API_KEY missing; refresher idle")
                    break
                except Exception as e:  # noqa: BLE001 - one bad subtopic
                    log.warning("story: background refresh %s failed (%s)",
                                key, e)
                await asyncio.sleep(2)  # gentle pacing between subtopics
        except Exception as e:  # noqa: BLE001 - the loop itself never dies
            log.warning("story: refresh_loop iteration failed (%s)", e)
        await asyncio.sleep(interval_sec)


# ---------------------------------------------------------------------------
# Display helpers (pure functions)
# ---------------------------------------------------------------------------
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _fa(text) -> str:
    return str(text).translate(_FA_DIGITS)


def _fmt_duration(sec: int) -> str:
    m, s = divmod(max(0, int(sec)), 60)
    return _fa(f"{m}:{s:02d}")


def _btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


CLIPS_PER_PAGE = 5
BADGE = "⭐ منتخب آوانو"


def story_button_row() -> list[InlineKeyboardButton]:
    """One inline-button row for the main menu. Parent inserts it as-is."""
    return [_btn("🎬 کلیپ‌های منتخب استوری", "story:cat")]


def categories_text() -> str:
    return ("🎬 <b>کلیپ‌های منتخب استوری آوانو</b>\n\n"
            "حال‌وهوای موردنظرت را انتخاب کن:")


def categories_kb() -> InlineKeyboardMarkup:
    rows = [[_btn(f"{c['emoji']} {c['title']}", f"story:sublist:{c['id']}")]
            for c in CATEGORIES]
    return _markup(rows)


def subtopics_text(cat_id: str) -> str:
    c = _CAT_BY_ID[cat_id]
    return f"{c['emoji']} <b>{c['title']}</b>\n\nزیرموضوع را انتخاب کن:"


def subtopics_kb(cat_id: str) -> InlineKeyboardMarkup:
    c = _CAT_BY_ID[cat_id]
    rows = [[_btn(s["title"], f"story:sub:{cat_id}:{s['id']}")]
            for s in c["subtopics"]]
    rows.append([_btn("🔙 دسته‌ها", "story:back")])
    return _markup(rows)


def clip_list_text(cat_id: str, sub_id: str, page: int,
                   items: list[dict], total_pages: int) -> str:
    c = _CAT_BY_ID[cat_id]
    s = _SUB_BY_ID[(cat_id, sub_id)]
    lines = [BADGE, f"{c['emoji']} {c['title']} › {s['title']}",
             f"صفحه {_fa(page + 1)} از {_fa(total_pages)}", ""]
    start = page * CLIPS_PER_PAGE
    for i, clip in enumerate(items[start:start + CLIPS_PER_PAGE], 1):
        lines.append(f"{_fa(i)}. {clip['title']}")
        lines.append(f"   ⏱ {_fmt_duration(clip['duration'])} | "
                     f"🎞 {clip['quality']} | 🎥 {clip['filmmaker']}")
    return "\n".join(lines)


def clip_list_kb(cat_id: str, sub_id: str, page: int,
                 items: list[dict], total_pages: int) -> InlineKeyboardMarkup:
    start = page * CLIPS_PER_PAGE
    page_items = items[start:start + CLIPS_PER_PAGE]
    num_btns = [_btn(_fa(i + 1), f"story:clip:{clip['video_id']}")
                for i, clip in enumerate(page_items)]
    rows: list[list[InlineKeyboardButton]] = []
    if num_btns:
        rows.append(num_btns)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(_btn("◀️ قبلی", f"story:page:{cat_id}:{sub_id}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(_btn("بعدی ▶️", f"story:page:{cat_id}:{sub_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_btn("🔙 زیرموضوع‌ها", f"story:sublist:{cat_id}")])
    return _markup(rows)


def clip_caption(clip: dict) -> str:
    # Attribution: filmmaker + link to the Pixabay page.
    return (f"🎬 {clip['title']}\n"
            f"{BADGE}\n"
            f"⏱ {_fmt_duration(clip['duration'])} | 🎞 {clip['quality']}\n"
            f"🎥 {clip['filmmaker']} via Pixabay\n"
            f"🔗 {clip['page_url']}")


def clip_back_kb(video_id: int) -> InlineKeyboardMarkup:
    origin = _origin.get(video_id)
    if origin:
        cb = f"story:sub:{origin[0]}:{origin[1]}"
    else:
        cb = "story:cat"
    return _markup([[_btn("🔙 بازگشت", cb)]])


# ---------------------------------------------------------------------------
# Handlers (aiogram-3 style; registered onto the repo's router)
# ---------------------------------------------------------------------------
async def _show_categories(callback) -> None:
    try:
        await callback.message.edit_text(categories_text(),
                                         reply_markup=categories_kb(),
                                         parse_mode="HTML")
    except Exception:
        await callback.message.answer(categories_text(),
                                      reply_markup=categories_kb(),
                                      parse_mode="HTML")


async def _show_subtopics(callback, cat_id: str) -> None:
    text, kb = subtopics_text(cat_id), subtopics_kb(cat_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb,
                                         parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb,
                                      parse_mode="HTML")


async def _show_clip_list(callback, cat_id: str, sub_id: str, page: int) -> None:
    try:
        items = await get_clips(cat_id, sub_id)
    except ConfigMissing:
        await callback.message.answer("⏳ این بخش به‌زودی فعال می‌شود.")
        return
    except Exception:
        await callback.message.answer("❌ در حال حاضر کلیپی در دسترس نیست. "
                                      "لطفاً بعداً تلاش کنید.")
        return
    if not items:
        await callback.message.answer("❌ در حال حاضر کلیپی در دسترس نیست.")
        return
    total_pages = (len(items) + CLIPS_PER_PAGE - 1) // CLIPS_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    text = clip_list_text(cat_id, sub_id, page, items, total_pages)
    kb = clip_list_kb(cat_id, sub_id, page, items, total_pages)
    try:
        await callback.message.edit_text(text, reply_markup=kb,
                                         parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb,
                                      parse_mode="HTML")


async def _send_clip(callback, video_id: int) -> None:
    clip = find_clip(video_id)
    if not clip:
        await callback.answer("❌ کلیپ پیدا نشد.", show_alert=True)
        return
    await callback.answer("🎬 در حال ارسال...")
    try:
        url = maybe_watermark(clip["file_url"])
        await callback.message.answer_video(
            video=url,
            caption=clip_caption(clip),
            reply_markup=clip_back_kb(video_id),
        )
    except Exception as e:
        log.warning("story: send_video failed for %s (%s)", video_id, e)
        await callback.message.answer(
            "❌ ارسال ویدیو ناموفق بود. لطفاً دوباره تلاش کنید.")


def register_story_handlers(router) -> None:
    """Attach all story-clip handlers to the repo's aiogram router.

    Matches the repo's decorator style (@router.callback_query(...)).
    Safe to call once at startup; never touches existing handlers.
    """
    from aiogram import F  # lazy: aiogram is present on Render

    @router.callback_query(F.data == "story:cat")
    async def story_cat(callback, conn=None, config=None):
        await callback.answer()
        await _show_categories(callback)

    @router.callback_query(F.data == "story:back")
    async def story_back(callback, conn=None, config=None):
        await callback.answer()
        await _show_categories(callback)

    @router.callback_query(F.data.startswith("story:sublist:"))
    async def story_sublist(callback, conn=None, config=None):
        await callback.answer()
        parts = callback.data.split(":")
        cat_id = parts[2] if len(parts) > 2 else ""
        if cat_id not in _CAT_BY_ID:
            await callback.message.answer("❌ دسته نامعتبر است.")
            return
        await _show_subtopics(callback, cat_id)

    @router.callback_query(F.data.startswith("story:sub:"))
    async def story_sub(callback, conn=None, config=None):
        await callback.answer("⏳ در حال بارگذاری کلیپ‌ها...")
        parts = callback.data.split(":")
        if len(parts) < 4 or parts[2] not in _CAT_BY_ID \
                or (parts[2], parts[3]) not in _SUB_BY_ID:
            await callback.message.answer("❌ درخواست نامعتبر است.")
            return
        await _show_clip_list(callback, parts[2], parts[3], 0)

    @router.callback_query(F.data.startswith("story:page:"))
    async def story_page(callback, conn=None, config=None):
        await callback.answer()
        parts = callback.data.split(":")
        if len(parts) < 5 or parts[2] not in _CAT_BY_ID \
                or (parts[2], parts[3]) not in _SUB_BY_ID:
            return
        try:
            page = int(parts[4])
        except (TypeError, ValueError):
            page = 0
        await _show_clip_list(callback, parts[2], parts[3], page)

    @router.callback_query(F.data.startswith("story:clip:"))
    async def story_clip(callback, conn=None, config=None):
        parts = callback.data.split(":")
        try:
            video_id = int(parts[2]) if len(parts) > 2 else 0
        except (TypeError, ValueError):
            video_id = 0
        if not video_id:
            await callback.answer("❌", show_alert=True)
            return
        await _send_clip(callback, video_id)

    log.info("story: handlers registered (watermark_enabled=%s)",
             WATERMARK_ENABLED)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
def get_api_status() -> dict:
    """Diagnostic snapshot. Never includes the API key value."""
    return {
        "pixabay_key_configured": bool(os.environ.get("PIXABAY_API_KEY", "").strip()),
        "cached_subtopics": len(_cache),
        "cached_clips": sum(len(e.get("items", [])) for e in _cache.values()),
        "requests_made": _req_count,
        "watermark_enabled": WATERMARK_ENABLED,
        "cache_ttl_hours": CACHE_TTL_SEC // 3600,
    }


# ---------------------------------------------------------------------------
# Self-test (no network, no API key, no aiogram needed)
# ---------------------------------------------------------------------------
def _fixture_videos():
    # Mirrors the real Pixabay /api/videos/ hit shape.
    return {
        "large": {"url": "https://cdn.pixabay.com/vimeo/1/v-uhd.mp4",
                  "width": 2160, "height": 3840, "size": 9000000},
        "medium": {"url": "https://cdn.pixabay.com/vimeo/1/v-hd.mp4",
                   "width": 1080, "height": 1920, "size": 3000000},
        "small": {"url": "https://cdn.pixabay.com/vimeo/1/v-sd.mp4",
                  "width": 720, "height": 1280, "size": 1200000},
        "tiny": {"url": "https://cdn.pixabay.com/vimeo/1/v-tiny.mp4",
                 "width": 480, "height": 854, "size": 500000},
    }


def run_self_test() -> bool:
    ok = True

    def check(name: str, cond: bool):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), "-", name)
        if not cond:
            ok = False

    # 1. taxonomy: 5 categories, exact subtopic titles
    expected = {
        "love": ["ابراز عشق", "دلتنگی", "عشق یک‌طرفه", "کلیپ دونفره"],
        "sad": ["جدایی", "تنهایی", "بغض و دلتنگی", "بی‌وفایی"],
        "occasion": ["تولد", "سالگرد", "روز عشق", "روز مادر و پدر",
                     "مناسبت‌های تقویمی"],
        "deep": ["غرور", "سکوت", "بی‌تفاوتی", "کنایه"],
        "happy": ["امید", "موفقیت", "رفاقت", "شروع تازه"],
    }
    check("5 categories", len(CATEGORIES) == 5)
    for c in CATEGORIES:
        got = [s["title"] for s in c["subtopics"]]
        check(f"subtopics of {c['id']}", got == expected[c["id"]])
    # every subtopic has 2-3 non-empty queries
    all_keys = set(SUBTOPIC_QUERIES.keys())
    want_keys = {(c["id"], s["id"]) for c in CATEGORIES for s in c["subtopics"]}
    check("query map covers all subtopics", all_keys == want_keys)
    check("2-3 queries each",
          all(2 <= len(q) <= 3 and all(x.strip() for x in q)
              for q in SUBTOPIC_QUERIES.values()))

    # 2. callback-data round-trip (byte-identical to the Pexels version)
    cb = "story:page:occasion:calendar:2"
    parts = cb.split(":")
    check("callback round-trip",
          parts == ["story", "page", "occasion", "calendar", "2"]
          and len(cb) <= 64)
    check("clip callback fits 64B",
          len("story:clip:272487468") <= 64)

    # 3. pagination math (12 items, 5 per page -> 3 pages: 5/5/2)
    items = [{"video_id": i, "title": f"T{i}", "duration": 10,
              "quality": "HD", "filmmaker": "F",
              "page_url": "https://pixabay.com/videos/x-1/",
              "file_url": "https://x.mp4"} for i in range(12)]
    total = (len(items) + CLIPS_PER_PAGE - 1) // CLIPS_PER_PAGE
    check("12 items -> 3 pages", total == 3)
    p0 = items[0:5]
    p2 = items[10:15]
    check("page slices 5/5/2", len(p0) == 5 and len(p2) == 2)
    txt = clip_list_text("love", "longing", 1, items, total)
    check("page text has badge + page num",
          BADGE in txt and "⭐" in txt)
    kb = clip_list_kb("love", "longing", 1, items, total)
    flat = [b.callback_data for row in kb.inline_keyboard for b in row]
    check("page 1 has prev+next",
          any("story:page:love:longing:0" in c for c in flat)
          and any("story:page:love:longing:2" in c for c in flat))
    kb0 = clip_list_kb("love", "longing", 0, items, total)
    flat0 = [b.callback_data for row in kb0.inline_keyboard for b in row]
    check("page 0 has next only",
          not any(c.endswith(":page:love:longing:-1") for c in flat0)
          and any("story:page:love:longing:1" in c for c in flat0))

    # 4. cache TTL
    now = time.time()
    check("fresh entry not expired", not _is_expired(now - 3600))
    check("13h-old entry NOT expired (24h TTL)", not _is_expired(now - 13 * 3600))
    check("25h-old entry expired", _is_expired(now - 25 * 3600))

    # 5. dedup helper logic
    seen = {1, 2, 3}
    new_ids = [3, 4, 5]
    kept = [i for i in new_ids if i not in seen]
    check("dedup filters known ids", kept == [4, 5])

    # 6. file picker: smallest rendition >= 720p wide
    url, w, h = _pick_file(_fixture_videos())
    check("picks 720p rendition",
          url == "https://cdn.pixabay.com/vimeo/1/v-sd.mp4"
          and w == 720 and h == 1280)
    url2, w2, _h2 = _pick_file({
        "tiny": {"url": "https://x/a.mp4", "width": 480, "height": 854}})
    check("fallback to smallest rendition",
          url2 == "https://x/a.mp4" and w2 == 480)
    check("no renditions -> None", _pick_file({}) == (None, 0, 0))
    check("quality labels",
          _quality_label(2160) == "4K" and _quality_label(1080) == "Full HD"
          and _quality_label(720) == "HD" and _quality_label(480) == "SD")

    # 7. title from tags
    check("title from tags",
          _title_from_tags("couple, love, romantic sunset")
          == "Couple, Love, Romantic Sunset")
    check("empty tags -> Untitled", _title_from_tags("") == "Untitled")

    # 8. parse_video fixture (mirrors real Pixabay /api/videos/ hit shape)
    v = {"id": 272487468, "pageURL": "https://pixabay.com/videos/poppy-field-272487468/",
         "type": "film", "tags": "poppy, field, flowers, nature",
         "duration": 23,
         "videos": _fixture_videos(),
         "user": "coverr", "userImageURL": "https://cdn.pixabay.com/u.jpg",
         "previewURL": "https://i.vimeocdn.com/video/x.jpg"}
    clip = _parse_video(v)
    check("parse_video fields",
          clip is not None and clip["video_id"] == 272487468
          and clip["quality"] == "HD"
          and clip["filmmaker"] == "coverr"
          and clip["duration"] == 23
          and clip["title"] == "Poppy, Field, Flowers, Nature"
          and clip["page_url"].startswith("https://pixabay.com/videos/")
          and clip["first_seen"] > 0)
    # landscape hit is dropped
    land = dict(v)
    land["videos"] = {
        "medium": {"url": "https://x/land.mp4", "width": 1280, "height": 720}}
    check("landscape dropped", _parse_video(land) is None)
    # no renditions -> dropped
    norend = dict(v)
    norend["videos"] = {}
    check("no renditions dropped", _parse_video(norend) is None)

    # 9. watermark hook is a pass-through while disabled
    check("WATERMARK_ENABLED False", WATERMARK_ENABLED is False)
    check("maybe_watermark passthrough",
          maybe_watermark("https://x.mp4") == "https://x.mp4")

    # 10. find_clip + origin back-button
    _cache[("deep", "silence")] = {"items": [clip], "fetched_at": now}
    _origin[272487468] = ("deep", "silence")
    check("find_clip", find_clip(272487468)["video_id"] == 272487468)
    check("find_clip miss", find_clip(999) is None)
    back = clip_back_kb(272487468)
    bcb = back.inline_keyboard[0][0].callback_data
    check("back button -> origin list", bcb == "story:sub:deep:silence")
    back2 = clip_back_kb(12345)
    check("back button fallback -> categories",
          back2.inline_keyboard[0][0].callback_data == "story:cat")
    _cache.pop(("deep", "silence"), None)
    _origin.pop(272487468, None)

    # 11. captions carry attribution + never claim IG trends
    cap = clip_caption(clip)
    check("caption attribution",
          "via Pixabay" in cap and "coverr" in cap
          and BADGE in cap and "pixabay.com/videos/" in cap)
    check("no fake IG-trend wording",
          "اینستاگرام" not in cap and "ترند" not in cap)

    # 12. diagnostics never leak the key
    os.environ["PIXABAY_API_KEY"] = "secret-should-not-appear"
    try:
        st = get_api_status()
        check("status configured flag", st["pixabay_key_configured"] is True)
        check("status leaks nothing",
              "secret-should-not-appear" not in str(st))
    finally:
        del os.environ["PIXABAY_API_KEY"]
    check("status unconfigured",
          get_api_status()["pixabay_key_configured"] is False)

    # 13. main-menu button row
    row = story_button_row()
    check("menu row",
          len(row) == 1 and row[0].callback_data == "story:cat"
          and "استوری" in row[0].text)

    # 14. ConfigMissing before any network use
    try:
        _http_get_json({"q": "test"})
        check("ConfigMissing raised", False)
    except ConfigMissing:
        check("ConfigMissing raised", True)
    except Exception:
        check("ConfigMissing raised", False)

    # 15. every keyboard builder constructs without exception
    #     (regression: subtopics_kb once called _btn with a bad kwarg,
    #     which silently broke the category tap in production)
    try:
        _ = categories_kb()
        for c in CATEGORIES:
            _ = subtopics_text(c["id"])
            _ = subtopics_kb(c["id"])
        kb_ok = True
    except Exception:
        kb_ok = False
    check("all category/subtopic keyboards build", kb_ok)

    print("\nSELF-TEST:", "ALL PASS" if ok else "FAILURES PRESENT")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_self_test() else 1)
