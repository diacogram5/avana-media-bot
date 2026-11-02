#!/usr/bin/env python3
"""Avano Motion Watermark — prototype (NOT wired into the bot yet).

Glassy animated @AvanoMediaBot overlay:
- appears at ~45% of clip duration, fades in, drifts very slowly,
  fades out before the end; never touches the centre/subject:
  position is chosen by edge-density analysis (emptiest region wins).
- original file is never modified; output is a new file.

CLI: python3 motion_watermark.py IN.mp4 OUT.mp4 [--text TEXT]
"""
import json
import math
import os
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

TEXT = "@AvanoMediaBot"
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
APPEAR_RATIO = 0.45      # watermark appears at 45% of duration
END_MARGIN = 0.7         # fades out END_MARGIN seconds before the end
FADE_D = 0.9             # fade in/out seconds
TEXT_ALPHA = 102         # ~40% opacity, white-greenish
DRIFT_X, DRIFT_Y = 10, 6  # px, very slow drift


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=width,height", "-of", "json", path],
        capture_output=True, text=True, check=True)
    j = json.loads(out.stdout)
    st = next(s for s in j["streams"] if s.get("width"))
    return float(j["format"]["duration"]), int(st["width"]), int(st["height"])


def make_watermark_png(text, video_w, out_path):
    """Glassy pill with the handle text. Size scales with video width."""
    pill_w = int(video_w * 0.54)
    font_size = max(18, int(video_w * 0.045))
    font = ImageFont.truetype(FONT_PATH, font_size)
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    tw, th = tmp.textbbox((0, 0), text, font=font)[2:]
    pad_x = int(font_size * 0.9)
    pill_h = int(th + font_size * 1.05)
    pill_w = max(pill_w, tw + pad_x * 2)

    pill = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(pill)
    r = pill_h // 2
    # glass body: soft white, blurred
    d.rounded_rectangle([0, 0, pill_w, pill_h], radius=r, fill=(255, 255, 255, 64))
    pill = pill.filter(ImageFilter.GaussianBlur(2))
    d = ImageDraw.Draw(pill)
    # subtle border
    d.rounded_rectangle([1, 1, pill_w - 2, pill_h - 2], radius=r - 1,
                        outline=(255, 255, 255, 110), width=2)
    # text: white with a green tint, ~40% opacity
    d.text(((pill_w - tw) / 2, (pill_h - th) / 2 - 1), text, font=font,
           fill=(232, 255, 240, TEXT_ALPHA))
    pill.save(out_path)
    return pill_w, pill_h


def pick_position(frame_path, W, H, wm_w, wm_h):
    """Choose the emptiest candidate region via edge density."""
    img = Image.open(frame_path).convert("L")
    sw, sh = 180, int(180 * img.height / img.width)
    small = img.resize((sw, sh))
    a = np.asarray(small, dtype=np.float32)
    gx = np.zeros_like(a)
    gy = np.zeros_like(a)
    gx[1:-1, 1:-1] = a[1:-1, 2:] - a[1:-1, :-2]
    gy[1:-1, 1:-1] = a[2:, 1:-1] - a[:-2, 1:-1]
    mag = np.sqrt(gx ** 2 + gy ** 2)

    mx, my = int(W * 0.05), int(H * 0.05)
    cands = {
        "bottom_left": (mx, H - my - wm_h),
        "bottom_center": ((W - wm_w) // 2, H - my - wm_h),
        "bottom_right": (W - mx - wm_w, H - my - wm_h),
        "top_left": (mx, my),
        "top_right": (W - mx - wm_w, my),
    }
    best, best_e = None, None
    for name, (x, y) in cands.items():
        x0, y0 = int(x / W * sw), int(y / H * sh)
        x1, y1 = int((x + wm_w) / W * sw), int((y + wm_h) / H * sh)
        e = mag[max(y0, 0):y1, max(x0, 0):x1].mean()
        if best_e is None or e < best_e:
            best, best_e = (name, x, y), e
    return best  # (name, x, y)


def apply_watermark(in_path, out_path, text=TEXT):
    dur, W, H = probe(in_path)
    start = dur * APPEAR_RATIO
    end = max(start + 2.0, dur - END_MARGIN)

    with tempfile.TemporaryDirectory() as td:
        wm_png = os.path.join(td, "wm.png")
        wm_w, wm_h = make_watermark_png(text, W, wm_png)
        frame = os.path.join(td, "frame.png")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-i", in_path,
             "-frames:v", "1", frame], check=True)
        name, x, y = pick_position(frame, W, H, wm_w, wm_h)

        fc = (
            "[1:v]format=rgba,"
            f"fade=t=in:st={start:.2f}:d={FADE_D}:alpha=1,"
            f"fade=t=out:st={end - FADE_D:.2f}:d={FADE_D}:alpha=1[wm];"
            "[0:v][wm]overlay="
            f"x='{x}+{DRIFT_X}*sin(2*PI*t/9)':"
            f"y='{y}+{DRIFT_Y}*cos(2*PI*t/13)':"
            f"enable='between(t,{start:.2f},{end:.2f})',format=yuv420p[v]"
        )
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", in_path,
               "-loop", "1", "-t", f"{dur:.2f}", "-i", wm_png,
               "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
               "-c:a", "copy", "-movflags", "+faststart", "-shortest", out_path]
        subprocess.run(cmd, check=True)

    return {"duration": round(dur, 2), "size": [W, H],
            "wm_window": [round(start, 2), round(end, 2)],
            "position": name, "wm_xy": [x, y], "wm_size": [wm_w, wm_h]}


if __name__ == "__main__":
    inp, outp = sys.argv[1], sys.argv[2]
    text = sys.argv[3] if len(sys.argv) > 3 else TEXT
    import time
    t0 = time.time()
    info = apply_watermark(inp, outp, text)
    info["elapsed_s"] = round(time.time() - t0, 2)
    print(json.dumps(info, ensure_ascii=False, indent=1))
