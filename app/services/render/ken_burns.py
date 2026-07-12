import os
import random
import subprocess
import tempfile
import threading
from typing import List

import numpy as np
from loguru import logger
from moviepy import Clip, ColorClip, CompositeVideoClip, vfx
from PIL import Image

from app.utils import utils

from ._common import (
    _get_configured_video_codec,
    _write_videofile_with_codec_fallback,
    close_clip,
    fps,
)

_BG_BRIGHTNESS = 0.5
_BG_BLUR_FRACTION = 0.06  # downscale-then-upscale blur strength

_PAN_Z = 1.04                    # zoom factor: subtle 4% motion, minimal content crop at peak zoom

_KEN_BURNS_ANIMATIONS = ("pan_lr", "pan_rl", "zoom_in", "zoom_out", "pan_ud", "fade")
# No-consecutive-repeat tracker shared by all fetch worker threads. The lock
# makes the read-choose-write atomic; note "last" means last-COMPLETED fetch,
# not last timeline position — under parallel fetch the no-repeat guarantee is
# best-effort by completion order, and the pick is inherently
# thread-schedule-dependent (i.e. not reproducible run-to-run) even with the
# per-clip seeded rng.
_last_ken_burns_animation: str | None = None
_anim_pick_lock = threading.Lock()
_3D_ANIM_DUR = 1.8  # seconds — tilt-to-flat transition; remaining duration holds flat

# Soft animation bias per mood effect — entries within the allowed pool are
# duplicated by their weight, so the random pick naturally favours them without
# overriding geometry constraints or the no-consecutive-repeat rule.
_EFFECT_ANIM_WEIGHTS: dict[str, dict[str, int]] = {
    "threat":      {"zoom_in": 3, "screen_3d_ud": 2},
    "cold":        {"pan_ud": 2},
    "mystery":     {"pan_ud": 3, "screen_3d_lr": 1},
    "warmth":      {"zoom_in": 2},
    "dream":       {"zoom_in": 2},
    "revelation":  {"zoom_in": 3, "screen_3d_lr": 2},
    "sepia":       {"pan_lr": 3},
    "hacker_tech": {"zoom_in": 2},
    "noir":        {"zoom_in": 2},
}


# ---------------------------------------------------------------------------
# Inlined from app/services/utils/video_effects.py
# ---------------------------------------------------------------------------

# FadeIn
def fadein_transition(clip: Clip, t: float) -> Clip:
    return clip.with_effects([vfx.FadeIn(t)])


# FadeOut
def fadeout_transition(clip: Clip, t: float) -> Clip:
    return clip.with_effects([vfx.FadeOut(t)])


# SlideIn
def slidein_transition(clip: Clip, t: float, side: str) -> Clip:
    width, height = clip.size

    # MoviePy 内置 SlideIn 在当前这条处理链里对全屏素材不稳定，
    # 会出现"逻辑上应用了转场，但画面几乎看不出变化"的情况。
    # 这里改成显式黑底 + 位移动画，保证转场效果可见且行为可控。
    def position(current_time: float):
        progress = min(max(current_time / max(t, 0.001), 0), 1)

        if side == "left":
            return (-width + width * progress, 0)
        if side == "right":
            return (width - width * progress, 0)
        if side == "top":
            return (0, -height + height * progress)
        if side == "bottom":
            return (0, height - height * progress)
        return (0, 0)

    background = ColorClip(size=(width, height), color=(0, 0, 0)).with_duration(
        clip.duration
    )
    moving_clip = clip.with_position(position)
    return CompositeVideoClip([background, moving_clip], size=(width, height)).with_duration(
        clip.duration
    )


# SlideOut
def slideout_transition(clip: Clip, t: float, side: str) -> Clip:
    width, height = clip.size
    transition_start = max(clip.duration - t, 0)

    # SlideOut 同样改成显式位移，保证片段末尾能稳定滑出画面。
    def position(current_time: float):
        if current_time <= transition_start:
            return (0, 0)

        progress = min(
            max((current_time - transition_start) / max(t, 0.001), 0), 1
        )

        if side == "left":
            return (-width * progress, 0)
        if side == "right":
            return (width * progress, 0)
        if side == "top":
            return (0, -height * progress)
        if side == "bottom":
            return (0, height * progress)
        return (0, 0)

    background = ColorClip(size=(width, height), color=(0, 0, 0)).with_duration(
        clip.duration
    )
    moving_clip = clip.with_position(position)
    return CompositeVideoClip([background, moving_clip], size=(width, height)).with_duration(
        clip.duration
    )


# ---------------------------------------------------------------------------
# Ken Burns helpers
# ---------------------------------------------------------------------------

def _pick_animation(
    allowed: list[str] | None = None,
    effect: str = "neutral",
    rng: random.Random = random,
) -> str:
    global _last_ken_burns_animation
    pool_source = list(allowed) if allowed else list(_KEN_BURNS_ANIMATIONS)
    weights = _EFFECT_ANIM_WEIGHTS.get(effect, {})
    with _anim_pick_lock:
        weighted: list[str] = []
        for a in pool_source:
            if a != _last_ken_burns_animation:
                weighted.extend([a] * weights.get(a, 1))
        if not weighted:
            weighted = pool_source  # single-entry pool: allow repeat rather than crash
        choice = rng.choice(weighted)
        _last_ken_burns_animation = choice
    return choice


def _blur_and_darken(frame: np.ndarray, brightness: float = _BG_BRIGHTNESS, blur_fraction: float = _BG_BLUR_FRACTION) -> np.ndarray:
    """Cheap blur (downscale + upscale) and brightness reduction for a background layer."""
    h, w = frame.shape[:2]
    small_w, small_h = max(1, int(w * blur_fraction)), max(1, int(h * blur_fraction))
    small = Image.fromarray(frame).resize((small_w, small_h), Image.BILINEAR)
    blurred = small.resize((w, h), Image.BICUBIC)
    arr = np.asarray(blurred, dtype=np.float32) * brightness
    return np.clip(arr, 0, 255).astype(np.uint8)


def _flatten_alpha_on_white(rgba: Image.Image) -> Image.Image:
    """Flatten an RGBA image onto a plain white background; return RGB.

    Transparent pixels in web-sourced PNGs (diagrams, logos) commonly store
    arbitrary — often black — RGB underneath the zero alpha. A naive
    `.convert("RGB")` bakes in that hidden color instead of treating it as
    background, which silently renders the whole cover-crop frame black.
    """
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.getchannel("A"))
    return bg


def _add_drop_shadow(
    rgba: Image.Image,
    shadow_blur: int = 14,
    shadow_alpha_frac: float = 0.38,
    shadow_offset: tuple = (0, 8),
) -> Image.Image:
    """Composite an RGBA image over a plain white background with a soft drop shadow; return RGB."""
    from PIL import ImageFilter as _IF
    alpha_ch = rgba.getchannel("A")
    shadow_layer = Image.new("RGBA", rgba.size, (0, 0, 0, int(255 * shadow_alpha_frac)))
    shadow_layer.putalpha(alpha_ch)
    shadow_layer = shadow_layer.filter(_IF.GaussianBlur(radius=shadow_blur))
    result = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    result.paste(shadow_layer, shadow_offset, shadow_layer)
    result.paste(rgba, (0, 0), rgba)
    return result.convert("RGB")


def _cover_crop_image(img: Image.Image, width: int, height: int) -> Image.Image:
    """Scale + center-crop a PIL image to exactly (width, height), covering the full frame."""
    scale = max(width / img.width, height / img.height)
    new_w = max(int(round(img.width * scale)), width)
    new_h = max(int(round(img.height * scale)), height)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    x0, y0 = (new_w - width) // 2, (new_h - height) // 2
    return resized.crop((x0, y0, x0 + width, y0 + height))


def _perspective_coeffs(dst_pts, src_pts) -> list:
    """Compute 8 PIL PERSPECTIVE inverse-map coefficients from 4 (dst→src) point pairs."""
    A, b = [], []
    for (dx, dy), (sx, sy) in zip(dst_pts, src_pts):
        A.append([dx, dy, 1,  0,  0, 0, -sx * dx, -sx * dy])
        b.append(sx)
        A.append([ 0,  0, 0, dx, dy, 1, -sy * dx, -sy * dy])
        b.append(sy)
    coeffs = np.linalg.solve(np.array(A, dtype=np.float64), np.array(b, dtype=np.float64))
    return coeffs.tolist()


def _render_3d_effect(
    image_path: str,
    duration: float,
    width: int,
    height: int,
    output_path: str,
    preset: str,
) -> bool:
    """
    Render a 3D screen-mockup animation: tilted perspective → flat full-frame.
    Re-implemented using MoviePy and PIL to bypass FFmpeg's perspective
    filter lacking `sendcmd` / timeline command support.
    """
    from moviepy.video.VideoClip import VideoClip as _VideoClip
    from PIL import Image as _PILImage, ImageFilter as _ImageFilter
    import numpy as np

    W, H = width, height
    D = duration * 0.50

    # Destination positions of each source corner at max tilt
    if preset == "screen_3d_lr":
        tilt_pts = [
            (W * 0.25, H * 0.18),  # TL
            (W * 0.94, H * 0.03),  # TR
            (W * 0.94, H * 0.97),  # BR
            (W * 0.25, H * 0.82),  # BL
        ]
        sdx, sdy = 48, 20
    else:  # screen_3d_ud
        tilt_pts = [
            (W * 0.16, H * 0.24),  # TL
            (W * 0.84, H * 0.24),  # TR
            (W * 0.96, H * 0.92),  # BR
            (W * 0.04, H * 0.92),  # BL
        ]
        sdx, sdy = 20, 48

    flat_pts = [(0, 0), (W, 0), (W, H), (0, H)]
    src_pts = [(0, 0), (W, 0), (W, H), (0, H)]

    # Fixed offset for the shadow
    sox, soy = sdx // 3, sdy // 3

    try:
        # 1. Load and cover-crop the base image to exactly WxH
        with _PILImage.open(image_path) as f:
            _has_alpha = f.mode in ("RGBA", "LA") or (f.mode == "P" and "transparency" in f.info)
            img_raw = _flatten_alpha_on_white(f.convert("RGBA")) if _has_alpha else f.convert("RGB")

        img = _cover_crop_image(img_raw, W, H)

        # Pre-compute blurred background once (heavy blur + darken, static across all frames)
        bg_arr = np.array(img, dtype=np.float32) * 0.45
        bg = _PILImage.fromarray(np.clip(bg_arr, 0, 255).astype(np.uint8))
        bg = bg.filter(_ImageFilter.GaussianBlur(radius=48))

        img = img.convert("RGBA")  # Requires Alpha for transparent bounds

        def make_frame(t):
            # ease-out cubic, 1→0
            ease = (1.0 - min(t, D) / D) ** 3

            # Zoom: progress 0→1 over the full clip duration.
            # screen_3d_lr zooms in (0.95→1.0); screen_3d_ud zooms out (1.05→1.0).
            zoom_t = t / duration
            if preset == "screen_3d_lr":
                scale = 0.95 + 0.05 * zoom_t
            else:
                scale = 1.05 - 0.05 * zoom_t

            # Interpolate the 4 corners towards the flat full-frame bounds
            current_dst = []
            for i in range(4):
                tx, ty = tilt_pts[i]
                fx, fy = flat_pts[i]
                current_dst.append((fx + (tx - fx) * ease, fy + (ty - fy) * ease))

            # Compute PIL PERSPECTIVE inverse-map coefficients (dst->src)
            coeffs = _perspective_coeffs(current_dst, src_pts)

            # Warp the image. fillcolor=(0,0,0,0) makes the outside transparent.
            warped = img.transform(
                (W, H),
                _PILImage.PERSPECTIVE,
                coeffs,
                _PILImage.BICUBIC,
                fillcolor=(0, 0, 0, 0)
            )

            # Apply zoom: scale the warped card and re-center on a WxH canvas.
            # For zoom-in (scale<1) the card is letterboxed; for zoom-out (scale>1)
            # the paste offset goes negative and PIL naturally center-crops it.
            new_w = max(1, round(W * scale))
            new_h = max(1, round(H * scale))
            scaled = warped.resize((new_w, new_h), _PILImage.BILINEAR)
            warped = _PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
            warped.paste(scaled, ((W - new_w) // 2, (H - new_h) // 2), mask=scaled.split()[3])

            # Blurred background base (same image, heavily blurred and darkened)
            frame = bg.copy()
            alpha = warped.split()[3]

            # Shadow creation (matches FFmpeg: gblur=sigma=20 + colorchannelmixer)
            shadow_mask = alpha.filter(_ImageFilter.GaussianBlur(radius=20))
            shadow_layer = _PILImage.new("RGB", (W, H), (38, 38, 38))  # 15% brightness

            # Composite shadow, then composite the warped image on top
            frame.paste(shadow_layer, (sox, soy), mask=shadow_mask)
            frame.paste(warped, (0, 0), mask=alpha)

            return np.array(frame)

        # 2. Render via MoviePy using the global fps variable
        clip = _VideoClip(make_frame, duration=duration).with_fps(fps)

        # 3. Write output leveraging existing codec fallback & hardware acceleration
        _write_videofile_with_codec_fallback(
            clip,
            output_file=output_path,
            codec="libx264",
            preset="fast",
            threads=os.cpu_count() or 4,
            logger=None,
        )
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0

    except Exception as exc:
        logger.warning(f"_render_3d_effect ({preset}) failed: {exc}")
        return False


def _resize_clip_to_aspect(clip, video_width: int, video_height: int):
    """Resize `clip` to exactly (video_width, video_height) via cover-crop.

    Scales to fill (no bars), then center-crops to the target resolution.
    """
    clip_w, clip_h = clip.size
    if clip_w == video_width and clip_h == video_height:
        return clip

    clip_ratio = clip_w / clip_h
    video_ratio = video_width / video_height
    logger.debug(
        f"resizing clip, source: {clip_w}x{clip_h}, ratio: {clip_ratio:.2f}, "
        f"target: {video_width}x{video_height}, ratio: {video_ratio:.2f}"
    )

    if clip_ratio == video_ratio:
        return clip.resized(new_size=(video_width, video_height))

    scale = max(video_width / clip_w, video_height / clip_h)
    new_w = int(round(clip_w * scale))
    new_h = int(round(clip_h * scale))
    return clip.resized(new_size=(new_w, new_h)).with_effects([
        vfx.Crop(x_center=new_w // 2, y_center=new_h // 2, width=video_width, height=video_height)
    ])


def apply_ken_burns(
    image_path: str,
    duration: float,
    width: int,
    height: int,
    frame_scale: float = 1.0,
    animation: str = "zoom_in",
):
    """MoviePy Ken Burns fallback with pan/zoom animations and ease-out timing.

    frame_scale >= 1.0 → cover mode (landscape): image fills the full frame via
    cover-crop; pan/zoom travels across the natural overflow area with no
    blurred background visible.

    frame_scale < 1.0 → FIT mode (portrait): image is letterboxed to
    frame_scale of the frame on a blurred+darkened background.
    """
    from moviepy.video.VideoClip import VideoClip as _VideoClip
    from PIL import Image as _PILImage

    with _PILImage.open(image_path) as f:
        _has_alpha = f.mode in ("RGBA", "LA") or (f.mode == "P" and "transparency" in f.info)
        _rgba_src = f.convert("RGBA") if _has_alpha else None
        if _rgba_src:
            _rgba_src.load()
        img = _flatten_alpha_on_white(_rgba_src) if _has_alpha else f.convert("RGB")
        img.load()
        src_w, src_h = img.size

    # ── Cover mode (landscape, full-frame fill) ───────────────────────────────
    if frame_scale >= 1.0:
        cover_scale = max(width / src_w, height / src_h)
        cv_w = max(int(round(src_w * cover_scale)), width)
        cv_h = max(int(round(src_h * cover_scale)), height)
        cover_arr = np.array(img.resize((cv_w, cv_h), _PILImage.LANCZOS))
        h_excess = max(0, cv_w - width)
        v_excess = max(0, cv_h - height)

        def make_frame_cover(t: float) -> np.ndarray:
            p = t / max(duration, 1e-6)
            pe = 1.0 - (1.0 - p) ** 2  # ease-out

            if animation == "zoom_in":
                z = 1.0 + (_PAN_Z - 1.0) * pe
                cw = max(1, int(width / z))
                ch = max(1, int(height / z))
                x0 = (cv_w - cw) // 2
                y0 = (cv_h - ch) // 2
                crop = _PILImage.fromarray(cover_arr[y0:y0 + ch, x0:x0 + cw])
                return np.array(crop.resize((width, height), _PILImage.LANCZOS))
            elif animation == "pan_lr":
                x0 = min(int(h_excess * pe), max(0, cv_w - width))
                y0 = v_excess // 2
            elif animation == "pan_rl":
                x0 = min(int(h_excess * (1.0 - p) ** 2), max(0, cv_w - width))
                y0 = v_excess // 2
            else:  # pan_ud
                x0 = h_excess // 2
                y0 = min(int(v_excess * pe), max(0, cv_h - height))

            return cover_arr[y0:y0 + height, x0:x0 + width].copy()

        clip = _VideoClip(make_frame_cover, duration=duration)
        return clip.with_fps(fps)

    # ── FIT mode (portrait, blurred background or white for transparent PNG) ──
    fg_budget_w = width * frame_scale
    fg_budget_h = height * frame_scale
    # Always fit inside the foreground budget — no cover-crop, no content clipping.
    fit_scale = min(fg_budget_w / src_w, fg_budget_h / src_h)
    fit_w = int(fit_scale * src_w)
    fit_h = int(fit_scale * src_h)

    if _has_alpha:
        bg_arr = np.full((height, width, 3), 255, dtype=np.uint8)
        fit_arr = np.array(_add_drop_shadow(_rgba_src.resize((fit_w, fit_h), _PILImage.LANCZOS)))
    else:
        bg_arr = _blur_and_darken(np.array(_cover_crop_image(img, width, height)))
        fit_arr = np.array(img.resize((fit_w, fit_h), _PILImage.LANCZOS))

    x_off = (width - fit_w) // 2
    y_off = (height - fit_h) // 2

    def make_frame(t: float) -> np.ndarray:
        if animation == "static":
            frame = bg_arr.copy()
            frame[y_off:y_off + fit_h, x_off:x_off + fit_w] = fit_arr
            return frame

        p = t / max(duration, 1e-6)
        pe = 1.0 - (1.0 - p) ** 2  # ease-out: fast start, decelerates

        if animation == "fade":
            fade_d = min(0.4, duration * 0.15)
            if t < fade_d:
                alpha = t / fade_d
            elif t > duration - fade_d:
                alpha = (duration - t) / max(fade_d, 1e-6)
            else:
                alpha = 1.0
            alpha = max(0.0, min(1.0, alpha))
            z = 1.0 + (_PAN_Z - 1.0) * pe
            crop_w = max(1, int(fit_w / z))
            crop_h = max(1, int(fit_h / z))
            x0 = (fit_w - crop_w) // 2
            y0 = (fit_h - crop_h) // 2
            crop = _PILImage.fromarray(fit_arr[y0:y0 + crop_h, x0:x0 + crop_w])
            zoomed = np.array(crop.resize((fit_w, fit_h), _PILImage.LANCZOS))
            bg_region = bg_arr[y_off:y_off + fit_h, x_off:x_off + fit_w]
            blended = (zoomed.astype(np.float32) * alpha + bg_region.astype(np.float32) * (1.0 - alpha)).astype(np.uint8)
            frame = bg_arr.copy()
            frame[y_off:y_off + fit_h, x_off:x_off + fit_w] = blended
            return frame

        if animation == "zoom_in":
            z = 1.0 + (_PAN_Z - 1.0) * pe
            crop_w = max(1, int(fit_w / z))
            crop_h = max(1, int(fit_h / z))
            x0 = (fit_w - crop_w) // 2
            y0 = (fit_h - crop_h) // 2
        elif animation == "zoom_out":
            z = 1.0 + (_PAN_Z - 1.0) * (1.0 - pe)
            crop_w = max(1, int(fit_w / z))
            crop_h = max(1, int(fit_h / z))
            x0 = (fit_w - crop_w) // 2
            y0 = (fit_h - crop_h) // 2
        elif animation == "pan_lr":
            crop_w = max(1, int(fit_w / _PAN_Z))
            crop_h = max(1, int(fit_h / _PAN_Z))
            x0 = int((fit_w - crop_w) * pe)
            y0 = (fit_h - crop_h) // 2
        elif animation == "pan_rl":
            crop_w = max(1, int(fit_w / _PAN_Z))
            crop_h = max(1, int(fit_h / _PAN_Z))
            x0 = int((fit_w - crop_w) * (1.0 - p) ** 2)
            y0 = (fit_h - crop_h) // 2
        else:  # pan_ud
            crop_w = max(1, int(fit_w / _PAN_Z))
            crop_h = max(1, int(fit_h / _PAN_Z))
            x0 = (fit_w - crop_w) // 2
            y0 = int((fit_h - crop_h) * pe)

        crop = _PILImage.fromarray(fit_arr[y0:y0 + crop_h, x0:x0 + crop_w])
        fg = np.array(crop.resize((fit_w, fit_h), _PILImage.LANCZOS))

        frame = bg_arr.copy()
        frame[y_off:y_off + fit_h, x_off:x_off + fit_w] = fg
        return frame

    clip = _VideoClip(make_frame, duration=duration)
    return clip.with_fps(fps)


def _render_ken_burns_ffmpeg(
    image_path: str,
    duration: float,
    width: int,
    height: int,
    output_path: str,
    threads: int = 2,
    frame_scale: float = 1.0,
    animation: str = "zoom_in",
) -> str:
    """
    FFmpeg Ken Burns renderer.

    frame_scale < 1.0 (portrait): FIT-scales the image to frame_scale of the
    frame, centers it on a blurred+darkened background — no content is cropped.

    frame_scale >= 1.0 (landscape): COVER-CROPs to fill the full frame with no
    background visible. Pan/zoom travel uses the image's natural cover-crop
    overflow (pixels that extend past the frame edge) for content-aware motion.
    Pan animations at 2× PIL scale give sub-pixel smooth motion via 2:1 lanczos
    downscale. zoom_in uses 8× zoompan for the same reason.
    """
    from PIL import Image as _PILImage

    ffmpeg_bin = utils.get_ffmpeg_binary()
    codec = _get_configured_video_codec()

    with _PILImage.open(image_path) as f:
        _has_alpha = f.mode in ("RGBA", "LA") or (f.mode == "P" and "transparency" in f.info)
        _rgba_src = f.convert("RGBA") if _has_alpha else None
        if _rgba_src:
            _rgba_src.load()
        img = _flatten_alpha_on_white(_rgba_src) if _has_alpha else f.convert("RGB")
        img.load()
        src_w, src_h = img.size

    dur = max(duration, 0.001)
    eot = f"(1-pow(1-t/{dur:.6f},2))"  # ease-out quadratic: 0→1, fast start

    tmp_pre = tmp_bg = None
    try:
        # ── Cover mode: landscape fill, no background ─────────────────────────
        if frame_scale >= 1.0:
            cover_scale = max(width / src_w, height / src_h)

            if animation == "zoom_in":
                # Cover-crop to exact frame size, then 8× zoompan adds _PAN_Z extra zoom.
                canvas_img = _cover_crop_image(img, width, height)
                _uz = 8
                up_w, up_h = width * _uz, height * _uz
                total_frames = max(int(round(duration * fps)), 1)
                d_minus_1 = max(total_frames - 1, 1)
                z_expr = f"1.0+{_PAN_Z - 1.0:.4f}*(1-pow(1-on/{d_minus_1},2))"
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                    tmp_pre = fh.name
                canvas_img.save(tmp_pre)
                vf = (
                    f"scale={up_w}:{up_h}:flags=lanczos,"
                    f"zoompan=z='{z_expr}':x='iw/2-iw/(2*zoom)':y='ih/2-ih/(2*zoom)':"
                    f"d={total_frames}:s={width}x{height}:fps={fps}"
                )

            elif animation in ("pan_lr", "pan_rl", "pan_ud"):
                # Resize to cover dimensions at 2× scale; FFmpeg crop(t) + 2:1 lanczos
                # scale gives sub-pixel smooth travel at output resolution.
                cw2 = max(int(src_w * cover_scale * 2 / 2) * 2, width * 2)
                ch2 = max(int(src_h * cover_scale * 2 / 2) * 2, height * 2)
                h_excess2 = cw2 - width * 2
                v_excess2 = ch2 - height * 2
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                    tmp_pre = fh.name
                img.resize((cw2, ch2), _PILImage.LANCZOS).save(tmp_pre)

                if animation == "pan_lr":
                    vf = (
                        f"crop=w={width * 2}:h={height * 2}"
                        f":x='{h_excess2}*{eot}':y={v_excess2 // 2},"
                        f"scale={width}:{height}:flags=lanczos"
                    )
                elif animation == "pan_rl":
                    vf = (
                        f"crop=w={width * 2}:h={height * 2}"
                        f":x='{h_excess2}*(1-{eot})':y={v_excess2 // 2},"
                        f"scale={width}:{height}:flags=lanczos"
                    )
                else:  # pan_ud
                    vf = (
                        f"crop=w={width * 2}:h={height * 2}"
                        f":x={h_excess2 // 2}:y='{v_excess2}*{eot}',"
                        f"scale={width}:{height}:flags=lanczos"
                    )

            elif animation == "fade":
                canvas_img = _cover_crop_image(img, width, height)
                _uz = 8
                up_w, up_h = width * _uz, height * _uz
                total_frames = max(int(round(duration * fps)), 1)
                d_minus_1 = max(total_frames - 1, 1)
                z_expr = f"1.0+{_PAN_Z - 1.0:.4f}*(1-pow(1-on/{d_minus_1},2))"
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                    tmp_pre = fh.name
                canvas_img.save(tmp_pre)
                fade_d = min(0.5, duration * 0.15)
                fade_out_st = max(0.0, duration - fade_d)
                vf = (
                    f"scale={up_w}:{up_h}:flags=lanczos,"
                    f"zoompan=z='{z_expr}':x='iw/2-iw/(2*zoom)':y='ih/2-ih/(2*zoom)':"
                    f"d={total_frames}:s={width}x{height}:fps={fps},"
                    f"fade=t=in:st=0:d={fade_d:.3f},"
                    f"fade=t=out:st={fade_out_st:.3f}:d={fade_d:.3f}"
                )

            else:  # static (cover) — safety fallback; landscape path won't request this
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                    tmp_pre = fh.name
                _cover_crop_image(img, width, height).save(tmp_pre)
                vf = f"scale={width}:{height}:flags=lanczos"

            # zoompan generates all output frames from ONE input frame
            # (d=total_frames), so feed the still once — with a looped input
            # the giant 8× upscale re-runs for every output frame for nothing.
            # The pan paths animate via crop x/y expressions evaluated per
            # input frame, so they still need the looped input.
            if animation in ("zoom_in", "fade"):
                input_flags = ["-f", "image2", "-i", tmp_pre]
            else:
                input_flags = ["-f", "image2", "-loop", "1", "-t", str(duration), "-i", tmp_pre]
            cmd = [
                ffmpeg_bin, "-y",
                "-sws_flags", "lanczos",
                *input_flags,
                "-vf", vf,
                "-t", str(duration), "-r", str(fps),
                "-c:v", codec, "-preset", "fast", "-an",
                "-threads", str(threads), output_path,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                logger.warning(f"FFmpeg Ken Burns cover ({animation}) stderr: {proc.stderr[-600:]}")
                return ""
            return output_path if os.path.exists(output_path) else ""

        # ── FIT mode: portrait, blurred background (white for transparent PNG) ─
        # Fit image into the foreground budget (frame_scale of screen).
        fg_budget_w = width * frame_scale
        fg_budget_h = height * frame_scale
        # Always fit inside the foreground budget — no cover-crop, no content clipping.
        # Portrait images get blurred bars on the sides or top/bottom.
        fit_scale = min(fg_budget_w / src_w, fg_budget_h / src_h)
        # Round to even for libx264 compatibility.
        fit_w = int(fit_scale * src_w / 2) * 2
        fit_h = int(fit_scale * src_h / 2) * 2

        # Centered overlay offset; static because foreground size never changes.
        fg_x = (width - fit_w) // 2
        fg_y = (height - fit_h) // 2

        if _has_alpha:
            _bg_pil = _PILImage.new("RGB", (width, height), (255, 255, 255))
            _fg_at_fit = _add_drop_shadow(_rgba_src.resize((fit_w, fit_h), _PILImage.LANCZOS))
        else:
            _bg_pil = _PILImage.fromarray(
                _blur_and_darken(np.array(_cover_crop_image(img, width, height)))
            )
            _fg_at_fit = img.resize((fit_w, fit_h), _PILImage.LANCZOS)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            tmp_bg = fh.name
        _bg_pil.save(tmp_bg)

        # Static path — portrait images displayed with no animation.
        if animation == "static":
            pre_resized = _fg_at_fit
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                tmp_pre = fh.name
            pre_resized.save(tmp_pre)
            filter_complex = f"[1:v][0:v]overlay=x={fg_x}:y={fg_y}"
            cmd = [
                ffmpeg_bin, "-y",
                "-sws_flags", "lanczos",
                "-f", "image2", "-loop", "1", "-t", str(duration), "-i", tmp_pre,
                "-f", "image2", "-loop", "1", "-t", str(duration), "-i", tmp_bg,
                "-filter_complex", filter_complex,
                "-t", str(duration), "-r", str(fps),
                "-c:v", codec, "-preset", "fast", "-an",
                "-threads", str(threads), output_path,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                logger.warning(f"FFmpeg Ken Burns (static) stderr: {proc.stderr[-600:]}")
                return ""
            return output_path if os.path.exists(output_path) else ""

        # Fade path — photo on its blurred background, composed once, then the
        # same sub-pixel zoompan recipe as the cover-mode fade (8× upscale,
        # 4% ease-out zoom, fade in/out). The old implementation grew the
        # overlay with a per-frame integer `scale` (trunc to even), which
        # stepped width and height by 2px on independent schedules — a blocky
        # axis-alternating stretch. The blurred backdrop now zooms the same 4%
        # as the photo; on a blurred, darkened layer that is imperceptible.
        if animation == "fade":
            composed = _bg_pil.copy()
            composed.paste(_fg_at_fit, (fg_x, fg_y))
            _uz = 8
            up_w, up_h = width * _uz, height * _uz
            total_frames = max(int(round(duration * fps)), 1)
            d_minus_1 = max(total_frames - 1, 1)
            z_expr = f"1.0+{_PAN_Z - 1.0:.4f}*(1-pow(1-on/{d_minus_1},2))"
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                tmp_pre = fh.name
            composed.save(tmp_pre)
            fade_d = min(0.4, duration * 0.15)
            fade_out_st = max(0.0, duration - fade_d)
            vf = (
                f"scale={up_w}:{up_h}:flags=lanczos,"
                f"zoompan=z='{z_expr}':x='iw/2-iw/(2*zoom)':y='ih/2-ih/(2*zoom)':"
                f"d={total_frames}:s={width}x{height}:fps={fps},"
                f"fade=t=in:st=0:d={fade_d:.3f},"
                f"fade=t=out:st={fade_out_st:.3f}:d={fade_d:.3f}"
            )
            cmd = [
                ffmpeg_bin, "-y",
                "-sws_flags", "lanczos",
                # zoompan generates all frames from ONE input frame (d=total_frames).
                "-f", "image2", "-i", tmp_pre,
                "-vf", vf,
                "-t", str(duration), "-r", str(fps),
                "-c:v", codec, "-preset", "fast", "-an",
                "-threads", str(threads), output_path,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                logger.warning(f"FFmpeg Ken Burns (fade) stderr: {proc.stderr[-600:]}")
                return ""
            return output_path if os.path.exists(output_path) else ""

        # Pan/zoom FIT animations — 2× canvas + crop(t) + overlay on blurred background.
        if animation == "pan_lr":
            cw = int(fit_w * 2 * _PAN_Z / 2) * 2
            ch = fit_h * 2
            travel = cw - fit_w * 2
            canvas_img = _cover_crop_image(img, cw, ch)
            filter_fg = (
                f"[0:v]crop=w={fit_w * 2}:h={ch}:x='{travel}*{eot}':y=0,"
                f"scale={fit_w}:{fit_h}:flags=lanczos[fg]"
            )
        elif animation == "pan_rl":
            cw = int(fit_w * 2 * _PAN_Z / 2) * 2
            ch = fit_h * 2
            travel = cw - fit_w * 2
            canvas_img = _cover_crop_image(img, cw, ch)
            filter_fg = (
                f"[0:v]crop=w={fit_w * 2}:h={ch}:x='{travel}*(1-{eot})':y=0,"
                f"scale={fit_w}:{fit_h}:flags=lanczos[fg]"
            )
        elif animation == "pan_ud":
            cw = fit_w * 2
            ch = int(fit_h * 2 * _PAN_Z / 2) * 2
            travel = ch - fit_h * 2
            canvas_img = _cover_crop_image(img, cw, ch)
            filter_fg = (
                f"[0:v]crop=w={cw}:h={fit_h * 2}:x=0:y='{travel}*{eot}',"
                f"scale={fit_w}:{fit_h}:flags=lanczos[fg]"
            )
        elif animation == "zoom_out":
            _uz = 8
            up_w = fit_w * _uz
            up_h = fit_h * _uz
            total_frames = max(int(round(duration * fps)), 1)
            d_minus_1 = max(total_frames - 1, 1)
            z_expr = f"1.0+{_PAN_Z - 1.0:.4f}*pow(1-on/{d_minus_1},2)"
            canvas_img = _fg_at_fit
            filter_fg = (
                f"[0:v]scale={up_w}:{up_h}:flags=lanczos,"
                f"zoompan=z='{z_expr}':x='iw/2-iw/(2*zoom)':y='ih/2-ih/(2*zoom)':"
                f"d={total_frames}:s={fit_w}x{fit_h}:fps={fps}[fg]"
            )

        else:  # zoom_in — 8× zoompan for sub-pixel accuracy.
            _uz = 8
            up_w = fit_w * _uz
            up_h = fit_h * _uz
            total_frames = max(int(round(duration * fps)), 1)
            d_minus_1 = max(total_frames - 1, 1)
            z_expr = f"1.0+{_PAN_Z - 1.0:.4f}*(1-pow(1-on/{d_minus_1},2))"
            canvas_img = _fg_at_fit
            filter_fg = (
                f"[0:v]scale={up_w}:{up_h}:flags=lanczos,"
                f"zoompan=z='{z_expr}':x='iw/2-iw/(2*zoom)':y='ih/2-ih/(2*zoom)':"
                f"d={total_frames}:s={fit_w}x{fit_h}:fps={fps}[fg]"
            )

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            tmp_pre = fh.name
        canvas_img.save(tmp_pre)

        # Same single-frame-input optimisation as the cover branch: the
        # zoompan foregrounds (zoom_in / zoom_out) generate every output frame
        # from one input frame, so don't loop the still through the 8× upscale.
        if animation in ("zoom_in", "zoom_out"):
            fg_input_flags = ["-f", "image2", "-i", tmp_pre]
        else:
            fg_input_flags = ["-f", "image2", "-loop", "1", "-t", str(duration), "-i", tmp_pre]
        filter_complex = f"{filter_fg};[1:v][fg]overlay=x={fg_x}:y={fg_y}"
        cmd = [
            ffmpeg_bin, "-y",
            "-sws_flags", "lanczos",
            *fg_input_flags,
            "-f", "image2", "-loop", "1", "-t", str(duration), "-i", tmp_bg,
            "-filter_complex", filter_complex,
            "-t", str(duration), "-r", str(fps),
            "-c:v", codec, "-preset", "fast", "-an",
            "-threads", str(threads), output_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            logger.warning(f"FFmpeg Ken Burns stderr: {proc.stderr[-600:]}")
            return ""

    finally:
        for p in (tmp_pre, tmp_bg):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass

    return output_path if os.path.exists(output_path) else ""


def render_ken_burns_clip(
    image_path: str,
    duration: float,
    width: int,
    height: int,
    output_path: str,
    threads: int = 2,
    effect: str = "",
    rng: random.Random = random,
) -> str:
    """
    Render a Ken Burns clip from image_path to an MP4 at output_path.
    Returns output_path on success, '' on failure.

    Portrait images (h ≥ w): FIT-scaled to 95% of frame, centered on a
    blurred background; animation picked from fade/zoom_in/zoom_out.

    Landscape images (w > h): cover-crop fills the full frame; animation is
    chosen randomly from a pool derived from the image's aspect-ratio overflow:
      - zoom_in: always available (adds 4% extra zoom on the cover-cropped base)
      - pan_lr / pan_rl: when the image is wider than the frame (h_excess > 2%)
      - pan_ud: when the image is taller than the frame after cover-scale (v_excess > 2%)
    Consecutive landscape clips get different animation types (no-repeat tracking).
    """
    from PIL import Image as _PILImg
    with _PILImg.open(image_path) as _im:
        is_landscape = _im.width > _im.height
        img_w, img_h = _im.width, _im.height

    if not is_landscape:
        # Portrait: FIT at 95%, blurred background, fade/zoom.
        frame_scale = 0.95
        animation = _pick_animation(["fade", "zoom_in", "zoom_out"], rng=rng)
    else:
        # Landscape: cover-crop + random animation from overflow-derived pool.
        cover_scale = max(width / img_w, height / img_h)
        h_excess = max(0.0, img_w * cover_scale - width)
        v_excess = max(0.0, img_h * cover_scale - height)

        allowed = ["zoom_in", "screen_3d_lr", "screen_3d_ud", "fade"]
        if h_excess > width * 0.02:
            allowed.extend(["pan_lr", "pan_rl"])
        if v_excess > height * 0.02:
            allowed.append("pan_ud")

        animation = _pick_animation(allowed, effect, rng=rng)
        frame_scale = 1.0
        logger.info(
            f"Ken Burns: landscape cover-mode, animation={animation}, "
            f"h_excess={h_excess:.0f}px, v_excess={v_excess:.0f}px, "
            f"pool={allowed}"
        )

    # 3D screen-mockup presets use PIL per-frame rendering, not FFmpeg filters.
    if animation in ("screen_3d_lr", "screen_3d_ud"):
        ok = _render_3d_effect(image_path, duration, width, height, output_path, animation)
        if ok:
            return output_path
        logger.warning(f"3D effect failed for {image_path}, falling back to pan/zoom")
        allowed_fallback = [a for a in allowed if "screen_3d" not in a]
        animation = _pick_animation(allowed_fallback or ["zoom_in"], effect, rng=rng)

    # Prefer FFmpeg (sub-pixel smooth motion) over MoviePy (integer rounding jitter).
    try:
        result = _render_ken_burns_ffmpeg(
            image_path, duration, width, height, output_path, threads,
            frame_scale=frame_scale,
            animation=animation,
        )
        if result:
            return result
        logger.warning(f"FFmpeg Ken Burns returned empty for {image_path}, falling back to MoviePy")
    except Exception as exc:
        logger.warning(f"FFmpeg Ken Burns exception for {image_path}: {exc} — falling back to MoviePy")

    clip = apply_ken_burns(image_path, duration, width, height, frame_scale=frame_scale, animation=animation)
    try:
        _write_videofile_with_codec_fallback(
            clip,
            output_file=output_path,
            codec=_get_configured_video_codec(),
            audio=False,
            fps=fps,
            threads=threads,
            logger=None,
        )
    except Exception as exc:
        logger.warning(f"Ken Burns render failed for {image_path}: {str(exc)}")
    finally:
        close_clip(clip)
    return output_path if os.path.exists(output_path) else ""
