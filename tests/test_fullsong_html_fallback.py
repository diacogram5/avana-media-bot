"""find_full_song_file: post-HTML fallback + content validation (Task 2).

These music sites strip download links from the WP API content, so when
the API content yields no file URL the resolver must fall back to the
post's public HTML page (the search result `url`) to find the real mp3.
Downloaded bytes must pass the strict magic-byte gate before the
duration probe; a mislabelled non-audio file is never delivered.

No network: httpx is faked; _fingerprint_duration is stubbed.
"""
import pytest

from app.search import fullsong as fs

ARTIST, TITLE = "Test Artist", "Test Song"
POST_TITLE = "دانلود آهنگ Test Song از Test Artist"
POST_URL = "https://post.example/p/7"
MP3_URL = "https://dl.example.com/Test%20Artist%20-%20Test%20Song.mp3"

MP3_BYTES = b"ID3\x04\x00" + b"\x00" * 300_000
JUNK_BYTES = b"definitely not audio, just text" * 12_000  # >100KB, no magic


class _FakeResp:
    def __init__(self, status=200, json_data=None, text="", content=b""):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.content = content

    def json(self):
        return self._json


class _FakeClient:
    """Routes by URL: WP search -> post API -> post HTML -> file bytes."""

    def __init__(self, *a, **k):
        self.requested = []
        self.file_bytes = MP3_BYTES
        self.api_content = "<p>no download links in the API content</p>"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        url = str(url)
        self.requested.append(url)
        if "wp-json/wp/v2/search" in url:
            return _FakeResp(json_data=[
                {"id": 7, "title": POST_TITLE, "url": POST_URL}])
        if "wp-json/wp/v2/posts/7" in url:
            return _FakeResp(json_data={
                "content": {"rendered": self.api_content}})
        if url.startswith(POST_URL):
            return _FakeResp(text=(
                '<html><body><a class="dl" href="%s">دانلود</a>'
                "</body></html>" % MP3_URL))
        if url.startswith("https://dl.example.com/"):
            return _FakeResp(content=self.file_bytes)
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


@pytest.mark.asyncio
async def test_html_fallback_finds_real_file(monkeypatch, fake_client):
    """WP API content has no links: the post's public HTML page is
    fetched and its mp3 is downloaded, validated and returned."""
    res = await fs.find_full_song_file(ARTIST, TITLE)
    assert res is not None
    assert res["ext"] == "mp3"
    assert res["data"] == MP3_BYTES
    assert res["duration_sec"] == 200.0
    assert res["source"] == "musics-fa.com"
    html_fetched = any(r.startswith(POST_URL) for c in fake_client
                       for r in c.requested)
    assert html_fetched


@pytest.mark.asyncio
async def test_api_content_link_needs_no_html_fallback(monkeypatch,
                                                       fake_client):
    """When the API content already carries the mp3, the public page is
    never fetched (no extra request)."""
    clients_holder = {}

    orig_factory = fs.httpx.AsyncClient

    def factory(*a, **k):
        c = orig_factory(*a, **k)
        c.api_content = '<p><a href="%s">dl</a></p>' % MP3_URL
        clients_holder.setdefault("clients", []).append(c)
        return c

    monkeypatch.setattr(fs.httpx, "AsyncClient", factory)
    res = await fs.find_full_song_file(ARTIST, TITLE)
    assert res is not None
    assert res["data"] == MP3_BYTES
    html_fetched = any(r.startswith(POST_URL)
                       for c in clients_holder["clients"]
                       for r in c.requested)
    assert not html_fetched


@pytest.mark.asyncio
async def test_non_audio_bytes_rejected_by_magic_gate(monkeypatch,
                                                      fake_client):
    """>100KB of non-audio bytes: the magic-byte gate rejects the file
    even though the (stubbed) duration probe would pass."""
    orig_factory = fs.httpx.AsyncClient

    def factory(*a, **k):
        c = orig_factory(*a, **k)
        c.file_bytes = JUNK_BYTES
        return c

    monkeypatch.setattr(fs.httpx, "AsyncClient", factory)
    res = await fs.find_full_song_file(ARTIST, TITLE)
    assert res is None


def test_looks_like_audio_gate():
    assert fs._looks_like_audio(b"ID3\x04\x00" + b"\x00" * 16)
    assert fs._looks_like_audio(b"\xff\xfb\x90\x00" + b"\x00" * 16)
    assert fs._looks_like_audio(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8)
    assert fs._looks_like_audio(b"OggS" + b"\x00" * 16)
    assert fs._looks_like_audio(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 8)
    assert not fs._looks_like_audio(b"definitely not audio" + b"\x00" * 16)
    assert not fs._looks_like_audio(b"short")
    assert not fs._looks_like_audio(b"")
