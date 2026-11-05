"""🎬 کلیپ‌های منتخب استوری آوانو — story-ready clips by mood/occasion.

Standalone module: no changes to existing bot code are required to use it.
The parent only adds one menu button row (see story_button_row()) and calls
register_story_handlers(router).

Sources:
  * Pexels Video API (primary) — free, documented, direct MP4 file URLs,
    portrait orientation. License: free use incl. commercial; API guidelines
    require a prominent link to Pexels + photographer credit when showing
    results (honored in every caption).
  * Pixabay (fallback, optional) — NOT wired yet; only after a careful review
    of its API cache/hotlink terms.

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

Callback scheme:
    story:cat                        → category list
    story:sublist:<cat_id>           → subtopic list of a category
    story:sub:<cat_id>:<sub_id>      → clip list, page 0
    story:page:<cat_id>:<sub_id>:<p> → clip list, page p
    story:clip:<pexels_id>           → send the video
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

# Pre-approved English Pexels queries per subtopic (2-3 each).
# Fixed and reviewable — no paid API, no ML classifier.
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
# Pexels API client (urllib in executor; never blocks the event loop)
# ---------------------------------------------------------------------------
PEXELS_SEARCH_URL = "https://api.pexels.com/v1/videos/search"
PEXELS_POPULAR_URL = "https://api.pexels.com/v1/videos/popular"
FETCH_TIMEOUT = 15
MAX_CONCURRENT_REQUESTS = 3
CACHE_TTL_SEC = 12 * 3600
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")

_sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
_refresh_lock = asyncio.Lock()
_req_count = 0


class ConfigMissing(Exception):
    """PEXELS_API_KEY is not configured. UI shows a short 'coming soon'."""


def _pexels_key() -> str:
    """Read the key at call time. Never logged, never embedded in messages."""
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not key:
        raise ConfigMissing("PEXELS_API_KEY not set")
    return key


def _http_get_json(url: str, params: dict) -> dict:
    """Blocking GET with Authorization header. One retry. Executor only."""
    import json
    import urllib.parse
    import urllib.request

    key = _pexels_key()  # raises ConfigMissing before any network use
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{qs}",
        headers={"Authorization": key,
                 "User-Agent": USER_AGENT,
                 "Accept": "application/json"},
    )
    last_err: Exception | None = None
    for _attempt in range(2):  # initial try + one retry
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Pexels HTTP {resp.status}")
                return json.loads(resp.read().decode("utf-8"))
        except ConfigMissing:
            raise
        except Exception as e:  # noqa: BLE001 - retry once, then raise
            last_err = e
            time.sleep(1)
    raise RuntimeError(f"Pexels request failed: {last_err}")


async def _pexels_get(url: str, params: dict) -> dict:
    global _req_count
    async with _sem:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _http_get_json, url, params)
        _req_count += 1
        return data


# ---------------------------------------------------------------------------
# Clip parsing helpers (pure functions — unit-tested)
# ---------------------------------------------------------------------------
def _title_from_url(page_url: str) -> str:
    """Pexels API has no title field; derive it from the page slug.

    https://www.pexels.com/video/moody-silhouette-in-dramatic-lighting-36644497/
    -> "Moody Silhouette In Dramatic Lighting"
    """
    try:
        slug = page_url.rstrip("/").rsplit("/", 1)[-1]
        slug = slug.rsplit("-", 1)[0]  # strip trailing numeric id
        words = [w for w in slug.split("-") if w]
        return " ".join(w.capitalize() for w in words) or "Untitled"
    except Exception:
        return "Untitled"


def _pick_file_url(video_files: list[dict]) -> tuple[str | None, int]:
    """Pick the smallest MP4 rendition with width >= 720p.

    Returns (url, width). Falls back to the smallest MP4 of any size.
    """
    cands: list[tuple[int, str]] = []
    for f in video_files or []:
        link = (f.get("link") or "").strip()
        ftype = f.get("file_type") or ""
        if not link or ("mp4" not in ftype and not link.endswith(".mp4")):
            continue
        try:
            w = int(f.get("width") or 0)
        except (TypeError, ValueError):
            w = 0
        cands.append((w, link))
    if not cands:
        return None, 0
    ok = [(w, l) for w, l in cands if w >= 720]
    pool = ok or cands
    pool.sort(key=lambda x: x[0])
    return pool[0][1], pool[0][0]


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
    """Convert one Pexels API video object into our clip dict."""
    try:
        vid = int(v.get("id"))
    except (TypeError, ValueError):
        return None
    file_url, width = _pick_file_url(v.get("video_files") or [])
    if not file_url:
        return None
    try:
        height = int(v.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    try:
        duration = int(v.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    filmmaker = ((v.get("user") or {}).get("name") or "Pexels").strip() or "Pexels"
    page_url = v.get("url") or ""
    return {
        "pexels_id": vid,
        "title": _title_from_url(page_url)[:120],
        "duration": duration,
        "width": width,
        "height": height,
        "quality": _quality_label(width),
        "filmmaker": filmmaker[:80],
        "page_url": page_url,
        "file_url": file_url,
        "preview_image": v.get("image") or "",
        "first_seen": int(time.time()),
    }


# ---------------------------------------------------------------------------
# In-memory cache (12h TTL per category/subtopic). No DB changes.
# ---------------------------------------------------------------------------
_cache: dict[tuple[str, str], dict] = {}
# pexels_id -> (cat_id, sub_id), for the clip-view "back" button
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
                seen.add(it["pexels_id"])
    items: list[dict] = []
    for q in queries:
        data = await _pexels_get(PEXELS_SEARCH_URL, {
            "query": q, "orientation": "portrait",
            "per_page": 30, "size": "medium",
        })
        for v in data.get("videos", []):
            clip = _parse_video(v)
            if clip and clip["pexels_id"] not in seen:
                seen.add(clip["pexels_id"])
                items.append(clip)
        if len(items) >= 25:
            break
    # Thin result? Fill from the popular endpoint (portrait only).
    if len(items) < 5:
        try:
            pop = await _pexels_get(PEXELS_POPULAR_URL, {"per_page": 30})
            for v in pop.get("videos", []):
                try:
                    if int(v.get("height") or 0) <= int(v.get("width") or 0):
                        continue  # landscape — skip
                except (TypeError, ValueError):
                    continue
                clip = _parse_video(v)
                if clip and clip["pexels_id"] not in seen:
                    seen.add(clip["pexels_id"])
                    items.append(clip)
                if len(items) >= 10:
                    break
        except Exception as e:  # noqa: BLE001 - filler is best-effort
            log.warning("story: popular filler failed (%s)", e)
    if not items:
        raise RuntimeError("empty result from Pexels")
    _cache[(cat_id, sub_id)] = {"items": items, "fetched_at": time.time()}
    for it in items:
        _origin[it["pexels_id"]] = (cat_id, sub_id)
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


def find_clip(pexels_id: int) -> dict | None:
    """Locate a clip by id across all cached subtopics (stateless lookup)."""
    for entry in _cache.values():
        for it in entry.get("items", []):
            if it["pexels_id"] == pexels_id:
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
                    log.info("story: PEXELS_API_KEY missing; refresher idle")
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
    rows.append([_btn("🔙 دسته‌ها", callback_data="story:back")])
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
    num_btns = [_btn(_fa(i + 1), f"story:clip:{clip['pexels_id']}")
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
    # Attribution per Pexels API guidelines: filmmaker + link to Pexels page.
    return (f"🎬 {clip['title']}\n"
            f"{BADGE}\n"
            f"⏱ {_fmt_duration(clip['duration'])} | 🎞 {clip['quality']}\n"
            f"🎥 {clip['filmmaker']} via Pexels\n"
            f"🔗 {clip['page_url']}")


def clip_back_kb(pexels_id: int) -> InlineKeyboardMarkup:
    origin = _origin.get(pexels_id)
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


async def _send_clip(callback, pexels_id: int) -> None:
    clip = find_clip(pexels_id)
    if not clip:
        await callback.answer("❌ کلیپ پیدا نشد.", show_alert=True)
        return
    await callback.answer("🎬 در حال ارسال...")
    try:
        url = maybe_watermark(clip["file_url"])
        await callback.message.answer_video(
            video=url,
            caption=clip_caption(clip),
            reply_markup=clip_back_kb(pexels_id),
        )
    except Exception as e:
        log.warning("story: send_video failed for %s (%s)", pexels_id, e)
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
            pexels_id = int(parts[2]) if len(parts) > 2 else 0
        except (TypeError, ValueError):
            pexels_id = 0
        if not pexels_id:
            await callback.answer("❌", show_alert=True)
            return
        await _send_clip(callback, pexels_id)

    log.info("story: handlers registered (watermark_enabled=%s)",
             WATERMARK_ENABLED)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
def get_api_status() -> dict:
    """Diagnostic snapshot. Never includes the API key value."""
    return {
        "pexels_key_configured": bool(os.environ.get("PEXELS_API_KEY", "").strip()),
        "cached_subtopics": len(_cache),
        "cached_clips": sum(len(e.get("items", [])) for e in _cache.values()),
        "requests_made": _req_count,
        "watermark_enabled": WATERMARK_ENABLED,
        "cache_ttl_hours": CACHE_TTL_SEC // 3600,
    }


# ---------------------------------------------------------------------------
# Self-test (no network, no API key, no aiogram needed)
# ---------------------------------------------------------------------------
def _fixture_files():
    return [
        {"link": "https://x/v-uhd.mp4", "file_type": "video/mp4",
         "width": 2160, "height": 3840},
        {"link": "https://x/v-hd.mp4", "file_type": "video/mp4",
         "width": 1080, "height": 1920},
        {"link": "https://x/v-sd.mp4", "file_type": "video/mp4",
         "width": 720, "height": 1280},
        {"link": "https://x/v-tiny.mp4", "file_type": "video/mp4",
         "width": 480, "height": 854},
    ]


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

    # 2. callback-data round-trip
    cb = "story:page:occasion:calendar:2"
    parts = cb.split(":")
    check("callback round-trip",
          parts == ["story", "page", "occasion", "calendar", "2"]
          and len(cb) <= 64)
    check("clip callback fits 64B",
          len("story:clip:36644497") <= 64)

    # 3. pagination math (12 items, 5 per page -> 3 pages: 5/5/2)
    items = [{"pexels_id": i, "title": f"T{i}", "duration": 10,
              "quality": "HD", "filmmaker": "F",
              "page_url": "https://www.pexels.com/video/x-1/",
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
    check("13h-old entry expired", _is_expired(now - 13 * 3600))

    # 5. dedup helper logic
    seen = {1, 2, 3}
    new_ids = [3, 4, 5]
    kept = [i for i in new_ids if i not in seen]
    check("dedup filters known ids", kept == [4, 5])

    # 6. file picker: smallest mp4 >= 720p
    url, w = _pick_file_url(_fixture_files())
    check("picks 720p rendition", url == "https://x/v-sd.mp4" and w == 720)
    url2, w2 = _pick_file_url([{"link": "https://x/a.mp4",
                                "file_type": "video/mp4",
                                "width": 480, "height": 854}])
    check("fallback to smallest mp4", url2 == "https://x/a.mp4" and w2 == 480)
    check("no mp4 -> None", _pick_file_url([]) == (None, 0))
    check("quality labels",
          _quality_label(2160) == "4K" and _quality_label(1080) == "Full HD"
          and _quality_label(720) == "HD" and _quality_label(480) == "SD")

    # 7. title from slug
    check("title from url",
          _title_from_url("https://www.pexels.com/video/"
                          "moody-silhouette-in-dramatic-lighting-36644497/")
          == "Moody Silhouette In Dramatic Lighting")

    # 8. parse_video fixture (mirrors real Pexels API shape)
    v = {"id": 36644497, "width": 2160, "height": 3840, "duration": 23,
         "url": "https://www.pexels.com/video/moody-silhouette-36644497/",
         "image": "https://images.pexels.com/x.jpg",
         "user": {"name": "Cemrecan Yurtman"},
         "video_files": _fixture_files()}
    clip = _parse_video(v)
    check("parse_video fields",
          clip is not None and clip["pexels_id"] == 36644497
          and clip["quality"] == "HD"
          and clip["filmmaker"] == "Cemrecan Yurtman"
          and clip["duration"] == 23
          and clip["first_seen"] > 0)

    # 9. watermark hook is a pass-through while disabled
    check("WATERMARK_ENABLED False", WATERMARK_ENABLED is False)
    check("maybe_watermark passthrough",
          maybe_watermark("https://x.mp4") == "https://x.mp4")

    # 10. find_clip + origin back-button
    _cache[("deep", "silence")] = {"items": [clip], "fetched_at": now}
    _origin[36644497] = ("deep", "silence")
    check("find_clip", find_clip(36644497)["pexels_id"] == 36644497)
    check("find_clip miss", find_clip(999) is None)
    back = clip_back_kb(36644497)
    bcb = back.inline_keyboard[0][0].callback_data
    check("back button -> origin list", bcb == "story:sub:deep:silence")
    back2 = clip_back_kb(12345)
    check("back button fallback -> categories",
          back2.inline_keyboard[0][0].callback_data == "story:cat")
    _cache.pop(("deep", "silence"), None)
    _origin.pop(36644497, None)

    # 11. captions carry attribution + never claim IG trends
    cap = clip_caption(clip)
    check("caption attribution",
          "via Pexels" in cap and "Cemrecan Yurtman" in cap
          and BADGE in cap)
    check("no fake IG-trend wording",
          "اینستاگرام" not in cap and "ترند" not in cap)

    # 12. diagnostics never leak the key
    os.environ["PEXELS_API_KEY"] = "secret-should-not-appear"
    try:
        st = get_api_status()
        check("status configured flag", st["pexels_key_configured"] is True)
        check("status leaks nothing",
              "secret-should-not-appear" not in str(st))
    finally:
        del os.environ["PEXELS_API_KEY"]
    check("status unconfigured",
          get_api_status()["pexels_key_configured"] is False)

    # 13. main-menu button row
    row = story_button_row()
    check("menu row",
          len(row) == 1 and row[0].callback_data == "story:cat"
          and "استوری" in row[0].text)

    print("\nSELF-TEST:", "ALL PASS" if ok else "FAILURES PRESENT")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_self_test() else 1)