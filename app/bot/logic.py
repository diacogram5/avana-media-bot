"""Pure bot logic: menus, texts and gate checks without a Bot instance.

Handlers call these functions, so the behaviour in tests is exactly the
behaviour in production. Feature toggles are read from the DB on every
call, so an admin change applies without restarting the bot.
"""
from __future__ import annotations

from .. import db as dbmod
from ..i18n import t

# User menu only: core features. No stats button and no other
# useless buttons — voice-to-text replaces them.
MENU_ITEMS = [
    ("song_search", "menu_song"),
    ("link_fetch", "menu_link"),
    ("voice_to_text", "menu_voice_to_text"),
    ("help_support", "menu_help"),
]


def main_menu_labels(conn, lang: str = "fa") -> list[str]:
    labels = []
    for feature_key, text_key in MENU_ITEMS:
        if dbmod.feature_enabled(conn, feature_key):
            labels.append(t(text_key, lang))
    return labels


def welcome_text(conn, lang: str = "fa") -> str:
    custom = dbmod.get_setting(conn, "welcome_message", "")
    if custom:
        return custom
    return t("welcome", lang)


def can_use_feature(conn, telegram_id: int, feature_key: str, daily_limit: int = 50) -> tuple[bool, str]:
    # MVP: only the DB feature toggle gates a feature. No daily limit,
    # channel gate, or block system in MVP.
    if not dbmod.feature_enabled(conn, feature_key):
        return False, "feature_disabled"
    return True, ""


def format_results(results, page: int = 1, page_size: int = 5) -> str:
    """Professional minimal list: clean "Title – Artist" lines only.

    No source tags, no durations, no URLs per line, no empty sections.
    The first result's audio + official download link are sent separately
    as the professional card (see clean_title / professional_audio_caption).
    """
    from ..search import paginate

    page_items, _total_pages = paginate(results, page, page_size)
    if not page_items:
        return ""
    lines = []
    for i, item in enumerate(page_items, start=1):
        artist = f" – {item.artist}" if item.artist else ""
        lines.append(f"{i}. {item.title}{artist}")
    return "\n".join(lines)


def clean_title(result) -> str:
    """The one clean title line for a found song: "Title – Artist"."""
    return f"{result.title} – {result.artist}" if result.artist else result.title


BRIEF_ERRORS = {
    "search": "⚠️ جستجو با خطا مواجه شد. دوباره تلاش کنید.",
    "identify": "⚠️ شناسایی آهنگ با خطا مواجه شد. ویدیو را مستقیم بفرستید.",
    "file": "⚠️ ارسال فایل با خطا مواجه شد. دوباره تلاش کنید.",
    "lyrics": "⚠️ متن آهنگ با خطا مواجه شد. دوباره تلاش کنید.",
    "voice": "⚠️ ساخت ویس با خطا مواجه شد. دوباره تلاش کنید.",
    "translate": "⚠️ ترجمه با خطا مواجه شد. دوباره تلاش کنید.",
    "suggest": "⚠️ پیشنهاد آهنگ با خطا مواجه شد. دوباره تلاش کنید.",
    "not_found": "😕 نتیجه‌ای پیدا نشد. عبارت دیگری را امتحان کنید.",
}


def brief_error(kind: str) -> str:
    """One short Persian line for a failure: what failed + what to do."""
    return BRIEF_ERRORS.get(kind, BRIEF_ERRORS["search"])


def attach_bot_id(text: str, bot_id: str) -> str:
    """Append the bot ID/link to shared content only. Empty ID -> unchanged.

    This is used ONLY for file captions and copy/share text, never for
    messages shown in the bot's own chat page (those stay clean).
    """
    bot_id = (bot_id or "").strip()
    if not bot_id:
        return text
    if not bot_id.startswith("@") and not bot_id.startswith("http"):
        bot_id = "@" + bot_id
    if bot_id in text:
        return text
    return f"{text}\n{bot_id}"


def professional_audio_caption(result, bot_id: str = "") -> str:
    """Caption for the professional song card: clean title + official link.

    The official catalog link is the download/purchase link and the
    catalog preview (sent as the audio message itself) is the legal
    downloadable preview. A full copyrighted file is never downloaded
    from an unauthorised source and resent; if no official link exists,
    the caption is just the clean title and the caller adds one honest
    line saying no official preview/download was found in the catalog.
    """
    title = clean_title(result)
    if result.url:
        caption = f"{title}\n⬇️ دانلود رسمی: {result.url}"
    else:
        caption = title
    # File caption = shared content, so the bot ID travels with forwards.
    return attach_bot_id(caption, bot_id)


VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv")


def is_video_file_url(url: str) -> bool:
    """True only for a direct public video-file URL (not a platform page)."""
    path = (url or "").lower().split("?")[0]
    return path.endswith(VIDEO_EXTENSIONS)


def link_video_delivery(outcome) -> tuple[str, str]:
    """How the video of a sent link is delivered — never a catalog search.

    - Direct public video file: the same file, sent for download.
    - Platform link (YouTube/Instagram/Aparat): the official video itself,
      via its official player/open link. No private/protected content is
      bypassed and no full file is fetched from the platform.
    - Anything else: the official link only.
    Returns (delivery_kind, message_text). delivery_kind is one of
    "direct_video_file", "platform_official", "official_link".
    """
    url = outcome.official_url or ""
    if outcome.kind == "direct_file" and is_video_file_url(url):
        return ("direct_video_file",
                "🎬 ویدیوی همان لینک برای دانلود:\n" + url)
    if outcome.kind in ("youtube", "instagram", "aparat", "video"):
        # Professional minimal: the official video itself, nothing else.
        # No lecture paragraph, no extra sections. Private/protected
        # content is still never bypassed (see media policy in code).
        return ("platform_official", "🎬 ویدیوی همان لینک:\n" + url)
    return ("official_link", "")


def _normalise(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9\u0600-\u06ff]+", "", (text or "").lower())


def match_identified_song(results, title: str, artist: str):
    """Pick the catalog result that IS the identified song, or None.

    Exact-ish match on normalised title + artist. A merely similar
    catalog result is never returned as the identified song — the caller
    then sends only the clean identified title with an honest note that
    no official preview/download was found in the catalog.
    """
    want_title = _normalise(title)
    want_artist = _normalise(artist)
    for item in results:
        if _normalise(item.title) != want_title:
            continue
        if want_artist and _normalise(item.artist) and _normalise(item.artist) != want_artist:
            continue
        return item
    return None


def format_user_line(user: dict) -> str:
    blocked = "⛔ مسدود" if user.get("blocked") else "✅ فعال"
    name = user.get("username") or user.get("first_name") or ""
    return f"• {user['telegram_id']} {name} — {blocked}"
