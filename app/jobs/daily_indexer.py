"""Daily auto-indexer: crawls Persian music sites for new songs.

This is the competitor's secret: an automated crawler that monitors
music sites daily, downloads new songs, and indexes their audio
fingerprints. When a user sends a reel, the bot matches against this
auto-updated database via the bank's audio fingerprinting.

Runs via cron daily. Only indexes songs not already in the bank.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

# Add app to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.search.adapters.persian_music import SITES, HEADERS, TIMEOUT
from app.features.bank import _fingerprint, _pack
import requests

log = logging.getLogger(__name__)

def get_db_path():
    """Find the bot database."""
    # Try common locations
    for p in [
        Path("bot.db"),
        Path("app/bot.db"),
        Path.home() / "workspace" / "telegram-media-platform" / "bot.db",
    ]:
        if p.exists():
            return str(p)
    return "bot.db"

def fetch_latest_songs(site, max_songs=20):
    """Fetch latest songs from a site's WP API."""
    if site.get("type") != "wp_api" or not site.get("api"):
        return []
    
    try:
        # Get latest posts (not search, just latest)
        api_base = site["api"].rsplit("/search", 1)[0]
        url = f"{api_base}/posts"
        params = {
            "per_page": max_songs,
            "_fields": "id,title,link",
            "orderby": "date",
            "order": "desc",
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return []
        
        posts = resp.json()
        songs = []
        for post in posts:
            title = post.get("title", {}).get("rendered", "")
            link = post.get("link", "")
            if title and link:
                # Clean HTML from title
                import re
                title = re.sub(r'<[^>]+>', '', title).strip()
                songs.append({"title": title, "url": link, "site": site["name"]})
        return songs
    except Exception as e:
        log.warning(f"Failed to fetch from {site['name']}: {e}")
        return []

def download_and_fingerprint(song_url, mp3_pattern, site_name):
    """Download song page, extract MP3, download and fingerprint."""
    try:
        # Fetch song page
        resp = requests.get(song_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        
        import re
        mp3_urls = re.findall(mp3_pattern, resp.text)
        if not mp3_urls:
            return None
        
        mp3_url = mp3_urls[0]
        # Download MP3 (first 2MB for fingerprinting - enough for landmark)
        resp = requests.get(mp3_url, headers=HEADERS, timeout=30, stream=True)
        if resp.status_code != 200:
            return None
        
        data = b""
        for chunk in resp.iter_content(chunk_size=8192):
            data += chunk
            if len(data) > 2 * 1024 * 1024:  # 2MB max for fingerprint
                break
        
        if len(data) < 100000:  # Too small
            return None
        
        # Fingerprint
        fp = _fingerprint(data)
        if not fp:
            return None
        
        return {
            "fingerprint": _pack(fp.get("landmarks", [])),
            "mp3_url": mp3_url,
            "size": len(data),
        }
    except Exception as e:
        log.warning(f"Failed to process {song_url}: {e}")
        return None

async def run_indexer(db_path: str):
    """Run one indexing pass. Called by the bot's background task."""
    logging.basicConfig(level=logging.INFO)
    log.info("Starting daily indexer...")
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Ensure bank table exists
    # (The bank module should have created it, but check)
    
    indexed_count = 0
    for site in SITES:
        log.info(f"Crawling {site['name']}...")
        songs = fetch_latest_songs(site, max_songs=10)
        log.info(f"Found {len(songs)} latest songs from {site['name']}")
        
        for song in songs:
            # Check if already indexed (by URL)
            existing = conn.execute(
                "SELECT id FROM audio_bank WHERE file_url = ?",
                (song["url"],)
            ).fetchone()
            if existing:
                continue
            
            # Download and fingerprint
            result = download_and_fingerprint(
                song["url"], 
                site["mp3_pattern"],
                site["name"]
            )
            if not result:
                continue
            
            # Store in bank
            # Title format: "SiteName - Song Title"
            title = f"{song['title']}"
            try:
                conn.execute(
                    """INSERT INTO audio_bank 
                       (title, artist, file_url, fingerprint, source, created_at)
                       VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                    (title, "", result["mp3_url"], result["fingerprint"], site["name"])
                )
                conn.commit()
                indexed_count += 1
                log.info(f"Indexed: {title[:50]}")
            except Exception as e:
                log.warning(f"Failed to index {title[:30]}: {e}")
    
    conn.close()
    log.info(f"Done. Indexed {indexed_count} new songs.")

if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else get_db_path()
    asyncio.run(run_indexer(db))
