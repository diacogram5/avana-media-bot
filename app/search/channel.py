"""Second engine: public Telegram channels (channel-only / AI songs).

- Current catalog engine is preserved and runs FIRST; this engine is
  only tried when the catalog finds nothing.
- Only public channels configured by the owner (DB setting
  channel_usernames or env CHANNEL_USERNAMES) are read, via their public
  preview page (t.me/s/<channel>). Private channels are never accessed.
- Accuracy gate: every distinctive word of the query (length > 2,
  stopwords removed) must appear in the post text, and the post must
  contain media (photo/video/audio block). A text-only post, or a post
  matching only one word, is NEVER returned as a song.
- Returns post links, never a claimed identification: the caller labels
  them as channel posts, not as catalog songs.
"""
from __future__ import annotations

import html
import re

import httpx

_STOPWORDS = {"آهنگ", "ترانه", "موزیک", "song", "music", "برای", "از", "در", "به", "و", "یا"}


def distinctive_words(query: str) -> list[str]:
    words = re.sub(r"[^\w\u0600-\u06ff]+", " ", (query or "").lower()).split()
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS]


def parse_channel_posts(page_html: str, channel: str) -> list[dict]:
    """Parse t.me/s/<channel> posts: {post_url, text, has_media}."""
    posts: list[dict] = []
    # Split on message blocks; keep it tolerant of markup changes.
    blocks = re.split(r'data-post="([^"]+)"', page_html or "")
    # blocks: [pre, post_id, body, post_id, body, ...]
    for i in range(1, len(blocks) - 1, 2):
        post_id, body = blocks[i], blocks[i + 1]
        text_m = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', body, re.S)
        raw = text_m.group(1) if text_m else ""
        text = re.sub(r"<[^>]+>", " ", html.unescape(raw))
        text = re.sub(r"\s+", " ", text).strip()
        has_media = bool(re.search(
            r"tgme_widget_message_(photo|video|audio|voice|document)", body))
        posts.append({"channel": channel,
                      "post_url": f"https://t.me/{channel}/{post_id}",
                      "text": text, "has_media": has_media})
    return posts


def channel_post_matches(post: dict, query: str) -> bool:
    if not post.get("has_media"):
        return False
    words = distinctive_words(query)
    if not words:
        return False
    text = (post.get("text") or "").lower()
    return all(w in text for w in words)


async def search_channels(channels: list[str], query: str,
                          fetcher=None) -> list[dict]:
    """Search configured public channels. Empty list on any failure."""
    if not channels or not query:
        return []
    out: list[dict] = []
    for ch in channels:
        ch = ch.strip().lstrip("@")
        if not ch:
            continue
        try:
            if fetcher is not None:
                page = await fetcher(ch)
            else:
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                    resp = await c.get(f"https://t.me/s/{ch}",
                                       headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
                    page = resp.text if resp.status_code == 200 else ""
        except Exception:
            continue
        for post in parse_channel_posts(page, ch):
            if channel_post_matches(post, query):
                out.append(post)
        if out:
            break  # stop on first channel with an exact post: saves calls
    return out
