#!/usr/bin/env python3
"""
Generate procedural overlay MP4s for visual effects.

Run once (from the repo root):
    python resource/overlays/generate_overlays.py

All overlays use a black background so they composite cleanly with
FFmpeg's 'screen' blend mode: black areas pass the clip through unchanged,
bright/coloured areas add luminance and colour on top.

Output: resource/overlays/<name>.mp4  (10 files, ~5–15 MB each)
"""

import math
import os
import subprocess
import sys
from typing import Iterator

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

W, H = 1920, 1080
FPS = 30
DUR = 12  # seconds — pipeline loops with -stream_loop -1, any length works

OUT_DIR = os.path.dirname(os.path.abspath(__file__))


# ── Pipe helper ───────────────────────────────────────────────────────────────

def _pipe(frames: Iterator[np.ndarray], out: str, crf: int = 26) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
        "-i", "pipe:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-crf", str(crf), "-preset", "fast",
        out,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for frame in frames:
            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
    proc.wait()
    print(f"  ✓  {os.path.basename(out)}")


def _ffmpeg_lavfi(vf: str, out: str, crf: int = 26) -> None:  # noqa: D401
    """Generate from FFmpeg lavfi source — no Python frame generation needed."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}",
        "-vf", vf, "-t", str(DUR),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-crf", str(crf), "-preset", "fast",
        out,
    ]
    subprocess.run(cmd, check=True, stderr=subprocess.DEVNULL)
    print(f"  ✓  {os.path.basename(out)}")


# ── Frame generators ──────────────────────────────────────────────────────────

def _gen_blood() -> Iterator[np.ndarray]:
    """Dark crimson splatter clusters that slowly pulse — threat."""
    rng = np.random.default_rng(7)
    base = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(base)

    for _ in range(6):
        cx = int(rng.integers(150, W - 150))
        cy = int(rng.integers(150, H - 150))
        for _ in range(rng.integers(10, 28)):
            dx = int(rng.integers(-150, 150))
            dy = int(rng.integers(-100, 100))
            r = int(rng.integers(6, 55))
            intensity = int(rng.integers(110, 210))
            color = (intensity, int(intensity * 0.04), int(intensity * 0.02))
            x, y = cx + dx, cy + dy
            draw.ellipse([x - r, y - r, x + r, y + r], fill=color)

    # Drip trails
    for _ in range(20):
        x = int(rng.integers(80, W - 80))
        y_s = int(rng.integers(40, H // 2))
        length = int(rng.integers(25, 180))
        w2 = int(rng.integers(2, 9))
        intensity = int(rng.integers(80, 190))
        draw.rectangle([x - w2, y_s, x + w2, y_s + length], fill=(intensity, 0, 0))

    base = base.filter(ImageFilter.GaussianBlur(radius=2))
    arr = np.array(base)

    for f in range(FPS * DUR):
        pulse = 0.82 + 0.18 * math.sin(f * 0.06)
        yield (arr * pulse).clip(0, 255).astype(np.uint8)


def _gen_snow() -> Iterator[np.ndarray]:
    """Falling white particles — cold."""
    rng = np.random.default_rng(42)
    n = 700
    x0 = rng.uniform(0, W, n)
    y0 = rng.uniform(0, H, n)
    speed = rng.uniform(2.0, 9.0, n)
    drift = rng.uniform(-0.5, 0.5, n)
    size = rng.integers(1, 5, n)
    bright = rng.integers(160, 256, n)

    for f in range(FPS * DUR):
        img = np.zeros((H, W, 3), dtype=np.uint8)
        ys = ((y0 + speed * f) % H).astype(int)
        xs = ((x0 + drift * f) % W).astype(int)
        # Fast center-pixel pass first
        img[ys, xs] = bright[:, None]
        # Size > 1: widen the bright pixels
        mask_big = size > 1
        for i in np.where(mask_big)[0]:
            s = int(size[i])
            y1 = max(0, int(ys[i]) - s)
            y2 = min(H, int(ys[i]) + s + 1)
            x1 = max(0, int(xs[i]) - s)
            x2 = min(W, int(xs[i]) + s + 1)
            img[y1:y2, x1:x2] = int(bright[i])
        yield img


def _gen_fog() -> Iterator[np.ndarray]:
    """Slow-drifting grey mist — mystery."""
    rng = np.random.default_rng(77)
    field_w, field_h = W + 300, H + 300
    noise = rng.integers(0, 200, (field_h, field_w), dtype=np.uint8)
    pil = Image.fromarray(noise).filter(ImageFilter.GaussianBlur(radius=70))
    field = np.array(pil, dtype=np.float32)

    for f in range(FPS * DUR):
        # Two layers drifting in opposite directions for organic feel
        dx1 = int(f * 0.7) % 300
        dy1 = int(f * 0.25) % 300
        dx2 = int(f * 0.4) % 300
        dy2 = int(300 - f * 0.15) % 300

        p1 = field[dy1:dy1 + H, dx1:dx1 + W]
        p2 = field[dy2:dy2 + H, dx2:dx2 + W]
        blended = ((p1 * 0.55 + p2 * 0.35) * 0.45).clip(0, 255).astype(np.uint8)
        yield np.stack([blended, blended, blended], axis=-1)


def _gen_bokeh() -> Iterator[np.ndarray]:
    """Soft pastel light orbs — dream. Rendered at 1/4 res, upscaled."""
    from PIL import Image
    rng = np.random.default_rng(55)
    sw, sh = W // 4, H // 4
    n = 22
    ox = rng.uniform(0, sw, n)
    oy = rng.uniform(0, sh, n)
    or_ = rng.uniform(12, 50, n)
    osx = rng.uniform(-0.07, 0.07, n)
    osy = rng.uniform(-0.05, 0.05, n)
    palette = np.array([
        [200, 145, 220], [145, 195, 220], [220, 195, 140],
        [175, 220, 175], [220, 155, 160], [190, 210, 235],
    ] * 4, dtype=np.float32)[:n]
    ob = rng.uniform(0.45, 0.95, n)

    yy, xx = np.mgrid[0:sh, 0:sw].astype(np.float32)

    for f in range(FPS * DUR):
        img = np.zeros((sh, sw, 3), dtype=np.float32)
        xs = (ox + osx * f) % sw
        ys = (oy + osy * f) % sh
        for i in range(n):
            dist2 = (xx - xs[i]) ** 2 + (yy - ys[i]) ** 2
            mask = np.exp(-dist2 / (2 * or_[i] ** 2)) * ob[i]
            img[:, :, 0] += mask * palette[i, 0]
            img[:, :, 1] += mask * palette[i, 1]
            img[:, :, 2] += mask * palette[i, 2]
        img = img.clip(0, 255).astype(np.uint8)
        yield np.array(Image.fromarray(img).resize((W, H), Image.BILINEAR))


def _gen_warmth_rays() -> Iterator[np.ndarray]:
    """Golden radial sun rays — warmth. Rendered at 1/4 res, upscaled."""
    rng = np.random.default_rng(11)
    sw, sh = W // 4, H // 4
    cx = sw * rng.uniform(0.35, 0.65)
    cy = sh * rng.uniform(-0.08, 0.22)

    n_rays = 15
    angles = rng.uniform(0, 2 * math.pi, n_rays)
    widths = rng.uniform(0.035, 0.11, n_rays)
    strengths = rng.uniform(0.55, 1.0, n_rays)

    yy, xx = np.mgrid[0:sh, 0:sw].astype(np.float32)
    dx = xx - cx
    dy = yy - cy
    dist = np.sqrt(dx ** 2 + dy ** 2) + 1.0
    theta = np.arctan2(dy, dx)

    ray_mask = np.zeros((sh, sw), dtype=np.float32)
    for i in range(n_rays):
        ang_diff = np.abs(((theta - angles[i] + math.pi) % (2 * math.pi)) - math.pi)
        ray_mask += np.exp(-ang_diff ** 2 / (2 * widths[i] ** 2)) * strengths[i]
    dist_fade = np.clip(sh * 1.6 / dist, 0, 1)
    ray_mask = (ray_mask * dist_fade).clip(0, 1)

    for f in range(FPS * DUR):
        shimmer = 0.72 + 0.28 * math.sin(f * 0.09)
        m = (ray_mask * 255 * shimmer).astype(np.uint8)
        rgb = np.stack([m, (m * 0.76).astype(np.uint8), (m * 0.20).astype(np.uint8)], axis=-1)
        yield np.array(Image.fromarray(rgb).resize((W, H), Image.BILINEAR))


def _gen_revelation_flare() -> Iterator[np.ndarray]:
    """Bright warm lens burst + cross-streaks — revelation. 1/4 res, upscaled."""
    sw, sh = W // 4, H // 4
    cx, cy = sw * 0.5, sh * 0.38

    yy, xx = np.mgrid[0:sh, 0:sw].astype(np.float32)
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) + 1.0

    # Cross-streak masks
    streak_h = np.exp(-((yy - cy) ** 2) / (sh * 0.028) ** 2) * np.exp(-((xx - cx) ** 2) / (sw * 3.0) ** 2)
    streak_v = np.exp(-((xx - cx) ** 2) / (sw * 0.028) ** 2) * np.exp(-((yy - cy) ** 2) / (sh * 3.0) ** 2)
    core = np.exp(-(dist ** 2) / (sh * 0.22) ** 2)
    halo = np.exp(-(dist ** 2) / (sh * 0.75) ** 2) * 0.35

    for f in range(FPS * DUR):
        pulse = 0.55 + 0.45 * abs(math.sin(f * 0.038 + 0.5))
        combined = ((core + halo + streak_h * 0.7 + streak_v * 0.7) * pulse).clip(0, 1)
        m = (combined * 255).astype(np.uint8)
        rgb = np.stack([m, (m * 0.88).astype(np.uint8), (m * 0.60).astype(np.uint8)], axis=-1)
        yield np.array(Image.fromarray(rgb).resize((W, H), Image.BILINEAR))


def _gen_noir_rain() -> Iterator[np.ndarray]:
    """Diagonal rain streaks — noir."""
    rng = np.random.default_rng(99)
    n = 550
    x0 = rng.uniform(0, W + 100, n).astype(np.float32)
    y0 = rng.uniform(0, H, n).astype(np.float32)
    speed = rng.uniform(12, 30, n)
    length = rng.integers(18, 50, n)
    bright = rng.integers(45, 120, n)

    for f in range(FPS * DUR):
        img = np.zeros((H, W), dtype=np.uint8)
        ys = ((y0 + speed * f) % H).astype(int)
        xs = x0.astype(int) % W

        for i in range(n):
            x = xs[i]
            y_base = ys[i]
            l = int(length[i])
            b = int(bright[i])
            y_end = min(H, y_base + l)
            seg_len = y_end - y_base
            if seg_len <= 0:
                continue
            seg_y = np.arange(y_base, y_end)
            seg_x = np.minimum(W - 1, x + (np.arange(seg_len) * 0.18).astype(int))
            np.maximum.at(img, (seg_y, seg_x), b)

        yield np.stack([img, img, img], axis=-1)


def _gen_nature_dust() -> Iterator[np.ndarray]:
    """Floating dust motes + soft light shafts — nature."""
    rng = np.random.default_rng(33)

    # Static shaft background
    shaft = Image.new("L", (W, H), 0)
    sd = ImageDraw.Draw(shaft)
    for _ in range(4):
        sx = int(rng.integers(W // 4, 3 * W // 4))
        spread = int(rng.integers(80, 220))
        sd.polygon([(sx - 25, 0), (sx + 25, 0),
                    (sx + spread, H), (sx - spread, H)], fill=28)
    shaft_arr = np.array(shaft.filter(ImageFilter.GaussianBlur(radius=35)), dtype=np.float32)

    n = 380
    mx = rng.uniform(0, W, n)
    my = rng.uniform(0, H, n)
    msx = rng.uniform(-0.25, 0.25, n)
    msy = rng.uniform(-0.5, 0.08, n)
    mb = rng.integers(70, 195, n)
    ms = rng.integers(1, 4, n)

    for f in range(FPS * DUR):
        img = shaft_arr.copy()
        xs = ((mx + msx * f) % W).astype(int)
        ys = ((my + msy * f) % H).astype(int)
        # Center pixels (vectorized)
        np.add.at(img, (ys, xs), mb.astype(np.float32))
        img = img.clip(0, 255).astype(np.uint8)
        yield np.stack([img, img, img], axis=-1)


def _gen_tech_scanlines() -> Iterator[np.ndarray]:
    """Subtle scan-line grid + sweep line — tech."""
    rng = np.random.default_rng(200)

    # Static scanline base (every 4th row slightly bright)
    base = np.zeros((H, W), dtype=np.uint8)
    base[::4, :] = 18

    for f in range(FPS * DUR):
        frame = base.copy()
        # Sweeping bright line
        sweep_y = int(f * 2.8) % H
        brightness = int(45 + 30 * math.sin(f * 0.22))
        y1 = sweep_y
        y2 = min(H, sweep_y + 2)
        frame[y1:y2, :] = np.maximum(frame[y1:y2, :], brightness)
        # Slight noise on scanlines
        noise = rng.integers(0, 12, (len(range(0, H, 4)), W), dtype=np.uint8)
        frame[::4, :] = np.clip(frame[::4, :].astype(np.int16) + noise, 0, 255).astype(np.uint8)
        yield np.stack([frame, frame, frame], axis=-1)


# ── Main ──────────────────────────────────────────────────────────────────────

OVERLAYS = [
    ("threat_blood.mp4",     _gen_blood,              26),
    ("cold_snow.mp4",        _gen_snow,               26),
    ("mystery_fog.mp4",      _gen_fog,                28),
    ("dream_bokeh.mp4",      _gen_bokeh,              26),
    ("warmth_rays.mp4",      _gen_warmth_rays,        26),
    ("revelation_flare.mp4", _gen_revelation_flare,   26),
    ("noir_rain.mp4",        _gen_noir_rain,          26),
    ("nature_dust.mp4",      _gen_nature_dust,        26),
    ("tech_scanlines.mp4",   _gen_tech_scanlines,     28),
]

# sepia_grain uses FFmpeg lavfi noise filter — no Python generation needed
SEPIA_GRAIN_VF = "noise=alls=55:allf=t,curves=all='0/0 1/0.55'"


def main() -> None:
    print(f"Generating {len(OVERLAYS) + 1} overlay MP4s → {OUT_DIR}\n")

    # sepia_grain via FFmpeg lavfi — high CRF because noise frames are inherently
    # incompressible; CRF 35 keeps it under 500 KB while still looking grainy.
    sepia_path = os.path.join(OUT_DIR, "sepia_grain.mp4")
    print("  generating sepia_grain.mp4 ...")
    _ffmpeg_lavfi(SEPIA_GRAIN_VF, sepia_path, crf=35)

    for filename, gen_fn, crf in OVERLAYS:
        out_path = os.path.join(OUT_DIR, filename)
        print(f"  generating {filename} ...")
        _pipe(gen_fn(), out_path, crf=crf)

    print(f"\nDone — {len(OVERLAYS) + 1} overlays ready.")


if __name__ == "__main__":
    main()
