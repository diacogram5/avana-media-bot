"""Source registry: registration, isolation, ordering, kind separation.

No network: all fetches are fakes. Registry modules register real
sources at import; these tests use unique names so they never collide.
"""
import asyncio

import pytest

from app.search import registry as reg
from app.search.registry import STATUS_PROVEN, STATUS_DEAD


@pytest.fixture(autouse=True)
def _clean():
    yield
    for name in list(reg._REGISTRY):
        if name.startswith("t_"):
            reg.unregister(name)


def _ok(results):
    async def fetch(*a, **k):
        return results
    return fetch


def _boom():
    async def fetch(*a, **k):
        raise RuntimeError("source exploded")
    return fetch


def test_register_and_priority_order():
    reg.register("t_c", "info", _ok(["c"]), priority=30, status=STATUS_PROVEN)
    reg.register("t_a", "info", _ok(["a"]), priority=10, status=STATUS_PROVEN)
    reg.register("t_b", "info", _ok(["b"]), priority=20, status=STATUS_PROVEN)
    names = [s.name for s in reg.sources("info") if s.name.startswith("t_")]
    assert names == ["t_a", "t_b", "t_c"]


def test_failing_source_never_kills_search():
    reg.register("t_bad", "info", _boom(), priority=5, status=STATUS_PROVEN)
    reg.register("t_good", "info", _ok(["ok"]), priority=10, status=STATUS_PROVEN)
    items, failed = asyncio.run(reg.collect("info", "q"))
    assert any(i == "ok" for i in items if isinstance(i, str))
    assert failed == ["t_bad"]


def test_first_hit_skips_failures_and_empties():
    reg.register("t_f1", "file", _boom(), priority=5, status=STATUS_PROVEN)
    reg.register("t_f2", "file", _ok(None), priority=10, status=STATUS_PROVEN)
    reg.register("t_f3", "file", _ok({"data": b"x"}), priority=20, status=STATUS_PROVEN)
    val, name, failed = asyncio.run(reg.first_hit("file", "a", "t"))
    assert val == {"data": b"x"} and name == "t_f3"
    assert failed == ["t_f1"]


def test_first_hit_all_fail_returns_none():
    reg.register("t_x", "file", _boom(), priority=5, status=STATUS_PROVEN)
    val, name, failed = asyncio.run(reg.first_hit("file", "a", "t"))
    assert val is None and name is None and failed == ["t_x"]


def test_kind_separation_info_never_in_file():
    reg.register("t_info_only", "info", _ok(["x"]), priority=1, status=STATUS_PROVEN)
    file_names = [s.name for s in reg.sources("file")]
    assert "t_info_only" not in file_names
    with pytest.raises(ValueError):
        reg.register("t_badkind", "notakind", _ok([]))


def test_dead_sources_never_run_but_stay_in_audit():
    calls = []

    async def fetch(*a, **k):
        calls.append(1)
        return ["x"]

    reg.register("t_dead", "info", fetch, status=STATUS_DEAD, enabled=False)
    assert "t_dead" not in [s.name for s in reg.sources("info")]
    items, failed = asyncio.run(reg.collect("info", "q"))
    assert calls == []
    assert any(r["name"] == "t_dead" and r["status"] == STATUS_DEAD
               for r in reg.audit())


def test_sync_fetch_supported():
    reg.register("t_sync", "info", lambda q: ["s1"], priority=1, status=STATUS_PROVEN)
    items, failed = asyncio.run(reg.collect("info", "q"))
    assert "s1" in items and failed == []


def test_timeout_isolated():
    async def slow(*a, **k):
        await asyncio.sleep(5)
        return ["late"]

    reg.register("t_slow", "info", slow, priority=1, timeout=0.05, status=STATUS_PROVEN)
    reg.register("t_fast", "info", _ok(["fast"]), priority=2, status=STATUS_PROVEN)
    items, failed = asyncio.run(reg.collect("info", "q"))
    assert "fast" in items and failed == ["t_slow"]


def test_decorator_form_and_unregister():
    @reg.source("t_deco", "similar", priority=7, status=STATUS_PROVEN)
    async def fetch(q):
        return ["d"]

    assert reg.get("t_deco").priority == 7
    assert reg.unregister("t_deco") is True
    assert reg.get("t_deco") is None


def test_default_registry_has_four_proven_file_sources():
    # sources.py registered at import; the 4 WP file sites keep working.
    import app.search.sources  # noqa: F401
    files = reg.sources("file")
    names = [s.name for s in files]
    assert names == ["wp:musics-fa.com", "wp:minevisam.ir",
                     "wp:musicviral.ir", "wp:musicsweb.ir"]
    assert all(s.status == STATUS_PROVEN and s.kind == "file" for s in files)
    # adding a 5th site needs only a registration call:
    reg.register("wp:example.ir", "file", _ok(None), priority=50,
                 status="unverified")
    assert "wp:example.ir" in [s.name for s in reg.sources("file")]


def test_per_site_file_source_runs_real_pipeline(monkeypatch):
    """End-to-end: registry file sources each call find_full_song_file()
    with only their site enabled; the musicsweb one returns the real
    (mocked) download through all honesty gates."""
    import app.search.fullsong as fs
    import app.search.sources  # noqa: F401
    from app.search import youtube_full as ytf
    async def _no_yt(a, t):
        return None
    monkeypatch.setattr(ytf, "find_youtube_audio", _no_yt)

    MP3_BYTES = b"ID3\x04\x00" + b"\x00" * 300_000

    class Resp:
        def __init__(self, status=200, json_data=None, text="", content=b""):
            self.status_code = status
            self._json = json_data
            self.text = text
            self.content = content

        def json(self):
            return self._json

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            url = str(url)
            if "rest_route=/wp/v2/search" in url:
                return Resp(json_data=[{
                    "id": 7,
                    "title": "دانلود آهنگ Test Song از Test Artist",
                    "url": "https://musicsweb.ir/content/7/"}])
            if "rest_route=/wp/v2/posts/7" in url:
                return Resp(json_data={"content": {"rendered": "stripped"}})
            if url.startswith("https://musicsweb.ir/content/7/"):
                return Resp(text='<a href="https://dl.musicsweb.ir/x/Test%20Song.mp3">')
            if url.startswith("https://dl.musicsweb.ir/"):
                return Resp(content=MP3_BYTES)
            return Resp(json_data=[])

    monkeypatch.setattr(fs.httpx, "AsyncClient", Client)
    monkeypatch.setattr(fs, "_fingerprint_duration", lambda data: 200.0)
    fs._CACHE.clear()

    async def run():
        return await reg.first_hit("file", "Test Artist", "Test Song")

    val, name, failed = asyncio.run(run())
    fs._CACHE.clear()
    assert val is not None, f"failed sources: {failed}"
    assert name == "wp:musicsweb.ir"
    assert val["source"] == "musicsweb.ir"
    assert val["duration_sec"] == 200.0
    assert len(val["data"]) == len(MP3_BYTES)


def test_no_core_change_for_new_source():
    # A brand-new info source plugs in with one call and is picked up
    # by collect() in priority order, without touching registry.py.
    reg.register("t_new", "info", _ok(["n1"]), priority=1, status=STATUS_PROVEN)
    items, failed = asyncio.run(reg.collect("info", "q"))
    assert "n1" in items
