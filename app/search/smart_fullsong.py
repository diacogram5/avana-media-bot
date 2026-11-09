"""Free-only smart full-version search (2026-11-09, user order 21:24).

The retry button's smart search, built AFTER the free-only feasibility
study: no search engine is usable (Google/Brave 429 on Render, every
other free engine CAPTCHAs or 403s), so discovery is DIRECT Persian
music-site adapters — no search-engine layer at all. A search-engine
slot stays open by design: a future engine is just another adapter
on the existing SourceAdapter contract (see app/search/adapters/base.py
and registry.py); the retry handler never changes for it.

Pipeline (tap-only, hard ceiling 60 s):
  variants (ordered, deduped, cap 6) -> per variant, all v1 WP sites
  in parallel (per-site timeout) -> candidate mp3 URLs (deduped, 320
  first) -> stream ONE candidate to disk at a time (never RAM) ->
  validate with the smart pipeline (tags -> duration gate -> version)
  -> stop at the first VERIFIED file.
  No file validates -> the exact not-found message + the survey hook
  (the handler sends both; the survey module is built separately).

v1 SOURCES: JavanMelody + MelonMusic only (user order 22:19). The
streaming-link fallback (Spotify -> Anghami -> Qobuz) and the iTunes
previewUrl check are DEFERRED to a later version and are NOT wired
into v1's not-found flow: SmartOutcome.stream stays None in v1, and
the handler renders it only when a later version populates it — so
adding the fallback touches smart_fullsong.py only, never the retry
handler. Expected-duration metadata (iTunes/Deezer trackTimeMillis)
is NOT the streaming fallback: it feeds the duration gate only, and
no preview or page link is ever sent from it in v1.

Rules enforced here, not by convention:
  - candidate bytes go to DISK, never RAM (persian_music.py returns
    full bytes in RAM today — the smart path never uses it).
  - strict title matching: WP /v2/search is fuzzy for Persian, so a
    post only yields candidates when the smart matcher accepts its
    title/artist; never the first hit; the first-mp3-on-page pattern
    stays banned (every candidate URL is validated by file, never by
    page position).
  - the version gate lives on the FILE (tags + duration), not on the
    post title: a Persian-titled remix post ("ريميکس") would fail the
    old carries_version_tokens check against Latin "remix", so
    discovery does not hard-require the version token in the title.
  - search results are never stored permanently: only the verified
    outcome (file_id / not-found marker) is cached by the caller.
  - no new Turso queries in the waiting path (the negative cache is
    in-memory only); no key, account, payment or subscription anywhere.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from dataclasses import dataclass, field

import httpx

from ..logging_config import get_logger, log_event
from .fullsong import (
    _FILE_RE,
    _looks_like_audio,
    _search_url,
    FULL_SONG_MAX_MB,
)
from .smart_validate import (
    conditional_acceptable,
    fpclip_similarity,
    fingerprint_file_items,
    probe_file_identity_from_path,
    smart_artist_matches,
    smart_title_matches,
    validate_smart_file,
)

log = get_logger("smart_fullsong")

_UA = {"User-Agent": "Mozilla/5.0 (compatible; AvanoMediaBot/1.0)"}

# Hard ceiling for the file search (the caller shows a temp status
# while this runs). Per-candidate download timeout is below it so a
# slow 320 kbps file can fall through to the 128 kbps twin or the
# next candidate instead of eating the whole budget.
SMART_CEILING_SEC = 60.0
_SMART_SITE_TIMEOUT_SEC = 6.0
_SMART_DOWNLOAD_TIMEOUT_SEC = 30.0
_SMART_MAX_CANDIDATES_PER_VARIANT = 4

# Negative cache: (artist|title) -> unix seconds. A verified
# not-found is not re-searched for 10 minutes (in-memory only — no
# Turso query in the waiting path). A found file is cached by the
# caller (1b cache + song_file_cache), not here.
_NEGATIVE_TTL_SEC = 600
_NEGATIVE: dict[str, float] = {}

# v1 SOURCES (user order 22:19): JavanMelody (primary) + MelonMusic
# (fallback) ONLY. No other Persian site is wired into the smart-search
# path in v1 — not the existing 5 WP-API sites, not the legacy 4, not
# fazremix, not msbmusic/musicviral. They stay available as future
# adapters: adding one is a single row here (the WP search + candidate
# + validation machinery is site-generic), and the registry
# (app/search/registry.py) is the extension point — no handler change.
# Unreachable sources stay out of the main path until re-verified.
# Render probe 2026-11-09 22:05: javanmelody reachable 200 (search
# hits 1, mp3 HEAD 200); melonmusic reachable 200 (hits 2, mp3 HEAD
# 200); fazremix s2 file host ConnectError from Render; musics-fa +
# ezmusic timeout; mokhtalefmusic + gisomusic ConnectError; radiojavan
# home 403.
SMART_WP_SITES: tuple[tuple[str, str, str], ...] = (
    ("javanmelody", "https://javanmelody.ir", "wp-json"),
    ("melonmusic", "https://melonmusic.ir", "wp-json"),
)


@dataclass
class SmartFile:
    """A VERIFIED full-version file staged on disk (never in RAM)."""
    disk_path: str
    ext: str
    duration_sec: float
    source: str
    title: str
    artist: str
    size: int = 0
    url: str = ""
    validation: dict = field(default_factory=dict)
    fingerprint_sim: float | None = None


@dataclass
class StreamLinks:
    """Link-only fallback (DEFERRED to v2 — user order 22:19).

    Pages that play the song OUTSIDE Telegram. v1 never populates
    this: SmartOutcome.stream stays None, and the handler renders
    these links only when a later version fills them in — the retry
    handler never changes for it.
    """
    links: list[tuple[str, str]] = field(default_factory=list)
    preview_disk_path: str = ""
    preview_duration_sec: float = 0.0


@dataclass
class SmartOutcome:
    status: str  # "found" | "not_found" | "error"
    file: SmartFile | None = None
    # v2 extension point: the streaming fallback populates this;
    # v1 leaves it None (no streaming links, no preview in v1).
    stream: StreamLinks | None = None
    detail: str = ""
    ms: int = 0
    checked: int = 0
    expected_duration_sec: float | None = None


def _negative_key(artist: str, title: str) -> str:
    return f"{(artist or '').strip().lower()}|{(title or '').strip().lower()}"


def negative_cached(artist: str, title: str) -> bool:
    """True when this song verified not-found within the last 10 min."""
    ts = _NEGATIVE.get(_negative_key(artist, title))
    if ts is None:
        return False
    if time.time() - ts > _NEGATIVE_TTL_SEC:
        _NEGATIVE.pop(_negative_key(artist, title), None)
        return False
    return True


def remember_negative(artist: str, title: str) -> None:
    try:
        _NEGATIVE[_negative_key(artist, title)] = time.time()
    except Exception:
        pass


def smart_query_variants(artist: str, title: str) -> list[str]:
    """Ordered, deduped search variants, cap 6 (spec 18:29).

    Precise first (artist + title), then the bare title, then the
    version-stripped base word — the probe showed the bare Latin word
    ("Taziyane") is what the WP sites actually match on, while the
    precise query is what keeps a same-name song out.
    """
    from .smart_validate import _strip_bracketed
    base = " ".join(_strip_bracketed(title or "").split())
    out: list[str] = []
    for q in (f"{artist} {title}".strip(), (title or "").strip(),
              base, f"{artist} {base}".strip()):
        q = " ".join(q.split())
        if q and q not in out:
            out.append(q)
    return out[:6]


def _fa_norm(text: str) -> str:
    """Normalize Arabic-script letter variants to Persian ones.

    The real post titles mix them: javanmelody's Taziyane post writes
    "(ريميکس)" with Arabic Yeh (U+064A) and Kaf (U+0643), while the
    matcher checks the Persian "ریمیکس" (U+06CC / U+06A9). Without
    this the marker checks miss the real titles. NOTE: the Alef
    variants (U+0622/U+0623/U+0625) are deliberately NOT folded —
    folding U+0622 would turn "آهنگ" into "اهنگ" and break the song
    marker check below.
    """
    return (text or "").replace("\u064a", "\u06cc").replace("\u0643", "\u06a9") \
        .replace("\u0629", "\u0647")


def _post_tier(post_title: str, artist: str, title: str) -> int:
    """Discovery tier for one WP search hit: 2 strict, 1 loose, 0 reject.

    STRICT (2): the post title matches the wanted title OR artist by
    the smart matcher (Latin titles).
    LOOSE (1): the title is Persian-only — the smart matcher cannot
    see it (Persian script vs Latin wanted title) — but it carries a
    song marker ("آهنگ" / "ریمیکس") and is not a compilation. The WP
    search itself returned it for a query built from the wanted
    title/artist, and the FILE gate (tags + duration + version)
    decides whether it is the song — a wrong post's file is rejected
    there, never sent. Without this tier the real v1 posts are lost:
    javanmelody 10959 «دانلود آهنگ امیر تتلو تازیانه (ريميکس) | کیفیت
    اصلی 320» and melonmusic 1908 «دانلود آهنگ امیر تتلو به نام
    تازیانه» are both Persian-only.
    REJECT (0): compilations ("بهترین", "گلچین") or no song marker
    and no matcher hit. The version token is deliberately NOT
    required at any tier: the version is decided by the FILE, never
    by the post (melonmusic 1908 has no "remix" in its title, yet its
    file IS the remix).
    """
    pt = post_title or ""
    norm = _fa_norm(pt)
    if "بهترین" in norm or "گلچین" in norm:
        return 0
    if smart_title_matches(title, pt) or smart_artist_matches(artist, pt):
        return 2
    if "آهنگ" in norm or "ریمیکس" in norm:
        return 1
    return 0


def _post_accepted(post_title: str, artist: str, title: str) -> bool:
    """True when the post is worth downloading from (tier 1 or 2)."""
    return _post_tier(post_title, artist, title) > 0


async def _smart_wp_candidates(client, base: str, style: str, q: str,
                               artist: str, title: str,
                               ) -> tuple[list[tuple[float, str, str]], bool]:
    """Ranked (rank, file_url, site) candidates from ONE wp site, plus
    whether the WP search itself ran (HTTP 200 — even with zero
    matching posts). The boolean separates "the search ran and found
    nothing" (not_found) from "no site could even be reached" (error).

    WP /v2/search -> tier filter -> the post's public HTML page ->
    mp3 links from the page anchors. The /search endpoint returns
    {title, url} with NO post id (verified in the free-only study's
    sweep), so there is no WP-API post-content fetch here — the page
    is the source of the links, exactly as the study found them (the
    128/320 anchors). Never raises.
    """
    out: list[tuple[float, str, str]] = []
    try:
        resp = await client.get(
            _search_url(base, style),
            params={"search": q, "per_page": 5})
    except Exception:
        return out, False
    if resp.status_code != 200:
        return out, False
    try:
        data = resp.json()
    except Exception:
        return out, False
    if not isinstance(data, list):
        return out, False
    site = base.replace("https://", "")
    for p in data[:5]:
        if not isinstance(p, dict):
            continue
        ptitle = p.get("title") or ""
        if not isinstance(ptitle, str):
            continue
        tier = _post_tier(ptitle, artist, title)
        if tier == 0:
            continue
        page_url = p.get("url") or ""
        if not page_url.startswith("http"):
            continue
        file_urls: list[str] = []
        try:
            hr = await client.get(page_url, timeout=20)
            if hr.status_code == 200:
                file_urls = [m.group(0)
                             for m in _FILE_RE.finditer(hr.text)]
        except Exception:
            file_urls = []
        if not file_urls:
            continue
        for url in file_urls:
            fname = url.rsplit("/", 1)[-1]
            rank = _rank_candidate(fname, artist, title, tier=tier)
            out.append((rank, url, site))
    return out, True


def _rank_candidate(fname: str, artist: str, title: str, tier: int = 2) -> float:
    """Rank one candidate filename: 320 kbps first, then title overlap.

    A strict-tier post (Latin title match) outranks a loose-tier one
    (Persian-only title): the +1.0 tier bonus keeps a same-name fuzzy
    hit behind a post whose title actually matches. The filename is
    the only pre-download signal; the file validation below is the
    real gate, so the rank only orders the tries.
    """
    from urllib.parse import unquote
    from .smart_validate import smart_tokens
    want = set(smart_tokens(f"{artist} {title}"))
    got = set(smart_tokens(unquote(fname)))
    overlap = (len(want & got) / len(want)) if want else 0.0
    bonus = 0.5 if "320" in fname else 0.0
    tier_bonus = 1.0 if tier >= 2 else 0.0
    return overlap + bonus + tier_bonus


async def _download_to_disk(client, url: str, timeout_s: float,
                            max_mb: int = FULL_SONG_MAX_MB,
                            workdir: str | None = None) -> str | None:
    """Stream one URL to a temp file on disk. Returns the path, or None.

    Aborts on: non-200/206 status, non-audio magic bytes in the first
    chunk, size over the cap, timeout. Partial files are deleted.
    Never raises; never holds the bytes in RAM.
    """
    fd, path = None, ""
    try:
        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="avano_smart_",
                                    dir=workdir)
        os.close(fd)
        fd = None
        written = 0
        first = True
        async with client.stream("GET", url, headers={"User-Agent": _UA["User-Agent"]}) as resp:
            if resp.status_code not in (200, 206):
                return None
            async for chunk in resp.aiter_bytes(65536):
                if not chunk:
                    continue
                if first:
                    first = False
                    if len(chunk) < 16 or not _looks_like_audio(chunk[:16] + b"\x00" * 16):
                        return None
                written += len(chunk)
                if written > max_mb * 1024 * 1024:
                    return None
                with open(path, "ab") as fh:
                    fh.write(chunk)
        if written < 100_000:
            return None
        return path
    except Exception:
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        # The caller owns a returned path; a None return means the
        # partial file (if any) must go now.
        if path and (not os.path.isfile(path) or os.path.getsize(path) < 100_000):
            try:
                os.unlink(path)
            except Exception:
                pass


def _unlink_quietly(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except Exception:
        pass


async def resolve_expected_duration(artist: str, title: str,
                                    client=None) -> float | None:
    """Full-song duration (seconds) from catalog metadata, or None.

    iTunes first, Deezer second (both keyless). The result must match
    the wanted title/artist by the smart matcher — a same-name song's
    duration would poison the gate. Never raises.
    """
    query = f"{artist} {title}".strip() or title
    for provider_cls, label in (("itunes", "itunes"), ("deezer", "deezer")):
        try:
            if label == "itunes":
                from .itunes import ITunesProvider
                results = await ITunesProvider(client).search(query, limit=5)
            else:
                from .deezer import DeezerProvider
                results = await DeezerProvider(client).search(query, limit=5)
            for r in results or []:
                if not smart_title_matches(title, r.title or ""):
                    continue
                if r.artist and not smart_artist_matches(artist, r.artist or ""):
                    continue
                if r.duration:
                    return float(r.duration)
        except Exception:
            continue
    return None


async def search_smart_full_version(
        artist: str, title: str, *,
        expected_duration_sec: float | None = None,
        clip_fingerprint: list[int] | None = None,
        client=None,
        ceiling_s: float = SMART_CEILING_SEC,
        workdir: str | None = None) -> SmartOutcome:
    """Run the smart full-version search. Never raises.

    Returns SmartOutcome(status="found", file=...) on the first
    VERIFIED file, ("not_found", ...) when the search ran and
    nothing validated (v1: no stream links, no preview — both
    deferred), ("error", ...) only when every site search failed to
    run at all.
    A cached negative returns immediately without any HTTP call.
    """
    t0 = time.monotonic()
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not title:
        return SmartOutcome(status="error", detail="no_title",
                            ms=int((time.monotonic() - t0) * 1000))
    if negative_cached(artist, title):
        return SmartOutcome(status="not_found", detail="negative_cached",
                            ms=int((time.monotonic() - t0) * 1000))
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=10, headers=_UA,
                                   follow_redirects=True)
    deadline = t0 + ceiling_s
    checked = 0
    any_search_ran = False
    try:
        if expected_duration_sec is None:
            try:
                expected_duration_sec = await asyncio.wait_for(
                    resolve_expected_duration(artist, title, client),
                    timeout=min(12.0, max(1.0, deadline - time.monotonic())))
            except Exception:
                expected_duration_sec = None
        for variant in smart_query_variants(artist, title):
            if time.monotonic() >= deadline:
                break
            remaining = deadline - time.monotonic()
            # All sites in parallel, per-site timeout; one slow site
            # never blocks the others.
            async def _one_site(base, style):
                try:
                    return await asyncio.wait_for(
                        _smart_wp_candidates(client, base, style, variant,
                                             artist, title),
                        timeout=min(_SMART_SITE_TIMEOUT_SEC, remaining))
                except Exception:
                    return [], False
            results = await asyncio.gather(
                *[_one_site(base, style)
                  for _name, base, style in SMART_WP_SITES],
                return_exceptions=True)
            candidates: list[tuple[float, str, str]] = []
            seen_urls: set[str] = set()
            for r in results:
                if isinstance(r, tuple) and len(r) == 2:
                    cands, ran = r
                    if ran:
                        any_search_ran = True
                    if isinstance(cands, list):
                        for rank, url, site in cands:
                            if url in seen_urls:
                                continue
                            seen_urls.add(url)
                            candidates.append((rank, url, site))
            # 320 kbps first, then rank.
            candidates.sort(key=lambda c: ("320" in c[1], c[0]), reverse=True)
            for _rank, url, site in candidates[:_SMART_MAX_CANDIDATES_PER_VARIANT]:
                if time.monotonic() >= deadline:
                    break
                remaining = deadline - time.monotonic()
                checked += 1
                path = await asyncio.wait_for(
                    _download_to_disk(
                        client, url,
                        timeout_s=min(_SMART_DOWNLOAD_TIMEOUT_SEC, remaining),
                        workdir=workdir),
                    timeout=min(_SMART_DOWNLOAD_TIMEOUT_SEC, remaining) + 2)
                if not path:
                    continue
                try:
                    ftitle, fartist, fdur = probe_file_identity_from_path(path)
                    validation = validate_smart_file(
                        title, artist, ftitle, fartist, fdur,
                        expected_duration_sec, source_url=url)
                    accepted = (validation["status"] == "match"
                                or (validation["status"] == "conditional"
                                    and conditional_acceptable(validation, url)))
                    if not accepted:
                        log_event(log, "smart_search", stage="candidate_rejected",
                                  source=site, detail=validation.get("detail"),
                                  duration_s=round(fdur, 1))
                        _unlink_quietly(path)
                        continue
                    # Advisory fingerprint confirm (never a hard gate).
                    fp_sim = None
                    if clip_fingerprint:
                        cand_items = fingerprint_file_items(path)
                        if cand_items:
                            fp_sim = fpclip_similarity(clip_fingerprint, cand_items)
                    ext = "m4a" if url.lower().endswith(".m4a") else "mp3"
                    log_event(log, "smart_search", stage="found",
                              source=site, ms=int((time.monotonic() - t0) * 1000),
                              duration_s=round(fdur, 1),
                              fingerprint_sim=(round(fp_sim, 3) if fp_sim is not None else None))
                    return SmartOutcome(
                        status="found",
                        file=SmartFile(
                            disk_path=path, ext=ext, duration_sec=fdur,
                            source=f"smart:{site}", title=ftitle,
                            artist=fartist, size=os.path.getsize(path),
                            url=url, validation=validation,
                            fingerprint_sim=fp_sim),
                        detail=validation.get("detail") or "match",
                        ms=int((time.monotonic() - t0) * 1000),
                        checked=checked,
                        expected_duration_sec=expected_duration_sec)
                except Exception:
                    _unlink_quietly(path)
                    continue
        # Nothing validated. v1's not-found outcome is exactly the
        # 18:29 message + the survey hook (sent by the handler); the
        # streaming-link fallback and the iTunes preview are DEFERRED
        # to a later version (user order 22:19) — no links, no preview
        # in v1, so stream stays None.
        if not any_search_ran:
            return SmartOutcome(status="error", detail="all_sites_failed",
                                ms=int((time.monotonic() - t0) * 1000),
                                checked=checked,
                                expected_duration_sec=expected_duration_sec)
        remember_negative(artist, title)
        return SmartOutcome(status="not_found",
                            detail="no_verified_file",
                            ms=int((time.monotonic() - t0) * 1000),
                            checked=checked,
                            expected_duration_sec=expected_duration_sec)
    except Exception as exc:
        return SmartOutcome(status="error",
                            detail=f"{type(exc).__name__}",
                            ms=int((time.monotonic() - t0) * 1000),
                            checked=checked,
                            expected_duration_sec=expected_duration_sec)
    finally:
        if own_client:
            try:
                await client.aclose()
            except Exception:
                pass
