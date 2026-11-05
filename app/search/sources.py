"""Default source registrations: every EXISTING search source under its
proper registry kind, plus new sources proven live by this round's probes.

Adding another source later needs no core change — just one more
register() call here (or anywhere).

Probes 2026-10-29 (this round, live egress via proxy):
  PROVEN:
    - MusicBrainz recording search: real JSON for artist "Ebi" —
      title "Nazi Naz Kon", artist-credit "Ebi" (disambiguation
      "Iranian singer"), length 316626 ms. kind=info.
    - Deezer /artist/{id}/related: real related-artist JSON (e.g. for
      Daft Punk id 27 -> Justice etc.). kind=similar.
  REJECTED (recorded as status=dead):
    - YouTube downloading: no yt-dlp, datacenter IP blocks (standing).
    - SoundCloud: needs client_id (standing).
    - Genius / Last.fm similar: key-gated.
    - musix.ir, music-rokni.ir, musicfa.xyz, musixfa.com: DNS/connect
      failures — dead.
    - iranmusics.com: /wp-json -> 404, ?rest_route= returns HTML — no WP API.
    - pop-music.ir: flaky (200 [] then timeouts) — unreliable.
    - musicsbaran.ir: WP search returns real song posts, but downloads
      are login-walled — rejected as a file source, not added as info
      (duplicates existing WP search info).
"""
from __future__ import annotations

import httpx

from . import SearchResult
from .audio_match import find_match
from .channel import search_channels
from .deezer import DeezerProvider
from .fullsong import MUSIC_SITES, find_full_song_file
from .itunes import ITunesProvider
from .lyrics import fetch_lyrics_with_source, translate_text
from .registry import STATUS_DEAD, STATUS_PROVEN, STATUS_UNVERIFIED, register
from .suggest import detect_mood, MOODS

_UA = {"User-Agent": "Mozilla/5.0 (compatible; AvanoMediaBot/1.0)"}


# ------------------------------------------------------------ info adapters

async def _itunes_search(query: str, limit: int = 10) -> list[SearchResult]:
    return await ITunesProvider().search(query, limit=limit)


async def _deezer_search(query: str, limit: int = 10) -> list[SearchResult]:
    return await DeezerProvider().search(query, limit=limit)


_MB_API = "https://musicbrainz.org/ws/2/recording/"


async def musicbrainz_search(query: str, limit: int = 10) -> list[SearchResult]:
    """Keyless recording metadata (title/artist/length). Info ONLY —
    MusicBrainz serves no audio files."""
    params = {"query": query, "fmt": "json", "limit": max(1, min(limit, 25))}
    async with httpx.AsyncClient(timeout=15, headers=_UA) as client:
        resp = await client.get(_MB_API, params=params)
        resp.raise_for_status()
        payload = resp.json()
    out: list[SearchResult] = []
    for rec in (payload.get("recordings") or [])[:limit]:
        title = rec.get("title") or ""
        if not title:
            continue
        credit = rec.get("artist-credit") or []
        artist = " & ".join(a.get("name", "") for a in credit if isinstance(a, dict)) or ""
        length_ms = rec.get("length")
        out.append(SearchResult(
            source="musicbrainz", id=rec.get("id", ""),
            title=title, artist=artist,
            duration=int(length_ms // 1000) if length_ms else None,
            url=f"https://musicbrainz.org/recording/{rec.get('id', '')}",
            kind="music"))
    return out


async def _channel_posts(query: str, channels: list[str] | None = None,
                         limit: int = 10) -> list[SearchResult]:
    posts = await search_channels(channels or [], query)
    out: list[SearchResult] = []
    for p in posts[:limit]:
        out.append(SearchResult(
            source="channel", id=p.get("post_url", ""),
            title=(p.get("text") or "")[:120] or f"پست کانال {p.get('channel', '')}",
            artist=p.get("channel", ""), url=p.get("post_url", ""),
            kind="music"))
    return out


# -------------------------------------------------------- identify adapters

def _audio_content_match(data: bytes, stored: list[dict] | None = None) -> dict | None:
    """Byte/chunk match against files the bot has actually seen.
    Returns the match record or None — labelled 'channel/file match',
    never a catalog identification."""
    return find_match(stored or [], data or b"")


# ------------------------------------------------------------ file adapters
# Each of the 4 proven WP music sites is its own file source, calling the
# same find_full_song_file() pipeline (all honesty gates unchanged) with
# only that site enabled.

def _file_source(base: str, style: str):
    async def fetch(artist: str, title: str) -> dict | None:
        return await find_full_song_file(artist, title, sites=((base, style),))
    fetch.__name__ = f"wp_file_{base.replace('https://', '').replace('.', '_')}"
    return fetch


# ---------------------------------------------------------- similar adapters

async def _mood_suggest(text: str, limit: int = 10) -> list[SearchResult]:
    """Mood -> real catalog search TERMS. Info-level suggestions, never files."""
    mood = detect_mood(text or "")
    if not mood:
        return []
    _keywords, terms = MOODS[mood]
    return [SearchResult(source="mood_suggest", id=t, title=t,
                         artist=f"حال‌وهوا: {mood}", kind="music")
            for t in terms[:limit]]


_DEEZER_BASE = "https://api.deezer.com"


async def deezer_related_artists(artist: str, limit: int = 10) -> list[SearchResult]:
    """Keyless similar-artist recommendations via Deezer's related-artists
    endpoint. Returns artist names + links — info only, never files."""
    async with httpx.AsyncClient(timeout=15, headers=_UA) as client:
        resp = await client.get(f"{_DEEZER_BASE}/search/artist",
                                params={"q": artist}, timeout=15)
        resp.raise_for_status()
        data = (resp.json().get("data") or [])
        if not data or not data[0].get("id"):
            return []
        aid = data[0]["id"]
        r2 = await client.get(f"{_DEEZER_BASE}/artist/{aid}/related", timeout=15)
        r2.raise_for_status()
        related = r2.json().get("data") or []
    out: list[SearchResult] = []
    for a in related[:limit]:
        name = a.get("name") or ""
        if not name:
            continue
        out.append(SearchResult(
            source="deezer_related", id=str(a.get("id", "")),
            title=name, artist=f"مشابه {artist}",
            thumbnail=(a.get("picture_medium") or ""),
            url=a.get("link", ""), kind="music"))
    return out


# ----------------------------------------------------------- lyrics adapters

async def _lyrics_pipeline(title: str, artist: str) -> dict | None:
    """Existing staged pipeline (LRCLIB -> lyrics.ovh -> Persian music
    sites), verbatim original-language text. Returns {"text", "source"}
    or None; outage is reported via "responded", never as not-found."""
    text, src, _responded = await fetch_lyrics_with_source(title, artist)
    if not text:
        return None
    return {"text": text, "source": src}


async def _mymemory_translate(text: str, target_lang: str) -> dict | None:
    out = await translate_text(text or "", target_lang or "fa")
    if not out:
        return None
    return {"text": out, "source": "mymemory"}


# ------------------------------------------------------------ registration

def register_defaults() -> None:
    """Register every existing source + this round's proven additions.
    Idempotent — re-registering replaces the same names."""
    # info: catalog text/metadata search (never files)
    register("itunes", "info", _itunes_search,
             priority=10, timeout=15, status=STATUS_PROVEN,
             note="iTunes Search API, keyless. Previews/metadata only.")
    register("deezer", "info", _deezer_search,
             priority=20, timeout=15, status=STATUS_PROVEN,
             note="Deezer search API, keyless. Previews/metadata only.")
    register("musicbrainz", "info", musicbrainz_search,
             priority=30, timeout=15, status=STATUS_PROVEN,
             note="Proven live 2026-10-29: recording search returns real "
                  "title/artist/length incl. Iranian artists. Metadata only.")
    register("channel_posts", "info", _channel_posts,
             priority=40, timeout=20, status=STATUS_UNVERIFIED,
             note="Public t.me channel post search; needs configured channels.")

    # identify: audio content matching
    register("audio_match", "identify", _audio_content_match,
             priority=10, timeout=10, status=STATUS_UNVERIFIED,
             note="Byte/chunk match vs files the bot has seen. "
                  "Labelled 'channel/file match', never a catalog ID.")

    # file: the 4 proven WP music sites, each individually addressable.
    # Same pipeline + honesty gates as the bundle; the bundle (default
    # find_full_song_file call) keeps working identically.
    for i, (base, style) in enumerate(MUSIC_SITES):
        register(f"wp:{base.replace('https://', '')}", "file",
                 _file_source(base, style),
                 priority=10 * (i + 1), timeout=60, status=STATUS_PROVEN,
                 note=f"Proven WP music site ({style} API style). "
                      "Full honesty gates: version gate, magic bytes, >=85s.")

    # similar
    register("mood_suggest", "similar", _mood_suggest,
             priority=10, timeout=10, status=STATUS_UNVERIFIED,
             note="Mood -> catalog search terms. Suggestions only, never files.")
    register("deezer_related", "similar", deezer_related_artists,
             priority=20, timeout=20, status=STATUS_PROVEN,
             note="Proven live 2026-10-29: /artist/{id}/related returns real "
                  "similar artists, keyless. Info only.")

    # lyrics
    register("lyrics_pipeline", "lyrics", _lyrics_pipeline,
             priority=10, timeout=30, status=STATUS_PROVEN,
             note="Existing staged pipeline: LRCLIB -> lyrics.ovh -> Persian "
                  "music sites. Verbatim original text, no fabrication.")
    register("mymemory_translate", "lyrics", _mymemory_translate,
             priority=20, timeout=30, status=STATUS_PROVEN,
             note="Existing MyMemory translation path (no key).")

    # rejected candidates — audit trail only, never run
    for name, why in (
        ("youtube_download", "no yt-dlp; datacenter IP blocks"),
        ("soundcloud", "needs client_id"),
        ("genius", "key-gated"),
        ("lastfm_similar", "key-gated"),
        ("wp:musix.ir", "dead: DNS/connect failure 2026-10-29"),
        ("wp:music-rokni.ir", "dead: connect failure 2026-10-29"),
        ("wp:musicfa.xyz", "dead: connect failure 2026-10-29"),
        ("wp:musixfa.com", "dead: DNS failure 2026-10-29"),
        ("wp:iranmusics.com", "dead: /wp-json 404, rest_route returns HTML"),
        ("wp:pop-music.ir", "dead: flaky 200 [] then timeouts"),
        ("wp:musicsbaran.ir", "rejected as file source: downloads login-walled"),
    ):
        register(name, "file" if name.startswith(("wp:", "youtube", "soundcloud")) else "similar",
                 lambda *a, **k: None, status=STATUS_DEAD, enabled=False,
                 note=f"REJECTED: {why}")


register_defaults()
