"""Tests for Instagram DASH audio+video muxing.

Root cause (2026-10-29, proven on real reel DdWUvpIoaOa bytes): Instagram's
crawler-DASH manifest serves video and audio as SEPARATE streams. The bot
used to download only the video stream, so every sent/downloaded Instagram
video was SILENT. fetch_merged_video() muxes both into one MP4.
"""
import subprocess
import sys

import pytest

sys.path.insert(0, ".")

from app.services.instagram import fetch_merged_video


def _make_video_only(path="/tmp/tmv_v.mp4"):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=duration=2:size=160x120:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", path],
        check=True, timeout=60)
    return open(path, "rb").read()


def _make_audio_only(path="/tmp/tmv_a.m4a"):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2",
         "-c:a", "aac", path],
        check=True, timeout=60)
    return open(path, "rb").read()


def _streams(data: bytes):
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(data)
        p = f.name
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", p],
            capture_output=True, timeout=30).stdout.decode()
        return out
    finally:
        os.unlink(p)


class _FakeResp:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


class _FakeClient:
    """httpx.AsyncClient stand-in keyed by URL substring."""
    def __init__(self, mapping: dict):
        self.mapping = mapping

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        for key, val in self.mapping.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return _FakeResp(val)
        raise RuntimeError("no mapping for " + url)


@pytest.mark.asyncio
async def test_merge_produces_video_with_audio(monkeypatch):
    import app.services.instagram as ig
    v = _make_video_only()
    a = _make_audio_only()
    # Sanity: the "video-only" input really has no audio track.
    assert "audio" not in _streams(v)
    monkeypatch.setattr(
        ig.httpx, "AsyncClient",
        lambda **kw: _FakeClient({"v.mp4": v, "a.m4a": a}))
    data, has_audio = await fetch_merged_video(
        "http://x/v.mp4", "http://x/a.m4a")
    assert has_audio is True
    types = _streams(data)
    assert "video" in types and "audio" in types


@pytest.mark.asyncio
async def test_no_audio_url_returns_video_unchanged(monkeypatch):
    import app.services.instagram as ig
    v = _make_video_only()
    monkeypatch.setattr(
        ig.httpx, "AsyncClient", lambda **kw: _FakeClient({"v.mp4": v}))
    data, has_audio = await fetch_merged_video("http://x/v.mp4", None)
    assert data == v
    assert has_audio is False


@pytest.mark.asyncio
async def test_failed_audio_download_falls_back_to_video(monkeypatch):
    import app.services.instagram as ig
    v = _make_video_only()
    monkeypatch.setattr(
        ig.httpx, "AsyncClient",
        lambda **kw: _FakeClient(
            {"v.mp4": v, "a.m4a": RuntimeError("boom")}))
    data, has_audio = await fetch_merged_video(
        "http://x/v.mp4", "http://x/a.m4a")
    assert data == v
    assert has_audio is False


@pytest.mark.asyncio
async def test_failed_video_download_returns_empty(monkeypatch):
    import app.services.instagram as ig
    monkeypatch.setattr(
        ig.httpx, "AsyncClient",
        lambda **kw: _FakeClient({"v.mp4": RuntimeError("boom")}))
    data, has_audio = await fetch_merged_video("http://x/v.mp4", None)
    assert data == b""
    assert has_audio is False
