"""Speed round (Task 2): identify-cache + WAV-reuse + single-resolve tests.

- Audio-content-hash identify cache: sha256(audio bytes) -> terminal
  result; URL keys let a repeat flow skip the download too. Errors are
  never cached; TTL and row-cap pruning are enforced.
- WAV reuse: the validation stage keeps its signable WAV and the Shazam
  lookup reuses it instead of transcoding the same input again.
- Single resolve: the link flow resolves Instagram media once and shares
  the tuple between the video-send stage and the identify stage.
"""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db as dbmod
from app.bot import handlers as H
from app.services import instagram as I
from app.services import shazam_id
from app.services.audio_id import AudioIdResult
from app.services.shazam_id import ShazamIdResult

AUDIO_URL = "https://cdn.example.com/audio.mp4"
AUDIO_BYTES = b"ID3\x04\x00" + b"\x00" * 200_000


def _outcome(url="https://www.instagram.com/reel/DexfQJaoNrR/", kind="instagram"):
    return SimpleNamespace(kind=kind, official_url=url, ok=True)


def _config(**kw):
    base = dict(max_file_mb=50, audd_token="")
    base.update(kw)
    return SimpleNamespace(**base)


class _NoNetClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        raise AssertionError(f"network must not be touched: {url}")


# ---------------------------------------------------------------------------
# DB layer: TTL, error exclusion, cap pruning
# ---------------------------------------------------------------------------

def test_identify_cache_roundtrip_and_ttl(db):
    dbmod.set_identify_cache(db, "hash:abc", "identified", "T", "A")
    got = dbmod.get_identify_cache(db, "hash:abc")
    assert got == {"status": "identified", "title": "T", "artist": "A"}
    # Expired entry reads as a miss.
    assert dbmod.get_identify_cache(db, "hash:abc", max_age=-1) is None
    # Missing key is a miss.
    assert dbmod.get_identify_cache(db, "hash:nope") is None


def test_identify_cache_never_stores_errors(db):
    dbmod.set_identify_cache(db, "hash:e1", "error", "T", "A")
    dbmod.set_identify_cache(db, "hash:e2", "bogus", "T", "A")
    assert dbmod.get_identify_cache(db, "hash:e1") is None
    assert dbmod.get_identify_cache(db, "hash:e2") is None


def test_identify_cache_prunes_oldest_beyond_cap(db, monkeypatch):
    monkeypatch.setattr(dbmod, "IDENTIFY_CACHE_MAX_ROWS", 5)
    for i in range(9):
        dbmod.set_identify_cache(db, f"hash:k{i}", "not_found")
    n = db.execute("SELECT COUNT(*) FROM identify_cache").fetchone()[0]
    assert n == 5
    # Newest rows survive, oldest are evicted.
    assert dbmod.get_identify_cache(db, "hash:k8") is not None
    assert dbmod.get_identify_cache(db, "hash:k0") is None


# ---------------------------------------------------------------------------
# _identify_audio: hash-cache hit skips lookup; terminal results are stored
# ---------------------------------------------------------------------------

def _ok_analysis():
    return {"ok": True, "reason": "ok", "duration_s": 10.0,
            "sample_rate": 22050, "channels": 1, "rms_db": -20.0}


@pytest.mark.asyncio
async def test_identify_audio_hash_hit_skips_lookup(db, monkeypatch):
    h = hashlib.sha256(AUDIO_BYTES).hexdigest()
    dbmod.set_identify_cache(db, f"hash:{h}", "identified", "Song", "Singer")

    async def forbidden_lookup(*a, **k):
        raise AssertionError("lookup must not run on a cache hit")

    monkeypatch.setattr(H, "identify_bytes_shazam", forbidden_lookup)
    res = await H._identify_audio(AUDIO_BYTES, "clip.mp4", _config(), db)
    assert res.status == "identified"
    assert res.title == "Song" and res.artist == "Singer"


@pytest.mark.asyncio
async def test_identify_audio_hash_hit_not_found(db, monkeypatch):
    h = hashlib.sha256(AUDIO_BYTES).hexdigest()
    dbmod.set_identify_cache(db, f"hash:{h}", "not_found")

    async def forbidden_lookup(*a, **k):
        raise AssertionError("lookup must not run on a cache hit")

    monkeypatch.setattr(H, "identify_bytes_shazam", forbidden_lookup)
    res = await H._identify_audio(AUDIO_BYTES, "clip.mp4", _config(), db)
    assert res.status == "not_found"


@pytest.mark.asyncio
async def test_identify_audio_stores_terminal_result(db, monkeypatch):
    async def fake_shazam(data, prepared_wav=None):
        return ShazamIdResult(status="identified", title="T", artist="A")

    monkeypatch.setattr(H, "identify_bytes_shazam", fake_shazam)
    monkeypatch.setattr(shazam_id, "analyze_audio_with_wav",
                        lambda data: (_ok_analysis(), b"fakewav"))
    res = await H._identify_audio(AUDIO_BYTES, "clip.mp4", _config(), db,
                                  cache_url_key=AUDIO_URL)
    assert res.status == "identified"
    h = hashlib.sha256(AUDIO_BYTES).hexdigest()
    assert dbmod.get_identify_cache(db, f"hash:{h}")["title"] == "T"
    assert dbmod.get_identify_cache(
        db, f"url:{AUDIO_URL}", dbmod.IDENTIFY_CACHE_URL_TTL)["artist"] == "A"


@pytest.mark.asyncio
async def test_identify_audio_never_caches_lookup_error(db, monkeypatch):
    async def fake_shazam(data, prepared_wav=None):
        return ShazamIdResult(status="error")

    monkeypatch.setattr(H, "identify_bytes_shazam", fake_shazam)
    monkeypatch.setattr(shazam_id, "analyze_audio_with_wav",
                        lambda data: (_ok_analysis(), b"fakewav"))
    res = await H._identify_audio(AUDIO_BYTES, "clip.mp4", _config(), db)
    assert res.status == "error"
    h = hashlib.sha256(AUDIO_BYTES).hexdigest()
    assert dbmod.get_identify_cache(db, f"hash:{h}") is None


@pytest.mark.asyncio
async def test_identify_audio_without_conn_disables_cache(monkeypatch):
    """conn=None (tests, scripts) keeps the old behavior exactly."""
    async def fake_shazam(data, prepared_wav=None):
        return ShazamIdResult(status="not_found")

    monkeypatch.setattr(H, "identify_bytes_shazam", fake_shazam)
    monkeypatch.setattr(shazam_id, "analyze_audio_with_wav",
                        lambda data: (_ok_analysis(), None))
    res = await H._identify_audio(AUDIO_BYTES, "clip.mp4", _config())
    assert res.status == "not_found"


# ---------------------------------------------------------------------------
# _identify_link_audio: URL-key pre-check skips the download entirely
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_link_audio_url_cache_hit_skips_download(db, monkeypatch):
    dbmod.set_identify_cache(db, f"url:{AUDIO_URL}", "identified",
                             "Cached Song", "Cached Singer")
    monkeypatch.setattr(H.httpx, "AsyncClient", _NoNetClient)

    async def fake_resolve(url):
        return AUDIO_URL, None, "page text", "crawler_dash"

    monkeypatch.setattr(I, "resolve_instagram_media", fake_resolve)

    async def forbidden_identify(*a, **k):
        raise AssertionError("_identify_audio must not run on a URL hit")

    monkeypatch.setattr(H, "_identify_audio", forbidden_identify)
    res = await H._identify_link_audio(_outcome(), _config(), db)
    assert res[0] == "identified"
    assert res[1] == "Cached Song" and res[2] == "Cached Singer"
    assert res[7] == AUDIO_URL  # media URL still threaded for bank arming


@pytest.mark.asyncio
async def test_link_audio_accepts_shared_resolve_without_reresolving(
        db, monkeypatch):
    """resolved= is honored: the resolver is never called again."""
    async def forbidden_resolve(url):
        raise AssertionError("resolver must run once per link, not twice")

    monkeypatch.setattr(I, "resolve_instagram_media", forbidden_resolve)
    monkeypatch.setattr(H.httpx, "AsyncClient", _NoNetClient)

    async def fake_identify(data, filename, config, *a, **k):
        raise AssertionError("unreachable: no media to download")

    monkeypatch.setattr(H, "_identify_audio", fake_identify)
    # No audio URL in the shared tuple and no network: honest needs_file.
    res = await H._identify_link_audio(
        _outcome(), _config(), db,
        resolved=(None, None, "caption", "none"))
    assert res[0] == "needs_file" and res[3] is True
    assert res[4] == "caption"


# ---------------------------------------------------------------------------
# Single resolve: the instagram link flow resolves media exactly once and
# shares the tuple between the video-send stage and the identify stage.
# ---------------------------------------------------------------------------

class _FakeChat:
    id = 7


class _FakeUser:
    id = 42
    username = "tester"
    first_name = "Test"


class _FakeSent:
    message_id = 1
    video = SimpleNamespace(file_id="f1")

    async def edit_reply_markup(self, **k):
        pass


class _FakeLinkMessage:
    def __init__(self, text):
        self.text = text
        self.from_user = _FakeUser()
        self.chat = _FakeChat()
        self.message_id = 9
        self.answers = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append(text)


class _FakeVideoClient:
    """Serves the reel's video bytes for the video-send stage."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        class R:
            status_code = 200
            content = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1000

            def raise_for_status(self):
                pass

        return R()


@pytest.mark.asyncio
async def test_instagram_link_flow_resolves_exactly_once(db, monkeypatch):
    from app.config import Config

    calls = []
    seen_resolved = {}

    async def counting_resolve(url):
        calls.append(url)
        return AUDIO_URL, "https://cdn.example.com/video.mp4", "caption", "crawler_dash"

    async def fake_identify_link(outcome, cfg, conn=None, resolved=None,
                                 *args, **kwargs):
        seen_resolved["resolved"] = resolved
        return ("identified", "T", "A", False, "caption", b"")

    async def fake_send_video(message, video, caption=None, **k):
        return _FakeSent()

    async def fake_card(message, conn, title, artist, **kw):
        pass

    monkeypatch.setattr(I, "resolve_instagram_media", counting_resolve)
    monkeypatch.setattr(H.httpx, "AsyncClient", _FakeVideoClient)
    monkeypatch.setattr(H, "_identify_link_audio", fake_identify_link)
    monkeypatch.setattr(H, "send_video", fake_send_video)
    monkeypatch.setattr(H, "_send_professional_song_card", fake_card)
    monkeypatch.setattr(H, "can_use_feature", lambda conn, uid, feat: (True, ""))

    msg = _FakeLinkMessage("https://www.instagram.com/reel/DexfQJaoNrR/")
    await H.text_handler(msg, db, Config())
    assert calls == ["https://www.instagram.com/reel/DexfQJaoNrR/"], \
        f"resolver must run exactly once per link, ran {len(calls)}x"
    assert seen_resolved["resolved"] == (
        AUDIO_URL, "https://cdn.example.com/video.mp4", "caption", "crawler_dash")

@pytest.mark.asyncio
# ---------------------------------------------------------------------------
# WAV reuse: no second transcode; windows are valid PCM slices
# ---------------------------------------------------------------------------

async def test_identify_bytes_shazam_reuses_prepared_wav(monkeypatch):
    seen = []

    async def fake_recognize(data):
        seen.append(data)
        if data == b"raw-aac":
            return None, True  # codec rejected by the signature core
        return {"track": {"title": "T", "subtitle": "A"}}, False

    monkeypatch.setattr(shazam_id, "_recognize_bytes", fake_recognize)

    def forbidden_transcode(*a, **k):
        raise AssertionError("prepared WAV must be reused, not re-transcoded")

    monkeypatch.setattr(shazam_id, "transcode_to_wav", forbidden_transcode)
    res = await shazam_id.identify_bytes_shazam(b"raw-aac",
                                                prepared_wav=b"PREPARED-WAV")
    assert res.status == "identified" and res.title == "T"
    assert seen == [b"raw-aac", b"PREPARED-WAV"]


def test_wav_time_slice_produces_valid_audio():
    # Real Instagram DASH audio captured by the speed benchmark, when
    # present; otherwise a synthetic fallback (may skip if ffmpeg
    # cannot decode it).
    real = Path(__file__).resolve().parent.parent / "scripts" / "bench_audio.mp4"
    src = real.read_bytes() if real.exists() else b"ID3\x04\x00" + bytes(400_000)
    wav = shazam_id.transcode_to_wav(src)
    if wav is None:
        pytest.skip("ffmpeg could not transcode the test input")
    full = shazam_id.wav_time_slice(wav, 0.0, 1.0)
    part = shazam_id.wav_time_slice(wav, 1 / 3, 1.0)
    assert full is not None and part is not None
    for w in (full, part):
        assert w[:4] == b"RIFF" and w[8:12] == b"WAVE"
    info_full = shazam_id._analyze_wav(full)
    info_part = shazam_id._analyze_wav(part)
    assert info_full["ok"] and info_part["ok"]
    # The later window is ~2/3 of the full duration.
    assert abs(info_part["duration_s"] - info_full["duration_s"] * 2 / 3) < 0.5


def test_analyze_audio_with_wav_matches_analyze_audio():
    analysis, wav = shazam_id.analyze_audio_with_wav(AUDIO_BYTES)
    assert analysis == shazam_id.analyze_audio(AUDIO_BYTES)
    if analysis["ok"]:
        assert wav is not None and wav[:4] == b"RIFF"
