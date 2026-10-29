"""Tests for Jamendo + Spotify source modules: no key -> [], HTTP errors ->
[], real payloads parsed. No network is used (mocked httpx)."""

import pytest

from app.search import jamendo, spotify


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        return self._resp

    async def post(self, url, headers=None, data=None):
        self.calls.append((url, data))
        return self._resp


@pytest.mark.asyncio
async def test_jamendo_no_key_returns_empty():
    assert await jamendo.search_tracks("Parvaz", "") == []


@pytest.mark.asyncio
async def test_jamendo_parses_results():
    payload = {"results": [{
        "name": "Parvaz", "artist_name": "Indie Artist",
        "audio": "https://stream.jamendo.com/x.mp3",
        "audiodownload": "https://dl.jamendo.com/x.mp3",
        "shareurl": "https://www.jamendo.com/track/1",
    }]}
    c = _Client(_Resp(200, payload))
    out = await jamendo.search_tracks("Parvaz", "key123", client=c)
    assert len(out) == 1
    assert out[0]["title"] == "Parvaz"
    assert out[0]["download_url"].startswith("https://")


@pytest.mark.asyncio
async def test_jamendo_http_error_returns_empty():
    c = _Client(_Resp(403, {}))
    assert await jamendo.search_tracks("Parvaz", "key123", client=c) == []


@pytest.mark.asyncio
async def test_spotify_no_key_returns_empty():
    assert await spotify.search_tracks("Deli", "", "") == []
    assert await spotify.search_tracks("Deli", "id", "") == []


@pytest.mark.asyncio
async def test_spotify_parses_results():
    token_resp = _Resp(200, {"access_token": "tok", "expires_in": 3600})
    search_resp = _Resp(200, {"tracks": {"items": [{
        "name": "Deli",
        "artists": [{"name": "Ice Spice"}],
        "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
        "preview_url": "https://p.scdn.co/mp3-preview/x",
        "album": {"name": "Like..?"},
    }]}})

    class _Seq:
        def __init__(self):
            self.n = 0

        async def post(self, *a, **k):
            return token_resp

        async def get(self, *a, **k):
            return search_resp

    spotify._token_cache["token"] = ""
    out = await spotify.search_tracks("Deli Ice Spice", "id", "secret", client=_Seq())
    assert len(out) == 1
    assert out[0]["title"] == "Deli"
    assert out[0]["spotify_url"].startswith("https://open.spotify.com")
