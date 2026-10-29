"""Lyrics web-parity extension tests: 82 languages, picker pagination,
chunking, not-found text, same-language guard, stage B/C helpers and
fetch_lyrics stage order. No network: httpx is monkeypatched.
"""
import pytest
import httpx

from app.search.lyrics import (
    LANG_NAMES, LANG_CODES, MAX_TRANSLATE_CHARS,
    language_picker_rows, lyrics_not_found_text, chunk_text,
    translate_text, same_language_notice, SAME_LANGUAGE_MSG,
    lyrics_title_variants, lyrics_exact_match, lyrics_token_overlap,
    extract_lyrics_section, fetch_lyrics, fetch_lyrics_with_source,
    MUSIC_SITES,
)

EXPECTED_CODES = ["fa", "en", "ar", "tr", "ur", "az", "ku", "ps", "tg",
                  "ru", "uk", "de", "fr", "es", "it", "pt", "nl", "sv",
                  "no", "da", "fi", "pl", "cs", "sk", "hu", "ro", "bg",
                  "el", "sr", "hr", "bs", "mk", "sq", "sl", "et", "lv",
                  "lt", "be", "zh", "ja", "ko", "hi", "bn", "ta", "te",
                  "ml", "mr", "gu", "pa", "ne", "si", "th", "vi", "id",
                  "ms", "tl", "km", "lo", "my", "sw", "am", "ha", "yo",
                  "ig", "zu", "af", "ka", "hy", "he", "uz", "kk", "ky",
                  "mn", "is", "ga", "cy", "eu", "ca", "gl", "mt", "la", "eo"]


# ------------------------------------------------------------- 82 languages
def test_82_languages_present_in_spec_order():
    assert len(LANG_NAMES) == 82
    assert list(LANG_NAMES) == EXPECTED_CODES
    assert len(LANG_CODES) == 82
    assert set(LANG_CODES) == set(EXPECTED_CODES)


def test_sample_labels_exact():
    assert LANG_NAMES["fa"] == "فارسی"
    assert LANG_NAMES["en"] == "انگلیسی"
    assert LANG_NAMES["tr"] == "ترکی"
    assert LANG_NAMES["zh"] == "چینی"
    assert LANG_NAMES["eo"] == "اسپرانتو"
    assert LANG_NAMES["tl"] == "فیلیپینی"
    assert LANG_NAMES["my"] == "برمه‌ای"
    assert LANG_NAMES["sw"] == "سواحلی"


def test_lang_codes_mymemory_style():
    assert LANG_CODES["fa"] == "fa-IR"
    assert LANG_CODES["tr"] == "tr-TR"
    assert LANG_CODES["en"] == "en-GB"
    assert LANG_CODES["ar"] == "ar-SA"
    assert LANG_CODES["eo"] == "eo"


# --------------------------------------------------- picker pagination
def _all_picker_codes(per_page=8):
    seen, pages = [], 0
    page = 0
    while True:
        rows, page, total = language_picker_rows(page, "k", per_page=per_page)
        pages = total
        for row in rows:
            assert len(row) == 1 and set(row[0]) == {"text", "code"}
            assert row[0]["text"] == LANG_NAMES[row[0]["code"]]
            seen.append(row[0]["code"])
        if page >= total - 1:
            break
        page += 1
    return seen, pages


def test_picker_covers_all_82_exactly_once():
    seen, total = _all_picker_codes()
    assert total == 11  # 82 / 8 -> 11 pages
    assert sorted(seen) == sorted(EXPECTED_CODES)
    assert len(seen) == len(set(seen)) == 82


def test_picker_other_page_size():
    seen, total = _all_picker_codes(per_page=20)
    assert total == 5
    assert len(seen) == len(set(seen)) == 82


def test_picker_page_clamped():
    rows, page, total = language_picker_rows(-5, "k")
    assert page == 0
    rows2, page2, _ = language_picker_rows(999, "k")
    assert page2 == total - 1
    assert rows2  # last page non-empty


# ----------------------------------------------------------------- chunking
def test_max_translate_chars():
    assert MAX_TRANSLATE_CHARS == 3500


def test_chunk_text_within_limit():
    text = ("line one\nline two\nline three\n" * 200)  # > 3500
    chunks = chunk_text(text, 3500)
    assert all(len(c) <= 3500 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert len(chunks) > 1


def test_chunk_text_short_text_unchanged():
    assert chunk_text("short", 3500) == ["short"]
    assert chunk_text("", 3500) == []


def test_chunk_text_long_line_word_boundary():
    long_line = "word " * 2000  # one very long line
    chunks = chunk_text(long_line, 500)
    assert all(len(c) <= 500 for c in chunks)
    assert " ".join(" ".join(c.split()) for c in chunks).split() == long_line.split()


# ------------------------------------------------------------- not-found text
def test_lyrics_not_found_text_exact():
    assert lyrics_not_found_text("دوست دارم") == (
        "متن ترانه «دوست دارم» در منابع متن آهنگ پیدا نشد. "
        "نام خواننده و آهنگ را با هم بنویسید، مثلاً: «مهدی احمدوند - دوست دارم».")


# ------------------------------------------------------- same-language guard
FA_LYRICS = "دوست دارم که با تو بگویم\nمی‌خواهم که بخندیم با هم"


def test_same_language_notice_persian_to_farsi():
    assert same_language_notice(FA_LYRICS, "fa") == SAME_LANGUAGE_MSG
    assert same_language_notice(FA_LYRICS, "fa") == "متن آهنگ از قبل به همین زبان است."


def test_same_language_notice_other_targets_none():
    assert same_language_notice(FA_LYRICS, "en") is None
    assert same_language_notice(FA_LYRICS, "tr") is None
    assert same_language_notice("", "fa") is None


def test_translate_text_keeps_signature_and_failure_none():
    # Signature unchanged: (text, target_lang, source_lang="auto", client=None)
    import inspect
    sig = inspect.signature(translate_text)
    assert list(sig.parameters) == ["text", "target_lang", "source_lang", "client"]


@pytest.mark.asyncio
async def test_translate_long_text_chunked_and_joined():
    calls = []

    def handler(request):
        calls.append(request.url.params["q"])
        return httpx.Response(200, json={"responseData": {"translatedText": "T"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    text = ("word " * 300).strip()  # ~1500 chars > 450 per-request chunk
    out = await translate_text(text, "fa", client=client)
    assert out is not None
    assert len(calls) > 1
    assert all(len(q) <= 450 for q in calls)
    assert out == "\n".join(["T"] * len(calls))


# ------------------------------------------------------------- stage helpers
def test_title_variants_strip_packaging():
    v = lyrics_title_variants("Dooset Daram (Remix)")
    assert "Dooset Daram (Remix)" in v and "Dooset Daram" in v
    v2 = lyrics_title_variants("Song feat. Someone")
    assert "Song" in v2
    v3 = lyrics_title_variants("Song - Live")
    assert "Song" in v3
    assert lyrics_title_variants("Plain") == ["Plain"]


def test_exact_match():
    assert lyrics_exact_match("Dooset Daram", "Mehdi Ahmadvand",
                              "Dooset Daram", "Mehdi Ahmadvand")
    assert not lyrics_exact_match("Dooset Daram", "Mehdi Ahmadvand",
                                  "Dooset Daram", "Somebody Else")
    assert not lyrics_exact_match("Dooset Daram", "Mehdi Ahmadvand",
                                  "Another Song", "Mehdi Ahmadvand")
    assert lyrics_token_overlap("دوست دارم", "دوست دارم مهدی احمدوند") >= 0.99


def test_extract_lyrics_section():
    html_doc = (
        "<div><h1>متن آهنگ</h1><p>● دوست دارم که با تو باشم ♪<br>"
        "می‌خواهم که بخندیم با هم<br>شعر: کسی<br>"
        "────────────<br>دانلود آهنگ با کیفیت 320</p></div>")
    # Need >= 4 lines / 120 chars of real lyrics
    html_doc = (
        "<div><h1>متن آهنگ</h1><p>"
        "دوست دارم که با تو باشم در این شب تاریک<br>"
        "می‌خواهم که بخندیم با هم تا صبح روشن<br>"
        "دل من برای تو تنگ شده ای یار قدیمی<br>"
        "بیا که با هم برویم به سوی فردای بهتر<br>"
        "شعر: ترانه‌سرا<br>────────<br>دانلود آهنگ</p></div>")
    lyrics = extract_lyrics_section(html_doc)
    assert lyrics is not None
    assert "دوست دارم که با تو باشم" in lyrics
    assert "شعر" not in lyrics
    assert "دانلود" not in lyrics
    assert "●" not in lyrics and "♪" not in lyrics


def test_extract_lyrics_section_no_marker():
    assert extract_lyrics_section("<p>no lyrics here</p>") is None
    assert extract_lyrics_section("") is None


def test_music_sites_configured():
    assert len(MUSIC_SITES) >= 1
    assert all(s.startswith("https://") for s in MUSIC_SITES)


# ------------------------------------------------------- fetch_lyrics stages
def _mock_client(route):
    return httpx.AsyncClient(transport=httpx.MockTransport(route))


LRCLIB_REC = {"trackName": "Dooset Daram", "artistName": "Mehdi Ahmadvand",
              "plainLyrics": ("دوست دارم که با تو باشم " * 10).strip(), "instrumental": False}


@pytest.mark.asyncio
async def test_fetch_lyrics_stage_a_exact_get():
    def route(request):
        url = str(request.url)
        if "lrclib.net/api/get" in url:
            return httpx.Response(200, json=LRCLIB_REC)
        return httpx.Response(404, json={})

    client = _mock_client(route)
    lyrics, source, responded = await fetch_lyrics_with_source(
        "Dooset Daram", "Mehdi Ahmadvand", client=client)
    assert lyrics == LRCLIB_REC["plainLyrics"]
    assert source == "LRCLIB"
    assert responded is True
    # original language text returned unchanged
    assert await fetch_lyrics("Dooset Daram", "Mehdi Ahmadvand", client=client) == LRCLIB_REC["plainLyrics"]


@pytest.mark.asyncio
async def test_fetch_lyrics_rejects_namesake():
    """Search returns a different song's lyrics -> honest None, never wrong words."""
    other = dict(LRCLIB_REC, trackName="Another Song", artistName="Somebody Else")

    def route(request):
        url = str(request.url)
        if "lrclib.net/api/get" in url:
            return httpx.Response(404, json={})
        if "lrclib.net/api/search" in url:
            return httpx.Response(200, json=[other])
        return httpx.Response(404, json={})

    client = _mock_client(route)
    lyrics, _source, responded = await fetch_lyrics_with_source(
        "Dooset Daram", "Mehdi Ahmadvand", client=client)
    assert lyrics is None
    assert responded is True  # sources answered; the text genuinely isn't there


@pytest.mark.asyncio
async def test_fetch_lyrics_outage_is_not_not_found():
    """No source answers at all -> responded False (outage, not a miss)."""
    def route(request):
        raise httpx.ConnectError("no route to host", request=request)

    client = _mock_client(route)
    lyrics, _source, responded = await fetch_lyrics_with_source(
        "Dooset Daram", "Mehdi Ahmadvand", client=client)
    assert lyrics is None
    assert responded is False


@pytest.mark.asyncio
async def test_fetch_lyrics_ovh_stage():
    def route(request):
        url = str(request.url)
        if "lrclib.net" in url:
            return httpx.Response(404, json={})
        if "api.lyrics.ovh" in url:
            return httpx.Response(200, json={"lyrics": "some english lyrics text here ok"})
        return httpx.Response(404, json={})

    client = _mock_client(route)
    lyrics, source, responded = await fetch_lyrics_with_source(
        "Hello", "Adele", client=client)
    assert lyrics == "some english lyrics text here ok"
    assert source == "lyrics.ovh"
    assert responded is True
