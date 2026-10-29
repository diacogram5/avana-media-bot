"""Exact lyrics + translation. No fabrication, ever.

Web parity: port-spec section 7 (lyrics card + 82 target languages).

Stage order (mirrors the web's getLyrics, additive — each stage runs
only when the earlier ones miss):
  A  LRCLIB exact-track GET, then LRCLIB search restricted to records
     that verify as this exact artist/title, then lyrics.ovh's exact
     artist/title endpoint.
  B  Packaging-stripped title variants ("(Remix)", "feat. …", " - Live")
     through the exact sources; single-field structured LRCLIB
     searches (track_name only, artist_name only); the swapped
     lyrics.ovh lookup. A variant counts only when the record still
     verifies as THIS track — a namesake's lyrics are never returned.
  C  Persian music sites' own «متن آهنگ» sections (public WordPress
     API), anchored to this track and extracted verbatim — never page
     boilerplate, never a reconstruction.

A confirmed miss (sources answered, none carries the text) returns
None; the caller must use lyrics_not_found_text(). An outage (no
source answered at all) is NOT "not found" — fetch_lyrics_with_source
reports it separately so the caller can say the sources are
unavailable. Nothing here writes or guesses a single lyric line.

Translation uses the free MyMemory API (no key). Text is chunked so
a long lyric is not silently truncated. If translation fails, or the
text is already in the target language, the caller gets None (and can
use same_language_notice()) and must say so briefly — never a
fabricated translation. The original lyric text is never modified;
translation is a separate message.
"""
from __future__ import annotations

import html
import re

import httpx

# ------------------------------------------------------------------ LANGS
# 82 destination languages, EXACT Persian labels and order from the spec.
LANG_NAMES = {
    "fa": "فارسی", "en": "انگلیسی", "ar": "عربی", "tr": "ترکی",
    "ur": "اردو", "az": "آذربایجانی", "ku": "کردی", "ps": "پشتو",
    "tg": "تاجیکی", "ru": "روسی", "uk": "اوکراینی", "de": "آلمانی",
    "fr": "فرانسوی", "es": "اسپانیایی", "it": "ایتالیایی", "pt": "پرتغالی",
    "nl": "هلندی", "sv": "سوئدی", "no": "نروژی", "da": "دانمارکی",
    "fi": "فنلاندی", "pl": "لهستانی", "cs": "چکی", "sk": "اسلواکی",
    "hu": "مجاری", "ro": "رومانیایی", "bg": "بلغاری", "el": "یونانی",
    "sr": "صربی", "hr": "کرواتی", "bs": "بوسنیایی", "mk": "مقدونی",
    "sq": "آلبانیایی", "sl": "اسلوونیایی", "et": "استونیایی",
    "lv": "لتونیایی", "lt": "لیتوانیایی", "be": "بلاروسی",
    "zh": "چینی", "ja": "ژاپنی", "ko": "کره‌ای", "hi": "هندی",
    "bn": "بنگالی", "ta": "تامیلی", "te": "تلوگو", "ml": "مالایالام",
    "mr": "مراتی", "gu": "گجراتی", "pa": "پنجابی", "ne": "نپالی",
    "si": "سینهالی", "th": "تایلندی", "vi": "ویتنامی",
    "id": "اندونزیایی", "ms": "مالایی", "tl": "فیلیپینی",
    "km": "خمر", "lo": "لائو", "my": "برمه‌ای", "sw": "سواحلی",
    "am": "امهری", "ha": "هوسا", "yo": "یوروبا", "ig": "ایگبو",
    "zu": "زولو", "af": "آفریکانس", "ka": "گرجی", "hy": "ارمنی",
    "he": "عبری", "uz": "ازبکی", "kk": "قزاقی", "ky": "قرقیزی",
    "mn": "مغولی", "is": "ایسلندی", "ga": "ایرلندی", "cy": "ولزی",
    "eu": "باسکی", "ca": "کاتالان", "gl": "گالیسی", "mt": "مالتی",
    "la": "لاتین", "eo": "اسپرانتو",
}
# MyMemory language codes (fa-IR / tr-TR style accepted, plain also works)
LANG_CODES = {**{k: k for k in LANG_NAMES}, "fa": "fa-IR", "tr": "tr-TR", "en": "en-GB", "ar": "ar-SA"}


def language_picker_rows(page: int = 0, key: str = "",
                         per_page: int = 8) -> tuple[list[list[dict]], int, int]:
    """Paginated language-picker rows for the 82 languages.

    Returns (rows, page, total_pages); each row is [{"text": <Persian
    label>, "code": <code>}] — one language per row. ``key`` is the
    integrator's result key (used in the callback data it builds:
    ``lyr:lang:{page}:{key}`` for paging, ``tl:{code}:{key}`` for a
    choice). Page is clamped into range.
    """
    codes = list(LANG_NAMES)
    total_pages = max(1, (len(codes) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = codes[page * per_page:(page + 1) * per_page]
    rows = [[{"text": LANG_NAMES[c], "code": c}] for c in chunk]
    return rows, page, total_pages


# ------------------------------------------------------------ NOT-FOUND TEXT
def lyrics_not_found_text(query: str) -> str:
    """Exact spec text: honest not-found for a lyrics request."""
    return (f"متن ترانه «{query}» در منابع متن آهنگ پیدا نشد. "
            f"نام خواننده و آهنگ را با هم بنویسید، مثلاً: «مهدی احمدوند - دوست دارم».")


# ------------------------------------------------------- SAME-LANGUAGE GUARD
SAME_LANGUAGE_MSG = "متن آهنگ از قبل به همین زبان است."

_PERSIAN_ONLY_LETTERS = frozenset("گچپژ")
_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")


def guess_script_lang(text: str) -> str | None:
    """Best-effort script-based language family guess.

    Returns "fa" for Arabic-script text carrying Persian-only letters
    (گ چ پ ژ), "ar" for other Arabic-script text, None when the script
    is Latin/unknown (cannot be told apart honestly).
    """
    letters = [ch for ch in (text or "") if ch.isalpha()]
    if not letters:
        return None
    arabic = sum(1 for ch in letters if _ARABIC_SCRIPT.match(ch))
    if arabic / len(letters) < 0.4:
        return None
    return "fa" if any(ch in _PERSIAN_ONLY_LETTERS for ch in letters) else "ar"


def same_language_notice(text: str, target_lang: str) -> str | None:
    """Return SAME_LANGUAGE_MSG when the text already looks like the
    target language (script-based guess); None otherwise. Call this
    BEFORE translate_text so the caller can say so honestly instead of
    "translating" into the same language."""
    if not text or not target_lang:
        return None
    base = LANG_CODES.get(target_lang, target_lang).split("-")[0].lower()
    if guess_script_lang(text) == base:
        return SAME_LANGUAGE_MSG
    return None


# --------------------------------------------------------------- NORMALIZE
def normalize_fa(text: str) -> str:
    """Normalize Persian/Arabic text for lyric matching."""
    t = (text or "").replace("‌", " ")
    t = re.sub("[\u064b-\u0652\u0670]", "", t)  # Arabic diacritics
    t = t.replace("ي", "ی").replace("ك", "ک").replace("ة", "ه")
    t = re.sub(r"[^\w\u0600-\u06ff]+", " ", t.lower())
    t = re.sub(r"\s+", " ", t).strip()
    # "ابر ها" -> "ابرها" variant is handled by the caller via variants;
    # here just collapse the standalone "ها" suffix marker.
    return t


def lyric_overlap(phrase: str, lyrics: str) -> float:
    """Fraction of phrase words present in the lyrics (0..1)."""
    words = [w for w in normalize_fa(phrase).split() if len(w) > 1]
    if not words:
        return 0.0
    lyric_words = set(normalize_fa(lyrics).split())
    # Also allow "ابرها" to match "ابر" + "ها" and vice versa.
    hit = 0
    for w in words:
        if w in lyric_words or any(lw.startswith(w) or w.startswith(lw) for lw in lyric_words if len(lw) > 2):
            hit += 1
    return hit / len(words)


def lyric_variants(phrase: str) -> list[str]:
    """Persian spelling variants of a lyric phrase, in order."""
    q = " ".join((phrase or "").split())
    variants = [q] if q else []
    for v in (
        q.replace(" ها ", "ها ").replace(" ها", "ها"),
        q.replace(" ها ", "ا ").replace(" ها", "ا"),  # colloquial: ابر ها -> ابرا
        q.replace("‌", " "),
        " ".join(q.split()[:4]),
        " ".join(q.split()[:5]),
        " ".join(q.split()[-4:]),
    ):
        if v and v not in variants:
            variants.append(v)
    return variants


# --------------------------------------------------------- EXACT VERIFICATION
def _match_tokens(s: str) -> list[str]:
    return [w for w in normalize_fa(s).split() if len(w) > 1]


def lyrics_token_overlap(wanted: str, candidate: str) -> float:
    """Fraction of wanted tokens present in the candidate (0..1)."""
    toks = _match_tokens(wanted)
    if not toks:
        return 1.0
    hay = set(_match_tokens(candidate))
    hit = sum(1 for t in toks if t in hay)
    return hit / len(toks)


def lyrics_exact_match(wanted_title: str, wanted_artist: str,
                       hit_title: str, hit_artist: str) -> bool:
    """A candidate hit counts as the exact track only when every
    distinctive token of the wanted title appears in its title AND every
    token of the wanted artist appears in its artist. Anything less is
    rejected — the caller reports an honest not-found instead."""
    if not hit_title:
        return False
    if lyrics_token_overlap(wanted_title, hit_title) < 0.99:
        return False
    if wanted_artist and wanted_artist.strip():
        if lyrics_token_overlap(wanted_artist, hit_artist) < 0.99:
            return False
    return True


def lyrics_title_variants(title: str) -> list[str]:
    """Packaging-stripped title variants of the SAME track for stage B."""
    out: list[str] = []

    def push(s: str) -> None:
        t = re.sub(r"\s{2,}", " ", (s or "")).strip()
        if t and t not in out:
            out.append(t)

    push(title)
    no_paren = re.sub(r"\[[^\]]*\]", "", re.sub(r"\([^)]*\)", "", title))
    push(no_paren)
    push(re.sub(r"\s+(feat\.?|ft\.?)\s+.*$", "", title, flags=re.IGNORECASE))
    push(re.sub(r"\s+[-–—]\s+(remix|live|acoustic|version|extended|radio|edit|unplugged)\b.*$",
                "", title, flags=re.IGNORECASE))
    return out


# ------------------------------------------------------------- STAGE SOURCES
_LYRICS_UA = {"User-Agent": "AvanoMediaBot/1.0 (lyrics)"}


def parse_lrclib_payload(payload) -> str | None:
    if not payload:
        return None
    item = payload[0] if isinstance(payload, list) else payload
    if not isinstance(item, dict):
        return None
    if item.get("instrumental"):
        return None
    lyrics = (item.get("plainLyrics") or item.get("syncedLyrics") or "").strip()
    return lyrics or None


def lrclib_records(payload) -> list[dict]:
    """LRCLIB records with usable plain lyrics (non-instrumental)."""
    if not payload:
        return []
    items = payload if isinstance(payload, list) else [payload]
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or item.get("instrumental"):
            continue
        lyrics = (item.get("plainLyrics") or "").strip()
        if len(lyrics) < 20:
            continue
        out.append({"title": item.get("trackName") or "",
                    "artist": item.get("artistName") or "",
                    "lyrics": lyrics})
    return out


async def _lrclib_exact_get(artist: str, title: str,
                            client: httpx.AsyncClient) -> tuple[str | None, str, bool]:
    """LRCLIB /api/get: one record for exactly this artist+title, or 404."""
    try:
        resp = await client.get(
            "https://lrclib.net/api/get",
            params={"artist_name": artist, "track_name": title},
            headers=_LYRICS_UA, timeout=12)
    except Exception:
        return None, "", False
    if resp.status_code == 404:
        return None, "", True
    try:
        resp.raise_for_status()
    except Exception:
        return None, "", False
    for rec in lrclib_records(resp.json()):
        if lyrics_exact_match(title, artist, rec["title"], rec["artist"]):
            return rec["lyrics"], "LRCLIB", True
    return None, "", True


async def _lrclib_search_exact(params: dict, wanted_title: str, wanted_artist: str,
                               client: httpx.AsyncClient) -> tuple[str | None, str, bool]:
    """LRCLIB /api/search, keeping ONLY records that verify as the wanted
    track. A near or namesake record is dropped, never returned."""
    try:
        resp = await client.get("https://lrclib.net/api/search", params=params,
                                headers=_LYRICS_UA, timeout=12)
        resp.raise_for_status()
    except Exception:
        return None, "", False
    for rec in lrclib_records(resp.json()):
        if lyrics_exact_match(wanted_title, wanted_artist, rec["title"], rec["artist"]):
            return rec["lyrics"], "LRCLIB", True
    return None, "", True


async def _lyrics_ovh(artist: str, title: str,
                      client: httpx.AsyncClient) -> tuple[str | None, str, bool]:
    """lyrics.ovh endpoint is itself an exact artist/title lookup."""
    if not artist or not title:
        return None, "", False
    try:
        resp = await client.get(
            f"https://api.lyrics.ovh/v1/{artist}/{title}",
            headers=_LYRICS_UA, timeout=12)
    except Exception:
        return None, "", False
    if resp.status_code != 200:
        return None, "", True
    lyrics = ((resp.json() or {}).get("lyrics") or "").strip()
    if len(lyrics) >= 20:
        return lyrics, "lyrics.ovh", True
    return None, "", True


def extract_lyrics_section(content_html: str) -> str | None:
    """Extract the lyric body from a music post's rendered content: the
    lines after its «متن آهنگ» marker, decorations (●♪♫, separators)
    and credit lines (شعر/ملودی/تنظیم) removed, stopping at the
    section's closing separator or at download boilerplate. Returns
    None when there is no real lyric section — never a reconstruction."""
    text = re.sub(r"<br\s*/?>", "\n", content_html or "", flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6]|blockquote|section|article)>", "\n",
                  text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [html.unescape(l).replace(" ", " ").strip() for l in text.split("\n")]
    marker = next((i for i, l in enumerate(lines) if "متن آهنگ" in l), -1)
    if marker < 0:
        return None
    credit = re.compile(r"^(شعر|ملودی|تنظیم|آهنگساز|ترانه‌سرا|ترانه سرا|خواننده)\s*[:：]")
    out: list[str] = []
    for raw in lines[marker + 1:]:
        bare = re.sub(r"[●♪♫♩♬♭─┤├\s]", "", raw)
        if not bare:
            # A pure separator line closes the section once lyrics began.
            if "─" in raw and out:
                break
            continue
        line = re.sub(r"[●♪♫♩♬♭]", "", raw)
        line = re.sub(r"[─┤├]", "", line).strip()
        if not line:
            continue
        if "دانلود آهنگ" in line or "http" in line:
            if out:
                break
            continue
        if credit.match(line):
            continue
        out.append(line)
        if len(out) >= 90:
            break
    lyrics = "\n".join(out)[:6000]
    if len(out) < 4 or len(lyrics) < 120:
        return None
    return lyrics


# Persian music sites whose posts carry a «متن آهنگ» section (web parity).
MUSIC_SITES = (
    "https://musics-fa.com",
    "https://minevisam.ir",
    "https://musicviral.ir",
)


async def _stage_c_music_sites(artist: str, title: str,
                               client: httpx.AsyncClient) -> tuple[str | None, str, bool]:
    """Stage C: music sites' own «متن آهنگ» sections. Returns
    (lyrics|None, source_name, responded). A post counts only when its
    own title anchors to the wanted artist/title (>=0.5 token overlap)."""
    responded = False
    queries = [q for q in (f"{artist} {title}".strip(), title, artist) if len(q) >= 2]
    for q in queries:
        for site in MUSIC_SITES:
            try:
                resp = await client.get(
                    f"{site}/wp-json/wp/v2/search",
                    params={"search": q, "per_page": 5},
                    headers=_LYRICS_UA, timeout=12)
                if resp.status_code == 200:
                    responded = True
                    data = resp.json()
                else:
                    continue
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            posts = []
            for p in data:
                if not isinstance(p, dict):
                    continue
                pid, ptitle = p.get("id"), p.get("title") or ""
                if not isinstance(pid, int) or not isinstance(ptitle, str):
                    continue
                if "آهنگ" not in ptitle or "بهترین" in ptitle:
                    continue
                posts.append((pid, ptitle))
            for pid, ptitle in posts[:3]:
                if not (lyrics_token_overlap(artist, ptitle) >= 0.5
                        or lyrics_token_overlap(title, ptitle) >= 0.5):
                    continue
                try:
                    pr = await client.get(
                        f"{site}/wp-json/wp/v2/posts/{pid}",
                        params={"_fields": "title,content"},
                        headers=_LYRICS_UA, timeout=12)
                    if pr.status_code != 200:
                        continue
                    content = ((pr.json() or {}).get("content") or {}).get("rendered")
                except Exception:
                    continue
                if not isinstance(content, str):
                    continue
                lyrics = extract_lyrics_section(content)
                if lyrics:
                    return lyrics, site.replace("https://", ""), responded
    return None, "", responded


async def _fetch_all_stages(title: str, artist: str,
                            client: httpx.AsyncClient) -> tuple[str | None, str, bool]:
    """Run stages A→B→C. Returns (lyrics|None, source_name, responded).
    ``responded`` is True when at least one source actually answered —
    False means an outage, NOT "not found"."""
    responded = False

    async def run(coro) -> tuple[str | None, str, bool] | None:
        nonlocal responded
        lyrics, source, answered = await coro
        responded = responded or answered
        return (lyrics, source, responded) if lyrics else None

    # Stage A — the exact sources, exact-verified.
    for make in (
        lambda: _lrclib_exact_get(artist, title, client),
        lambda: _lrclib_search_exact({"track_name": title, "artist_name": artist} if artist
                                     else {"track_name": title}, title, artist, client),
        lambda: _lyrics_ovh(artist, title, client),
    ):
        got = await run(make())
        if got:
            return got
    # Stage B — alternate spellings/forms of the SAME track.
    for variant in lyrics_title_variants(title)[1:]:
        for make in (lambda: _lrclib_exact_get(artist, variant, client),
                     lambda: _lyrics_ovh(artist, variant, client)):
            got = await run(make())
            if got:
                return got
    for params in ({"track_name": title},
                   {"artist_name": artist} if artist else None):
        if params is None:
            continue
        got = await run(_lrclib_search_exact(params, title, artist, client))
        if got:
            return got
    # Swapped ovh lookup: ovh/v1/{title}/{artist}.
    got = await run(_lyrics_ovh(title, artist, client))
    if got:
        return got
    # Stage C — the music sites' «متن آهنگ» sections.
    lyrics, source, answered = await _stage_c_music_sites(artist, title, client)
    responded = responded or answered
    return lyrics, source, responded


async def fetch_lyrics_with_source(title: str, artist: str,
                                   client: httpx.AsyncClient | None = None
                                   ) -> tuple[str | None, str, bool]:
    """(lyrics|None, source_name, responded). ``responded`` is True when at
    least one source answered — False means an outage, NOT "not found".
    The lyrics text is always the source's original language, unchanged."""
    if not title:
        return None, "", False
    if client is not None:
        return await _fetch_all_stages(title, artist, client)
    async with httpx.AsyncClient(timeout=15) as c:
        return await _fetch_all_stages(title, artist, c)


async def fetch_lyrics(title: str, artist: str,
                       client: httpx.AsyncClient | None = None) -> str | None:
    """Exact-match lyrics only, original language, unchanged.
    Returns None when not found (use lyrics_not_found_text())."""
    lyrics, _source, _responded = await fetch_lyrics_with_source(title, artist, client=client)
    return lyrics


# --------------------------------------------------------------- TRANSLATION
MAX_TRANSLATE_CHARS = 3500
# MyMemory free GET has a small per-request size limit; keep each
# request well under it.
_MYMEMORY_CHUNK = 450


def chunk_text(text: str, limit: int = MAX_TRANSLATE_CHARS) -> list[str]:
    """Split text into chunks of at most ``limit`` chars, preferring
    line boundaries, then word boundaries (hard split as last resort)."""
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.splitlines():
        piece = line
        while len(piece) > limit:
            cut = piece.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(piece[:cut].rstrip())
            piece = piece[cut:].lstrip()
        if cur and len(cur) + 1 + len(piece) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = (cur + "\n" + piece) if cur else piece
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c]


async def translate_text(text: str, target_lang: str, source_lang: str = "auto",
                         client: httpx.AsyncClient | None = None) -> str | None:
    """Translate text to target_lang (e.g. 'fa'). None on any failure,
    or when the text is already in the target language (check
    same_language_notice() first for the honest message). Long texts
    are translated in chunks and joined. The original text is never
    modified."""
    if not text or not target_lang:
        return None
    target = LANG_CODES.get(target_lang, target_lang)
    langpair = f"{source_lang}|{target}"
    out: list[str] = []
    try:
        for chunk in chunk_text(text, _MYMEMORY_CHUNK):
            if client is not None:
                resp = await client.get(
                    "https://api.mymemory.translated.net/get",
                    params={"q": chunk, "langpair": langpair}, timeout=20)
                resp.raise_for_status()
                data = resp.json()
            else:
                async with httpx.AsyncClient(timeout=20) as c:
                    resp = await c.get(
                        "https://api.mymemory.translated.net/get",
                        params={"q": chunk, "langpair": langpair}, timeout=20)
                    resp.raise_for_status()
                    data = resp.json()
            translated = ((data.get("responseData") or {}).get("translatedText") or "").strip()
            if not translated or translated.upper().startswith("QUERY LENGTH LIMIT"):
                return None
            out.append(translated)
    except Exception:
        return None
    return "\n".join(out) or None
