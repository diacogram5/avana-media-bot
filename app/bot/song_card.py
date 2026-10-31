"""Graphical song list card generator.

Generates a dark neon-style card image for song search results,
matching the reference design: header with title, ranked song rows
with play buttons, durations, and download icons.

Usage:
    from .song_card import generate_top_songs_card
    img_bytes = generate_top_songs_card(songs, title="برترین آهنگ‌های هفته")
    # songs: list of dicts with keys: title, artist, duration (sec), rank
"""
from __future__ import annotations

import io
from PIL import Image, ImageDraw, ImageFont


# Colors (dark neon theme)
BG = (18, 18, 35)
CARD_BG = (30, 30, 55)
GOLD = (255, 215, 0)
NEON_BLUE = (0, 200, 255)
NEON_PURPLE = (180, 80, 255)
WHITE = (255, 255, 255)
GRAY = (150, 150, 170)
ROW_BG = (25, 25, 45)
ROW_BORDER = (60, 60, 100)


def _get_font(size: int, bold: bool = False):
    """Get a font, falling back to default if not found."""
    # Try common fonts
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _format_duration(sec: int | None) -> str:
    if not sec:
        return "--:--"
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def generate_top_songs_card(songs: list[dict], title: str = "برترین آهنگ‌های هفته",
                            width: int = 800) -> bytes:
    """Generate a PNG card image for a list of songs.

    Args:
        songs: List of dicts with 'title', 'artist', 'duration' (seconds).
        title: Card header title.
        width: Image width in pixels.

    Returns:
        PNG image bytes.
    """
    # Layout
    header_h = 140
    row_h = 110
    row_gap = 12
    padding = 20

    n = min(len(songs), 10)  # Max 10 songs
    height = header_h + n * (row_h + row_gap) + padding * 2

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    # Header background (gradient-like)
    draw.rectangle([0, 0, width, header_h], fill=(25, 20, 50))

    # Title
    font_title = _get_font(36, bold=True)
    # Center the title (approximate for RTL)
    bbox = draw.textbbox((0, 0), title, font=font_title)
    tw = bbox[2] - bbox[0]
    draw.text(((width - tw) // 2, 40), title, font=font_title, fill=(255, 150, 255))

    # Crown emoji substitute (text)
    font_small = _get_font(24)
    draw.text((width // 2 - 100, 10), "👑", font=font_small, fill=GOLD)

    # Song rows
    font_rank = _get_font(28, bold=True)
    font_song = _get_font(22, bold=True)
    font_artist = _get_font(18)
    font_dur = _get_font(18)

    y = header_h + padding
    for i, song in enumerate(songs[:n]):
        rank = i + 1
        x0, y0 = padding, y
        x1, y1 = width - padding, y + row_h

        # Row background with border
        # Highlight #1 with gold border
        border_color = GOLD if rank == 1 else ROW_BORDER
        border_w = 3 if rank == 1 else 1
        draw.rounded_rectangle([x0, y0, x1, y1], radius=15, fill=ROW_BG,
                               outline=border_color, width=border_w)

        # Rank circle
        cx, cy = x0 + 40, y0 + row_h // 2
        r = 22
        rank_bg = GOLD if rank == 1 else (80, 80, 120)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=rank_bg)
        rank_text = str(rank)
        rb = draw.textbbox((0, 0), rank_text, font=font_rank)
        rtw, rth = rb[2] - rb[0], rb[3] - rb[1]
        draw.text((cx - rtw // 2, cy - rth // 2 - 2), rank_text,
                  font=font_rank, fill=(0, 0, 0) if rank == 1 else WHITE)

        # Play button (circle)
        px = x0 + 100
        draw.ellipse([px - 20, cy - 20, px + 20, cy + 20],
                     outline=NEON_BLUE, width=2)
        # Triangle
        draw.polygon([(px - 7, cy - 10), (px - 7, cy + 10), (px + 10, cy)],
                     fill=NEON_BLUE)

        # Song title and artist
        title_text = str(song.get("title", ""))[:40]
        artist_text = str(song.get("artist", ""))[:40]
        tx = x0 + 140
        draw.text((tx, y0 + 20), title_text, font=font_song, fill=WHITE)
        draw.text((tx, y0 + 55), artist_text, font=font_artist, fill=GRAY)

        # Duration (right side)
        dur_text = "🎧 " + _format_duration(song.get("duration"))
        db = draw.textbbox((0, 0), dur_text, font=font_dur)
        dtw = db[2] - db[0]
        draw.text((x1 - dtw - 60, cy - 12), dur_text, font=font_dur, fill=GRAY)

        # Download icon (text)
        draw.text((x1 - 35, cy - 15), "⬇", font=font_small, fill=WHITE)

        y += row_h + row_gap

    # Export to bytes
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
