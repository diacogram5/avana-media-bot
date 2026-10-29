"""Best-effort extraction of a public media file URL from a link page.

Purpose: a platform link (e.g. an Instagram reel) carries no audio file
the bot can sign directly. Its public page sometimes advertises the
media file itself (og:video, or a "video_url"/"playback_url" field in the
page JSON). Extracting that public URL lets the bot sign the actual
audio with Shazam — the same approach the browser test version used on
the user's reel — without any login, paywall or private content.

Rules enforced here:
- Only a public page is fetched, with the same SSRF rules as links
  (no private IPs, no userinfo, http/https only).
- Only a URL that itself passes link validation as a direct media file
  is returned; a player page or an unknown host is never returned.
- If nothing is found, the caller gets None and must fall back honestly
  (AudD URL if a token is set, otherwise "send the video file itself").
- Nothing here downloads or resends a full copyrighted file to a user;
  the downloaded bytes are used only for fingerprint identification and
  are discarded afterwards.
"""
from __future__ import annotations

import html
import json
import re

from ..security import validate_link

_VIDEO_URL_RE = re.compile(
    r'"(?:video_url|playback_url|contentUrl)"\s*:\s*"([^"]+)"', re.IGNORECASE)
_OG_VIDEO_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:video(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
_OG_VIDEO_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:video(?::secure_url)?["\']',
    re.IGNORECASE)


def extract_media_url(page_html: str) -> str | None:
    """Return the first public media-file URL advertised by a page, or None.

    Pure function (no network) so it is unit-testable. The URL must pass
    link validation as a direct media file; otherwise it is rejected.
    """
    if not page_html:
        return None
    candidates: list[str] = []
    for pattern in (_OG_VIDEO_RE, _OG_VIDEO_RE2):
        match = pattern.search(page_html)
        if match:
            candidates.append(html.unescape(match.group(1)))
    for match in _VIDEO_URL_RE.finditer(page_html):
        candidates.append(match.group(1).replace("\\/", "/").replace("\\u0026", "&"))
    for url in candidates:
        url = (url or "").strip()
        if not url:
            continue
        validation = validate_link(url)
        # Accept only a direct media file on a public host.
        if validation.ok and validation.kind == "direct_file":
            return url
    return None


_TITLE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
_TITLE_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\']',
    re.IGNORECASE)
_DESC_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:description|twitter:description)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.S)


def extract_page_text(page_html: str) -> str:
    """Best-effort title/caption text advertised by a page, or "".

    Instagram pages rarely advertise a public media file, but they
    often advertise a title/caption. That text is the only query the
    channel engine can run on a link — without it, the channel stage
    is skipped entirely (the old bug). Never treated as an
    identification, only as a search query.
    """
    if not page_html:
        return ""
    for pattern in (_TITLE_RE, _TITLE_RE2, _DESC_RE):
        match = pattern.search(page_html)
        if match:
            text = html.unescape(match.group(1)).strip()
            if text:
                return text
    match = _TITLE_TAG_RE.search(page_html)
    if match:
        text = re.sub(r"<[^>]+>", " ", html.unescape(match.group(1))).strip()
        return text
    return ""


def extract_json_media_url(page_html: str) -> str | None:
    """Extract a media URL from JSON blobs embedded in a page, or None.

    Kept separate from extract_media_url so tests can target it directly;
    extract_media_url already covers the same fields via regex.
    """
    if not page_html:
        return None
    for match in re.finditer(r'\{[^{}]*"(?:video_url|playback_url)"[^{}]*\}', page_html):
        try:
            obj = json.loads(match.group(0))
        except ValueError:
            continue
        url = obj.get("video_url") or obj.get("playback_url") or ""
        validation = validate_link(url)
        if validation.ok and validation.kind == "direct_file":
            return url
    return None
