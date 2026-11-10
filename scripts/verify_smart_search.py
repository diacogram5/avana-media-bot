"""Sandbox tests for the FREE-ONLY smart full-version search v1
(app/search/smart_fullsong.py + the retry-button wiring in
app/bot/handlers.py).

v1 SOURCES (user order 22:19): JavanMelody (primary) + MelonMusic
(fallback) ONLY. The streaming-link fallback and the iTunes preview
are DEFERRED — v1's not-found outcome is exactly the 18:29 message
+ the survey hook. The survey module does NOT exist yet, so the hook
is tested both with the module absent and with a fake present.

No network, no Telegram, no real Turso, no account / API key /
payment anywhere. HTTP is faked with the REAL response shapes from
~/workspace/free-only-study/sweep.json (WP /v2/search returns
{title, url} with NO id; both real post titles are Persian-only);
mp3 downloads are served from the 4 validated fixtures.

Cases:
  T1  Discovery tiers on the REAL post titles: javanmelody 10959
      and melonmusic 1908 are LOOSE (Persian-only, song marker);
      the melonmusic compilation and "بهترین" posts are rejected;
      a Latin title is STRICT; _fa_norm maps Arabic ي/ك.
  T2  Query variants: ordered, deduped, cap 6, precise first.
  T3  Found via javanmelody (real flow: search -> page -> mp3 ->
      tags + duration + version): status found, file on DISK,
      validation "match", source smart:javanmelody.ir; both sites
      were queried in parallel.
  T3b Expected duration resolved from the fake iTunes lookup
      (341026 ms -> 341.0 s) when not passed in.
  T4  Found via melonmusic only: conditional (no Remix in tags)
      accepted via the version in the URL.
  T5  Not found: both searches ran with zero hits -> not_found,
      stream is None (v1: no links, no preview), no mp3 fetched.
  T6  Wrong version only: a 7:23 file (same tags) is rejected on
      the duration gate -> not_found.
  T7  Error: both site searches fail -> error, all_sites_failed.
  T8  Negative dedupe: the second identical search makes ZERO new
      HTTP calls (10-min in-memory negative cache).
  T9  Ceiling: a site sleeping 5 s with ceiling 2 s returns in
      < 5 s (bounded, never the full 60 s).
  T10 Disk streaming: _download_to_disk writes the fixture bytes
      to disk (never RAM); tiny / non-audio / 404 -> None with no
      partial file; SmartFile carries no data field.
  T11 Gate: _smart_search_eligible true only for audio message +
      link/file entry + clip hit with a title.
  T12 Tap found: full file sent with the LOCKED layout (identified
      caption + [playlist, lyrics] one row); file_id stored in the
      1b cache as kind "full"; disk file deleted; retry restored.
  T13 Tap not found: exactly the 18:29 text, retry restored, no
      audio, survey module absent -> no crash, no extra message.
  T14 Tap error: exactly the honest error text, retry restored.
  T15 Survey hook PRESENT (fake module): send_survey called once
      with (message, conn, op_context incl. search_result).
  T16 Survey hook helper RAISES: the tap still answers only the
      not-found text — never a crash, never a delay.
  T17 Full video_identify run (not-found): the temp status is
      edited to the exact smart text, the not-found text is sent,
      the retry button is restored, the normal identify flow never
      runs.
  T18 Exact texts: m_smart_searching / m_smart_not_found byte-equal
      to the 18:29 spec (FA).
  T19 v1 scope (source scan): SMART_WP_SITES is exactly
      javanmelody + melonmusic; no other site, no streaming code
      in the smart module; the Google button URL is untouched;
      the survey hook exists in handlers.py.
  T20 Probe code gone: no brave_probe.py, no /brave-probe route,
      no probe verify suites.
  T21 msbmusic pattern reverted: persian_music.py still has the
      vip1 pattern, no vip\\d* change (normal path unchanged).
  T22 Live tree untouched: stage-render-probe still has the probe
      code and has NO smart-search files.
  T23 (fix 1B) eligibility requires CONFIRMED identification:
      unconfirmed / not_found hits are NOT eligible; confirmed is.
  T24 (fix 1D) one slow post page fails PER-POST: the site still
      returns the fast post's candidates, in < 5 s (old code aborted
      the whole site call on the 6 s backstop).
  T25 (fix 1G) iTunes+Deezer both fail -> expected_duration_sec is
      None AND a "no_expected_duration" warning is logged.
  T26 (fix 2B) the search raising escapes nothing: the tap still
      sends exactly the honest error text and returns "error".
  T27 (fix 5) per-site logging: a site_search line exists for BOTH
      sites even when one returns zero candidates (the live logs had
      zero "melon" lines because nothing was logged per site).
  T28 (fix 6) the tap returns its outcome status (found /
      not_found / error); a full video_identify run with a found
      outcome logs identify_stage total status "found", not "error".
  T29 (fix 9 + 3-song test, REAL fixtures): _extract_mp3_urls on
      the REAL melonmusic post-4021 page finds both mp3s where
      _FILE_RE finds none; Sob Zohr Shab (Ho3ein, 196.8 s) is FOUND
      via melonmusic (javanmelody has only the 230.1 s DJ Alvan
      remix, correctly rejected on duration); Joft 6 is NOT FOUND
      as identified (HesamTiem) and as catalog (Amir Tataloo) — the
      javanmelody file is the right song but its tags say "Joft
      shish", a title_mismatch (digit "6" vs word "shish"); En
      Güzel Tesadüfüm (Tefo & SEKO, reel Dd1niVHN07n) is NOT FOUND
      with checked 0 — both v1 sources genuinely carry no Tefo
      post, so the failure there is upstream (identify), not search.
  T30 (fix round 2 — retry-tap regression, live 268cb98a): a retry
      tap on a non-eligible clip hit fell through to the
      identify-tap cache-hit branch and re-sent the clip audio, and
      _set_video_result_kb stamped the video keyboard onto the
      AUDIO message. (a) retry on unconfirmed clip -> 0 audio
      sends, honest m_identify_head_notfound text, exactly the
      retry button; (a2) two SEQUENTIAL retry taps (the live
      00:54 case: requests 3a0c1b0c + 7bfa1573, 4.6 s apart, each
      one cache-hit send) -> each sends 0 audio; (b) retry on
      identified clip still runs the smart search; (c) identify
      tap from a VIDEO on a clip hit -> audio sent exactly once
      and the video keyboard lands on the VIDEO message; (d)
      video_identify never calls _set_video_result_kb with an
      audio message.
  T31 (fix round 3 — Q4 supplementary identify, live 07e2f956):
      a retry tap on a non-eligible clip hit answered the honest
      not-found after ~565 ms with ZERO work (log req 82c83d0d).
      (a) Q4 CONFIRMS on the middle slice -> the 1b entry becomes
      identified (clip file kept) and the smart search runs;
      (b) Q4 finds nothing on either slice -> honest head-not-found,
      zero audio, smart search never runs, per-slice lines logged;
      (c) no clip bytes anywhere -> honest text, _identify_audio
      never called; (d) a hung slice is abandoned by the per-slice
      timeout (tap finishes fast); (e) _loudest_wav_window picks
      the loudest 10 s span; (f) Q-C: _identify_audio logs each
      provider's title/artist on lookup_response lines.
  T32 (fix round 4 — Q4 hoisted + cheapened, live cb7df25a):
      (a) Q4 runs on a cache-hit VIDEO tap (unconfirmed clip with
      file_id) instead of re-sending the clip audio; (b) Q4 runs
      after a bg-wait completes with an unconfirmed clip, before
      the honest message; (c) a slice exceeding its budget logs
      detail=slice_timeout + stage_in_flight and the retry still
      shows the honest status; (d) Q4 confirming after a bg wait
      triggers the smart search (both v1 sources queried).

Run: python3 scripts/verify_smart_search.py ; exit 0 = all PASS.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Strip proxy env vars (sandbox httpx gotcha) and Turso config.
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)
os.environ.pop("TURSO_URL", None)
os.environ.pop("TURSO_AUTH_TOKEN", None)
os.environ.pop("TURSO_DATABASE_URL", None)

import verify_speedfix as vs  # noqa: E402  (fake aiogram + handlers)
import app.bot.handlers as h  # noqa: E402
import app.search.smart_fullsong as sf  # noqa: E402
from app.i18n import t  # noqa: E402

check = vs.check

FIXTURES = Path.home() / "workspace" / "free-only-study"
JAVAN = FIXTURES / "javanmelody-taziyane-remix-320.mp3"
MELON = FIXTURES / "melonmusic-taziyane-remix-320.mp3"
MSB = FIXTURES / "msbmusic-taziyane.mp3"
# Songs 2–3 of the mandatory 3-song test (fix round 1): REAL files
# downloaded 2026-11-10 from the REAL post pages (ffprobe-verified
# tags + durations). Dd1niVHN07n's song has no file in either v1
# source, so it needs no fixture (T29e proves the empty searches).
SONG23 = FIXTURES / "song2-3"
JOFT6 = SONG23 / "joft6-javan-320.mp3"
SOB = SONG23 / "sobzohrshab-melon-128.mp3"
SOB_REMIX = SONG23 / "sobzohrshab-remix-javan-128.mp3"
JAVAN11303_HTML = SONG23 / "javan11303.html"
JAVAN1657_HTML = SONG23 / "javan1657.html"
MELON4021_HTML = SONG23 / "melon4021.html"

ARTIST = "Amir Tataloo"
TITLE = "Taziyane (Remix)"
EXPECTED = 341.0

# Real titles / URLs from the free-only study (sweep.json).
JAVAN_TITLE = "دانلود آهنگ امیر تتلو تازیانه (ريميکس) | کیفیت اصلی 320"
JAVAN_URL = "https://javanmelody.ir/10959/%d8%a7%d9%85%db%8c%d8%b1-%d8%aa%d8%aa%d9%84%d9%88-%d8%aa%d8%a7%d8%b2%db%8c%d8%a7%d9%86%d9%87-%d8%b1%d9%8a%d9%85%d9%8a%da%a9%d8%b3"
JAVAN_MP3 = "https://dl.javanmelody.ir/dl/1402/03/Amir-Tataloo-Taziyane-(Remix)-abanmusics.com-320.mp3"
MELON_COMP_TITLE = "دانلود گلچین معروف ترین آهنگ های امیر تتلو شاد و غمگین"
MELON_COMP_URL = "https://melonmusic.ir/3357/%da%af%d9%84%da%86%db%8c%d9%86-%d8%a2%d9%87%d9%86%da%af-%d9%87%d8%a7%db%8c-%d8%a7%d9%85%db%8c%d8%b1-%d8%aa%d8%aa%d9%84%d9%88/"
MELON_TITLE = "دانلود آهنگ امیر تتلو به نام تازیانه"
MELON_URL = "https://melonmusic.ir/1908/%d8%a7%d9%85%db%8c%d8%b1-%d8%aa%d8%aa%d9%84%d9%88-%d8%aa%d8%a7%d8%b2%db%8c%d8%a7%d9%86%d9%87/"
MELON_MP3 = "https://dl.melonmusic.ir/Music/Amir%20Tataloo%20-%20Tazianeh%20%28Remix-320%29.mp3"

SMART_SEARCHING = "🔍 آوانو در حال جستجوی هوشمند نسخه کامل آهنگ در منابع موسیقی است..."
SMART_NOT_FOUND = ("💜 جستجوی تکمیلی انجام شد، اما نسخه کامل و قابل‌دریافت این آهنگ پیدا نشد. "
                   "صدای استخراج‌شده از کلیپ همچنان برای شنیدن در دسترس شماست.")


# ---------------------------------------------------------------------------
# Fake HTTP
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeStreamResp:
    def __init__(self, status, data: bytes):
        self.status_code = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self, n):
        for i in range(0, len(self._data), n):
            yield self._data[i:i + n]


class _FakeClient:
    """Routes by URL substring.

    searches: {host: hits | 'raise' | http-status}; pages:
    {url-substring: html | 'raise'}; mp3s: {url-substring: bytes};
    sleep_hosts: {host: seconds} (sleeps before answering a search);
    sleep_pages: {url-substring: seconds} (sleeps before a page).
    iTunes is query-aware: the term decides which catalog record is
    returned (Taziyane default; "Joft" / "Sob" / "Tesad" for the
    3-song test), so resolve_expected_duration exercises the real
    title/artist matching per song.
    """

    def __init__(self, *, itunes=True, searches=None, pages=None,
                 mp3s=None, sleep_hosts=None, sleep_pages=None):
        self.itunes = itunes
        self.searches = searches or {}
        self.pages = pages or {}
        self.mp3s = mp3s or {}
        self.sleep_hosts = sleep_hosts or {}
        self.sleep_pages = sleep_pages or {}
        self.calls: list[tuple[str, str]] = []

    async def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url))
        if "itunes.apple.com/search" in url:
            if not self.itunes:
                return _FakeResp(404)
            term = ((params or {}).get("term") or "")
            if "Joft" in term:
                rec = {"trackName": "Joft 6", "artistName": "Amir Tataloo",
                       "trackTimeMillis": 225555,
                       "trackViewUrl": "https://music.apple.com/us/album/x",
                       "previewUrl": "https://audio-ssl.itunes.apple.com/x.m4a"}
            elif "Sob" in term:
                rec = {"trackName": "Sob Zohr Shab", "artistName": "Ho3ein",
                       "trackTimeMillis": 196807,
                       "trackViewUrl": "https://music.apple.com/us/album/x",
                       "previewUrl": "https://audio-ssl.itunes.apple.com/x.m4a"}
            elif "Tesad" in term or "Güzel" in term:
                rec = {"trackName": "En Güzel Tesadüfüm",
                       "artistName": "Tefo & SEKO",
                       "trackTimeMillis": 164656,
                       "trackViewUrl": "https://music.apple.com/us/album/x",
                       "previewUrl": "https://audio-ssl.itunes.apple.com/x.m4a"}
            else:
                rec = {"trackName": TITLE, "artistName": ARTIST,
                       "trackTimeMillis": 341026,
                       "trackViewUrl": "https://music.apple.com/us/album/x",
                       "previewUrl": "https://audio-ssl.itunes.apple.com/x.m4a"}
            return _FakeResp(200, {"results": [rec]})
        if "api.deezer.com/search" in url:
            return _FakeResp(200, {"data": []})
        for host, hits in self.searches.items():
            if host in url and "wp-json/wp/v2/search" in url:
                if host in self.sleep_hosts:
                    await asyncio.sleep(self.sleep_hosts[host])
                if hits == "raise":
                    raise RuntimeError("connect failed")
                if isinstance(hits, int):
                    return _FakeResp(hits)
                return _FakeResp(200, hits)
        for sub, html in self.pages.items():
            if sub in url:
                if sub in self.sleep_pages:
                    await asyncio.sleep(self.sleep_pages[sub])
                if html == "raise":
                    raise RuntimeError("page failed")
                return _FakeResp(200, text=html)
        return _FakeResp(404)

    def stream(self, method, url, headers=None, timeout=None):
        self.calls.append(("STREAM", url))
        for sub, data in self.mp3s.items():
            if sub in url:
                return _FakeStreamResp(200, data)
        return _FakeStreamResp(404, b"")

    async def aclose(self):
        pass


def _javan_hits():
    return [{"title": JAVAN_TITLE, "url": JAVAN_URL}]


def _melon_hits():
    return [{"title": MELON_COMP_TITLE, "url": MELON_COMP_URL},
            {"title": MELON_TITLE, "url": MELON_URL}]


def _javan_page(mp3=JAVAN_MP3):
    return (f'<html><a href="{mp3}">320</a>'
            '<a href="https://dl.javanmelody.ir/dl/1402/03/'
            'Amir-Tataloo-Taziyane-(Remix)-abanmusics.com-128.mp3">128</a></html>')


def _melon_page():
    return f'<html><a href="{MELON_MP3}">320</a></html>'


# --- 3-song test (fix round 1): REAL WP /v2/search responses + REAL
# post pages, fetched 2026-11-10 via the browser fetcher (browser.open)
# and curl. Titles are the sites' own Persian titles, byte-identical
# to the live responses; pages are the live HTML files saved under
# ~/workspace/free-only-study/song2-3/.
def _sob_javan_hits():
    return [{"title": "دانلود آهنگ حصین صب ظهر شاب (دی جی الوان ریمیکس) | کیفیت اصلی 320",
             "url": "https://javanmelody.ir/1657/%d8%ad%d8%b5%db%8c%d9%86-%d8%b5%d8%a8-%d8%b8%d9%87%d8%b1-%d8%b4%d8%a8-%d8%af%db%8c-%d8%ac%db%8c-%d8%a7%d9%84%d9%88%d8%a7%d9%86-%d8%b1%db%8c%d9%85%db%8c%da%a9%d8%b3"}]


def _sob_melon_hits():
    return [{"title": "دانلود آهنگ حصین اه نام صباح ظهر شاب",
             "url": "https://melonmusic.ir/4021/%d8%ad%d8%b5%db%8c%d9%86-%d8%b5%d8%a8%d8%ad-%d8%b8%d9%87%d8%b1-%d8%b4%d8%a8/"},
            {"title": "دانلود گلچین معروف ترین آهنگ های حصین شاد و غمگین",
             "url": "https://melonmusic.ir/4065/%da%af%d9%84%da%86%db%8c%d9%86-%d8%a2%d9%87%d9%86%da%af-%d9%87%d8%a7%db%8c-%d8%ad%d8%b5%db%8c%d9%86/"},
            {"title": "ریمیکس آهنگ ای ای سی ریمیکس اه نام صبح ظهر شب",
             "url": "https://melonmusic.ir/20925/%d8%b1%db%8c%d9%85%db%8c%da%a9%d8%b3-%d8%b5%d8%a8%d8%ad-%d8%b8%d9%87%d8%b1-%d8%b4%d8%a8-%d8%a8%db%8c-%d8%a8%db%8c-%d8%b3%db%8c-%d8%b1%db%8c%d9%85%db%8c%da%a9%d8%b3/"}]


def _joft_javan_hits():
    return [{"title": "دانلود آهنگ امیر تتلو جفت 6 | کیفیت اصلی 320",
             "url": "https://javanmelody.ir/11303/%d8%a7%d9%85%db%8c%d8%b1-%d8%aa%d8%aa%d9%84%d9%88-%d8%ac%d9%81%d8%aa-6"},
            {"title": "دانلود آهنگ ابراهیم چاوشی جفت شش | کیفیت اصلی 320",
             "url": "https://javanmelody.ir/29260/%d8%a7%d8%a8%d8%b1%d8%a7%d9%87%db%8c%d9%85-%da%86%d8%a7%d9%88%d8%b4%db%8c-%d8%ac%d9%81%d8%aa-%d8%b4%d8%b4"},
            {"title": "دانلود آهنگ ابراهیم چاوشی جفت شیش | کیفیت اصلی 320",
             "url": "https://javanmelody.ir/29173/%d8%a7%d8%a8%d8%b1%d8%a7%d9%87%db%8c%d9%85-%da%86%d8%a7%d9%88%d8%b4%db%8c-%d8%ac%d9%81%d8%aa-%d8%b4%db%8c%d8%b4"},
            {"title": "دانلود آهنگ مصطفی تفتیش جفت منی | کیفیت اصلی 320",
             "url": "https://javanmelody.ir/5211/%d9%85%d8%b5%d8%b7%d9%81%db%8c-%d8%aa%d9%81%d8%aa%db%8c%d8%b4-%d8%ac%d9%81%d8%aa-%d9%85%d9%86%db%8c"}]


def _joft_melon_hits():
    return [{"title": "دانلود آهنگ امیر خلوت و دارا اه نام جفت پوچ",
             "url": "https://melonmusic.ir/35866/%d8%a7%d9%85%db%8c%d8%b1-%d8%ae%d9%84%d9%88%d8%aa-%d8%ac%d9%81%d8%aa-%d9%be%d9%88%da%86/"},
            {"title": "دانلود گلچین معروف ترین آهنگ های مرجان شاد و غمگین",
             "url": "https://melonmusic.ir/3866/%da%af%d9%84%da%86%db%8c%d9%86-%d8%a2%d9%87%d9%86%da%af-%d9%87%d8%a7%db%8c-%d9%85%d8%b1%d8%ac%d8%a7%d9%86/"},
            {"title": "دانلود آهنگ شایان اشراقی اه نام ساعت جفت",
             "url": "https://melonmusic.ir/18821/%d8%b4%d8%a7%db%8c%d8%a7%d9%86-%d8%a7%d8%b4%d8%b1%d8%a7%d9%82%db%8c-%d8%b3%d8%a7%d8%b9%d8%aa-%d8%ac%d9%81%d8%aa/"},
            {"title": "دانلود آهنگ الیاد اه نام ساعت جفت",
             "url": "https://melonmusic.ir/10582/%d8%a7%d9%84%db%8c%d8%a7%d8%af-%d8%b3%d8%a7%d8%b9%d8%aa-%d8%ac%d9%81%d8%aa/"},
            {"title": "دانلود آهنگ نوان اه نام ساعت جفت",
             "url": "https://melonmusic.ir/3059/%d9%86%d9%88%d8%a7%d9%86-%d8%b3%d8%a7%d8%b9%d8%aa-%d8%ac%d9%81%d8%aa/"}]


def _real_page_html(path: Path) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _fixture_bytes(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Fake Telegram message / callback (extends the speedfix fakes)
# ---------------------------------------------------------------------------
class _Sent(vs._FakeSent):
    def __init__(self):
        super().__init__()
        self.edited: list[str] = []
        self.deleted = False

    async def edit_text(self, text, **kw):
        self.edited.append(text)
        return self

    async def delete(self):
        self.deleted = True
        return None


class _Msg(vs._FakeMessage):
    def __init__(self, with_audio=True):
        super().__init__()
        self.audio = (SimpleNamespace(file_id="CLIP_AUDIO_ID")
                      if with_audio else None)
        self.sent: list[_Sent] = []
        self.edits_markup: list = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        s = _Sent()
        self.sent.append(s)
        return s

    async def edit_reply_markup(self, reply_markup=None, **kw):
        self.edits_markup.append(reply_markup)
        return None


class _Cb:
    def __init__(self, msg, data="vid:id:tok123"):
        self.message = msg
        self.bot = SimpleNamespace()
        self.from_user = SimpleNamespace(id=572595158)
        self.data = data
        self.answered: list = []

    async def answer(self, text=None, **kw):
        self.answered.append(text)
        return None


def _clip_hit(**over):
    d = {"title": TITLE, "artist": ARTIST, "kind": "clip",
         "status": "identified"}
    d.update(over)
    return d


def _link_entry():
    return {"kind": "link", "file_id": "",
            "link": "https://www.instagram.com/reel/C7FmVMNNzTt/"}


async def _run_search(client, **kw):
    sf._NEGATIVE.clear()
    kw.setdefault("expected_duration_sec", EXPECTED)
    kw.setdefault("workdir", tempfile.mkdtemp(prefix="smartsearch_"))
    return await sf.search_smart_full_version(ARTIST, TITLE, client=client, **kw)


async def main() -> int:
    for p in (JAVAN, MELON, MSB, JOFT6, SOB, SOB_REMIX,
              JAVAN11303_HTML, JAVAN1657_HTML, MELON4021_HTML):
        if not p.is_file():
            print(f"missing fixture: {p}")
            return 1
    javan_bytes = _fixture_bytes(JAVAN)
    melon_bytes = _fixture_bytes(MELON)
    msb_bytes = _fixture_bytes(MSB)
    joft6_bytes = _fixture_bytes(JOFT6)
    sob_bytes = _fixture_bytes(SOB)
    sob_remix_bytes = _fixture_bytes(SOB_REMIX)
    javan11303_html = _real_page_html(JAVAN11303_HTML)
    javan1657_html = _real_page_html(JAVAN1657_HTML)
    melon4021_html = _real_page_html(MELON4021_HTML)

    # T1: discovery tiers on the REAL post titles.
    check("T1 tier javanmelody 10959 is LOOSE (Persian-only)",
          sf._post_tier(JAVAN_TITLE, ARTIST, TITLE) == 1,
          f"tier={sf._post_tier(JAVAN_TITLE, ARTIST, TITLE)}")
    check("T1 tier melonmusic 1908 is LOOSE (Persian-only)",
          sf._post_tier(MELON_TITLE, ARTIST, TITLE) == 1)
    check("T1 tier melonmusic compilation rejected",
          sf._post_tier(MELON_COMP_TITLE, ARTIST, TITLE) == 0)
    check("T1 tier 'بهترین' compilation rejected",
          sf._post_tier("دانلود بهترین آهنگ های امیر تتلو", ARTIST, TITLE) == 0)
    check("T1 tier Latin title is STRICT",
          sf._post_tier("Taziyane (Remix) - Amir Tataloo", ARTIST, TITLE) == 2)
    check("T1 tier unrelated Persian title rejected",
          sf._post_tier("دانلود مداحی علم اگر از دست علمدار", ARTIST, TITLE) == 0)
    check("T1 _fa_norm maps Arabic ي/ك to Persian",
          sf._fa_norm("ريميکس") == "ریمیکس")

    # T2: query variants.
    variants = sf.smart_query_variants(ARTIST, TITLE)
    check("T2 variants ordered/deduped, cap 6, precise first",
          len(variants) <= 6 and len(set(variants)) == len(variants)
          and variants[0] == f"{ARTIST} {TITLE}"
          and TITLE in variants and "Taziyane" in variants,
          str(variants))

    # T3: found via javanmelody — the real flow.
    client = _FakeClient(
        searches={"javanmelody.ir": _javan_hits(), "melonmusic.ir": []},
        pages={"javanmelody.ir/10959": _javan_page()},
        mp3s={"dl.javanmelody.ir": javan_bytes})
    out = await _run_search(client)
    check("T3 found via javanmelody (real flow)",
          out.status == "found" and out.file is not None
          and out.file.source == "smart:javanmelody.ir",
          f"status={out.status} detail={out.detail}")
    if out.file is not None:
        check("T3 file on disk, real mp3 bytes, validation match",
              os.path.isfile(out.file.disk_path)
              and os.path.getsize(out.file.disk_path) == len(javan_bytes)
              and out.file.validation.get("status") == "match",
              f"size={out.file.size} validation={out.file.validation}")
        check("T3 SmartFile carries no data field (bytes never in RAM)",
              "data" not in sf.SmartFile.__dataclass_fields__)
        sf._unlink_quietly(out.file.disk_path)
    check("T3 both v1 sites were queried in parallel",
          any("javanmelody.ir" in u for _, u in client.calls)
          and any("melonmusic.ir" in u for _, u in client.calls))

    # T3b: expected duration resolved from the fake iTunes lookup.
    client = _FakeClient(
        searches={"javanmelody.ir": _javan_hits(), "melonmusic.ir": []},
        pages={"javanmelody.ir/10959": _javan_page()},
        mp3s={"dl.javanmelody.ir": javan_bytes})
    out = await sf.search_smart_full_version(
        ARTIST, TITLE, client=client,
        workdir=tempfile.mkdtemp(prefix="smartsearch_"))
    check("T3b iTunes resolves expected duration 341.0 s",
          out.status == "found"
          and out.expected_duration_sec == 341.0,
          f"status={out.status} expected={out.expected_duration_sec}")
    if out.file is not None:
        sf._unlink_quietly(out.file.disk_path)

    # T4: found via melonmusic only (conditional via URL version).
    client = _FakeClient(
        searches={"javanmelody.ir": [], "melonmusic.ir": _melon_hits()},
        pages={"melonmusic.ir/1908": _melon_page()},
        mp3s={"dl.melonmusic.ir": melon_bytes})
    out = await _run_search(client)
    check("T4 found via melonmusic (conditional, URL version)",
          out.status == "found" and out.file is not None
          and out.file.source == "smart:melonmusic.ir"
          and out.file.validation.get("status") == "conditional",
          f"status={out.status} detail={out.detail} "
          f"validation={out.file.validation if out.file else None}")
    if out.file is not None:
        sf._unlink_quietly(out.file.disk_path)

    # T5: not found — both searches ran, zero hits.
    client = _FakeClient(searches={"javanmelody.ir": [],
                                   "melonmusic.ir": []})
    out = await _run_search(client)
    check("T5 not found: searches ran, zero hits",
          out.status == "not_found" and out.detail == "no_verified_file",
          f"status={out.status} detail={out.detail}")
    check("T5 v1 not-found carries NO stream links and NO preview",
          out.stream is None)
    check("T5 no mp3 was fetched",
          not any(m == "STREAM" for m, _ in client.calls))

    # T6: wrong version only (7:23 file, same tags) -> duration rejects.
    client = _FakeClient(
        searches={"javanmelody.ir": _javan_hits(), "melonmusic.ir": []},
        pages={"javanmelody.ir/10959": _javan_page(
            "https://dl.javanmelody.ir/dl/1402/03/Amir-Tataloo-Taziyane-320.mp3")},
        mp3s={"dl.javanmelody.ir": msb_bytes})
    out = await _run_search(client)
    check("T6 7:23 file rejected on duration -> not found",
          out.status == "not_found",
          f"status={out.status} detail={out.detail}")

    # T7: error — both site searches fail.
    client = _FakeClient(searches={"javanmelody.ir": "raise",
                                   "melonmusic.ir": 500})
    out = await _run_search(client)
    check("T7 error when every site search fails",
          out.status == "error" and out.detail == "all_sites_failed",
          f"status={out.status} detail={out.detail}")

    # T8: negative dedupe — the second identical search makes no calls.
    client = _FakeClient(searches={"javanmelody.ir": [],
                                   "melonmusic.ir": []})
    sf._NEGATIVE.clear()
    out1 = await sf.search_smart_full_version(
        ARTIST, TITLE, client=client, expected_duration_sec=EXPECTED,
        workdir=tempfile.mkdtemp(prefix="smartsearch_"))
    n_calls = len(client.calls)
    out2 = await sf.search_smart_full_version(
        ARTIST, TITLE, client=client, expected_duration_sec=EXPECTED,
        workdir=tempfile.mkdtemp(prefix="smartsearch_"))
    check("T8 negative cache: second search makes zero HTTP calls",
          out1.status == "not_found" and out2.status == "not_found"
          and out2.detail == "negative_cached"
          and len(client.calls) == n_calls,
          f"calls {n_calls} -> {len(client.calls)} detail={out2.detail}")
    sf._NEGATIVE.clear()

    # T9: ceiling respected — a 5 s site with a 2 s ceiling.
    client = _FakeClient(
        searches={"javanmelody.ir": _javan_hits(), "melonmusic.ir": "raise"},
        pages={"javanmelody.ir/10959": _javan_page()},
        mp3s={"dl.javanmelody.ir": javan_bytes},
        sleep_hosts={"javanmelody.ir": 5.0})
    sf._NEGATIVE.clear()
    t0 = time.monotonic()
    out = await sf.search_smart_full_version(
        ARTIST, TITLE, client=client, expected_duration_sec=EXPECTED,
        ceiling_s=2.0, workdir=tempfile.mkdtemp(prefix="smartsearch_"))
    elapsed = time.monotonic() - t0
    check("T9 ceiling: 5 s site + 2 s ceiling returns in < 5 s",
          elapsed < 5.0 and out.status in ("error", "not_found"),
          f"elapsed={elapsed:.2f}s status={out.status}")
    if out.file is not None:
        sf._unlink_quietly(out.file.disk_path)
    sf._NEGATIVE.clear()

    # T10: disk streaming.
    td = tempfile.mkdtemp(prefix="smartsearch_dl_")
    client = _FakeClient(mp3s={"dl.javanmelody.ir": javan_bytes})
    path = await sf._download_to_disk(client, JAVAN_MP3, timeout_s=30,
                                      workdir=td)
    check("T10 download streams the fixture bytes to disk",
          path is not None and os.path.isfile(path)
          and open(path, "rb").read() == javan_bytes,
          f"path={path}")
    if path:
        os.unlink(path)
    client2 = _FakeClient(mp3s={"x": b"ID3" + b"\x00" * 100})
    path2 = await sf._download_to_disk(client2, "https://x.invalid/a.mp3",
                                       timeout_s=30, workdir=td)
    check("T10 tiny file rejected, no partial left",
          path2 is None and not os.listdir(td),
          f"path2={path2} leftovers={os.listdir(td)}")
    client3 = _FakeClient(mp3s={"x": b"NOT-AUDIO-AT-ALL-1234" + b"\x00" * 200000})
    path3 = await sf._download_to_disk(client3, "https://x.invalid/b.mp3",
                                       timeout_s=30, workdir=td)
    check("T10 non-audio first chunk rejected",
          path3 is None and not os.listdir(td))
    shutil.rmtree(td, ignore_errors=True)

    # T11: the tap gate.
    cb_audio = _Cb(_Msg(with_audio=True))
    cb_video = _Cb(_Msg(with_audio=False))
    check("T11 gate: audio msg + link entry + clip hit -> True",
          h._smart_search_eligible(cb_audio, _link_entry(), _clip_hit()))
    check("T11 gate: video msg (no audio) -> False",
          not h._smart_search_eligible(cb_video, _link_entry(), _clip_hit()))
    check("T11 gate: kind full hit -> False",
          not h._smart_search_eligible(
              cb_audio, _link_entry(), _clip_hit(kind="full")))
    check("T11 gate: audio entry kind -> False",
          not h._smart_search_eligible(
              cb_audio, {"kind": "audio", "file_id": "x"}, _clip_hit()))
    check("T11 gate: title-less hit -> False",
          not h._smart_search_eligible(
              cb_audio, _link_entry(), _clip_hit(title="")))
    check("T11 gate: pending hit -> False",
          not h._smart_search_eligible(
              cb_audio, _link_entry(), _clip_hit(kind="pending")))

    # T12–T16: the tap, with a monkeypatched search.
    conn = vs._make_conn()
    cache_key = "ig:C7FmVMNNzTt"
    token = "tok123"
    orig_search = sf.search_smart_full_version

    async def _fake_found(artist, title, clip_fingerprint=None):
        td2 = tempfile.mkdtemp(prefix="smartsearch_tap_")
        copy = os.path.join(td2, "found.mp3")
        shutil.copyfile(JAVAN, copy)
        return sf.SmartOutcome(
            status="found",
            file=sf.SmartFile(
                disk_path=copy, ext="mp3", duration_sec=340.8,
                source="smart:javanmelody.ir", title=TITLE, artist=ARTIST,
                size=os.path.getsize(copy), url=JAVAN_MP3,
                validation={"status": "match", "detail": "match"},
                fingerprint_sim=None),
            detail="match", ms=120, checked=1,
            expected_duration_sec=EXPECTED)

    async def _fake_not_found(artist, title, clip_fingerprint=None):
        return sf.SmartOutcome(status="not_found", detail="no_verified_file",
                               ms=90, checked=0,
                               expected_duration_sec=EXPECTED)

    async def _fake_error(artist, title, clip_fingerprint=None):
        return sf.SmartOutcome(status="error", detail="all_sites_failed",
                               ms=10, checked=0,
                               expected_duration_sec=EXPECTED)

    sys.modules.pop("app.features.survey", None)
    # T12: tap found.
    sf.search_smart_full_version = _fake_found
    try:
        msg = _Msg(with_audio=True)
        await h._run_smart_search_tap(
            _Cb(msg), conn, _link_entry(), _clip_hit(), cache_key, token, "fa")
    finally:
        sf.search_smart_full_version = orig_search
    sent_path = None
    if msg.audio_calls:
        _kind, _obj = msg.audio_calls[0]
        sent_path = getattr(_obj, "path", None)
    stored = h._identify_result_get(cache_key)
    check("T12 tap found: full file sent via FSInputFile",
          len(msg.audio_calls) == 1 and sent_path is not None
          and sent_path.endswith("found.mp3"),
          f"audio_calls={len(msg.audio_calls)} path={sent_path}")
    check("T12 tap found: disk file deleted after send",
          sent_path is None or not os.path.exists(sent_path))
    check("T12 tap found: file_id stored in 1b cache as kind full",
          isinstance(stored, dict) and stored.get("kind") == "full"
          and stored.get("file_id") == "BYTES_FILE_ID",
          f"stored={stored}")
    check("T12 tap found: no error text, retry button restored",
          not any("خطا" in a or "مشکلی" in a for a in msg.answers)
          and len(msg.edits_markup) == 1 and msg.edits_markup[0] is not None)

    # T13: tap not found, survey module absent.
    sys.modules.pop("app.features.survey", None)
    sf.search_smart_full_version = _fake_not_found
    try:
        msg = _Msg(with_audio=True)
        await h._run_smart_search_tap(
            _Cb(msg), conn, _link_entry(), _clip_hit(), cache_key, token, "fa")
    finally:
        sf.search_smart_full_version = orig_search
    check("T13 tap not found: exactly the 18:29 text, nothing else",
          msg.answers == [t("m_smart_not_found", "fa")],
          f"answers={msg.answers}")
    check("T13 tap not found: no audio, retry restored",
          not msg.audio_calls and len(msg.edits_markup) == 1)
    check("T13 survey module absent -> skipped silently",
          "app.features.survey" not in sys.modules)

    # T14: tap error.
    sf.search_smart_full_version = _fake_error
    try:
        msg = _Msg(with_audio=True)
        await h._run_smart_search_tap(
            _Cb(msg), conn, _link_entry(), _clip_hit(), cache_key, token, "fa")
    finally:
        sf.search_smart_full_version = orig_search
    check("T14 tap error: exactly the honest error text",
          msg.answers == [t("m_smart_error", "fa")],
          f"answers={msg.answers}")
    check("T14 tap error: retry restored",
          len(msg.edits_markup) == 1)

    # T15: survey hook PRESENT (fake module).
    survey_calls: list = []

    async def _fake_send_survey(message, conn_, op_context):
        survey_calls.append((message, conn_, op_context))

    fake_survey = types.ModuleType("app.features.survey")
    fake_survey.send_survey = _fake_send_survey
    sys.modules["app.features.survey"] = fake_survey
    sf.search_smart_full_version = _fake_not_found
    try:
        msg = _Msg(with_audio=True)
        await h._run_smart_search_tap(
            _Cb(msg), conn, _link_entry(), _clip_hit(), cache_key, token, "fa")
    finally:
        sf.search_smart_full_version = orig_search
        sys.modules.pop("app.features.survey", None)
    check("T15 survey present: send_survey called once after not-found",
          len(survey_calls) == 1,
          f"calls={len(survey_calls)}")
    if survey_calls:
        _m, _c, ctx = survey_calls[0]
        check("T15 survey op_context carries the search identity",
              ctx.get("search_result") == "not_found"
              and ctx.get("title") == TITLE
              and ctx.get("artist") == ARTIST
              and ctx.get("cache_key") == cache_key,
              f"ctx={ctx}")
    check("T15 survey present: the not-found text still sent first",
          msg.answers == [t("m_smart_not_found", "fa")],
          f"answers={msg.answers}")

    # T16: survey helper RAISES -> still only the not-found text.
    async def _raising_send_survey(message, conn_, op_context):
        raise RuntimeError("survey db down")

    fake_survey2 = types.ModuleType("app.features.survey")
    fake_survey2.send_survey = _raising_send_survey
    sys.modules["app.features.survey"] = fake_survey2
    sf.search_smart_full_version = _fake_not_found
    try:
        msg = _Msg(with_audio=True)
        await h._run_smart_search_tap(
            _Cb(msg), conn, _link_entry(), _clip_hit(), cache_key, token, "fa")
    finally:
        sf.search_smart_full_version = orig_search
        sys.modules.pop("app.features.survey", None)
    check("T16 survey raising: tap still answers only the not-found text",
          msg.answers == [t("m_smart_not_found", "fa")],
          f"answers={msg.answers}")

    # T17: full video_identify run (not-found path).
    token2 = "tok-smart-t17"
    entry = _link_entry()
    entry["user_id"] = 572595158
    h._VIDEO_IDENTIFY[token2] = entry
    key2 = h._identify_result_key(link=entry["link"])
    # A cache entry needs created_at + ttl or _identify_result_get
    # reads it as expired (the real store hook always sets both).
    h._identify_result_put(key2, {**_clip_hit(),
                                  "created_at": time.time(), "ttl": 600})
    sf.search_smart_full_version = _fake_not_found
    try:
        msg = _Msg(with_audio=True)
        cb = _Cb(msg, data=f"vid:id:{token2}")
        await h.video_identify(cb, conn, SimpleNamespace())
    finally:
        sf.search_smart_full_version = orig_search
        h._VIDEO_IDENTIFY.pop(token2, None)
        h._VIDEO_IDENTIFY_RUNNING.discard(token2)
    status_edits = [e for s in msg.sent for e in s.edited]
    check("T17 video_identify: temp status edited to the exact smart text",
          t("m_smart_searching", "fa") in status_edits,
          f"edits={status_edits}")
    check("T17 video_identify: not-found text sent, retry restored",
          t("m_smart_not_found", "fa") in msg.answers
          and len(msg.edits_markup) >= 1)
    check("T17 video_identify: normal flow never ran (no other answers)",
          msg.answers == [t("m_identify_searching", "fa"),
                          t("m_smart_not_found", "fa")],
          f"answers={msg.answers}")

    # T18: exact texts.
    check("T18 m_smart_searching is byte-exact (FA)",
          t("m_smart_searching", "fa") == SMART_SEARCHING)
    check("T18 m_smart_not_found is byte-exact (FA)",
          t("m_smart_not_found", "fa") == SMART_NOT_FOUND)

    # T19: v1 scope (source scan).
    check("T19 v1 sources are exactly javanmelody + melonmusic",
          sf.SMART_WP_SITES == (
              ("javanmelody", "https://javanmelody.ir", "wp-json"),
              ("melonmusic", "https://melonmusic.ir", "wp-json")),
          str(sf.SMART_WP_SITES))
    smart_src = (ROOT / "app" / "search" / "smart_fullsong.py").read_text()
    # Check base URLs, not site names: the module comments NAME the
    # deferred sites (probe notes), but no other site's URL is wired.
    _other_urls = ("msbmusic.ir", "fazremix.ir", "musicdel.ir",
                   "beys-music.ir", "mifa-music.ir", "download1music.ir",
                   "musics-fa.com", "minevisam.ir", "musicviral.ir",
                   "musicsweb.ir", "ezmusic.ir", "mokhtalefmusic.ir",
                   "gisomusic.ir")
    check("T19 no other Persian site is wired into the smart module",
          not any(s in smart_src for s in _other_urls),
          f"found={[s for s in _other_urls if s in smart_src]}")
    check("T19 no streaming code in the smart module",
          "KNOWN_STREAMS" not in smart_src
          and "_streaming_fallback" not in smart_src
          and "_preview_verifies" not in smart_src)
    handlers_src = (ROOT / "app" / "bot" / "handlers.py").read_text()
    check("T19 Google button URL untouched",
          'url="https://www.google.com/search?q=" + q' in handlers_src)
    check("T19 survey hook exists in handlers.py",
          "from ..features.survey import send_survey" in handlers_src)

    # T20: probe code gone.
    check("T20 brave_probe.py removed",
          not (ROOT / "app" / "services" / "brave_probe.py").exists())
    check("T20 /brave-probe route removed from webpanel.py",
          "brave-probe" not in (ROOT / "app" / "webpanel.py").read_text())
    check("T20 probe verify suites removed",
          not (ROOT / "scripts" / "verify_brave_probe.py").exists()
          and not (ROOT / "scripts" / "verify_google_probe.py").exists()
          and not (ROOT / "scripts" / "verify_render_probe.py").exists())

    # T21: msbmusic pattern reverted (normal path unchanged).
    pm_src = (ROOT / "app" / "search" / "adapters"
              / "persian_music.py").read_text()
    check("T21 msbmusic pattern is the original vip1 (reverted)",
          r"vip1\.msbmusic\.ir" in pm_src and r"vip\d*" not in pm_src)

    # T22: the live tree was never touched.
    live = Path.home() / "workspace" / "stage-render-probe"
    check("T22 live tree still has the probe code (untouched)",
          (live / "app" / "services" / "brave_probe.py").exists())
    check("T22 live tree has NO smart-search files",
          not (live / "app" / "search" / "smart_fullsong.py").exists()
          and not (live / "scripts" / "verify_smart_search.py").exists())

    # --- FIX ROUND 1 (2026-11-10) -------------------------------------
    # T23 (fix 1B): eligibility requires CONFIRMED identification.
    cb_audio = _Cb(_Msg(with_audio=True))
    check("T23 gate: unconfirmed hit -> NOT eligible (never searched)",
          not h._smart_search_eligible(
              cb_audio, _link_entry(),
              _clip_hit(status="unconfirmed")))
    check("T23 gate: not_found hit with a name -> NOT eligible",
          not h._smart_search_eligible(
              cb_audio, _link_entry(),
              _clip_hit(status="not_found", title="X", artist="Y")))
    check("T23 gate: confirmed clip hit -> eligible",
          h._smart_search_eligible(
              cb_audio, _link_entry(), _clip_hit(status="identified")))

    # T24 (fix 1D): one slow post page fails PER-POST, never the site.
    client = _FakeClient(
        searches={"javanmelody.ir": [
            {"title": "دانلود آهنگ امیر تتلو جفت 6 | کیفیت اصلی 320",
             "url": "https://javanmelody.ir/slow/x"},
            {"title": JAVAN_TITLE,
             "url": "https://javanmelody.ir/fast/x"}]},
        pages={"javanmelody.ir/slow": "<html>no links</html>",
               "javanmelody.ir/fast": _javan_page()},
        sleep_pages={"javanmelody.ir/slow": 8.0})
    t0 = time.monotonic()
    cands, ran = await sf._smart_wp_candidates(
        client, "https://javanmelody.ir", "wp-json", "q", ARTIST, TITLE)
    elapsed = time.monotonic() - t0
    check("T24 slow page fails per-post: site still returns the fast post",
          ran and any("Taziyane" in u and "320" in u for _, u, _ in cands),
          f"ran={ran} cands={cands} elapsed={elapsed:.2f}s")
    check("T24 slow page does not abort the site (returns in < 5 s)",
          elapsed < 5.0, f"elapsed={elapsed:.2f}s")

    # T25 (fix 1G): iTunes+Deezer both fail -> None + warning logged.
    # App loggers set propagate=False, so the capture handler must sit
    # on the smart module's own logger (same pattern as verify_speedfix).
    sf.log.addHandler(vs._ListHandler())
    vs.LOG_LINES.clear()
    client = _FakeClient(itunes=False,
                         searches={"javanmelody.ir": [],
                                   "melonmusic.ir": []})
    out = await sf.search_smart_full_version(
        ARTIST, TITLE, client=client, expected_duration_sec=None,
        workdir=tempfile.mkdtemp(prefix="smartsearch_"))
    check("T25 no catalog match: expected_duration_sec is None (gate skipped, not silent)",
          out.status == "not_found" and out.expected_duration_sec is None,
          f"status={out.status} expected={out.expected_duration_sec}")
    check("T25 no_expected_duration warning logged",
          any('"stage": "no_expected_duration"' in ln for ln in vs.LOG_LINES),
          f"lines={len(vs.LOG_LINES)}")

    # T26 (fix 2B): the search raising escapes nothing.
    async def _fake_raising(artist, title, clip_fingerprint=None):
        raise RuntimeError("search exploded")

    sf.search_smart_full_version = _fake_raising
    try:
        msg = _Msg(with_audio=True)
        status = await h._run_smart_search_tap(
            _Cb(msg), conn, _link_entry(), _clip_hit(), cache_key,
            token, "fa", req_id="req-t26")
    finally:
        sf.search_smart_full_version = orig_search
    check("T26 raising search: no exception escapes, tap returns 'error'",
          status == "error", f"status={status}")
    check("T26 raising search: exactly the honest error text sent",
          msg.answers == [t("m_smart_error", "fa")],
          f"answers={msg.answers}")

    # T27 (fix 5): per-site logging — both sites visible, even at 0.
    vs.LOG_LINES.clear()
    client = _FakeClient(
        searches={"javanmelody.ir": _javan_hits(), "melonmusic.ir": []},
        pages={"javanmelody.ir/10959": _javan_page()},
        mp3s={"dl.javanmelody.ir": javan_bytes})
    out = await _run_search(client)
    site_lines = [ln for ln in vs.LOG_LINES
                  if '"stage": "site_search"' in ln]
    check("T27 per-site log: a site_search line exists for BOTH sites",
          any('"site": "javanmelody.ir"' in ln for ln in site_lines)
          and any('"site": "melonmusic.ir"' in ln for ln in site_lines),
          f"site_lines={len(site_lines)}")
    check("T27 per-site log: melonmusic ran with 0 candidates is VISIBLE",
          any('"site": "melonmusic.ir"' in ln and '"ran": true' in ln
              and '"candidates": 0' in ln for ln in site_lines),
          f"melon={[ln for ln in site_lines if 'melonmusic' in ln]}")
    check("T27 per-site log: javanmelody line carries the WP hit count",
          any('"site": "javanmelody.ir"' in ln and '"wp_posts": 1' in ln
              for ln in site_lines))

    # T28 (fix 6): the tap returns its outcome status.
    for _fake, _want, _label in ((_fake_found, "found", "found"),
                                 (_fake_not_found, "not_found", "not_found"),
                                 (_fake_error, "error", "error")):
        sf.search_smart_full_version = _fake
        try:
            msg = _Msg(with_audio=True)
            status = await h._run_smart_search_tap(
                _Cb(msg), conn, _link_entry(), _clip_hit(), cache_key,
                token, "fa", req_id="req-t28")
        finally:
            sf.search_smart_full_version = orig_search
        check(f"T28 tap returns '{_want}' for {_label} outcome",
              status == _want, f"status={status}")
    check("T28 the tap's outcome lines carry the request id",
          any('"req_id": "req-t28"' in ln for ln in vs.LOG_LINES))
    # T28b: full video_identify run with a FOUND outcome logs total "found".
    vs.LOG_LINES.clear()
    token3 = "tok-smart-t28"
    entry3 = _link_entry()
    entry3["user_id"] = 572595158
    h._VIDEO_IDENTIFY[token3] = entry3
    key3 = h._identify_result_key(link=entry3["link"])
    h._identify_result_put(key3, {**_clip_hit(),
                                  "created_at": time.time(), "ttl": 600})
    sf.search_smart_full_version = _fake_found
    try:
        msg = _Msg(with_audio=True)
        cb = _Cb(msg, data=f"vid:id:{token3}")
        await h.video_identify(cb, conn, SimpleNamespace())
    finally:
        sf.search_smart_full_version = orig_search
        h._VIDEO_IDENTIFY.pop(token3, None)
        h._VIDEO_IDENTIFY_RUNNING.discard(token3)
    total_lines = [ln for ln in vs.LOG_LINES
                   if '"stage": "total"' in ln
                   and '"event": "identify_stage"' in ln]
    check("T28 video_identify total status is 'found' after a found smart tap",
          any('"status": "found"' in ln for ln in total_lines),
          f"total_lines={total_lines}")

    # T29 (fix 9 + 3-song test, REAL fixtures).
    # T29a: the extractor on the REAL melonmusic post-4021 page.
    got29a = sf._extract_mp3_urls(melon4021_html)
    check("T29a _extract_mp3_urls finds both mp3s on the REAL melonmusic 4021 page",
          set(got29a) == {
              "https://dl.melonmusic.ir/Music/Ho3ein%20-%20Sob%20Zohr%20Shab%20%5B128%5D.mp3",
              "https://dl.melonmusic.ir/Music/Ho3ein%20-%20Sob%20Zohr%20Shab%20%5B320%5D.mp3"},
          f"found={got29a}")
    check("T29a _FILE_RE alone finds NOTHING on that page (the live bug)",
          sf._FILE_RE.findall(melon4021_html) == [])

    # T29b: Sob Zohr Shab (Ho3ein, 196.8 s) — FOUND via melonmusic.
    sf._NEGATIVE.clear()
    client = _FakeClient(
        searches={"javanmelody.ir": _sob_javan_hits(),
                  "melonmusic.ir": _sob_melon_hits()},
        pages={"javanmelody.ir/1657": javan1657_html,
               "melonmusic.ir/4021": melon4021_html},
        mp3s={"Dj-Elvan-Remix": sob_remix_bytes,
              "dl.melonmusic.ir": sob_bytes})
    out = await sf.search_smart_full_version(
        "Ho3ein", "Sob Zohr Shab", client=client,
        workdir=tempfile.mkdtemp(prefix="smartsearch_"))
    check("T29b Sob Zohr Shab FOUND via melonmusic (javanmelody has only the remix)",
          out.status == "found" and out.file is not None
          and out.file.source == "smart:melonmusic.ir"
          and out.file.validation.get("status") == "match",
          f"status={out.status} detail={out.detail} "
          f"file={out.file.source if out.file else None} "
          f"validation={out.file.validation if out.file else None}")
    if out.file is not None:
        check("T29b found file is the original (196.7 s, not the 230.1 s remix)",
              abs(out.file.duration_sec - 196.7) < 1.0,
              f"duration={out.file.duration_sec}")
        sf._unlink_quietly(out.file.disk_path)
    sf._NEGATIVE.clear()

    # T29c: Joft 6 AS IDENTIFIED (HesamTiem) — NOT FOUND (correct rejections).
    sf._NEGATIVE.clear()
    client = _FakeClient(
        searches={"javanmelody.ir": _joft_javan_hits(),
                  "melonmusic.ir": _joft_melon_hits()},
        pages={"javanmelody.ir/11303": javan11303_html},
        mp3s={"Amir-Tataloo-Joft-shish": joft6_bytes})
    out = await sf.search_smart_full_version(
        "HesamTiem", "Joft 6", client=client,
        workdir=tempfile.mkdtemp(prefix="smartsearch_"))
    check("T29c Joft 6 as identified (HesamTiem) is NOT FOUND — the file is Tataloo's",
          out.status == "not_found" and out.checked >= 2,
          f"status={out.status} detail={out.detail} checked={out.checked}")
    sf._NEGATIVE.clear()

    # T29d: Joft 6 AS CATALOG (Amir Tataloo) — still NOT FOUND: "Joft shish".
    sf._NEGATIVE.clear()
    client = _FakeClient(
        searches={"javanmelody.ir": _joft_javan_hits(),
                  "melonmusic.ir": _joft_melon_hits()},
        pages={"javanmelody.ir/11303": javan11303_html},
        mp3s={"Amir-Tataloo-Joft-shish": joft6_bytes})
    out = await sf.search_smart_full_version(
        "Amir Tataloo", "Joft 6", client=client,
        workdir=tempfile.mkdtemp(prefix="smartsearch_"))
    check("T29d Joft 6 as catalog (Amir Tataloo) is NOT FOUND — 'Joft shish' title_mismatch",
          out.status == "not_found" and out.checked >= 2,
          f"status={out.status} detail={out.detail} checked={out.checked}")
    check("T29d the rejection reason is title_mismatch (digit 6 vs word shish)",
          sf.validate_smart_file(
              "Joft 6", "Amir Tataloo",
              "Joft shish [Amir Tataloo] From abanmusics.com",
              "Amir Tataloo", 225.4, 225.5).get("detail") == "title_mismatch")
    sf._NEGATIVE.clear()

    # T29e: En Güzel Tesadüfüm (Tefo & SEKO, reel Dd1niVHN07n) — NOT FOUND,
    # checked 0: both v1 sources genuinely carry no Tefo post (WP search
    # returned [] live 2026-11-10). The failure here is upstream
    # (identify), not search — even a confirmed name finds nothing.
    sf._NEGATIVE.clear()
    client = _FakeClient(searches={"javanmelody.ir": [],
                                   "melonmusic.ir": []})
    out = await sf.search_smart_full_version(
        "Tefo & SEKO", "En Güzel Tesadüfüm", client=client,
        workdir=tempfile.mkdtemp(prefix="smartsearch_"))
    check("T29e En Güzel Tesadüfüm NOT FOUND, checked 0 — v1 sources carry no Tefo post",
          out.status == "not_found" and out.detail == "no_verified_file"
          and out.checked == 0 and out.expected_duration_sec == 164.0,
          f"status={out.status} detail={out.detail} checked={out.checked} "
          f"expected={out.expected_duration_sec}")
    check("T29e both v1 sites WERE queried (searches ran, zero hits)",
          any("javanmelody.ir" in u for _, u in client.calls)
          and any("melonmusic.ir" in u for _, u in client.calls))
    sf._NEGATIVE.clear()

    # --- FIX ROUND 2 (2026-11-10): retry-tap regression ---------------
    # Live 268cb98a: a retry tap on a non-eligible clip hit fell
    # through to the identify-tap cache-hit branch and re-sent the
    # clip audio, and _set_video_result_kb stamped the video
    # keyboard onto the AUDIO message. Log corroboration (00:54
    # test, "Disconnect" — Jopitoo, unconfirmed): TWO sequential
    # requests 3a0c1b0c + 7bfa1573, 4.6 s apart, each exactly one
    # cache-hit send — the fix makes EACH invocation send nothing;
    # it never relies on the concurrent-only _VIDEO_IDENTIFY_RUNNING
    # guard. The keyboard stamp is code-only: the logs record no
    # keyboards at all.
    def _kb_buttons(kb):
        rows = getattr(kb, "inline_keyboard", None) or []
        return [b for row in rows for b in row]

    # T30a: retry tap on clip/unconfirmed/titled hit -> 0 audio sends.
    token30 = "tok-fix2-t30a"
    entry30 = _link_entry()
    entry30["user_id"] = 572595158
    h._VIDEO_IDENTIFY[token30] = entry30
    key30 = h._identify_result_key(link=entry30["link"])
    h._identify_result_put(key30, {**_clip_hit(status="unconfirmed"),
                                   "created_at": time.time(), "ttl": 600})
    vs.LOG_LINES.clear()
    try:
        msg = _Msg(with_audio=True)
        cb = _Cb(msg, data=f"vid:id:{token30}")
        await h.video_identify(cb, conn, SimpleNamespace())
    finally:
        h._VIDEO_IDENTIFY.pop(token30, None)
        h._VIDEO_IDENTIFY_RUNNING.discard(token30)
    check("T30a retry on unconfirmed clip: ZERO audio sends",
          msg.audio_calls == [], f"audio_calls={msg.audio_calls}")
    check("T30a retry on unconfirmed clip: honest not-identified text sent",
          msg.answers == [t("m_identify_searching", "fa"),
                          t("m_identify_head_notfound", "fa")],
          f"answers={msg.answers}")
    check("T30a retry on unconfirmed clip: no smart-search / error text",
          t("m_smart_not_found", "fa") not in msg.answers
          and t("m_smart_error", "fa") not in msg.answers,
          f"answers={msg.answers}")
    retry_btns = _kb_buttons(
        msg.edits_markup[-1] if msg.edits_markup else None)
    check("T30a retry on unconfirmed clip: audio keeps exactly the retry button",
          len(msg.edits_markup) == 2 and len(retry_btns) == 1
          and retry_btns[0].callback_data == f"vid:id:{token30}"
          and retry_btns[0].text == t("m_extracted_retry_btn", "fa"),
          f"edits={len(msg.edits_markup)} "
          f"btns={[(b.text, b.callback_data) for b in retry_btns]}")
    check("T30a retry on unconfirmed clip: no video keyboard on the audio",
          not any((b.url or "").startswith("https://www.google.com")
                  or (b.url or "").startswith("https://www.youtube.com")
                  or (b.callback_data or "").startswith("wrong:")
                  for b in retry_btns),
          f"btns={[(b.text, b.url, b.callback_data) for b in retry_btns]}")
    total30 = [ln for ln in vs.LOG_LINES
               if '"stage": "total"' in ln
               and '"event": "identify_stage"' in ln]
    check("T30a retry on unconfirmed clip: total status is 'unconfirmed', not 'error'",
          any('"status": "unconfirmed"' in ln for ln in total30),
          f"total={total30}")

    # T30a2: TWO sequential retry taps on the same unconfirmed clip
    # (the live 00:54 double-audio case) -> EACH tap sends 0 audio.
    h._VIDEO_IDENTIFY[token30] = entry30
    try:
        for _tap in (1, 2):
            msg = _Msg(with_audio=True)
            cb = _Cb(msg, data=f"vid:id:{token30}")
            await h.video_identify(cb, conn, SimpleNamespace())
            check(f"T30a2 sequential retry tap {_tap}: ZERO audio sends",
                  msg.audio_calls == [], f"audio_calls={msg.audio_calls}")
            check(f"T30a2 sequential retry tap {_tap}: honest text sent",
                  t("m_identify_head_notfound", "fa") in msg.answers,
                  f"answers={msg.answers}")
    finally:
        h._VIDEO_IDENTIFY.pop(token30, None)
        h._VIDEO_IDENTIFY_RUNNING.discard(token30)

    # T30b: retry tap on clip/IDENTIFIED hit still runs the smart search.
    token31 = "tok-fix2-t30b"
    entry31 = _link_entry()
    entry31["user_id"] = 572595158
    h._VIDEO_IDENTIFY[token31] = entry31
    key31 = h._identify_result_key(link=entry31["link"])
    h._identify_result_put(key31, {**_clip_hit(),
                                   "created_at": time.time(), "ttl": 600})
    sf.search_smart_full_version = _fake_not_found
    try:
        msg = _Msg(with_audio=True)
        cb = _Cb(msg, data=f"vid:id:{token31}")
        await h.video_identify(cb, conn, SimpleNamespace())
    finally:
        sf.search_smart_full_version = orig_search
        h._VIDEO_IDENTIFY.pop(token31, None)
        h._VIDEO_IDENTIFY_RUNNING.discard(token31)
    check("T30b retry on identified clip: smart search still runs (not-found text)",
          t("m_smart_not_found", "fa") in msg.answers
          and t("m_identify_head_notfound", "fa") not in msg.answers,
          f"answers={msg.answers}")
    check("T30b retry on identified clip: no audio re-sent",
          msg.audio_calls == [], f"audio_calls={msg.audio_calls}")

    # T30c: identify tap from a VIDEO on a clip hit -> audio once,
    # video keyboard on the VIDEO message.
    token32 = "tok-fix2-t30c"
    entry32 = _link_entry()
    entry32["user_id"] = 572595158
    h._VIDEO_IDENTIFY[token32] = entry32
    key32 = h._identify_result_key(link=entry32["link"])
    h._identify_result_put(key32, {**_clip_hit(status="unconfirmed",
                                               file_id="CLIP_FILE_ID"),
                                   "created_at": time.time(), "ttl": 600})
    try:
        msg = _Msg(with_audio=False)
        msg.video = SimpleNamespace(file_id="VID_FILE_ID")
        cb = _Cb(msg, data=f"vid:id:{token32}")
        await h.video_identify(cb, conn, SimpleNamespace())
    finally:
        h._VIDEO_IDENTIFY.pop(token32, None)
        h._VIDEO_IDENTIFY_RUNNING.discard(token32)
    check("T30c identify tap from video: clip audio sent exactly once",
          len(msg.audio_calls) == 1
          and msg.audio_calls[0][1] == "CLIP_FILE_ID",
          f"audio_calls={msg.audio_calls}")
    video_btns = _kb_buttons(
        msg.edits_markup[-1] if msg.edits_markup else None)
    check("T30c identify tap from video: video keyboard lands on the video message",
          len(msg.edits_markup) == 2 and len(video_btns) == 2
          and video_btns[0].text == t("m_video_google_btn", "fa")
          and (video_btns[0].url or "").startswith(
              "https://www.google.com/search?q=")
          and video_btns[1].text == t("m_video_youtube_btn", "fa")
          and (video_btns[1].url or "").startswith(
              "https://www.youtube.com/results?search_query="),
          f"edits={len(msg.edits_markup)} "
          f"btns={[(b.text, b.url) for b in video_btns]}")

    # T30d: _set_video_result_kb is never invoked for an audio message.
    orig_set_kb = h._set_video_result_kb
    kb_calls: list = []

    async def _recording_set_kb(message, *a, **k):
        kb_calls.append(getattr(message, "audio", None) is not None)
        return await orig_set_kb(message, *a, **k)

    h._set_video_result_kb = _recording_set_kb
    try:
        # Retry tap on the unconfirmed clip: the guard + early return
        # mean the video-keyboard setter is never even called.
        token33 = "tok-fix2-t30d"
        entry33 = _link_entry()
        entry33["user_id"] = 572595158
        h._VIDEO_IDENTIFY[token33] = entry33
        key33 = h._identify_result_key(link=entry33["link"])
        h._identify_result_put(key33, {**_clip_hit(status="unconfirmed"),
                                       "created_at": time.time(), "ttl": 600})
        try:
            msg = _Msg(with_audio=True)
            cb = _Cb(msg, data=f"vid:id:{token33}")
            await h.video_identify(cb, conn, SimpleNamespace())
        finally:
            h._VIDEO_IDENTIFY.pop(token33, None)
            h._VIDEO_IDENTIFY_RUNNING.discard(token33)
        check("T30d retry tap: _set_video_result_kb never called at all",
              kb_calls == [], f"kb_calls={kb_calls}")
        # Identify tap from a video: the setter IS called, on a video
        # message (audio is None).
        token34 = "tok-fix2-t30d2"
        entry34 = _link_entry()
        entry34["user_id"] = 572595158
        h._VIDEO_IDENTIFY[token34] = entry34
        key34 = h._identify_result_key(link=entry34["link"])
        h._identify_result_put(key34, {**_clip_hit(status="unconfirmed",
                                                   file_id="CLIP_FILE_ID"),
                                       "created_at": time.time(), "ttl": 600})
        try:
            msg = _Msg(with_audio=False)
            msg.video = SimpleNamespace(file_id="VID_FILE_ID")
            cb = _Cb(msg, data=f"vid:id:{token34}")
            await h.video_identify(cb, conn, SimpleNamespace())
        finally:
            h._VIDEO_IDENTIFY.pop(token34, None)
            h._VIDEO_IDENTIFY_RUNNING.discard(token34)
        check("T30d identify tap from video: setter called, never on audio",
              len(kb_calls) == 1 and kb_calls[0] is False,
              f"kb_calls={kb_calls}")
    finally:
        h._set_video_result_kb = orig_set_kb

    # --- FIX ROUND 3 (2026-11-10): Q4 supplementary identify ---------
    # Live 07e2f956: a retry tap on a non-eligible clip hit answered
    # m_identify_head_notfound after ~565 ms with ZERO work (fix
    # round 2 early return; log req 82c83d0d). Q4 re-identifies the
    # extracted clip from DIFFERENT windows (middle 25–75% + loudest
    # 10 s) before any not-found, and proceeds to the smart search
    # when a slice CONFIRMS the song.
    import verify_fullsong_match as vm  # noqa: E402 (small mp3 fixture)
    from app.services.audio_id import AudioIdResult  # noqa: E402

    orig_identify = h._identify_audio
    _clip_mp3 = vm._make_mp3(TITLE, ARTIST, seconds=12)
    _clip_path = os.path.join(tempfile.mkdtemp(prefix="q4_clip_"), "clip.mp3")
    with open(_clip_path, "wb") as _fh:
        _fh.write(_clip_mp3)

    def _seed_q4_clip(token, status="unconfirmed"):
        entry = _link_entry()
        entry["user_id"] = 572595158
        h._VIDEO_IDENTIFY[token] = entry
        key = h._identify_result_key(link=entry["link"])
        h._identify_result_put(key, {**_clip_hit(
            status=status, file_id="CLIP_FILE_ID", disk_path=_clip_path),
            "created_at": time.time(), "ttl": 600})
        return key

    def _drop_q4_token(token):
        h._VIDEO_IDENTIFY.pop(token, None)
        h._VIDEO_IDENTIFY_RUNNING.discard(token)

    # T31a: Q4 CONFIRMS on the middle slice -> the 1b entry becomes
    # identified (clip file kept) and the smart search runs.
    _q4_calls: list[dict] = []

    async def _fake_q4_identify(data, filename, config, conn=None,
                                cache_url_key=None, req_id=None, t_req=None,
                                exclude=None, skip_bank=False, **kw):
        _q4_calls.append({"data": bytes(data), "filename": filename,
                          "cache_url_key": cache_url_key,
                          "exclude": exclude,
                          "q4_fast": kw.get("q4_fast"),
                          "predecoded_wav": kw.get("predecoded_wav")})
        return AudioIdResult(status="identified", title=TITLE, artist=ARTIST,
                             confirmed=True, source="shazam+audd")

    key31a = _seed_q4_clip("tok-fix3-t31a")
    h._identify_audio = _fake_q4_identify
    sf.search_smart_full_version = _fake_not_found
    try:
        msg = _Msg(with_audio=True)
        cb = _Cb(msg, data="vid:id:tok-fix3-t31a")
        await h.video_identify(cb, conn, SimpleNamespace())
    finally:
        h._identify_audio = orig_identify
        sf.search_smart_full_version = orig_search
        _drop_q4_token("tok-fix3-t31a")
    check("T31a Q4 confirms: _identify_audio ran on a WAV slice (not the mp3 bytes)",
          len(_q4_calls) == 1 and _q4_calls[0]["data"][:4] == b"RIFF"
          and _q4_calls[0]["data"] != _clip_mp3,
          f"calls={len(_q4_calls)}")
    check("T31a Q4 confirms: cache_url_key never passed (Stage-0 bypass)",
          all(c["cache_url_key"] is None for c in _q4_calls),
          f"calls={_q4_calls}")
    check("T31a Q4 confirms: smart search ran (not-found text, no head-not-found)",
          t("m_smart_not_found", "fa") in msg.answers
          and t("m_identify_head_notfound", "fa") not in msg.answers,
          f"answers={msg.answers}")
    check("T31a Q4 confirms: no audio re-sent",
          msg.audio_calls == [], f"audio_calls={msg.audio_calls}")
    _upd31a = h._identify_result_get(key31a)
    check("T31a Q4 confirms: 1b entry is now identified, clip file kept",
          _upd31a is not None and _upd31a.get("status") == "identified"
          and _upd31a.get("kind") == "clip"
          and _upd31a.get("file_id") == "CLIP_FILE_ID"
          and _upd31a.get("title") == TITLE,
          f"entry={_upd31a}")
    check("T31a Q4 confirms: fast path used (q4_fast=True, slice WAV reused)",
          len(_q4_calls) == 1 and _q4_calls[0].get("q4_fast") is True
          and _q4_calls[0].get("predecoded_wav") == _q4_calls[0]["data"],
          f"calls={_q4_calls}")

    # T31b: Q4 finds nothing on either slice -> honest head-not-found,
    # zero audio, smart search NEVER runs, per-slice lines logged.
    _q4_calls_b: list[dict] = []

    async def _fake_q4_unconfirmed(data, filename, config, conn=None,
                                   cache_url_key=None, req_id=None, t_req=None,
                                   exclude=None, skip_bank=False, **kw):
        _q4_calls_b.append({"data": bytes(data)})
        return AudioIdResult(status="unconfirmed", title="Pistol",
                             artist="Tiem & Salii", confirmed=False,
                             source="shazam")

    async def _recording_search_must_not_run(artist, title,
                                             clip_fingerprint=None):
        raise AssertionError(
            "smart search must not run when Q4 does not confirm")

    _seed_q4_clip("tok-fix3-t31b")
    h._identify_audio = _fake_q4_unconfirmed
    sf.search_smart_full_version = _recording_search_must_not_run
    vs.LOG_LINES.clear()
    try:
        msg = _Msg(with_audio=True)
        cb = _Cb(msg, data="vid:id:tok-fix3-t31b")
        await h.video_identify(cb, conn, SimpleNamespace())
    finally:
        h._identify_audio = orig_identify
        sf.search_smart_full_version = orig_search
        _drop_q4_token("tok-fix3-t31b")
    check("T31b Q4 unconfirmed: BOTH slices tried with different bytes",
          len(_q4_calls_b) == 2
          and _q4_calls_b[0]["data"] != _q4_calls_b[1]["data"],
          f"calls={len(_q4_calls_b)}")
    check("T31b Q4 unconfirmed: honest head-not-found, no smart text",
          t("m_identify_head_notfound", "fa") in msg.answers
          and t("m_smart_not_found", "fa") not in msg.answers,
          f"answers={msg.answers}")
    check("T31b Q4 unconfirmed: ZERO audio sends",
          msg.audio_calls == [], f"audio_calls={msg.audio_calls}")
    check("T31b Q4 unconfirmed: supplementary_identify logged per slice",
          any('"stage": "supplementary_identify"' in ln
              and '"slice": "middle"' in ln for ln in vs.LOG_LINES)
          and any('"stage": "supplementary_identify"' in ln
                  and '"slice": "loudest"' in ln for ln in vs.LOG_LINES),
          f"lines={[ln for ln in vs.LOG_LINES if 'supplementary' in ln]}")

    # T31c: no clip bytes anywhere (no disk file, no file_id) ->
    # honest text, no crash, _identify_audio never called.
    async def _fake_q4_must_not_run(data, *a, **k):
        raise AssertionError(
            "_identify_audio must not run without clip bytes")

    token31c = "tok-fix3-t31c"
    entry31c = _link_entry()
    entry31c["user_id"] = 572595158
    h._VIDEO_IDENTIFY[token31c] = entry31c
    key31c = h._identify_result_key(link=entry31c["link"])
    h._identify_result_put(key31c, {**_clip_hit(status="unconfirmed",
                                                file_id="", disk_path=""),
                                    "created_at": time.time(), "ttl": 600})
    h._identify_audio = _fake_q4_must_not_run
    try:
        msg = _Msg(with_audio=True)
        cb = _Cb(msg, data=f"vid:id:{token31c}")
        await h.video_identify(cb, conn, SimpleNamespace())
    finally:
        h._identify_audio = orig_identify
        _drop_q4_token(token31c)
    check("T31c Q4 no bytes: honest head-not-found, ZERO audio",
          t("m_identify_head_notfound", "fa") in msg.answers
          and msg.audio_calls == [], f"answers={msg.answers}")

    # T31d: a hung slice is abandoned by the per-slice timeout (the
    # tap never hangs): slice 1 sleeps, slice 2 answers unconfirmed.
    _q4_calls_d: list[dict] = []

    async def _fake_q4_hang_then_answer(data, filename, config, conn=None,
                                        cache_url_key=None, req_id=None,
                                        t_req=None, exclude=None,
                                        skip_bank=False, **kw):
        _q4_calls_d.append({"data": bytes(data)})
        if len(_q4_calls_d) == 1:
            await asyncio.sleep(30)
        return AudioIdResult(status="unconfirmed", title="Pistol",
                             artist="Tiem & Salii", confirmed=False,
                             source="shazam")

    _old_slice_to = h._Q4_SLICE_TIMEOUT_SEC
    _old_budget = h._Q4_TOTAL_BUDGET_SEC
    h._Q4_SLICE_TIMEOUT_SEC = 0.2
    h._Q4_TOTAL_BUDGET_SEC = 2.0
    _seed_q4_clip("tok-fix3-t31d")
    h._identify_audio = _fake_q4_hang_then_answer
    sf.search_smart_full_version = _recording_search_must_not_run
    _t31d = time.perf_counter()
    try:
        msg = _Msg(with_audio=True)
        cb = _Cb(msg, data="vid:id:tok-fix3-t31d")
        await h.video_identify(cb, conn, SimpleNamespace())
    finally:
        h._identify_audio = orig_identify
        sf.search_smart_full_version = orig_search
        h._Q4_SLICE_TIMEOUT_SEC = _old_slice_to
        h._Q4_TOTAL_BUDGET_SEC = _old_budget
        _drop_q4_token("tok-fix3-t31d")
    _dt31d = time.perf_counter() - _t31d
    check("T31d Q4 timeout: hung slice abandoned, tap finished fast",
          _dt31d < 5.0, f"elapsed={_dt31d:.2f}s")
    check("T31d Q4 timeout: both slices attempted, honest text sent",
          len(_q4_calls_d) == 2
          and t("m_identify_head_notfound", "fa") in msg.answers,
          f"calls={len(_q4_calls_d)} answers={msg.answers}")

    # T31e: _loudest_wav_window picks the loudest 10 s span.
    import struct as _struct

    def _make_wav(seconds=20, loud=(12, 14), amp=10000):
        sr = 22050
        n = seconds * sr
        samples = [0] * n
        for s in range(loud[0] * sr, loud[1] * sr):
            samples[s] = amp
        pcm = _struct.pack(f"<{n}h", *samples)
        header = _struct.pack("<4sI4s4sIHHIIHH4sI",
                              b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16,
                              1, 1, sr, sr * 2, 2, 16, b"data", len(pcm))
        return header + pcm

    _wav20 = _make_wav(20, loud=(12, 14))
    _loud_win = h._loudest_wav_window(_wav20, 10.0)
    check("T31e loudest window: 10 s slice, not the whole clip",
          _loud_win is not None and len(_loud_win) == 44 + 10 * 22050 * 2,
          f"len={None if _loud_win is None else len(_loud_win)}")
    _pcm = _loud_win[44:] if _loud_win is not None else b""
    _maxamp = max((abs(_v) for _v in _struct.unpack(
        f"<{len(_pcm) // 2}h", _pcm)), default=0) if _pcm else 0
    check("T31e loudest window: slice covers the loud burst",
          _maxamp == 10000, f"maxamp={_maxamp}")
    _wav5 = _make_wav(5, loud=(1, 2))
    check("T31e loudest window: short clip returns the whole WAV",
          h._loudest_wav_window(_wav5, 10.0) == _wav5)
    check("T31e loudest window: non-WAV returns None",
          h._loudest_wav_window(b"not a wav", 10.0) is None)

    # T31f (Q-C): _identify_audio logs each provider's title/artist
    # on lookup_response lines (the disagreement cause was invisible).
    # ACRCloud is not exercised: its env vars are unset by user order.
    orig_shazam = h.identify_bytes_shazam
    orig_audd = h.identify_bytes

    async def _fake_shazam_qc(probe, prepared_wav=None, req_id=None):
        return AudioIdResult(status="identified", title="Pistol",
                             artist="Tiem & Salii", confirmed=True,
                             source="shazam")

    async def _fake_audd_qc(data, filename, token):
        return AudioIdResult(status="identified", title="Other Song",
                             artist="Someone Else", confirmed=True,
                             source="audd")

    h.identify_bytes_shazam = _fake_shazam_qc
    h.identify_bytes = _fake_audd_qc
    vs.LOG_LINES.clear()
    try:
        _res_qc = await h._identify_audio(
            vm._make_mp3("Pistol", "Tiem & Salii", seconds=6), "clip.mp3",
            SimpleNamespace(audd_token="fake", acrcloud_host="",
                            acrcloud_key="", acrcloud_secret=""),
            conn, req_id="qc-test")
    finally:
        h.identify_bytes_shazam = orig_shazam
        h.identify_bytes = orig_audd
    check("T31f Q-C: disagreement stays unconfirmed (Shazam name shown)",
          _res_qc.status == "unconfirmed" and _res_qc.title == "Pistol",
          f"res={_res_qc}")
    _lookups = [ln for ln in vs.LOG_LINES
                if '"stage": "lookup_response"' in ln]
    check("T31f Q-C: Shazam lookup_response carries its title/artist",
          any('"window": 0' in ln and '"title": "Pistol"' in ln
              and '"artist": "Tiem & Salii"' in ln for ln in _lookups),
          f"lookups={_lookups}")
    check("T31f Q-C: AudD lookup_response carries its title/artist",
          any('"window": "audd"' in ln and '"title": "Other Song"' in ln
              and '"artist": "Someone Else"' in ln for ln in _lookups),
          f"lookups={_lookups}")

    # --- FIX ROUND 4 (2026-11-11): Q4 hoisted + cheapened -----------
    # (a) video tap on a cache-hit unconfirmed clip runs Q4 instead
    # of re-sending the clip audio that is already on screen.
    _q4_calls_32a: list[dict] = []

    async def _fake_q4_32a(data, filename, config, conn=None,
                           cache_url_key=None, req_id=None, t_req=None,
                           exclude=None, skip_bank=False, **kw):
        _q4_calls_32a.append({"data": bytes(data)})
        return AudioIdResult(status="unconfirmed", title="Pistol",
                             artist="Tiem & Salii", confirmed=False,
                             source="shazam")

    _seed_q4_clip("tok-fix4-t32a")
    h._identify_audio = _fake_q4_32a
    vs.LOG_LINES.clear()
    try:
        msg = _Msg(with_audio=False)
        msg.video = SimpleNamespace(file_id="VID_FILE_ID")
        cb = _Cb(msg, data="vid:id:tok-fix4-t32a")
        await h.video_identify(cb, conn, SimpleNamespace())
    finally:
        h._identify_audio = orig_identify
        _drop_q4_token("tok-fix4-t32a")
    check("T32a video cache-hit: Q4 ran (BOTH slices tried)",
          len(_q4_calls_32a) == 2
          and _q4_calls_32a[0]["data"] != _q4_calls_32a[1]["data"],
          f"calls={len(_q4_calls_32a)}")
    check("T32a video cache-hit: NO second audio sent",
          msg.audio_calls == [], f"audio_calls={msg.audio_calls}")
    check("T32a video cache-hit: honest head-not-found text sent",
          msg.answers == [t("m_identify_searching", "fa"),
                          t("m_identify_head_notfound", "fa")],
          f"answers={msg.answers}")
    retry_btns_32a = _kb_buttons(
        msg.edits_markup[-1] if msg.edits_markup else None)
    check("T32a video cache-hit: retry button restored on the video",
          len(msg.edits_markup) == 2 and len(retry_btns_32a) == 1
          and retry_btns_32a[0].callback_data == "vid:id:tok-fix4-t32a"
          and retry_btns_32a[0].text == t("m_extracted_retry_btn", "fa"),
          f"edits={len(msg.edits_markup)} "
          f"btns={[(b.text, b.callback_data) for b in retry_btns_32a]}")

    # --- T32b/d helper: drive a tap whose BG phase awaits a fake bg
    # job. The job stores `bg_entry` (or nothing) only once the tap
    # is actually inside _await_identify_bg, so the tap can never
    # race ahead and spawn a real bg job.
    orig_await_bg = h._await_identify_bg
    orig_send_hit = h._send_identify_cache_hit

    async def _drive_bg_tap(token, with_audio, bg_entry):
        entry = _link_entry()
        entry["user_id"] = 572595158
        h._VIDEO_IDENTIFY[token] = entry
        key = h._identify_result_key(link=entry["link"])
        # No cache hit at tap time (the evening bg-wait case): drop
        # any entry an earlier test left under this shortcode, or
        # the tap would answer from the cache and never reach the
        # BG phase.
        h._IDENTIFY_RESULT_CACHE.pop(key, None)
        _release = asyncio.Event()
        _entered = asyncio.Event()
        _drive_error = ""

        async def _fake_bg_job():
            await _release.wait()
            if bg_entry is not None:
                h._identify_result_put(key, {**bg_entry,
                                             "created_at": time.time(),
                                             "ttl": 600})

        async def _await_bg_tracked(k, timeout=120.0):
            _entered.set()
            return await orig_await_bg(k, timeout=timeout)

        _bg_task = asyncio.create_task(_fake_bg_job())
        h._IDENTIFY_BG_TASKS[key] = _bg_task
        h._await_identify_bg = _await_bg_tracked
        msg = _Msg(with_audio=with_audio)
        if not with_audio:
            msg.video = SimpleNamespace(file_id="VID_FILE_ID")
        cb = _Cb(msg, data=f"vid:id:{token}")
        _vtask = None
        try:
            _vtask = asyncio.create_task(
                h.video_identify(cb, conn, SimpleNamespace()))
            await asyncio.wait_for(_entered.wait(), timeout=10)
            _release.set()
            await _vtask
        except Exception as exc:
            _drive_error = f"{type(exc).__name__}: {exc}"
        finally:
            _release.set()
            if _vtask is not None and not _vtask.done():
                _vtask.cancel()
            h._await_identify_bg = orig_await_bg
            h._IDENTIFY_BG_TASKS.pop(key, None)
            _drop_q4_token(token)
        return msg, key, _drive_error

    # T32b: Q4 runs after a bg-wait completes with an unconfirmed
    # clip (the evening 10.6/38.5/85.9 s waits never got a Q4
    # attempt). The bg send is mocked to fail so the tap reaches
    # call site C; the smart search must not run on an unconfirmed
    # name.
    _q4_calls_32b: list[dict] = []
    _search_calls_32b: list = []

    async def _fake_q4_32b(data, filename, config, conn=None,
                           cache_url_key=None, req_id=None, t_req=None,
                           exclude=None, skip_bank=False, **kw):
        _q4_calls_32b.append({"data": bytes(data)})
        return AudioIdResult(status="unconfirmed", title="Pistol",
                             artist="Tiem & Salii", confirmed=False,
                             source="shazam")

    async def _fake_send_hit_32b(*a, **k):
        return None

    async def _rec_search_32b(artist, title, clip_fingerprint=None):
        _search_calls_32b.append((artist, title))
        return await _fake_not_found(artist, title, clip_fingerprint)

    _bg_entry_32b = {**_clip_hit(status="unconfirmed", file_id="",
                                 disk_path=_clip_path)}
    h._identify_audio = _fake_q4_32b
    h._send_identify_cache_hit = _fake_send_hit_32b
    sf.search_smart_full_version = _rec_search_32b
    vs.LOG_LINES.clear()
    try:
        msg32b, _key32b, _err32b = await _drive_bg_tap(
            "tok-fix4-t32b", False, _bg_entry_32b)
    finally:
        h._identify_audio = orig_identify
        h._send_identify_cache_hit = orig_send_hit
        sf.search_smart_full_version = orig_search
    check("T32b bg-wait: tap completed without hanging",
          _err32b == "" and msg32b is not None, _err32b)
    if msg32b is not None:
        check("T32b bg-wait: Q4 ran on the bg entry (BOTH slices)",
              len(_q4_calls_32b) == 2
              and _q4_calls_32b[0]["data"] != _q4_calls_32b[1]["data"],
              f"calls={len(_q4_calls_32b)}")
        check("T32b bg-wait: smart search never ran (unconfirmed)",
              _search_calls_32b == [], f"calls={_search_calls_32b}")
        check("T32b bg-wait: NO audio sent",
              msg32b.audio_calls == [], f"audio_calls={msg32b.audio_calls}")
        _bg_fail = t("h_clip_audio_failed", "fa").format(
            head=t("m_identify_error", "fa"))
        check("T32b bg-wait: honest BG FAILURE text sent",
              _bg_fail in msg32b.answers, f"answers={msg32b.answers}")
        _supp32b = [i for i, ln in enumerate(vs.LOG_LINES)
                    if '"stage": "supplementary_identify"' in ln]
        _send32b = [i for i, ln in enumerate(vs.LOG_LINES)
                    if '"stage": "result_send"' in ln]
        check("T32b bg-wait: supplementary slices logged BEFORE the honest message",
              bool(_supp32b) and bool(_send32b)
              and min(_supp32b) < min(_send32b),
              f"supp={_supp32b} send={_send32b}")

    # T32c: a slice exceeding its budget logs the abandon reason
    # (detail=slice_timeout + stage_in_flight) with the REAL
    # _identify_audio and a hanging Shazam — the evening slices
    # died at exactly 15,001 ms with NO reason in the logs.
    orig_shazam = h.identify_bytes_shazam
    orig_audd = h.identify_bytes

    async def _fake_shazam_hang(probe, prepared_wav=None, req_id=None):
        await asyncio.sleep(30)
        return AudioIdResult(status="identified", title="Pistol",
                             artist="Tiem & Salii", confirmed=True,
                             source="shazam")

    async def _fake_audd_nf(data, filename, token):
        return AudioIdResult(status="not_found")

    h.identify_bytes_shazam = _fake_shazam_hang
    h.identify_bytes = _fake_audd_nf
    _old_slice_to = h._Q4_SLICE_TIMEOUT_SEC
    _old_budget = h._Q4_TOTAL_BUDGET_SEC
    h._Q4_SLICE_TIMEOUT_SEC = 3.0
    h._Q4_TOTAL_BUDGET_SEC = 12.0
    _seed_q4_clip("tok-fix4-t32c")
    vs.LOG_LINES.clear()
    _t32c = time.perf_counter()
    try:
        msg = _Msg(with_audio=True)
        cb = _Cb(msg, data="vid:id:tok-fix4-t32c")
        await h.video_identify(
            cb, conn,
            SimpleNamespace(audd_token="fake", acrcloud_host="",
                            acrcloud_key="", acrcloud_secret=""))
    finally:
        h.identify_bytes_shazam = orig_shazam
        h.identify_bytes = orig_audd
        h._Q4_SLICE_TIMEOUT_SEC = _old_slice_to
        h._Q4_TOTAL_BUDGET_SEC = _old_budget
        _drop_q4_token("tok-fix4-t32c")
    _dt32c = time.perf_counter() - _t32c
    _supp32c = [ln for ln in vs.LOG_LINES
                if '"stage": "supplementary_identify"' in ln]
    check("T32c slice timeout: tap finished fast, no hang/crash",
          _dt32c < 20.0, f"elapsed={_dt32c:.2f}s")
    check("T32c slice timeout: honest head-not-found text sent",
          t("m_identify_head_notfound", "fa") in msg.answers,
          f"answers={msg.answers}")
    check("T32c slice timeout: slice logged detail=slice_timeout",
          any('"detail": "slice_timeout"' in ln for ln in _supp32c),
          f"lines={_supp32c}")
    check("T32c slice timeout: slice logged stage_in_flight=shazam_window:1",
          any('"stage_in_flight": "shazam_window:1"' in ln
              for ln in _supp32c),
          f"lines={_supp32c}")

    # T32d: Q4 confirming AFTER a bg wait triggers the smart search
    # (call site C routing): audio tap, no cache hit at tap time,
    # bg completes with an unconfirmed clip, Q4 confirms on the
    # first slice, and the smart search queries BOTH v1 sources.
    _q4_calls_32d: list[dict] = []
    _search_clients_32d: list = []

    async def _fake_q4_32d(data, filename, config, conn=None,
                           cache_url_key=None, req_id=None, t_req=None,
                           exclude=None, skip_bank=False, **kw):
        _q4_calls_32d.append({"data": bytes(data)})
        return AudioIdResult(status="identified", title=TITLE, artist=ARTIST,
                             confirmed=True, source="shazam+audd")

    async def _fake_send_hit_32d(*a, **k):
        return None

    async def _real_search_32d(artist, title, clip_fingerprint=None):
        _client = _FakeClient(
            searches={"javanmelody.ir": _javan_hits(),
                      "melonmusic.ir": _melon_hits()},
            pages={"javanmelody.ir/10959": _javan_page(),
                   "melonmusic.ir/1908": _melon_page()},
            mp3s={"dl.javanmelody.ir": javan_bytes,
                  "dl.melonmusic.ir": melon_bytes})
        _search_clients_32d.append(_client)
        return await orig_search(
            artist, title, clip_fingerprint=clip_fingerprint,
            client=_client, expected_duration_sec=EXPECTED,
            workdir=tempfile.mkdtemp(prefix="smartsearch_"))

    _bg_entry_32d = {**_clip_hit(status="unconfirmed", file_id="",
                                 disk_path=_clip_path)}
    sf._NEGATIVE.clear()
    h._identify_audio = _fake_q4_32d
    h._send_identify_cache_hit = _fake_send_hit_32d
    sf.search_smart_full_version = _real_search_32d
    vs.LOG_LINES.clear()
    try:
        msg32d, _key32d, _err32d = await _drive_bg_tap(
            "tok-fix4-t32d", True, _bg_entry_32d)
    finally:
        h._identify_audio = orig_identify
        h._send_identify_cache_hit = orig_send_hit
        sf.search_smart_full_version = orig_search
    check("T32d bg-wait confirm: tap completed without hanging",
          _err32d == "" and msg32d is not None, _err32d)
    if msg32d is not None:
        check("T32d bg-wait confirm: Q4 confirmed on the FIRST slice",
              len(_q4_calls_32d) == 1, f"calls={len(_q4_calls_32d)}")
        _calls32d = (_search_clients_32d[0].calls
                     if _search_clients_32d else [])
        check("T32d bg-wait confirm: smart search queried BOTH v1 sources",
              len(_search_clients_32d) == 1
              and any("javanmelody.ir" in u for _, u in _calls32d)
              and any("melonmusic.ir" in u for _, u in _calls32d),
              f"calls={_calls32d}")
        check("T32d bg-wait confirm: full file sent exactly once",
              len(msg32d.audio_calls) == 1,
              f"audio_calls={msg32d.audio_calls}")
        _total32d = [ln for ln in vs.LOG_LINES
                     if '"stage": "total"' in ln
                     and '"event": "identify_stage"' in ln]
        check("T32d bg-wait confirm: total status is 'found'",
              any('"status": "found"' in ln for ln in _total32d),
              f"total={_total32d}")

    failed = [r for r in vs.RESULTS if not r[1]]
    print(f"\n{len(vs.RESULTS) - len(failed)}/{len(vs.RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
