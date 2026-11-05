"""Smart downloader (dl:) — no network in any test.

httpx.AsyncClient is monkeypatched with fakes; format detection, queue
semantics, caching, retry, DB helpers and exact spec strings are tested.
"""
import asyncio
import json

import httpx
import pytest

from app.config import Config
from app.features import downloader as dl
from app.features.downloader import (
    CONTENT_TYPE_LABELS,
    QUEUE_EMPTY,
    QUEUE_STATUS_LABELS,
    STAGE_LABELS,
    TIKTOK_NOTE,
    YOUTUBE_NOTE,
    DlStates,
    _SEMAPHORE,
    create_job,
    delete_job,
    detect_formats,
    detect_source,
    find_cached,
    get_job,
    list_jobs,
    normalize_url,
    render_job,
    render_queue,
    retry_job,
    run_job,
    update_job,
    validation_line,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status=200, headers=None, text=""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text


class _FakeStream:
    def __init__(self, data=b"fake-bytes", status=200, headers=None):
        self._data = data
        self.status_code = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self, chunk_size=65536):
        yield self._data


class _FakeClient:
    """Monkeypatched in place of httpx.AsyncClient."""

    head_resp = _FakeResp()
    get_resp = _FakeResp()
    stream_resp = _FakeStream()
    head_exc = None
    get_exc = None
    stream_exc = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def head(self, url, **kwargs):
        if self.head_exc:
            raise self.head_exc
        return self.head_resp

    async def get(self, url, **kwargs):
        if self.get_exc:
            raise self.get_exc
        return self.get_resp

    def stream(self, method, url, **kwargs):
        if self.stream_exc:
            raise self.stream_exc
        return self.stream_resp


@pytest.fixture()
def fake_http(monkeypatch):
    _FakeClient.head_resp = _FakeResp()
    _FakeClient.get_resp = _FakeResp()
    _FakeClient.stream_resp = _FakeStream()
    _FakeClient.head_exc = None
    _FakeClient.get_exc = None
    _FakeClient.stream_exc = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return _FakeClient


class _FakeSent:
    def __init__(self, file_id):
        self._file_id = file_id

    @property
    def video(self):
        return self

    @property
    def audio(self):
        return self

    @property
    def document(self):
        return self

    @property
    def photo(self):
        return [self]

    @property
    def file_id(self):
        return self._file_id


class _FakeMessage:
    def __init__(self):
        self.edits = []
        self.sent = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append(text)

    async def answer_document(self, *args, **kwargs):
        self.sent.append("document")
        return _FakeSent("fid-doc-1")

    async def answer_video(self, *args, **kwargs):
        self.sent.append("video")
        return _FakeSent("fid-video-1")

    async def answer_audio(self, *args, **kwargs):
        self.sent.append("audio")
        return _FakeSent("fid-audio-1")

    async def answer_photo(self, *args, **kwargs):
        self.sent.append("photo")
        return _FakeSent("fid-photo-1")


def _cfg():
    return Config(max_file_mb=50)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_creates_download_jobs(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(download_jobs)")]
    assert cols == [
        "id", "user_id", "url", "source", "content_type", "status",
        "stage", "progress", "formats_json", "chosen_format",
        "result_file_id", "result_url", "error", "created_at", "updated_at",
        "audio_url",
    ]
    idx = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='download_jobs'")]
    assert "idx_download_jobs_user" in idx


def test_fsm_state_name_exact():
    assert DlStates.await_url.state == "dl:await_url"


def test_semaphore_cap_is_two():
    assert _SEMAPHORE._value == 2


# ---------------------------------------------------------------------------
# Source detection (same rules as process_link + spec TikTok exception)
# ---------------------------------------------------------------------------


def test_detect_source_direct():
    assert detect_source("https://cdn.example.com/song.mp3") == "direct"


def test_detect_source_youtube():
    assert detect_source("https://www.youtube.com/watch?v=abc") == "youtube"
    assert detect_source("https://youtu.be/abc") == "youtube"


def test_detect_source_instagram():
    assert detect_source("https://www.instagram.com/reel/abc/") == "instagram"


def test_detect_source_aparat():
    assert detect_source("https://www.aparat.com/v/abc") == "aparat"


def test_detect_source_tiktok():
    # TikTok is not in the base allowlist but is an explicitly
    # spec-supported downloader source.
    assert detect_source("https://www.tiktok.com/@u/video/123") == "tiktok"


def test_detect_source_invalid():
    assert detect_source("not a link") == "invalid"
    assert detect_source("ftp://example.com/x.mp3") == "invalid"
    assert detect_source("http://127.0.0.1/x.mp4") == "invalid"
    assert detect_source("https://evil.example/page") == "invalid"


# ---------------------------------------------------------------------------
# Exact validation lines (spec section 4)
# ---------------------------------------------------------------------------


def test_validation_lines_exact():
    assert validation_line("empty_or_too_long") == (
        "این متن شبیه لینک نیست. یک نشانی کامل (https://…) بفرستید "
        "یا با /cancel از این بخش خارج شوید."
    )
    assert validation_line("scheme_not_allowed") == (
        "فقط لینک‌های http و https پشتیبانی می‌شوند."
    )
    assert validation_line("private_ip_blocked") == (
        "این نشانی داخلی یا خصوصی است و به دلایل امنیتی پردازش نمی‌شود."
    )
    assert validation_line("domain_not_allowed") == (
        "این دامنه در فهرست منابع پشتیبانی‌شده نیست. منابع فعلی: یوتیوب، "
        "اینستاگرام، تیک‌تاک، آپارات و لینک مستقیم فایل صوتی/تصویری."
    )
    assert validation_line("something_else") == (
        "لینک معتبر نیست؛ یک نشانی کامل با https:// بفرستید."
    )


# ---------------------------------------------------------------------------
# Format detection per source (no network — faked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_video_gets_file_format(fake_http):
    fake_http.head_resp = _FakeResp(
        headers={"content-type": "video/mp4", "content-length": "1234"})
    res = await detect_formats("https://cdn.example.com/v.mp4", "direct")
    assert res.formats == [{"id": "file", "label": "دانلود فایل"}]
    assert res.content_type == "video"
    assert res.error == ""


@pytest.mark.asyncio
async def test_direct_audio_gets_file_format(fake_http):
    fake_http.head_resp = _FakeResp(
        headers={"content-type": "audio/mpeg", "content-length": "999"})
    res = await detect_formats("https://cdn.example.com/s.mp3", "direct")
    assert res.formats == [{"id": "file", "label": "دانلود فایل"}]
    assert res.content_type == "audio"


@pytest.mark.asyncio
async def test_direct_html_gets_no_format(fake_http):
    fake_http.head_resp = _FakeResp(headers={"content-type": "text/html"})
    res = await detect_formats("https://cdn.example.com/page", "direct")
    assert res.formats == []


@pytest.mark.asyncio
async def test_direct_head_failure_gets_no_format(fake_http):
    fake_http.head_exc = httpx.ConnectError("down")
    res = await detect_formats("https://cdn.example.com/v.mp4", "direct")
    assert res.formats == []


@pytest.mark.asyncio
async def test_youtube_player_only_with_exact_note(fake_http):
    res = await detect_formats("https://www.youtube.com/watch?v=abc", "youtube")
    assert res.formats == [
        {"id": "yt_player", "label": "پخش با پلیر رسمی یوتیوب"}]
    assert res.note == YOUTUBE_NOTE
    assert res.note == (
        "یوتیوب فایل مستقیم عمومی نمی‌دهد؛ دانلود فایل از این منبع ممکن نیست")


@pytest.mark.asyncio
async def test_tiktok_player_only_with_exact_note(fake_http):
    res = await detect_formats("https://www.tiktok.com/@u/video/1", "tiktok")
    assert res.formats == [
        {"id": "tt_player", "label": "پخش با پلیر رسمی تیک‌تاک"}]
    assert res.note == TIKTOK_NOTE
    assert res.note == (
        "تیک‌تاک فایل مستقیم عمومی نمی‌دهد؛ دانلود فایل از این منبع ممکن نیست")


@pytest.mark.asyncio
async def test_instagram_with_public_file(fake_http):
    fake_http.get_resp = _FakeResp(text=(
        '<html><head><meta property="og:video" '
        'content="https://cdn.example.com/reel.mp4"></head></html>'))
    res = await detect_formats("https://www.instagram.com/reel/abc/", "instagram")
    assert res.formats == [{"id": "file", "label": "دانلود فایل"}]
    assert res.file_url == "https://cdn.example.com/reel.mp4"
    assert res.error == ""


@pytest.mark.asyncio
async def test_instagram_empty_shell_is_distinct_error(fake_http):
    # HTTP 200 shell with zero media refs: no more silent official-player
    # fallback — the miss is classified (here: no gating signal, oEmbed
    # body not JSON-parseable by the fake) and gets its own honest line.
    fake_http.get_resp = _FakeResp(text="<html><head><title>x</title></head></html>")
    res = await detect_formats("https://www.instagram.com/reel/abc/", "instagram")
    assert res.formats == []
    assert res.error == "empty_shell"
    assert dl.EMPTY_SHELL == (
        "⚠️ اینستاگرام فایل این پست را به‌صورت عمومی نمی‌دهد؛ "
        "برای دریافت، خود ویدیو را مستقیم بفرستید.")


@pytest.mark.asyncio
async def test_aparat_without_file_falls_back_to_player(fake_http):
    fake_http.get_resp = _FakeResp(text="<html><head><title>x</title></head></html>")
    res = await detect_formats("https://www.aparat.com/v/abc", "aparat")
    assert res.formats == [
        {"id": "ap_player", "label": "پخش با پلیر رسمی آپارات"}]


@pytest.mark.asyncio
async def test_private_or_deleted_page_is_unavailable(fake_http):
    fake_http.get_resp = _FakeResp(status=404, text="not found")
    res = await detect_formats("https://www.instagram.com/reel/abc/", "instagram")
    assert res.formats == []
    assert res.error == "unavailable"
    assert dl.UNAVAILABLE == (
        "فایل این لینک در دسترس نیست؛ ممکن است پست خصوصی یا حذف شده باشد.")


@pytest.mark.asyncio
async def test_page_fetch_error_is_unavailable(fake_http):
    fake_http.get_exc = httpx.ConnectError("down")
    res = await detect_formats("https://www.aparat.com/v/abc", "aparat")
    assert res.formats == []
    assert res.error == "unavailable"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def test_job_crud_roundtrip(db):
    job_id = create_job(db, 7, "https://cdn.example.com/a.mp3", "direct",
                        "audio", [{"id": "file", "label": "دانلود فایل"}])
    assert job_id
    job = get_job(db, job_id)
    assert job["user_id"] == 7
    assert job["status"] == "queued"
    assert job["stage"] == "queued"
    assert job["progress"] == 0
    assert json.loads(job["formats_json"])[0]["id"] == "file"
    update_job(db, job_id, status="working", progress=42)
    job = get_job(db, job_id)
    assert job["status"] == "working" and job["progress"] == 42
    jobs = list_jobs(db, 7)
    assert len(jobs) == 1 and jobs[0]["id"] == job_id
    assert list_jobs(db, 8) == []
    delete_job(db, job_id)
    assert get_job(db, job_id) is None


def test_retry_resets_failed_to_queued(db):
    job_id = create_job(db, 7, "https://cdn.example.com/a.mp3", "direct",
                        "audio", [])
    update_job(db, job_id, status="failed", stage="retrying",
               error="x", result_file_id="fid")
    assert retry_job(db, job_id) is True
    job = get_job(db, job_id)
    assert job["status"] == "queued"
    assert job["stage"] == "queued"
    assert job["progress"] == 0
    assert job["error"] == ""


def test_retry_refuses_non_failed(db):
    job_id = create_job(db, 7, "https://cdn.example.com/a.mp3", "direct",
                        "audio", [])
    assert retry_job(db, job_id) is False
    assert retry_job(db, 999999) is False


def test_cache_hit_reuses_file_id(db):
    url = "https://cdn.example.com/song.mp3"
    job_id = create_job(db, 7, normalize_url(url), "direct", "audio",
                        [{"id": "file", "label": "x"}])
    update_job(db, job_id, status="ready", stage="done",
               result_file_id="fid-cached-1")
    hit = find_cached(db, url)
    assert hit is not None
    assert hit["result_file_id"] == "fid-cached-1"
    # Trailing-slash variant normalizes to the same cache entry.
    hit2 = find_cached(db, url + "/")
    assert hit2 is not None and hit2["id"] == job_id
    assert find_cached(db, "https://cdn.example.com/other.mp3") is None


def test_cache_ignores_unfinished_jobs(db):
    url = "https://cdn.example.com/song.mp3"
    create_job(db, 7, normalize_url(url), "direct", "audio", [])
    assert find_cached(db, url) is None


# ---------------------------------------------------------------------------
# Rendering — exact spec strings
# ---------------------------------------------------------------------------


def test_stage_labels_exact():
    assert STAGE_LABELS == {
        "queued": "در صف پردازش",
        "preparing": "در حال آماده‌سازی",
        "fetching": "در حال دریافت فایل از منبع",
        "saving": "در حال ذخیره فایل",
        "done": "دانلود کامل شد",
        "cached": "آماده است (از کش)",
        "player": "آماده پخش با پلیر رسمی",
        "retrying": "خطای موقت؛ تلاش دوباره",
        "failed": "ناموفق",
    }


def test_queue_status_labels_exact():
    assert QUEUE_STATUS_LABELS == {
        "ready": "آماده",
        "ready_cached": "آماده · از کش",
        "failed": "ناموفق",
        "working": "در حال پردازش",
        "queued": "در صف",
    }


def test_content_type_labels_exact():
    assert CONTENT_TYPE_LABELS == {
        "video": "ویدیو",
        "audio": "صوتی",
        "photo": "عکس",
        "post": "پست",
        "unknown": "نامشخص",
    }


def test_render_job_progress_format():
    job = {"source": "youtube", "content_type": "post",
           "stage": "fetching", "progress": 42}
    assert render_job(job) == (
        "دانلودر هوشمند\nیوتیوب · پست\nدر حال دریافت فایل از منبع · 42٪")


def test_render_queue_empty_exact():
    assert render_queue([]) == QUEUE_EMPTY
    assert QUEUE_EMPTY == "صف خالی است؛ یک لینک بفرستید تا اینجا به صف اضافه شود."


def test_render_queue_lists_statuses(db):
    a = create_job(db, 7, "https://a.example/x.mp4", "direct", "video", [])
    b = create_job(db, 7, "https://b.example/y", "youtube", "post", [])
    update_job(db, b, status="failed", stage="failed")
    text = render_queue(list_jobs(db, 7))
    assert "در صف" in text and "ناموفق" in text


def test_invalid_message_exact():
    assert dl.INVALID_LINK == "این لینک قابل پردازش نیست."


# ---------------------------------------------------------------------------
# run_job execution (faked message + faked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_job_player_format_needs_no_download(db, fake_http):
    job_id = create_job(db, 7, "https://www.youtube.com/watch?v=abc",
                        "youtube", "post",
                        [{"id": "yt_player", "label": "پخش با پلیر رسمی یوتیوب"}])
    update_job(db, job_id, chosen_format="yt_player")
    msg = _FakeMessage()
    await run_job(msg, db, _cfg(), job_id)
    job = get_job(db, job_id)
    assert job["status"] == "ready"
    assert job["stage"] == "player"
    assert job["progress"] == 100
    assert msg.sent == []  # nothing downloaded
    assert any("آماده پخش با پلیر رسمی · 100٪" in e for e in msg.edits)


@pytest.mark.asyncio
async def test_run_job_downloads_file_and_caches_file_id(db, fake_http):
    fake_http.stream_resp = _FakeStream(
        data=b"x" * 100, headers={"content-length": "100"})
    job_id = create_job(db, 7, "https://cdn.example.com/v.mp4", "direct",
                        "video", [{"id": "file", "label": "دانلود فایل"}])
    update_job(db, job_id, chosen_format="file")
    msg = _FakeMessage()
    await run_job(msg, db, _cfg(), job_id)
    job = get_job(db, job_id)
    assert job["status"] == "ready"
    assert job["stage"] == "done"
    assert job["result_file_id"] == "fid-video-1"
    assert msg.sent == ["video"]
    assert any("دانلود کامل شد · 100٪" in e for e in msg.edits)
    # Second identical URL hits the cache.
    hit = find_cached(db, "https://cdn.example.com/v.mp4")
    assert hit["result_file_id"] == "fid-video-1"


@pytest.mark.asyncio
async def test_run_job_transient_error_marks_retryable(db, fake_http):
    fake_http.stream_exc = httpx.ConnectError("down")
    job_id = create_job(db, 7, "https://cdn.example.com/v.mp4", "direct",
                        "video", [{"id": "file", "label": "دانلود فایل"}])
    update_job(db, job_id, chosen_format="file")
    msg = _FakeMessage()
    await run_job(msg, db, _cfg(), job_id)
    job = get_job(db, job_id)
    assert job["status"] == "failed"
    assert job["stage"] == "retrying"
    assert any("خطای موقت؛ تلاش دوباره" in e for e in msg.edits)
    # Retry resets and the rerun succeeds.
    assert retry_job(db, job_id) is True
    fake_http.stream_exc = None
    fake_http.stream_resp = _FakeStream(
        data=b"x" * 10, headers={"content-length": "10"})
    await run_job(msg, db, _cfg(), job_id)
    job = get_job(db, job_id)
    assert job["status"] == "ready"


@pytest.mark.asyncio
async def test_run_job_gone_file_is_permanent_failure(db, fake_http):
    fake_http.stream_resp = _FakeStream(data=b"", status=404)
    job_id = create_job(db, 7, "https://cdn.example.com/v.mp4", "direct",
                        "video", [{"id": "file", "label": "دانلود فایل"}])
    update_job(db, job_id, chosen_format="file")
    msg = _FakeMessage()
    await run_job(msg, db, _cfg(), job_id)
    job = get_job(db, job_id)
    assert job["status"] == "failed"
    assert job["stage"] == "failed"
    assert "خصوصی یا حذف شده" in job["error"]


@pytest.mark.asyncio
async def test_run_job_skips_non_queued(db, fake_http):
    job_id = create_job(db, 7, "https://cdn.example.com/v.mp4", "direct",
                        "video", [{"id": "file", "label": "دانلود فایل"}])
    update_job(db, job_id, status="working", chosen_format="file")
    msg = _FakeMessage()
    await run_job(msg, db, _cfg(), job_id)
    assert msg.edits == [] and msg.sent == []


@pytest.mark.asyncio
async def test_run_job_edits_same_message_for_progress(db, fake_http):
    chunks = [b"a" * 30000, b"b" * 30000, b"c" * 30000, b"d" * 10000]

    class _ChunkStream(_FakeStream):
        async def aiter_bytes(self, chunk_size=65536):
            for c in chunks:
                yield c

    fake_http.stream_resp = _ChunkStream(
        headers={"content-length": str(sum(len(c) for c in chunks))})
    job_id = create_job(db, 7, "https://cdn.example.com/v.mp4", "direct",
                        "unknown", [{"id": "file", "label": "دانلود فایل"}])
    update_job(db, job_id, chosen_format="file")
    msg = _FakeMessage()
    await run_job(msg, db, _cfg(), job_id)
    job = get_job(db, job_id)
    assert job["status"] == "ready"
    assert msg.sent == ["document"]
    assert len(msg.edits) >= 3  # preparing/fetching/progress/saving/done
    assert msg.edits[0].startswith("دانلودر هوشمند\n")


def test_no_traceback_strings_in_brief():
    for value in dl.BRIEF.values():
        assert "Traceback" not in value and "Error" not in value


def test_register_exposes_router():
    from aiogram import Router
    r = Router()
    dl.register(r)  # must not raise; callback prefix dl: only
    assert r is not None
