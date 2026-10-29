"""Link->identify classification tests (Task 1).

_identify_link_audio must never collapse "media fetched but lookup
failed" into needs_file=True (the echo-back "video not handed over"
card): the real status ("not_found"/"error") and the heard bytes are
carried so the caller shows the honest line and arms the bank sample.
A genuine miss (no media at all) returns needs_file=True with the
5-way taxonomy cause, and _link_needs_file_card states the true cause
instead of a generic line.

No network: the resolver, gating, _identify_audio and httpx are faked.
"""
from types import SimpleNamespace

import pytest

from app.bot import handlers as H
from app.features import downloader as dlfeat
from app.services import instagram as I
from app.services.audio_id import AudioIdResult

IG_URL = "https://www.instagram.com/reel/DdWUvpIoaOa/"
AUDIO_URL = "https://cdn.example.com/audio.mp4"
# Magic-valid MP3-ish bytes, comfortably under the size cap.
AUDIO_BYTES = b"ID3\x04\x00" + b"\x00" * 200_000


def _outcome(url=IG_URL, kind="instagram"):
    return SimpleNamespace(kind=kind, official_url=url, ok=True)


def _config(**kw):
    base = dict(max_file_mb=50, audd_token=None)
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeResp:
    def __init__(self, content=b"", status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"http {self.status_code}")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        if str(url).startswith(AUDIO_URL):
            return _FakeResp(AUDIO_BYTES)
        raise AssertionError(f"unexpected GET {url}")


@pytest.fixture()
def fake_http(monkeypatch):
    monkeypatch.setattr(H.httpx, "AsyncClient", _FakeClient)


def _patch_resolver(monkeypatch, audio_url=AUDIO_URL):
    async def fake_resolve(url):
        return audio_url, None, "page text", "crawler_dash"
    monkeypatch.setattr(I, "resolve_instagram_media", fake_resolve)


def _patch_gating(monkeypatch, value, seen=None):
    async def fake_gating(url):
        if seen is not None:
            seen.append(url)
        return value
    monkeypatch.setattr(I, "check_instagram_gating", fake_gating)


def _patch_identify_audio(monkeypatch, status, detail="lookup_error"):
    async def fake_identify(data, filename, config):
        assert data == AUDIO_BYTES
        return AudioIdResult(status=status, detail=detail)
    monkeypatch.setattr(H, "_identify_audio", fake_identify)


# ---------------------------------------------------------------------------
# Fetched bytes are never misreported as "video not handed over"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetched_but_lookup_failed_is_not_needs_file(
        monkeypatch, fake_http):
    """Media fetched, Shazam lookup errored: status "error" is carried,
    needs_file stays False, and the heard bytes survive for the bank."""
    _patch_resolver(monkeypatch)
    _patch_identify_audio(monkeypatch, "error")
    res = await H._identify_link_audio(_outcome(), _config())
    assert res[0] == "error"
    assert res[3] is False
    assert res[5] == AUDIO_BYTES


@pytest.mark.asyncio
async def test_fetched_but_not_found_carries_bytes(monkeypatch, fake_http):
    """Media fetched, genuinely not identified: "not_found" (not
    needs_file) with the bytes, so the caller arms the bank sample."""
    _patch_resolver(monkeypatch)
    _patch_identify_audio(monkeypatch, "not_found", detail="")
    res = await H._identify_link_audio(_outcome(), _config())
    assert res[0] == "not_found"
    assert res[3] is False
    assert res[5] == AUDIO_BYTES


@pytest.mark.asyncio
async def test_audd_fallback_still_runs_after_shazam_miss(
        monkeypatch, fake_http):
    """The AudD fallback is preserved: a Shazam miss with a token set
    still tries identify_url before giving up."""
    _patch_resolver(monkeypatch)
    _patch_identify_audio(monkeypatch, "not_found", detail="")

    async def fake_identify_url(url, token):
        assert token == "tok"
        return AudioIdResult(status="identified", title="T", artist="A")
    monkeypatch.setattr(H, "identify_url", fake_identify_url)
    res = await H._identify_link_audio(_outcome(), _config(audd_token="tok"))
    assert res[0] == "identified"
    assert res[1] == "T" and res[2] == "A"


@pytest.mark.asyncio
async def test_audd_miss_after_fetched_bytes_keeps_status(
        monkeypatch, fake_http):
    """Shazam missed, AudD missed too: the fetched-bytes status wins —
    still not needs_file."""
    _patch_resolver(monkeypatch)
    _patch_identify_audio(monkeypatch, "not_found", detail="")

    async def fake_identify_url(url, token):
        return AudioIdResult(status="not_found")
    monkeypatch.setattr(H, "identify_url", fake_identify_url)
    res = await H._identify_link_audio(_outcome(), _config(audd_token="tok"))
    assert res[0] == "not_found"
    assert res[3] is False
    assert res[5] == AUDIO_BYTES


# ---------------------------------------------------------------------------
# Genuine misses: needs_file + the true taxonomy cause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genuine_miss_returns_taxonomy_cause(monkeypatch, fake_http):
    """Resolver yields nothing: needs_file=True with the gating-derived
    cause ("private_or_deleted" -> "unavailable")."""
    async def fake_resolve(url):
        return None, None, "", "none"
    monkeypatch.setattr(I, "resolve_instagram_media", fake_resolve)
    seen = []
    _patch_gating(monkeypatch, "private_or_deleted", seen)
    res = await H._identify_link_audio(_outcome(), _config())
    assert res[0] == "needs_file"
    assert res[3] is True
    assert res[5] == b""
    assert res[6] == "unavailable"
    assert seen == [IG_URL]


@pytest.mark.asyncio
async def test_genuine_miss_without_gating_signal_is_empty_shell(
        monkeypatch, fake_http):
    """No media and no gating signal: the honest "empty_shell" cause,
    not a guess."""
    async def fake_resolve(url):
        return None, None, "", "none"
    monkeypatch.setattr(I, "resolve_instagram_media", fake_resolve)
    _patch_gating(monkeypatch, None)
    res = await H._identify_link_audio(_outcome(), _config())
    assert res[3] is True
    assert res[6] == "empty_shell"


# ---------------------------------------------------------------------------
# The card states the true cause, never a generic line
# ---------------------------------------------------------------------------


def test_needs_file_card_states_true_cause():
    card = H._link_needs_file_card(_outcome(), "empty_shell")
    assert dlfeat.EMPTY_SHELL in card
    assert "فایل ویدیو را به بات نداد" not in card
    card = H._link_needs_file_card(_outcome(), "unavailable")
    assert dlfeat.UNAVAILABLE in card
    card = H._link_needs_file_card(_outcome(), "timeout")
    assert dlfeat.FETCH_TIMEOUT in card
    card = H._link_needs_file_card(_outcome(), "network_error")
    assert dlfeat.FETCH_NETWORK in card
    # The official link stays on the card.
    assert IG_URL in card


def test_needs_file_card_without_cause_keeps_legacy_line():
    """Non-Instagram platforms have no taxonomy: the legacy generic
    line is kept, not dropped."""
    card = H._link_needs_file_card(_outcome())
    assert "فایل ویدیو را به بات نداد" in card
