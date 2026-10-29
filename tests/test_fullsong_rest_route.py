"""find_full_song_file: rest_route API style (musicsweb.ir).

musicsweb.ir disables pretty permalinks for the WP API: /wp-json/... is
empty, but ?rest_route=/wp/v2/... works (proven live 2026-10-29: WP
search + post HTML + real 9.6 MB / 220 s MP3). The resolver must query
both URL styles and validate downloads identically.

No network: httpx is faked; _fingerprint_duration is stubbed.
"""
import pytest

from app.search import fullsong as fs

ARTIST, TITLE = "Test Artist", "Test Song"
POST_TITLE = "دانلود آهنگ Test Song از Test Artist"
POST_URL = "https://musicsweb.ir/content/99/"
MP3_URL = "https://dl.musicsweb.ir/musics/04/10/Test%20Artist%20-%20Test%20Song.mp3"

MP3_BYTES = b"ID3\x04\x00" + b"\x00" * 300_000


class _FakeResp:
    def __init__(self, status=200, json_data=None, text="", content=b""):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.content = content

    def json(self):
        return self._json


class _FakeClient:
    """Only the rest_route site answers; wp-json sites return []."""

    def __init__(self, *a, **k):
        self.requested = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        url = str(url)
        self.requested.append(url)
        if "rest_route=/wp/v2/search" in url:
            return _FakeResp(json_data=[
                {"id": 99, "title": POST_TITLE, "url": POST_URL}])
        if "wp-json/wp/v2/search" in url:
            return _FakeResp(json_data=[])
        if "rest_route=/wp/v2/posts/99" in url:
            return _FakeResp(json_data={
                "content": {"rendered": "<p>stripped, no links</p>"}})
        if url.startswith(POST_URL):
            return _FakeResp(text=(
                '<html><body><a href="%s">دانلود</a></body></html>' % MP3_URL))
        if url.startswith("https://dl.musicsweb.ir/"):
            return _FakeResp(content=MP3_BYTES)
        raise AssertionError(f"unexpected GET {url}")


@pytest.fixture()
def fake_client(monkeypatch):
    clients = []

    def factory(*a, **k):
        c = _FakeClient(*a, **k)
        clients.append(c)
        return c

    monkeypatch.setattr(fs.httpx, "AsyncClient", factory)
    monkeypatch.setattr(fs, "_fingerprint_duration", lambda data: 200.0)
    fs._CACHE.clear()
    return clients


def test_url_helpers():
    assert fs._search_url("https://x.ir", "wp-json") == \
        "https://x.ir/wp-json/wp/v2/search"
    assert fs._search_url("https://x.ir", "rest_route") == \
        "https://x.ir/?rest_route=/wp/v2/search"
    assert fs._post_url("https://x.ir", "wp-json", 5) == \
        "https://x.ir/wp-json/wp/v2/posts/5"
    assert fs._post_url("https://x.ir", "rest_route", 5) == \
        "https://x.ir/?rest_route=/wp/v2/posts/5"


def test_music_sites_include_rest_route_source():
    styles = dict(fs.MUSIC_SITES)
    assert styles.get("https://musicsweb.ir") == "rest_route"
    assert len(fs.MUSIC_SITES) >= 4


@pytest.mark.asyncio
async def test_find_full_song_via_rest_route(fake_client):
    res = await fs.find_full_song_file(ARTIST, TITLE)
    assert res is not None
    assert res["data"] == MP3_BYTES
    assert res["ext"] == "mp3"
    assert res["duration_sec"] == 200.0
    assert res["source"] == "musicsweb.ir"
    searched = [u for u in fake_client[0].requested if "v2/search" in u]
    # All four sites queried; the rest_route one answered.
    assert any("rest_route=/wp/v2/search" in u for u in searched)
    assert sum("wp-json/wp/v2/search" in u for u in searched) == 3


@pytest.mark.asyncio
async def test_hard_query_still_honest_none(fake_client):
    # Underground track on no probed source: honest None, never a fake.
    res = await fs.find_full_song_file("Kaka3yd", "KHORKHEH")
    assert res is None


@pytest.mark.asyncio
async def test_highest_bitrate_candidate_wins(monkeypatch, fake_client):
    """Among valid candidates the highest real bitrate is returned.

    Two fake MP3s (128k vs 320k, distinguished by _probe_bitrate_kbps
    stub): the 320k file must win even though both pass every gate.
    """
    low = b"ID3\x04\x00" + b"\x00" * 300_000
    high = b"ID3\x04\x00" + b"\x01" * 300_000

    class _TwoFiles(_FakeClient):
        async def get(self, url, **k):
            url = str(url)
            self.requested.append(url)
            if "rest_route=/wp/v2/search" in url:
                return _FakeResp(json_data=[
                    {"id": 99, "title": POST_TITLE, "url": POST_URL}])
            if "wp-json/wp/v2/search" in url:
                return _FakeResp(json_data=[])
            if "rest_route=/wp/v2/posts/99" in url:
                return _FakeResp(json_data={
                    "content": {"rendered": "<p>stripped</p>"}})
            if url.startswith(POST_URL):
                return _FakeResp(text=(
                    '<html><body>'
                    '<a href="https://dl.musicsweb.ir/a-128.mp3">128</a>'
                    '<a href="https://dl.musicsweb.ir/a-320.mp3">320</a>'
                    '</body></html>'))
            if url.endswith("a-128.mp3"):
                return _FakeResp(content=low)
            if url.endswith("a-320.mp3"):
                return _FakeResp(content=high)
            raise AssertionError(f"unexpected GET {url}")

    def factory(*a, **k):
        return _TwoFiles(*a, **k)

    monkeypatch.setattr(fs.httpx, "AsyncClient", factory)
    monkeypatch.setattr(
        fs, "_probe_bitrate_kbps",
        lambda data, ext: 320 if data == high else 128)
    fs._CACHE.clear()
    res = await fs.find_full_song_file(ARTIST, TITLE)
    assert res is not None
    assert res["data"] == high
