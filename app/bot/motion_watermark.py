#!/usr/bin/env python3
"""Avano Motion Watermark v2 — live in the story-clips send pipeline
(app/bot/story_clips.py calls apply_watermark for every clip).

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


def _audio_codec(path):
    """First audio stream's codec name, or None when the file is silent."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    return out.stdout.strip() or None


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
            f"enable='between(t,{start:.2f},{end:.2f})',"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[v]"
        )
        # audio: preserved whenever the source has it. AAC is copied
        # losslessly; any other codec is re-encoded to AAC 128k (Telegram
        # compatible). Silent sources stay silent — never add fake audio.
        acodec = _audio_codec(in_path)
        if acodec == "aac":
            audio_args = ["-map", "0:a?", "-c:a", "copy"]
        elif acodec:
            audio_args = ["-map", "0:a?", "-c:a", "aac", "-b:a", "128k"]
        else:
            audio_args = []
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", in_path,
               "-loop", "1", "-t", f"{dur:.2f}", "-i", pill_path,
               "-loop", "1", "-t", f"{dur:.2f}", "-i", glow_path,
               "-filter_complex", fc, "-map", "[v]", *audio_args,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
               "-movflags", "+faststart", "-shortest", out_path]
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

# ==================================================== Avano Waveframe v3 ===
# Chosen by the user (2026-11-05) as the official story-clips watermark.
# "Living" edition: the EQ signature pulses like real audio, the badge
# enters with a cinematic ease-out, a light sweep crosses the glass every
# few seconds, and the whole badge gently floats. Custom to AvanoMediaBot.
# apply_watermark() (v2 glass pill, mid-clip) is kept untouched for the
# legacy Pixabay flow until it is retired.
DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
WAVEFRAME_ALPHA = 0.28  # on-screen opacity for the fixed badge
WAVEFRAME_FPS = 15      # animation frame rate (smooth enough, cheap)
WAVEFRAME_SS = 2        # supersample factor (2026-11-06 real-test fix):
# the badge is rasterized at 2x its on-screen size and downscaled with
# LANCZOS in the ffmpeg overlay chain, so text and edges stay sharp
# after the re-encode. The on-screen size stays 0.47 * video width.


def _vgrad(w, h, top_c, bot_c):
    g = Image.new("RGBA", (1, h))
    for yy in range(h):
        t = yy / max(h - 1, 1)
        g.putpixel((0, yy), tuple(int(top_c[i] + (bot_c[i] - top_c[i]) * t)
                                  for i in range(4)))
    return g.resize((w, h))


def _chamfer_points(x0, y0, x1, y1, ch):
    return [(x0 + ch, y0), (x1 - ch, y0), (x1, y0 + ch), (x1, y1 - ch),
            (x1 - ch, y1), (x0 + ch, y1), (x0, y1 - ch), (x0, y0 + ch)]


def build_waveframe_badge(video_w, eq_ratios=None, scale=1.0, ss=1.0):
    """Build the Avano Waveframe badge as a transparent RGBA asset.

    eq_ratios: 5 EQ bar heights (0..1). scale: entrance zoom (1.0 = final).
    ss: supersample factor — the badge is rasterized at ss x its on-screen
    size (all geometry, incl. padding, line widths and blur radii, scales
    with ss); the caller downscales it back with LANCZOS. ss=1.0 keeps
    the legacy 1x render.
    """
    bw = int(video_w * 0.47 * ss)
    bh = int(video_w * 0.104 * ss)
    ch = int(bh * 0.30)
    pad = int(44 * ss)
    aw, ah = bw + pad * 2, bh + pad * 2 + int(18 * ss)
    ox, oy = pad, pad
    base = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
    pts = _chamfer_points(ox, oy, ox + bw, oy + bh, ch)

    # drop shadow
    sh = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
    ImageDraw.Draw(sh).polygon([(x, y + int(9 * ss)) for x, y in pts],
                               fill=(0, 0, 0, 95))
    base = Image.alpha_composite(base, sh.filter(
        ImageFilter.GaussianBlur(int(10 * ss))))

    # glass gradient body
    grad = _vgrad(bw, bh, (34, 96, 88, 255), (10, 42, 38, 255))
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).polygon([(x - ox, y - oy) for x, y in pts], fill=255)
    base.paste(grad, (ox, oy), mask)

    # outer neon glow ring following the chamfered silhouette
    glow_pts = _chamfer_points(ox - int(5 * ss), oy - int(5 * ss),
                               ox + bw + int(5 * ss), oy + bh + int(5 * ss),
                               ch + int(5 * ss))
    gl = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
    ImageDraw.Draw(gl).line(glow_pts + [glow_pts[0]],
                            fill=(110, 255, 185, 65),
                            width=max(1, int(8 * ss)), joint="curve")
    base = Image.alpha_composite(base, gl.filter(
        ImageFilter.GaussianBlur(int(7 * ss))))

    d = ImageDraw.Draw(base)
    d.line(pts + [pts[0]], fill=(212, 255, 232, 160),
           width=max(1, int(2 * ss)), joint="curve")
    d.line([(ox + ch + 8, oy + 3), (ox + bw - ch - 8, oy + 3)],
           fill=(235, 255, 242, 120), width=max(1, int(2 * ss)))

    # sheen across the upper area
    sheen = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
    ImageDraw.Draw(sheen).polygon(
        [(ox + ch, oy + int(9 * ss)), (ox + bw - ch, oy + int(9 * ss)),
         (ox + bw - ch - int(16 * ss), oy + int(bh * 0.40)),
         (ox + ch + int(16 * ss), oy + int(bh * 0.40))],
        fill=(255, 255, 255, 20))
    base = Image.alpha_composite(base, sheen.filter(
        ImageFilter.GaussianBlur(max(1, int(2 * ss)))))
    d = ImageDraw.Draw(base)

    # monogram: chamfered plaque (same design language as the frame)
    m = int(bh * 0.31)
    mcx, mcy = ox + int(bh * 0.55), oy + bh // 2
    mc = int(m * 0.45)
    mpts = _chamfer_points(mcx - m, mcy - m, mcx + m, mcy + m, mc)
    mdisc = _vgrad(m * 2, m * 2, (56, 132, 116, 255), (24, 70, 62, 255))
    mmask = Image.new("L", (m * 2, m * 2), 0)
    ImageDraw.Draw(mmask).polygon(
        [(x - (mcx - m), y - (mcy - m)) for x, y in mpts], fill=255)
    base.paste(mdisc, (mcx - m, mcy - m), mmask)
    d = ImageDraw.Draw(base)
    d.line(mpts + [mpts[0]], fill=(215, 255, 232, 175),
           width=max(1, int(2 * ss)), joint="curve")
    af = ImageFont.truetype(DEJAVU_BOLD, int(m * 1.02))
    ab = af.getbbox("A")
    d.text((mcx - (ab[0] + ab[2]) / 2, mcy - (ab[1] + ab[3]) / 2),
           "A", font=af, fill=(230, 255, 242, 255))

    # divider
    x_div = mcx + m + int(bh * 0.20)
    d.line([x_div, oy + int(bh * 0.27), x_div, oy + bh - int(bh * 0.27)],
           fill=(190, 255, 220, 75), width=max(1, int(1 * ss)))

    # EQ signature pulse (animated via eq_ratios)
    ratios = eq_ratios or [0.35, 0.62, 1.0, 0.62, 0.35]
    nbars = len(ratios)
    bw_bar, gap = max(3, int(bh * 0.045)), max(3, int(bh * 0.038))
    max_h = int(bh * 0.46)
    total_w = nbars * bw_bar + (nbars - 1) * gap
    ex = ox + bw - int(bh * 0.24) - total_w
    ecy = oy + bh // 2
    bars = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bars)
    for i, r in enumerate(ratios):
        hbar = max(4, int(max_h * r))
        x = ex + i * (bw_bar + gap)
        bd.rounded_rectangle([x, ecy - hbar / 2, x + bw_bar, ecy + hbar / 2],
                             radius=bw_bar // 2, fill=(150, 255, 195, 235))
    base = Image.alpha_composite(base, bars.filter(
        ImageFilter.GaussianBlur(max(1, int(1 * ss)))))
    base = Image.alpha_composite(base, bars)
    d = ImageDraw.Draw(base)

    # wordmark between divider and equalizer
    tf = ImageFont.truetype(FONT_PATH, int(bh * 0.255))
    spacing = max(1, int(bh * 0.022))
    widths = [d.textlength(ch_, font=tf) for ch_ in TEXT]
    total = sum(widths) + spacing * (len(TEXT) - 1)
    x0 = x_div + int(bh * 0.20)
    x1 = ex - int(bh * 0.26)
    sx = x0 + max(0, (x1 - x0 - total) / 2)
    tb = tf.getbbox(TEXT)
    sy = mcy - (tb[1] + tb[3]) / 2
    x = sx
    for ch_, wch in zip(TEXT, widths):
        d.text((x, sy), ch_, font=tf, fill=(242, 255, 248, 255))
        x += wch + spacing

    if abs(scale - 1.0) > 1e-6:
        sw_, sh_ = max(1, int(aw * scale)), max(1, int(ah * scale))
        small = base.resize((sw_, sh_), Image.BICUBIC)
        canvas = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
        canvas.paste(small, ((aw - sw_) // 2, (ah - sh_) // 2), small)
        base = canvas
    return base


def _waveframe_frame(video_w, t, alpha, ss=1.0):
    """One animation frame of the living badge at time t (seconds).

    ss: supersample factor (see build_waveframe_badge) — the frame is
    rasterized at ss x its on-screen size.
    """
    # cinematic entrance: ease-out zoom + fade over the first 1.2s
    TIN = 1.2
    if t < TIN:
        e = max(t, 0.0) / TIN
        sc = 0.90 + 0.10 * (1 - (1 - e) ** 3)
        ain = min(1.0, max(t, 0.0) / 0.7)
    else:
        sc, ain = 1.0, 1.0
    # living EQ: each bar dances at its own frequency/phase + a slow beat
    beat = 0.8 + 0.2 * math.sin(2 * math.pi * 1.3 * t + 0.5)
    ratios = []
    for i in range(5):
        f = 0.8 + i * 0.27
        ph = i * 1.9
        r = 0.15 + 0.85 * (0.5 + 0.5 * math.sin(2 * math.pi * f * t + ph))
        ratios.append(max(0.12, min(1.0, r * beat)))
    badge = build_waveframe_badge(video_w, eq_ratios=ratios, scale=sc, ss=ss)
    # light sweep across the glass every 7s (starts at t=2s)
    SWEEP_EVERY, SWEEP_DUR = 7.0, 1.4
    cyc = (t - 2.0) % SWEEP_EVERY
    if 0 <= cyc <= SWEEP_DUR:
        p = cyc / SWEEP_DUR
        Wb, Hb = badge.size
        sw = Image.new("RGBA", (Wb, Hb), (0, 0, 0, 0))
        xc = Wb * (-0.25 + 1.5 * p)
        bwid = Wb * 0.10
        ImageDraw.Draw(sw).polygon(
            [(xc - bwid, 0), (xc + bwid * 0.4, 0),
             (xc - bwid * 0.4, Hb), (xc - bwid, Hb)],
            fill=(255, 255, 255, int(46 * math.sin(math.pi * p))))
        badge = Image.alpha_composite(badge, sw.filter(
            ImageFilter.GaussianBlur(max(1, int(3 * ss)))))
    # bake the global opacity (badge alpha * entrance fade)
    k = alpha * ain
    if k < 1.0:
        arr = np.asarray(badge).astype(np.float32)
        arr[..., 3] *= k
        badge = Image.fromarray(arr.astype(np.uint8), "RGBA")
    return badge


def render_waveframe_frames(video_w, dur, alpha, fps=WAVEFRAME_FPS,
                            out_dir=None, ss=1.0):
    """Render the living-badge PNG sequence covering the whole clip.

    ss: supersample factor — frames are rasterized at ss x on-screen size
    (the caller downscales them with LANCZOS before overlay).
    """
    n = int(math.ceil(dur * fps)) + 1
    for i in range(n):
        t = i / fps
        frame = _waveframe_frame(video_w, t, alpha, ss=ss)
        frame.save(os.path.join(out_dir, f"f{i:04d}.png"))
    return n


def _audio_args_for(in_path):
    acodec = _audio_codec(in_path)
    if acodec == "aac":
        return ["-map", "0:a?", "-c:a", "copy"]
    if acodec:
        return ["-map", "0:a?", "-c:a", "aac", "-b:a", "128k"]
    return []


def apply_waveframe_badge(in_path, out_path, alpha=WAVEFRAME_ALPHA,
                          animated=True, fps=WAVEFRAME_FPS):
    """Apply the Avano Waveframe corner badge for the whole clip.

    animated=True (default): the living edition — EQ pulses like real audio,
    cinematic ease-out entrance, light sweep across the glass every 7s and a
    gentle float. animated=False: the original static badge.
    Quality (2026-11-06 real-test fix): the badge is rasterized at 2x its
    on-screen size (WAVEFRAME_SS) and downscaled with LANCZOS in the
    overlay chain, so the wordmark and edges stay sharp after the
    re-encode; the on-screen size stays 0.47 * video width. Encode is
    CRF 18 (preset veryfast). Audio is preserved (AAC copied, other
    codecs re-encoded to AAC, silent sources stay silent).
    """
    dur, W, H = probe(in_path)
    ss = WAVEFRAME_SS
    with tempfile.TemporaryDirectory() as td:
        if animated:
            render_waveframe_frames(W, dur, alpha, fps, td, ss=ss)
            badge_probe = Image.open(os.path.join(td, "f0000.png"))
            aw2, ah2 = badge_probe.size
            # on-screen (1x) badge size — the frames are ss x larger
            aw, ah = aw2 // ss, ah2 // ss
            mx = int(W * 0.06)
            x, y = W - aw - mx, int(H * 0.90) - ah
            fc = (
                f"[1:v]scale=iw/{ss}:ih/{ss}:flags=lanczos,format=rgba[wm];"
                "[0:v][wm]overlay="
                f"x={x}:y='{y}+4*sin(2*PI*t/5)':eof_action=pass,"
                "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[v]"
            )
            inputs = ["-i", in_path, "-framerate", str(fps),
                      "-i", os.path.join(td, "f%04d.png")]
        else:
            badge_path = os.path.join(td, "waveframe.png")
            badge = build_waveframe_badge(W, ss=ss)
            badge.save(badge_path)
            aw2, ah2 = badge.size
            aw, ah = aw2 // ss, ah2 // ss
            mx = int(W * 0.06)
            x, y = W - aw - mx, int(H * 0.90) - ah
            fc = (
                f"[1:v]scale=iw/{ss}:ih/{ss}:flags=lanczos,format=rgba,"
                f"colorchannelmixer=aa={alpha},"
                "fade=t=in:st=0:d=0.5:alpha=1[wm];"
                "[0:v][wm]overlay="
                f"x={x}:y={y},"
                "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[v]"
            )
            inputs = ["-i", in_path, "-loop", "1", "-t", f"{dur:.2f}",
                      "-i", badge_path]
        cmd = ["ffmpeg", "-v", "error", "-y", *inputs,
               "-filter_complex", fc, "-map", "[v]",
               *_audio_args_for(in_path),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
               "-movflags", "+faststart", "-shortest", out_path]
        subprocess.run(cmd, check=True)

    return {"duration": round(dur, 2), "size": [W, H],
            "badge": [aw, ah], "position": [x, y], "alpha": alpha,
            "animated": animated, "style": "waveframe-fixed"}


OUTRO_ASSET = os.path.join(os.path.dirname(__file__), "..", "assets",
                           "avano_outro_wf_2s5.mp4")
OUTRO_DUR = 2.5  # seconds of the AVANO MEDIA end bumper


def overlay_outro(in_path, out_path, outro_path=None):
    """Overlay the AVANO MEDIA end bumper on a clip's final seconds.

    Final order 2026-11-06: the end motion graphic is kept for clips
    whose processing and republishing is allowed. The bumper (2.5s,
    Waveframe hero, Persian tagline) is overlaid on the clip's FINAL
    2.5s — the clip's own audio plays on untouched, no silence is ever
    added, and the total duration does not change (same design as the
    approved Phase 2 process_clip_for_approval).

    The bumper is a vertical 9:16 asset, so it applies only to portrait
    clips (H > W) of at least OUTRO_DUR + 1s; otherwise the input is
    copied unchanged and applied=False is returned. Never raises on a
    missing asset — callers treat applied=False as "send the badged
    file". Encode: libx264, preset veryfast, CRF 18 (same as the badge);
    audio preserved (AAC copied, other codecs re-encoded to AAC).
    """
    import shutil
    outro = outro_path or OUTRO_ASSET
    dur, W, H = probe(in_path)
    if H <= W or dur < OUTRO_DUR + 1.0 or not os.path.exists(outro):
        shutil.copyfile(in_path, out_path)
        reason = ("not_portrait" if H <= W else
                  "too_short" if dur < OUTRO_DUR + 1.0 else "no_asset")
        return {"applied": False, "duration": round(dur, 2),
                "size": [W, H], "reason": reason}
    start = dur - OUTRO_DUR
    fc = (
        f"[1:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
        f"format=yuv420p[ov];"
        f"[0:v][ov]overlay=enable='gte(t,{start:.2f})':eof_action=pass,"
        f"scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[v]"
    )
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-i", in_path,
           "-itsoffset", f"{start:.2f}", "-i", outro,
           "-filter_complex", fc, "-map", "[v]",
           *_audio_args_for(in_path),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-movflags", "+faststart", out_path]
    subprocess.run(cmd, check=True)
    return {"applied": True, "duration": round(dur, 2), "size": [W, H],
            "outro_start": round(start, 2),
            "outro": os.path.basename(outro)}


# ------------------------------------------------------- user badge (v3) ---
# MAIN brand badge (user order 2026-11-05): the user-designed plaque
# (neon cyan->violet rim, octagon A emblem, "AvanoMediaBot" wordmark with a
# play-button "o", gradient swoosh underline, live EQ at the right end).
# Brand placement: FIXED bottom-right corner for the whole video (like a TV
# channel bug — never wanders), 0.38 * W wide, 0.7 opacity, gentle 1s
# fade-in only; the ONLY motion is the live EQ inside the badge.
# Asset: app/assets/user_badge.png — the EQ-less rebuild (EQ is drawn live
# per frame). Built by ~/workspace/wm_demo/build_user_badge.py.
USER_BADGE_ASSET = os.path.join(os.path.dirname(__file__), "..", "assets",
                                "user_badge.png")
USER_BADGE_SCALE = 0.38
USER_BADGE_ALPHA = 0.7
USER_BADGE_MARGIN_F = 0.05
USER_BADGE_FPS = 15
USER_BADGE_SS = 2         # supersample factor (2026-11-06 real-test fix):
# the badge is rasterized at 2x its on-screen size and downscaled with
# LANCZOS in the ffmpeg overlay chain, so text and edges stay sharp
# after the re-encode. The on-screen size stays 0.38 * video width
# (or the boosted scale below for small videos).
# Measured wordmark cap height in the asset (944x264): 42px (p95 of ink
# columns; play-o circle excluded). At 360px phone width the cap height
# is 42 * scale * 360 / 944 — 6.1px at the standard 0.38 scale, which is
# why the wordmark was unreadable on small clips.
_USER_BADGE_TEXT_H = 42
_USER_BADGE_ASSET_W = 944
_USER_BADGE_MIN_TEXT_PHONE = 9.0   # px at 360px phone width
_USER_BADGE_SMALL_W = 720          # boost applies only below this width


def _user_badge_scale_for(W):
    """Badge scale (fraction of video width). Small videos (<720px wide)
    get a boost so the wordmark cap height reaches >= 9px at 360px phone
    width; larger videos keep the standard discreet 0.38 scale."""
    if W >= _USER_BADGE_SMALL_W:
        return USER_BADGE_SCALE
    need = (_USER_BADGE_MIN_TEXT_PHONE * _USER_BADGE_ASSET_W
            / (_USER_BADGE_TEXT_H * 360.0))
    return max(USER_BADGE_SCALE, need)
# EQ live area in the badge asset's design coords (944x264).
_USER_BADGE_EQ = (808, 100, 86, 64)
_USER_BADGE_NEON = [(34, 211, 238), (59, 130, 246),
                    (139, 92, 246), (217, 70, 239)]


def _user_badge_eq_frame(w, h, t):
    """5 live EQ bars (right end of the badge), cyan->violet."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    n = 5
    bw_bar = max(3, w // 9)
    gap = (w - n * bw_bar) / (n - 1) if n > 1 else 0
    beat = 0.8 + 0.2 * math.sin(2 * math.pi * 1.3 * t + 0.5)
    ratios = [0.45, 0.8, 1.0, 0.7, 0.5]
    for i in range(n):
        f = 0.9 + i * 0.31
        ph = i * 1.7
        dance = 0.15 + 0.85 * (0.5 + 0.5 * math.sin(
            2 * math.pi * f * t + ph)) * beat
        r = max(0.2, min(1.0, dance)) * ratios[i]
        bh = max(4, int((h - 4) * r))
        x = int(i * (bw_bar + gap))
        y = (h - bh) // 2
        g = (i / max(1, n - 1)) * (len(_USER_BADGE_NEON) - 1)
        gi = min(len(_USER_BADGE_NEON) - 2, int(g))
        gf = g - gi
        c = tuple(int(_USER_BADGE_NEON[gi][k]
                      + (_USER_BADGE_NEON[gi + 1][k]
                         - _USER_BADGE_NEON[gi][k]) * gf)
                  for k in range(3)) + (240,)
        d.rounded_rectangle([x, y, x + bw_bar, y + bh], radius=bw_bar // 2,
                            fill=c)
    return img


def _user_badge_frame(video_w, t, alpha, ss=1.0, scale=None):
    """One overlay frame (badge-sized): badge + live EQ, faded in.

    ss: supersample factor — the badge is rasterized at ss x its on-screen
    size (asset scaled with LANCZOS, EQ drawn at ss x); the caller
    downscales it back with LANCZOS. scale: badge-scale override
    (fraction of video width); default is the small-video-aware
    _user_badge_scale_for(video_w).
    """
    src = Image.open(USER_BADGE_ASSET).convert("RGBA")
    sc = ((scale if scale is not None else _user_badge_scale_for(video_w))
          * video_w * ss) / src.width
    bw, bh = int(src.width * sc), int(src.height * sc)
    badge = src.resize((bw, bh), Image.LANCZOS)
    ex0, ey0, ewd, ehd = _USER_BADGE_EQ
    eq = _user_badge_eq_frame(max(8, int(ewd * sc)),
                              max(8, int(ehd * sc)), t)
    badge.alpha_composite(eq, (int(ex0 * sc), int(ey0 * sc)))
    # gentle 1s ease-out fade-in, then hold at `alpha`
    e = max(0.0, min(1.0, t / 1.0))
    ease = 1 - (1 - e) ** 3
    badge.putalpha(badge.split()[3].point(
        lambda v: int(v * alpha * ease)))
    return badge


def apply_user_badge(in_path, out_path, alpha=USER_BADGE_ALPHA,
                     animated=True, fps=USER_BADGE_FPS):
    """Apply the MAIN user badge as a fixed bottom-right corner bug.

    animated=True (default): the EQ inside the badge dances like real
    audio; animated=False: one mid-dance EQ pose, static.
    Quality (2026-11-06 real-test fix): the badge is rasterized at 2x its
    on-screen size (USER_BADGE_SS) and downscaled with LANCZOS in the
    overlay chain, so the wordmark and edges stay sharp after the
    re-encode; the on-screen size stays 0.38 * video width, except small
    videos (<720px wide) where the scale is boosted so the wordmark cap
    height reaches >= 9px at 360px phone width. Encode is CRF 18
    (preset veryfast). Audio is preserved (AAC copied, other codecs
    re-encoded to AAC, silent sources stay silent). The badge never
    moves, never covers the clip's main subject area beyond its
    discreet corner.
    """
    dur, W, H = probe(in_path)
    ss = USER_BADGE_SS
    scale = _user_badge_scale_for(W)
    with tempfile.TemporaryDirectory() as td:
        if animated:
            n = int(math.ceil(dur * fps)) + 1
            for i in range(n):
                _user_badge_frame(W, i / fps, alpha, ss=ss,
                                  scale=scale).save(
                    os.path.join(td, f"f{i:04d}.png"))
            badge_probe = Image.open(os.path.join(td, "f0000.png"))
            aw2, ah2 = badge_probe.size
            # on-screen (1x) badge size — the frames are ss x larger
            aw, ah = aw2 // ss, ah2 // ss
            inputs = ["-i", in_path, "-framerate", str(fps),
                      "-i", os.path.join(td, "f%04d.png")]
        else:
            badge_path = os.path.join(td, "user_badge.png")
            _user_badge_frame(W, 0.6, alpha, ss=ss, scale=scale).save(
                badge_path)
            aw2, ah2 = Image.open(badge_path).size
            aw, ah = aw2 // ss, ah2 // ss
            inputs = ["-i", in_path, "-loop", "1", "-t", f"{dur:.2f}",
                      "-i", badge_path]
        m = int(W * USER_BADGE_MARGIN_F)
        x, y = W - aw - m, H - ah - m
        fc = (
            f"[1:v]scale=iw/{ss}:ih/{ss}:flags=lanczos,format=rgba[wm];"
            "[0:v][wm]overlay="
            f"x={x}:y={y}:eof_action=pass,"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[v]"
        )
        cmd = ["ffmpeg", "-v", "error", "-y", *inputs,
               "-filter_complex", fc, "-map", "[v]",
               *_audio_args_for(in_path),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
               "-movflags", "+faststart", "-shortest", out_path]
        subprocess.run(cmd, check=True)

    return {"duration": round(dur, 2), "size": [W, H],
            "badge": [aw, ah], "position": [x, y], "alpha": alpha,
            "animated": animated, "style": "user-badge-fixed"}
