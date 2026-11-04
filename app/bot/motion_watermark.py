#!/usr/bin/env python3
"""Avano Motion Watermark v2 — final design prototype (NOT enabled for users).

Reference: the attached brand PNG (glassy neon-green pill, play button,
@AvanoMediaBot, clapperboard). The attached PNG has NO real alpha channel
(checkerboard baked into RGB), so the design is faithfully REBUILT here as a
true transparent asset — the checkerboard never enters the video.

Final spec (2026-11-03):
- pill width 22-28% of video width (target 25%), glassy, neon-green tint
- final on-screen opacity 20-30% (applied at composite time, aa=0.25)
- appears at 40-50% of duration, 800ms fade in, very slow float,
  a subtle green light orbiting the glass edge (constant intensity,
  no blinking), fade out in the last seconds
- position: emptiest region via edge density + story safe margins,
  never the centre; small enough to never cover the subject
- original file never modified; temp files cleaned up

CLI: python3 motion_watermark.py IN.mp4 OUT.mp4
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
PILL_RATIO = 0.25        # pill width = 25% of video width
FINAL_ALPHA = 0.25       # 25% on-screen opacity (spec: 20-30%)
APPEAR_RATIO = 0.45
END_MARGIN = 1.0         # fully gone 1s before the end
FADE_D = 0.8             # 800ms fade in/out
GLOW_ORBIT_S = 7.0       # one slow orbit every 7s
FLOAT_X, FLOAT_Y = 6, 4  # px drift, very subtle


# ---------------------------------------------------------------- assets ---
def _rounded(canvas_size, rect, radius, fill):
    layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(rect, radius=radius, fill=fill)
    return layer


def build_pill(video_w):
    """Rebuild the brand pill as a true transparent asset."""
    pw = int(video_w * PILL_RATIO)
    ph = int(pw * 0.30)
    W, H = pw + 40, ph + 56  # padding for glow + clapperboard
    ox, oy = 20, 28

    base = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # soft neon glow behind the pill
    glow = _rounded((W, H), [ox - 6, oy - 6, ox + pw + 6, oy + ph + 6],
                    ph // 2, (90, 255, 175, 46))
    base = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(7)))

    # glass body: dark teal, blurred for the frosted feel
    body = _rounded((W, H), [ox, oy, ox + pw, oy + ph], ph // 2,
                    (18, 58, 52, 255))
    body = body.filter(ImageFilter.GaussianBlur(1.2))
    base = Image.alpha_composite(base, body)

    # glass edge: thin bright rim, slightly stronger at the bottom
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([ox + 1, oy + 1, ox + pw - 1, oy + ph - 1],
                        radius=ph // 2 - 1, outline=(190, 255, 220, 95), width=2)
    d.line([ox + 8, oy + ph - 3, ox + pw - 8, oy + ph - 3],
           fill=(120, 255, 190, 70), width=2)

    # top sheen (glass highlight)
    sheen = _rounded((W, H), [ox + 6, oy + 3, ox + pw - 6, oy + ph // 2],
                     ph // 4, (255, 255, 255, 26))
    base = Image.alpha_composite(base, sheen)

    # play button: glass circle + neon triangle, left side
    cr = int(ph * 0.40)
    cx, cy = ox + int(ph * 0.52), oy + ph // 2
    circ = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dc = ImageDraw.Draw(circ)
    dc.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=(32, 96, 84, 255))
    base = Image.alpha_composite(base, circ.filter(ImageFilter.GaussianBlur(0.6)))
    d = ImageDraw.Draw(base)
    d.ellipse([cx - cr, cy - cr, cx + cr, cy + cr],
              outline=(200, 255, 225, 120), width=2)
    tr = int(cr * 0.52)
    d.polygon([(cx - tr * 0.55, cy - tr), (cx - tr * 0.55, cy + tr),
               (cx + tr * 0.85, cy)], fill=(175, 255, 208, 255))

    # handle text, shrunk to fit the remaining width
    avail = pw - (cx - ox + cr) - int(ph * 0.28)
    size = int(ph * 0.36)
    font = ImageFont.truetype(FONT_PATH, size)
    tw = ImageDraw.Draw(Image.new("RGBA", (8, 8))).textbbox(
        (0, 0), TEXT, font=font)[2]
    while tw > avail and size > 8:
        size -= 1
        font = ImageFont.truetype(FONT_PATH, size)
        tw = ImageDraw.Draw(Image.new("RGBA", (8, 8))).textbbox(
            (0, 0), TEXT, font=font)[2]
    tx = cx + cr + int(ph * 0.14)
    asc, _ = font.getmetrics()
    d.text((tx, cy - asc // 2 - 1), TEXT, font=font, fill=(238, 255, 244, 255))

    # clapperboard mini icon, top-right, tilted feel via offset stripes
    bw, bh = int(ph * 0.62), int(ph * 0.44)
    bx, by = ox + pw - bw - int(ph * 0.10), oy - int(ph * 0.30)
    d.rounded_rectangle([bx, by + 4, bx + bw, by + 4 + bh], radius=3,
                        fill=(22, 70, 62, 255),
                        outline=(200, 255, 225, 110), width=1)
    for i in range(4):  # slate stripes
        x0 = bx + 3 + i * (bw // 4)
        d.line([x0, by, x0 + 5, by + 7], fill=(235, 255, 242, 230), width=2)
    d.rounded_rectangle([bx - 2, by - 3, bx + bw + 2, by + 5], radius=2,
                        outline=(235, 255, 242, 200), width=2)
    return base, (ox, oy, pw, ph)


def build_glow():
    """Small soft green radial dot — the travelling edge light."""
    s = 72
    g = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    px = g.load()
    for y in range(s):
        for x in range(s):
            r = math.hypot(x - s / 2, y - s / 2) / (s / 2)
            if r < 1:
                a = int(235 * (1 - r) ** 2)
                px[x, y] = (150, 255, 195, a)
    return g


# -------------------------------------------------------------- position ---
def pick_position(frame_path, W, H, pw, ph):
    """Emptiest candidate region (edge density) inside story safe margins."""
    img = Image.open(frame_path).convert("L")
    sw = 180
    sh = int(180 * img.height / img.width)
    a = np.asarray(img.resize((sw, sh)), dtype=np.float32)
    gx = np.zeros_like(a)
    gy = np.zeros_like(a)
    gx[1:-1, 1:-1] = a[1:-1, 2:] - a[1:-1, :-2]
    gy[1:-1, 1:-1] = a[2:, 1:-1] - a[:-2, 1:-1]
    mag = np.sqrt(gx ** 2 + gy ** 2)

    mx = int(W * 0.06)
    top, bottom = int(H * 0.12), int(H * 0.80)  # story safe margins
    cands = {
        "bottom_left": (mx, bottom - ph),
        "bottom_center": ((W - pw) // 2, bottom - ph),
        "bottom_right": (W - mx - pw, bottom - ph),
        "top_left": (mx, top),
        "top_right": (W - mx - pw, top),
    }
    best, best_e = None, None
    for name, (x, y) in cands.items():
        x0, y0 = int(x / W * sw), int(y / H * sh)
        x1, y1 = int((x + pw) / W * sw), int((y + ph) / H * sh)
        e = mag[max(y0, 0):y1, max(x0, 0):x1].mean()
        if best_e is None or e < best_e:
            best, best_e = (name, x, y), e
    return best


# ------------------------------------------------------------------ main ---
def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=width,height,r_frame_rate", "-of", "json", path],
        capture_output=True, text=True, check=True)
    j = json.loads(out.stdout)
    st = next(s for s in j["streams"] if s.get("width"))
    return (float(j["format"]["duration"]), int(st["width"]),
            int(st["height"]))


def apply_watermark(in_path, out_path, text=TEXT):
    dur, W, H = probe(in_path)
    start = dur * APPEAR_RATIO
    end = max(start + 2.0, dur - END_MARGIN)

    with tempfile.TemporaryDirectory() as td:
        pill_path = os.path.join(td, "pill.png")
        glow_path = os.path.join(td, "glow.png")
        pill, (_ox, _oy, pw, ph) = build_pill(W)
        pill.save(pill_path)
        glow = build_glow()
        gw, gh = glow.size
        glow.save(glow_path)

        frame = os.path.join(td, "frame.png")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-i", in_path,
             "-frames:v", "1", frame], check=True)
        name, x, y = pick_position(frame, W, H, pw, ph)

        # pill layer: 25% opacity, 800ms fades; glow dot: same timing,
        # orbiting the pill ellipse once every 7s (position only — no blink)
        a = FINAL_ALPHA
        fc = (
            "[1:v]format=rgba,"
            f"colorchannelmixer=aa={a},"
            f"fade=t=in:st={start:.2f}:d={FADE_D}:alpha=1,"
            f"fade=t=out:st={end - FADE_D:.2f}:d={FADE_D}:alpha=1[wm];"
            "[2:v]format=rgba,"
            "colorchannelmixer=aa=0.55,"
            f"fade=t=in:st={start:.2f}:d={FADE_D}:alpha=1,"
            f"fade=t=out:st={end - FADE_D:.2f}:d={FADE_D}:alpha=1[gl];"
            "[0:v][wm]overlay="
            f"x='{x}+{_ox}+{FLOAT_X}*sin(2*PI*t/11)':"
            f"y='{y}+{_oy}+{FLOAT_Y}*cos(2*PI*t/14)':"
            f"enable='between(t,{start:.2f},{end:.2f})'[tmp];"
            "[tmp][gl]overlay="
            f"x='{x}+{_ox}+{pw/2:.1f}+({pw/2+16:.1f})*cos(2*PI*(t-{start:.2f})/{GLOW_ORBIT_S})-{gw/2}':"
            f"y='{y}+{_oy}+{ph/2:.1f}+({ph/2+12:.1f})*sin(2*PI*(t-{start:.2f})/{GLOW_ORBIT_S})-{gh/2}':"
            f"enable='between(t,{start:.2f},{end:.2f})',format=yuv420p[v]"
        )
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", in_path,
               "-loop", "1", "-t", f"{dur:.2f}", "-i", pill_path,
               "-loop", "1", "-t", f"{dur:.2f}", "-i", glow_path,
               "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
               "-c:a", "copy", "-movflags", "+faststart", "-shortest", out_path]
        subprocess.run(cmd, check=True)

    return {"duration": round(dur, 2), "size": [W, H],
            "wm_window": [round(start, 2), round(end, 2)],
            "position": name, "pill": [pw, ph], "alpha": a}


if __name__ == "__main__":
    import time
    t0 = time.time()
    info = apply_watermark(sys.argv[1], sys.argv[2])
    info["elapsed_s"] = round(time.time() - t0, 2)
    print(json.dumps(info, ensure_ascii=False, indent=1))
