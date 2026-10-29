import pytest

from app.bot.logic import can_use_feature, main_menu_labels
from app.media import process_link, within_telegram_limit
from app.search import Provider, SearchEngine, SearchResult, dedup, paginate
from app.search.itunes import parse_itunes_payload
from app.security import RateLimiter, validate_link


def test_link_allowlist_and_blocks():
    assert validate_link("https://www.youtube.com/watch?v=abc123")
    assert validate_link("https://youtu.be/abc123")
    assert validate_link("https://www.aparat.com/v/xyz")
    assert validate_link("https://example.com/song.mp3").kind == "direct_file"
    assert not validate_link("https://example.com/page.html")  # not allowlisted, not a file
    assert not validate_link("http://127.0.0.1/admin")
    assert not validate_link("http://10.0.0.5/x.mp3")
    assert not validate_link("http://localhost:8000/x")
    assert not validate_link("ftp://example.com/song.mp3")
    assert not validate_link("https://user:pass@youtube.com/watch?v=x")


def test_process_link_official_only():
    out = process_link("https://www.youtube.com/watch?v=abc")
    assert out.ok and not out.downloadable and out.official_url.endswith("abc")
    bad = process_link("http://192.168.1.1/x")
    assert not bad.ok


def test_telegram_limit_and_rate_limiter():
    assert within_telegram_limit(10 * 1024 * 1024)
    assert not within_telegram_limit(60 * 1024 * 1024)
    limiter = RateLimiter()
    assert all(limiter.allow("u1", 3, 60) for _ in range(3))
    assert not limiter.allow("u1", 3, 60)


def test_itunes_parser():
    payload = {"results": [
        {"trackId": 1, "trackName": "Song A", "artistName": "Artist",
         "trackTimeMillis": 180000, "trackViewUrl": "https://example.com/a"},
        {"trackId": 1, "trackName": "Song A", "artistName": "Artist"},
    ]}
    parsed = parse_itunes_payload(payload)
    assert len(parsed) == 2 and parsed[0].duration == 180
    assert len(dedup(parsed)) == 1


class FailingProvider(Provider):
    def __init__(self):
        super().__init__(name="failing", enabled=True, priority=1)

    async def search(self, query, limit=10):
        raise RuntimeError("boom")


class OkProvider(Provider):
    def __init__(self):
        super().__init__(name="ok", enabled=True, priority=2)

    async def search(self, query, limit=10):
        return [SearchResult(source="ok", id="1", title=f"{query} Song", artist="X")]


@pytest.mark.asyncio
async def test_search_failure_isolation_and_pagination():
    engine = SearchEngine([FailingProvider(), OkProvider()])
    results, failed = await engine.search("test", limit=10)
    assert failed == ["failing"]
    assert len(results) == 1 and results[0].title == "test Song"
    items = list(range(12))
    page, total = paginate(items, 2, 5)
    assert page == [5, 6, 7, 8, 9] and total == 3


def test_menu_and_feature_gate(db):
    assert "🎵 جستجوی آهنگ" in main_menu_labels(db)
    allowed, _ = can_use_feature(db, 1, "song_search")
    assert allowed
    from app import db as dbmod
    dbmod.set_feature(db, "song_search", enabled=False, by_id=1)
    allowed, reason = can_use_feature(db, 1, "song_search")
    assert not allowed and reason == "feature_disabled"
