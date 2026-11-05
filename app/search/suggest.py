"""Smart AI song suggestion by mood, in plain Persian.

The user writes a mood sentence; the bot maps it to real catalog
search terms and returns ONLY real catalog results. No song is ever
invented, and a mood with no catalog result returns [].
"""
from __future__ import annotations

# mood key -> (keywords, catalog search terms)
# Term order (fix 2026-11-05): the PERSIAN term runs FIRST — the audience
# is Persian, and the first term with real results wins, so an English-first
# list filled the suggestions with Western songs.
MOODS = {
    "sad_night": (["غمگین", "غم", "sad", "شب", "night", "دلتنگ"],
                  ["غمگین", "sad song", "night sad"]),
    "happy": (["شاد", "happy", "انرژی", "پرانرژی", "رقص"],
              ["شاد", "happy song", "dance"]),
    "gym": (["باشگاه", "ورزش", "gym", "workout", "تمرین"],
            ["باشگاه", "workout", "gym music"]),
    "driving": (["رانندگی", "ماشین", "driving", "سفر", "جاده"],
                ["رانندگی", "driving song", "road trip"]),
    "bass": (["بیس", "bass", "سیستم", "بم", "قدرتمند"],
             ["بیس دار", "bass", "bass boosted"]),
    "calm": (["آرام", "calm", "ریلکس", "آرامش", "خواب"],
             ["آرام", "calm song", "relaxing"]),
    "love": (["عاشقانه", "عشق", "love", "رومانتیک"],
             ["عاشقانه", "love song", "romantic"]),
}


def detect_mood(text: str) -> str | None:
    t = (text or "").lower()
    for key, (keywords, _terms) in MOODS.items():
        if any(k in t for k in keywords):
            return key
    return None


def is_mood_request(text: str) -> bool:
    """A plain mood sentence, not a song title/artist search."""
    t = (text or "")
    if not any(w in t for w in ("می‌خوام", "میخوام", "می‌خواهم", "بده", "پیشنهاد", "want")):
        return False
    return detect_mood(t) is not None


async def suggest_songs(text: str, catalog_search) -> tuple[str | None, list]:
    """Return (mood_key, real catalog results) or (None, [])."""
    mood = detect_mood(text)
    if mood is None:
        return None, []
    _keywords, terms = MOODS[mood]
    seen: set[str] = set()
    out: list = []
    for term in terms:
        try:
            results = await catalog_search(term)
        except Exception:
            continue
        for r in results or []:
            key = f"{r.title.lower()}|{r.artist.lower()}"
            if key not in seen:
                seen.add(key)
                out.append(r)
        if out:
            break  # first term with real results wins: saves API calls
    return mood, out[:8]
