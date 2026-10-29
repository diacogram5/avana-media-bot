"""Link->download pipeline audit tests: Instagram resolver wiring + error taxonomy.

The «دریافت از لینک» downloader must call the anonymous multi-strategy
resolver (resolve_instagram_media) and offer its (audio_url, video_url)
output as the real download — and every failure branch must be gated on
a real signal with its own brief Persian line:

  signal                          -> error code      -> user line
  resolver yields video_url       -> file format     (real download)
  resolver yields only audio_url  -> file format     (real audio download)
  oEmbed gating markers          -> "age_gated"     AGE_GATED
  oEmbed 400/404                 -> "unavailable"   UNAVAILABLE (spec-exact)
  HTTP 200 shell, zero media     -> "empty_shell"   EMPTY_SHELL
  oEmbed transport timeout       -> "timeout"       FETCH_TIMEOUT
  oEmbed other transport failure -> "network_error" FETCH_NETWORK

No network in any test: the resolver/gating functions and httpx are
monkeypatched with fakes.
"""
import json

import httpx
import pytest

from app.features import downloader as dl
from app.features.downloader import detect_formats, download_error_text
from app.services import instagram as I

IG_URL = "https://www.instagram.com/reel/abc/"


@pytest.fixture()
def no_http(monkeypatch):
    """Block real network: any unpatched httpx call fails loudly."""
    class _NoClient:
        def __init__(self, *a, **k):
            raise AssertionError("unexpected real httpx call")
    monkeypatch.setattr(I.httpx, "AsyncClient", _NoClient)


def _patch_resolver(monkeypatch, audio_url=None, video_url=None):
    async def fake_resolve(url):
        return audio_url, video_url, "", "crawler_dash"
    monkeypatch.setattr(I, "resolve_instagram_media", fake_resolve)


def _patch_gating(monkeypatch, value):
    async def fake_gating(url):
        return value
    monkeypatch.setattr(I, "check_instagram_gating", fake_gating)


# ---------------------------------------------------------------------------
# Resolver wiring: (audio_url, video_url) -> download stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_video_url_becomes_file_download(monkeypatch, no_http):
    _patch_resolver(monkeypatch, audio_url="https://cdn.example.com/a.mp4",
                    video_url="https://cdn.example.com/v.mp4")
    res = await detect_formats(IG_URL, "instagram")
    assert res.error == ""
    assert res.formats == [{"id": "file", "label": "دانلود فایل"}]
    assert res.file_url == "https://cdn.example.com/v.mp4"
    assert res.content_type == "video"


@pytest.mark.asyncio
async def test_resolver_audio_only_becomes_audio_download(monkeypatch, no_http):
    _patch_resolver(monkeypatch, audio_url="https://cdn.example.com/a.mp4",
                    video_url=None)
    res = await detect_formats(IG_URL, "instagram")
    assert res.error == ""
    assert res.formats == [{"id": "file", "label": "دانلود فایل"}]
    assert res.file_url == "https://cdn.example.com/a.mp4"
    assert res.content_type == "audio"


@pytest.mark.asyncio
async def test_resolver_exception_is_classified_not_crashed(monkeypatch, no_http):
    async def boom(url):
        raise RuntimeError("resolver blew up")
    monkeypatch.setattr(I, "resolve_instagram_media", boom)
    _patch_gating(monkeypatch, None)
    res = await detect_formats(IG_URL, "instagram")
    assert res.formats == []
    assert res.error == "empty_shell"


# ---------------------------------------------------------------------------
# Taxonomy branches (resolver misses; gating signal decides)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_age_gated_gets_dedicated_message(monkeypatch, no_http):
    _patch_resolver(monkeypatch)
    _patch_gating(monkeypatch, "age_gated")
    res = await detect_formats(IG_URL, "instagram")
    assert res.formats == []
    assert res.error == "age_gated"
    assert download_error_text(res.error) == (
        "⚠️ این پست اینستاگرام محدودیت سنی یا منطقه‌ای دارد و "
        "بدون ورود به اینستاگرام قابل دریافت نیست.")


@pytest.mark.asyncio
async def test_private_or_deleted_is_unavailable(monkeypatch, no_http):
    _patch_resolver(monkeypatch)
    _patch_gating(monkeypatch, "private_or_deleted")
    res = await detect_formats(IG_URL, "instagram")
    assert res.formats == []
    assert res.error == "unavailable"
    # Spec-exact line, unchanged.
    assert download_error_text(res.error) == (
        "فایل این لینک در دسترس نیست؛ ممکن است پست خصوصی یا حذف شده باشد.")


@pytest.mark.asyncio
async def test_empty_shell_gets_own_message(monkeypatch, no_http):
    _patch_resolver(monkeypatch)
    _patch_gating(monkeypatch, None)
    res = await detect_formats(IG_URL, "instagram")
    assert res.formats == []
    assert res.error == "empty_shell"
    assert download_error_text(res.error) == (
        "⚠️ اینستاگرام فایل این پست را به‌صورت عمومی نمی‌دهد؛ "
        "برای دریافت، خود ویدیو را مستقیم بفرستید.")


@pytest.mark.asyncio
async def test_timeout_gets_own_message(monkeypatch, no_http):
    _patch_resolver(monkeypatch)
    _patch_gating(monkeypatch, "timeout")
    res = await detect_formats(IG_URL, "instagram")
    assert res.error == "timeout"
    assert download_error_text(res.error) == (
        "⚠️ اتصال به اینستاگرام بیش از حد طول کشید؛ "
        "لطفاً دوباره تلاش کنید.")


@pytest.mark.asyncio
async def test_network_error_gets_own_message(monkeypatch, no_http):
    _patch_resolver(monkeypatch)
    _patch_gating(monkeypatch, "network_error")
    res = await detect_formats(IG_URL, "instagram")
    assert res.error == "network_error"
    assert download_error_text(res.error) == (
        "⚠️ خطای اتصال به اینستاگرام؛ اتصال اینترنت را بررسی و "
        "دوباره تلاش کنید.")


def test_download_error_text_unknown_code_falls_back():
    assert download_error_text("bogus") == dl.LINK_FAILED
    assert download_error_text("") == dl.LINK_FAILED


# ---------------------------------------------------------------------------
# check_instagram_gating unit tests: only explicit signals classify
# ---------------------------------------------------------------------------


class _JsonResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _client_with(monkeypatch, resp=None, exc=None):
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, url, **kwargs):
            if exc is not None:
                raise exc
            return resp
    monkeypatch.setattr(I.httpx, "AsyncClient", _FakeClient)


GATED_PAYLOAD = {
    "message": "geoblock_required",
    "gating_type": "unappealable",
    "title": "People under 20 can't see this content",
    "blocks_logging_data": "MIN_AGE_ACCOUNT",
}


@pytest.mark.asyncio
async def test_gating_detects_real_age_gate_signals(monkeypatch):
    _client_with(monkeypatch, resp=_JsonResp(200, GATED_PAYLOAD))
    assert await I.check_instagram_gating(IG_URL) == "age_gated"


@pytest.mark.asyncio
async def test_gating_oembed_404_is_private_or_deleted(monkeypatch):
    _client_with(monkeypatch, resp=_JsonResp(404, {"message": "not found"}))
    assert await I.check_instagram_gating(IG_URL) == "private_or_deleted"


@pytest.mark.asyncio
async def test_gating_oembed_400_is_private_or_deleted(monkeypatch):
    _client_with(monkeypatch, resp=_JsonResp(400, {}))
    assert await I.check_instagram_gating(IG_URL) == "private_or_deleted"


@pytest.mark.asyncio
async def test_gating_timeout_is_distinct(monkeypatch):
    _client_with(monkeypatch, exc=httpx.TimeoutException("slow"))
    assert await I.check_instagram_gating(IG_URL) == "timeout"


@pytest.mark.asyncio
async def test_gating_connect_error_is_network_error(monkeypatch):
    _client_with(monkeypatch, exc=httpx.ConnectError("down"))
    assert await I.check_instagram_gating(IG_URL) == "network_error"


@pytest.mark.asyncio
async def test_gating_clean_oembed_is_not_classified(monkeypatch):
    _client_with(monkeypatch, resp=_JsonResp(200, {"title": "a reel",
                                                  "author_name": "someone"}))
    assert await I.check_instagram_gating(IG_URL) is None


@pytest.mark.asyncio
async def test_gating_non_json_body_is_not_classified(monkeypatch):
    _client_with(monkeypatch, resp=_JsonResp(200, ValueError("no json")))
    assert await I.check_instagram_gating(IG_URL) is None


@pytest.mark.asyncio
async def test_gating_uses_oembed_endpoint(monkeypatch):
    seen = {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, url, **kwargs):
            seen["url"] = url
            return _JsonResp(200, {"title": "x"})
    monkeypatch.setattr(I.httpx, "AsyncClient", _FakeClient)
    await I.check_instagram_gating("https://www.instagram.com/reel/abc/?x=1")
    assert seen["url"].startswith("https://www.instagram.com/oauth/oembed/?url=")
    assert "reel%2Fabc" in seen["url"] or "reel/abc" in seen["url"]


def test_gating_payload_shape_is_json_serializable():
    # The exact shape the 2026-10-29 investigation saw on Dd23bq7vkPr.
    blob = json.dumps(GATED_PAYLOAD, ensure_ascii=False)
    assert "geoblock_required" in blob and "MIN_AGE_ACCOUNT" in blob
