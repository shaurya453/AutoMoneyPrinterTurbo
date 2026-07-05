import os
import subprocess
from typing import Optional

from loguru import logger

from app.utils import utils

from ._common import (
    _fast_preset_args,
    _get_configured_video_codec,
    _probe_duration,
)

_VALID_VISUAL_EFFECTS = frozenset({
    "threat", "cold", "warmth", "mystery", "sepia",
    "tech", "hacker_tech", "dream", "noir", "nature", "revelation",
})

_OVERLAY_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "resource", "overlays")
)

# Per-effect overlay config: file basename + FFmpeg blend mode + opacity.
# All overlays use a black background; screen blend treats black as transparent.
_EFFECT_OVERLAYS: dict[str, dict] = {
    "threat":      {"file": "threat_blood.mp4",     "mode": "screen",   "opacity": 1.0},
    "cold":        {"file": "cold_snow.mp4",         "mode": "screen",   "opacity": 1.0},
    "mystery":     {"file": "mystery_fog.mp4",       "mode": "screen",   "opacity": 1.0},
    "dream":       {"file": "dream_bokeh.mp4",       "mode": "screen",   "opacity": 1.0},
    "warmth":      {"file": "warmth_rays.mp4",       "mode": "screen",   "opacity": 1.0},
    "revelation":  {"file": "revelation_flare.mp4",  "mode": "screen",   "opacity": 1.0},
    "noir":        {"file": "noir_rain.mp4",         "mode": "screen",   "opacity": 1.0},
    "sepia":       {"file": "sepia_grain.mp4",       "mode": "multiply", "opacity": 1.0},
    "nature":      {"file": "nature_dust.mp4",       "mode": "screen",   "opacity": 1.0},
    "tech":        {"file": "tech_scanlines.mp4",    "mode": "screen",   "opacity": 1.0},
    "hacker_tech": {"file": "hacker_tech.mp4",       "mode": "screen",   "opacity": 1.0},
}

_OVERLAY_FADE_DUR = 0.5   # seconds — fade-in at start, fade-out at end

# Duration constants for lower_third compositing.
_LT_ANIM_IN  = 0.40   # seconds — fade-in
_LT_ANIM_OUT = 0.35   # seconds — fade-out

# Optional user-supplied full-frame RGBA blob PNG for the lower_third backdrop.
_LT_BLOB_PNG = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "resource", "graphics", "lower_third_shadow.png")
)


def apply_visual_effect(
    clip_path: str,
    effect: str,
    output_path: str,
    width: int = 1920,
    height: int = 1080,
    threads: int = 4,
) -> str:
    """Composite a motion overlay onto clip_path for its full natural duration.

    The overlay plays ONCE from start to finish (no looping). If the clip is
    shorter than the overlay, the overlay is trimmed to fit. If the clip is
    longer, the overlay plays for its natural duration, then the remainder of
    the clip plays clean via a neutral-color pad that makes the blend a
    mathematical identity:
      - screen blend: pad with black  (screen(clip, 0) = clip)
      - multiply blend: pad with white (multiply(clip, 1) = clip)

    A fade-in and fade-out of _OVERLAY_FADE_DUR seconds is applied.
    Returns output_path on success, clip_path unchanged on any failure.
    Audio is passed through unchanged.
    """
    if effect not in _VALID_VISUAL_EFFECTS:
        return clip_path

    overlay_cfg = _EFFECT_OVERLAYS.get(effect)
    if not overlay_cfg:
        return clip_path

    overlay_path = os.path.join(_OVERLAY_DIR, overlay_cfg["file"])
    if not os.path.exists(overlay_path):
        logger.warning(f"apply_visual_effect({effect}): overlay not found — {overlay_path}")
        return clip_path

    clip_dur = _probe_duration(clip_path)
    ov_dur   = _probe_duration(overlay_path)
    if not clip_dur or not ov_dur or clip_dur <= 0 or ov_dur <= 0:
        logger.warning(f"apply_visual_effect({effect}): duration probe failed")
        return clip_path

    blend_mode = overlay_cfg["mode"]
    opacity    = overlay_cfg["opacity"]

    # Play the full overlay once, trimmed to clip duration if the clip is shorter.
    accent = min(ov_dur, clip_dur)
    # Clamp fade so it never exceeds 25% of the overlay window.
    fade   = min(_OVERLAY_FADE_DUR, accent / 4)
    fade_out_start = max(0.0, accent - fade)

    # ── Build filter_complex ────────────────────────────────────────────────
    # Keep everything in gbrp (planar RGB) so the blend operates in RGB colour
    # space, matching what Filmora and other NLEs do. Blending in YUV applies
    # the screen/multiply formula to offset chroma channels and introduces a
    # colour cast (typically purple/teal).
    chains: list[str] = []

    # Scale, trim to accent duration, and fade in/out.
    chains.append(
        f"[0:v]scale={width}:{height},format=gbrp,"
        f"trim=0:{accent:.6f},setpts=PTS-STARTPTS,"
        f"fade=t=in:st=0:d={fade:.3f},"
        f"fade=t=out:st={fade_out_start:.6f}:d={fade:.3f}"
        f"[_ov_trimmed]"
    )

    # Pad the remainder of the clip with a neutral color so the blend is a
    # mathematical identity after the accent window ends.
    if clip_dur > accent:
        pad_dur   = clip_dur - accent
        # screen(clip, black)=clip; multiply(clip, white)=clip
        pad_color = "white" if blend_mode == "multiply" else "black"
        chains.append(
            f"color=c={pad_color}:s={width}x{height}:r=30:d={pad_dur:.6f},"
            f"format=gbrp[_ov_pad]"
        )
        chains.append(
            f"[_ov_trimmed][_ov_pad]concat=n=2:v=1:a=0,setpts=PTS-STARTPTS[_ov_final]"
        )
        ov_label = "_ov_final"
    else:
        ov_label = "_ov_trimmed"

    # Blend and convert back to yuv420p for the encoder.
    # Scale the clip to match the overlay — Pexels sometimes delivers non-standard
    # resolutions (e.g. 2048×1080) that would cause the blend to fail with -22.
    chains.append(f"[1:v]scale={width}:{height},format=gbrp[_clip]")
    chains.append(
        f"[_clip][{ov_label}]"
        f"blend=all_mode={blend_mode}:all_opacity={opacity},"
        f"format=yuv420p"
        f"[out]"
    )

    filter_complex = ";".join(chains)

    codec = _get_configured_video_codec()
    cmd = [
        utils.get_ffmpeg_binary(), "-y",
        "-i", overlay_path,
        "-i", clip_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "1:a?", "-c:a", "copy",
        "-t", f"{clip_dur:.6f}",
        "-c:v", codec, *_fast_preset_args(codec),
        "-pix_fmt", "yuv420p",
        "-threads", str(threads),
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-400:]
            # FFmpeg sometimes receives SIGTERM from the process manager after
            # encoding completes but before it exits cleanly.  If the output
            # file exists and its duration is within 10% of the target, treat
            # it as a success rather than discarding a valid encode.
            if os.path.exists(output_path):
                got_dur = _probe_duration(output_path)
                if got_dur and abs(got_dur - clip_dur) / clip_dur < 0.10:
                    logger.warning(
                        f"apply_visual_effect({effect}) non-zero exit but output is valid "
                        f"({got_dur:.2f}s ≈ {clip_dur:.2f}s) — using it"
                    )
                    return output_path
            logger.error(f"apply_visual_effect({effect}) failed: {stderr}")
            return clip_path
        return output_path
    except Exception as exc:
        logger.error(f"apply_visual_effect({effect}) exception: {exc}")
        return clip_path


def composite_lower_third(
    footage_path: str,
    graphic_path: str,
    output_path: str,
    width: int,
    height: int,
    threads: int = 4,
) -> Optional[str]:
    """
    Composite a lower_third Revideo clip (white text on black) over stock footage.

    Two-pass blend strategy — no chroma key needed:
      1. Blob PNG (RGBA): overlaid on footage with its native alpha channel.
         FFmpeg `overlay` respects the PNG's alpha, so the feathered blob appears
         correctly without any colour contamination.
      2. Text clip (white on black): composited via `screen` blend.
         screen(footage, black=0) = footage  → black bg disappears
         screen(footage, white=1) = white    → text stays white

    Both the blob and the text fade in/out via separate FFmpeg `fade` filters
    so they stay in sync.  Falls back to text-only (no blob) if the PNG is absent.
    """
    codec = _get_configured_video_codec()
    footage_dur = _probe_duration(footage_path)
    gfx_dur     = _probe_duration(graphic_path)
    if not footage_dur or not gfx_dur:
        logger.warning("composite_lower_third: could not probe clip durations")
        return None

    fi       = min(_LT_ANIM_IN,  gfx_dur / 4)
    fo       = min(_LT_ANIM_OUT, gfx_dur / 4)
    fo_start = max(0.0, gfx_dur - fo)

    has_blob = os.path.isfile(_LT_BLOB_PNG)

    if has_blob:
        # Input 0: footage | Input 1: Revideo text clip | Input 2: blob PNG (via -loop 1)
        # overlay filter requires rgba on both inputs to honour the PNG's alpha channel.
        # After the overlay we convert to gbrp (planar RGB, no alpha) which is the correct
        # format for screen blend — gbrp treats all channels numerically, whereas rgba would
        # bleed the alpha channel into the blend arithmetic.
        filter_complex = (
            # Footage: scale, convert to rgba so overlay can read its pixels correctly
            f"[0:v]scale={width}:{height},format=rgba[footage];"
            # Blob PNG: scale, keep rgba, fade alpha channel in/out
            f"[2:v]scale={width}:{height},format=rgba,"
            f"fade=t=in:st=0:d={fi:.3f}:alpha=1,"
            f"fade=t=out:st={fo_start:.3f}:d={fo:.3f}:alpha=1[blob];"
            # Overlay blob using its native alpha; drop alpha → gbrp for screen blend
            f"[footage][blob]overlay=0:0,format=gbrp[with_blob];"
            # Revideo text clip: gbrp (white text on black), fade for screen blend
            f"[1:v]scale={width}:{height},format=gbrp,"
            f"fade=t=in:st=0:d={fi:.3f},"
            f"fade=t=out:st={fo_start:.3f}:d={fo:.3f}[text];"
            # screen(footage_with_blob, black=0)=footage; screen(…, white=1)=white
            f"[with_blob][text]blend=all_mode=screen,format=yuv420p[out]"
        )
        cmd = [
            utils.get_ffmpeg_binary(), "-y",
            "-i", footage_path,
            "-i", graphic_path,
            "-loop", "1", "-t", f"{gfx_dur + 0.1:.3f}", "-i", _LT_BLOB_PNG,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "0:a?", "-c:a", "copy",
            "-t", f"{footage_dur:.6f}",
            "-c:v", codec, *_fast_preset_args(codec),
            "-pix_fmt", "yuv420p",
            "-threads", str(threads),
            output_path,
        ]
    else:
        # No blob PNG — just screen-blend the text over footage
        filter_complex = (
            f"[0:v]scale={width}:{height},format=gbrp[footage];"
            f"[1:v]scale={width}:{height},format=gbrp,"
            f"fade=t=in:st=0:d={fi:.3f},"
            f"fade=t=out:st={fo_start:.3f}:d={fo:.3f}[text];"
            f"[footage][text]blend=all_mode=screen,format=yuv420p[out]"
        )
        cmd = [
            utils.get_ffmpeg_binary(), "-y",
            "-i", footage_path,
            "-i", graphic_path,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "0:a?", "-c:a", "copy",
            "-t", f"{footage_dur:.6f}",
            "-c:v", codec, *_fast_preset_args(codec),
            "-pix_fmt", "yuv420p",
            "-threads", str(threads),
            output_path,
        ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            logger.warning(
                f"composite_lower_third failed (exit {result.returncode}): {stderr[-400:]}"
            )
            return None
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.warning("composite_lower_third: output file is empty")
            return None
        return output_path
    except Exception as exc:
        logger.warning(f"composite_lower_third exception: {exc}")
        return None
