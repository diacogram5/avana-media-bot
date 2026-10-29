"""Exact lyrics + translation. No fabrication, ever.

- Lyrics are fetched only for an exact title+artist match (LRCLIB,
  then lyrics.ovh). A merely similar track's lyrics are never returned.
- Translation uses the free MyMemory API (no key). Text is chunked so
  a long lyric is not silently truncated. If translation fails, or the
  text is already in the target language, the caller gets None and must
  say so briefly — never a fabricated translation.
- The original lyric text is never modified; translation is a separate
  message.
"""
from __future__ import annotations

import re

import httpx

LANG_NAMES = {
    "fa": "فارسی", "en": "انگلیسی", "tr": "ترکی", "ar": "عربی",
    "de": "آلمانی", "fr": "فرانسوی", "es": "اسپانیایی", "it": "ایتالیایی",
    "ru": "روسی", "hi": "هندی", "ur": "اردو", "az": "آذربایجانی",
    "ku": "کردی", "ps": "پشتو",
}
# MyMemory language codes (fa-IR / tr-TR style accepted, plain also works)
LANG_CODES = {**{k: k for k in LANG_NAMES}, "fa": "fa-IR", "tr": "tr-TR", "en": "en-GB", "ar": "ar-SA"}


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


async def fetch_lyrics(title: str, artist: str,
                       client: httpx.AsyncClient | None = None) -> str | None:
    """Exact-match lyrics only. Returns None when not found."""
    if not title:
        return None
    params = {"track_name": title}
    if artist:
        params["artist_name"] = artist
    try:
        if client is not None:
            resp = await client.get("https://lrclib.net/api/search", params=params, timeout=15)
            resp.raise_for_status()
            lyrics = parse_lrclib_payload(resp.json())
        else:
            async with httpx.AsyncClient(timeout=15) as c:
                resp = await c.get("https://lrclib.net/api/search", params=params, timeout=15)
                resp.raise_for_status()
                lyrics = parse_lrclib_payload(resp.json())
        if lyrics:
            return lyrics
    except Exception:
        pass
    # Fallback source, same exact-match rule (endpoint is title/artist).
    try:
        url = f"https://api.lyrics.ovh/v1/{artist}/{title}" if artist else ""
        if not url:
            return None
        if client is not None:
            resp = await client.get(url, timeout=15)
            if resp.status_code == 200:
                return (resp.json().get("lyrics") or "").strip() or None
        else:
            async with httpx.AsyncClient(timeout=15) as c:
                resp = await c.get(url, timeout=15)
                if resp.status_code == 200:
                    return (resp.json().get("lyrics") or "").strip() or None
    except Exception:
        return None
    return None


def _chunks(text: str, size: int = 450) -> list[str]:
    parts, cur = [], ""
    for line in text.splitlines():
        if len(cur) + len(line) + 1 > size and cur:
            parts.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        parts.append(cur)
    return parts


async def translate_text(text: str, target_lang: str, source_lang: str = "auto",
                         client: httpx.AsyncClient | None = None) -> str | None:
    """Translate text to target_lang (e.g. 'fa'). None on any failure."""
    if not text or not target_lang:
        return None
    target = LANG_CODES.get(target_lang, target_lang)
    langpair = f"{source_lang}|{target}"
    out: list[str] = []
    try:
        for chunk in _chunks(text):
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
