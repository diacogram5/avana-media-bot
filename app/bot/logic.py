"""Pure bot logic: menus, texts and gate checks without a Bot instance.

Handlers call these functions, so the behaviour in tests is exactly the
behaviour in production. Feature toggles are read from the DB on every
call, so an admin change applies without restarting the bot.
"""
from __future__ import annotations

import functools

from .. import db as dbmod
from ..i18n import t, toggle_texts

# Legacy alias: the live user menu is the dynamic «قابلیت‌ها» menu built
# from the features table (see capabilities_menu). Kept so existing
# callers/tests keep working; titles come from the DB (Persian).
def main_menu_labels(conn, lang: str = "fa") -> list[str]:
    return [f["title"] for f in capabilities_menu(conn)]


def welcome_text(conn, lang: str = "fa") -> str:
    custom = dbmod.get_setting(conn, "welcome_message", "")
    if custom:
        return custom
    return t("welcome", lang)


def can_use_feature(conn, telegram_id: int, feature_key: str, daily_limit: int = 50) -> tuple[bool, str]:
    # Gating is on `enabled` only. `visible` controls the «قابلیت‌ها» menu
    # display: buttonless/auto features (audio_id, link_fetch, tts,
    # voice_to_text) stay enabled while hidden from the menu.
    feat = dbmod.get_feature(conn, feature_key)
    if feat is None or not feat.get("enabled"):
        return False, "feature_disabled"
    return True, ""


def capabilities_menu(conn) -> list[dict]:
    """Dynamic «قابلیت‌ها» menu: visible+enabled features in menu order."""
    return [f for f in dbmod.list_features(conn)
            if f.get("enabled") and f.get("visible")]


# Persistent-menu collapse toggle labels. The Persian constants stay as the
# fa source of truth (existing callers/tests); rendering uses
# ``toggle_close_text(lang)`` / ``toggle_open_text(lang)``. A tap is
# recognised in ANY language variant, because the user taps the button in
# whatever language it was last rendered in.
MENU_TOGGLE_CLOSE = "🔽 بستن منو"
MENU_TOGGLE_OPEN = "☰ منو"


def toggle_close_text(lang: str = "fa") -> str:
    return t("m_toggle_close", lang)


def toggle_open_text(lang: str = "fa") -> str:
    return t("m_toggle_open", lang)


def _all_toggle_texts() -> set[str]:
    out = set()
    for kind in ("close", "open"):
        out.update(toggle_texts(kind).values())
    return out


def is_menu_toggle(text: str) -> bool:
    """True when the text is one of the menu-collapse toggle labels, in any
    of the three UI languages."""
    return (text or "").strip() in _all_toggle_texts()


def is_menu_toggle_close(text: str) -> bool:
    """True when the text is a «close menu» label in any language."""
    return (text or "").strip() in set(toggle_texts("close").values())


def feature_title_localized(conn, key: str, lang: str) -> str | None:
    """Localized title for a persistent-menu feature button.

    Persian ("fa") keeps the admin-managed DB title byte-identical
    (admins can rename buttons); other languages render the fixed
    ``ftitle_<key>`` chrome string. Custom (admin-added) features have no
    fixed translation and keep their DB title in every language.
    """
    if lang == "fa":
        return None
    title = t("ftitle_" + key, lang)
    if title == "ftitle_" + key:
        return None  # no fixed translation (custom feature) -> DB title
    return title


def menu_title_key(conn, text: str, lang: str = "fa") -> str | None:
    """Map a message text to a feature key when it matches a visible menu
    button title (handles admin renames).

    Both sides are stripped before comparing: a stored title with stray
    leading/trailing whitespace (e.g. the seeded ' 🎵 حرفه ای') must still
    match the stripped text that arrives when the button is tapped.
    Without this, the tap misses the lookup and falls through to a
    catalog search instead of opening the feature.

    In non-Persian UI languages the tap carries the LOCALIZED title, so
    those variants are matched too (fa keeps matching the DB title).
    """
    want = (text or "").strip()
    if not want:
        return None
    for f in capabilities_menu(conn):
        if (f.get("title") or "").strip() == want:
            return f["key"]
        if lang != "fa":
            loc = feature_title_localized(conn, f["key"], lang)
            if loc is not None and loc.strip() == want:
                return f["key"]
    return None


def menu_escape(handler):
    """Decorator for free-text state handlers: a main-menu button title
    always escapes the current flow instead of being consumed as flow
    input. The stale FSM state is cleared and the menu action runs, so
    the user can never be trapped in a dead-end step.

    The menu-collapse toggle («🔽 بستن منو» / «☰ منو») is intercepted here
    too, so it works from every screen: it only flips the keyboard and
    never clears the flow state (a collapse tap is a display change, not
    a navigation away from the flow).

    The ``handlers`` import is deferred to call time because
    ``app.bot.handlers`` imports the feature modules.
    """
    @functools.wraps(handler)
    async def wrapper(message, *args, **kwargs):
        conn = kwargs.get("conn")
        state = kwargs.get("state")
        text = (getattr(message, "text", None) or "").strip()
        if conn is not None and text:
            from . import handlers as _H
            if is_menu_toggle(text):
                # Pure display change: flip the keyboard, keep the flow.
                await _H._run_menu_toggle(
                    message, conn, kwargs.get("config"), text)
                return
            if state is not None:
                ulang = dbmod.user_lang_or_default(
                    conn, getattr(getattr(message, "from_user", None), "id", None))
                key = menu_title_key(conn, text, ulang)
                if key is not None:
                    try:
                        await state.clear()
                    except Exception:
                        pass
                    await _H._run_menu_title(
                        message, conn, kwargs.get("config"), state, key)
                    return
        return await handler(message, *args, **kwargs)
    return wrapper


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
    "search": "err_search",
    "identify": "err_identify",
    "file": "err_file",
    "lyrics": "err_lyrics",
    "voice": "err_voice",
    "translate": "err_translate",
    "suggest": "err_suggest",
    "not_found": "no_results",
}
# fa literals kept for any direct reference; rendering goes through t().
BRIEF_ERRORS_FA = {
    "search": "⚠️ جستجو با خطا مواجه شد. دوباره تلاش کنید.",
    "identify": "⚠️ شناسایی آهنگ با خطا مواجه شد. ویدیو را مستقیم بفرستید.",
    "file": "⚠️ ارسال فایل با خطا مواجه شد. دوباره تلاش کنید.",
    "lyrics": "⚠️ متن آهنگ با خطا مواجه شد. دوباره تلاش کنید.",
    "voice": "⚠️ ساخت ویس با خطا مواجه شد. دوباره تلاش کنید.",
    "translate": "⚠️ ترجمه با خطا مواجه شد. دوباره تلاش کنید.",
    "suggest": "⚠️ پیشنهاد آهنگ با خطا مواجه شد. دوباره تلاش کنید.",
    "not_found": "😕 نتیجه‌ای پیدا نشد. عبارت دیگری را امتحان کنید.",
}


def brief_error(kind: str, lang: str = "fa") -> str:
    """One short line for a failure: what failed + what to do.

    UI chrome only — never a traceback, never translated song content.
    """
    key = BRIEF_ERRORS.get(kind, BRIEF_ERRORS["search"])
    return t(key, lang)


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
    """Caption for the professional song card: clean title only.

    No source URLs and no bot-ID links under media (no-links policy);
    the unified media pipeline appends the honest quality label.
    ``bot_id`` is kept for signature compatibility and ignored.
    """
    return clean_title(result)


VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv")


def is_video_file_url(url: str) -> bool:
    """True only for a direct public video-file URL (not a platform page)."""
    path = (url or "").lower().split("?")[0]
    return path.endswith(VIDEO_EXTENSIONS)


def link_video_delivery(outcome, lang: str = "fa") -> tuple[str, str]:
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
                t("m_link_video_dl_head", lang).format(url=url))
    if outcome.kind in ("youtube", "instagram", "aparat", "video"):
        # Professional minimal: the official video itself, nothing else.
        # No lecture paragraph, no extra sections. Private/protected
        # content is still never bypassed (see media policy in code).
        return ("platform_official", t("m_link_video_head", lang) + url)
    return ("official_link", "")


def _normalise(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9\u0600-\u06ff]+", "", (text or "").lower())


def match_identified_song(results, title: str, artist: str):
    """Pick the catalog result that IS the identified song, or None.

    Version-aware acceptance (web parity): the card must carry the
    identity's version tokens (remix/live/…), add no extra distinctive
    title words, and match title+artist at >=0.99 token overlap. A
    merely similar catalog result is never returned as the identified
    song — the caller then uses the identified-but-no-file state.
    """
    for item in results:
        if identified_card_acceptable(title, artist, item.title, item.artist,
                                      getattr(item, "album", "") or ""):
            return item
    return None


# --- version-aware acceptance (exact-match only, no language bias) --------
import re as _re

_FA_CHAR_MAP = {
    "ا": "a", "آ": "a", "ب": "b", "پ": "p", "ت": "t", "ث": "s", "ج": "j",
    "چ": "ch", "ح": "h", "خ": "kh", "د": "d", "ذ": "z", "ر": "r", "ز": "z",
    "ژ": "zh", "س": "s", "ش": "sh", "ص": "s", "ض": "z", "ط": "t", "ظ": "z",
    "ع": "a", "غ": "gh", "ف": "f", "ق": "gh", "ک": "k", "گ": "g", "ل": "l",
    "م": "m", "ن": "n", "و": "o", "ه": "h", "ی": "i", "ي": "i", "ك": "k",
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4", "۵": "5",
    "۶": "6", "۷": "7", "۸": "8", "۹": "9",
    # Half-space (ZWNJ) treated as space for tokenization
    "‌": " ",
}

_PACKAGING_STOPWORDS = frozenset({
    "official", "video", "music", "audio", "lyric", "lyrics", "hd", "hq",
    "4k", "clip", "song", "track", "new", "2024", "2025", "2026",
})


def _skeleton_token(token: str) -> str:
    """Phonetic skeleton so Persian and Finglish spellings can meet."""
    out = []
    for ch in token.lower():
        if ch in _FA_CHAR_MAP:
            out.append(_FA_CHAR_MAP[ch])
        elif ch.isalnum():
            out.append(ch)
    return "".join(out)


def _distinctive_tokens(text: str) -> list[str]:
    toks = []
    for raw in _re.findall(r"[\w\u0600-\u06ff]+", (text or "").lower()):
        sk = _skeleton_token(raw)
        if len(sk) >= 2 and sk not in _PACKAGING_STOPWORDS:
            toks.append(sk)
    return toks


def token_overlap_ratio(want_text: str, cand_text: str) -> float:
    """Fraction of the wanted distinctive tokens found in the candidate."""
    want = _distinctive_tokens(want_text)
    if not want:
        return 0.0
    cand = set(_distinctive_tokens(cand_text))
    hit = sum(1 for t in want if t in cand)
    return hit / len(want)


def _strip_brackets(text: str) -> str:
    return _re.sub(r"[\(\[].*?[\)\]]", " ", text or "")


def identified_version_tokens(title: str) -> list[str]:
    """Version words are part of the identity: raw tokens minus the
    bracket-stripped (packaging) tokens. E.g. «Vay Az Dastet (Arash
    Mohseni Remix)» -> [arash, mohseni, remix]."""
    raw = set(_distinctive_tokens(title))
    clean = set(_distinctive_tokens(_strip_brackets(title)))
    return sorted(raw - clean)


def carries_version_tokens(title: str, hay_text: str) -> bool:
    """Every version token of the identity must appear in the candidate."""
    vt = identified_version_tokens(title)
    if not vt:
        return True
    hay = set(_distinctive_tokens(hay_text))
    return all(t in hay for t in vt)


def title_has_extra_tokens(want_title: str, card_title: str) -> bool:
    """The card's own title must carry no extra distinctive words —
    «Kheili Dooset Daram Ye Rooz» is not «Dooset Daram»."""
    want = set(_distinctive_tokens(want_title))
    version = set(identified_version_tokens(want_title))
    extra = [t for t in _distinctive_tokens(card_title)
             if t not in want and t not in version]
    return bool(extra)


def identified_card_acceptable(title: str, artist: str, card_title: str,
                               card_artist: str, card_album: str = "") -> bool:
    """Two-directional, field-aware acceptance of an identified card."""
    hay = f"{card_title} {card_album}"
    if not carries_version_tokens(title, hay):
        return False
    if title_has_extra_tokens(title, card_title):
        return False
    if token_overlap_ratio(title, hay) < 0.99:
        return False
    if (artist or "").strip() and token_overlap_ratio(artist, card_artist) < 0.99:
        return False
    return True


def format_user_line(user: dict) -> str:
    blocked = "⛔ مسدود" if user.get("blocked") else "✅ فعال"
    name = user.get("username") or user.get("first_name") or ""
    return f"• {user['telegram_id']} {name} — {blocked}"
