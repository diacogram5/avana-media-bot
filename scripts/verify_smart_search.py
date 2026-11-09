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
    sleep_hosts: {host: seconds} (sleeps before answering a search).
    """

    def __init__(self, *, itunes=True, searches=None, pages=None,
                 mp3s=None, sleep_hosts=None):
        self.itunes = itunes
        self.searches = searches or {}
        self.pages = pages or {}
        self.mp3s = mp3s or {}
        self.sleep_hosts = sleep_hosts or {}
        self.calls: list[tuple[str, str]] = []

    async def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url))
        if "itunes.apple.com/search" in url:
            if not self.itunes:
                return _FakeResp(404)
            return _FakeResp(200, {"results": [{
                "trackName": TITLE, "artistName": ARTIST,
                "trackTimeMillis": 341026,
                "trackViewUrl": "https://music.apple.com/us/album/x",
                "previewUrl": "https://audio-ssl.itunes.apple.com/x.m4a"}]})
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
                if html == "raise":
                    raise RuntimeError("page failed")
                return _FakeResp(200, text=html)
        return _FakeResp(404)

    def stream(self, method, url, headers=None):
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
    for p in (JAVAN, MELON, MSB):
        if not p.is_file():
            print(f"missing fixture: {p}")
            return 1
    javan_bytes = _fixture_bytes(JAVAN)
    melon_bytes = _fixture_bytes(MELON)
    msb_bytes = _fixture_bytes(MSB)

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

    failed = [r for r in vs.RESULTS if not r[1]]
    print(f"\n{len(vs.RESULTS) - len(failed)}/{len(vs.RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
