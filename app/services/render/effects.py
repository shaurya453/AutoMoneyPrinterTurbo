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
    "urgency", "euphoria", "corporate", "glitch_soft", "confusion",
    "network", "royalty", "static_dread", "toxic",
})

_OVERLAY_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "resource", "overlays")
)

# Per-effect overlay config: file basename + FFmpeg blend mode.
# All overlays use a black background; screen blend treats black as transparent.
_EFFECT_OVERLAYS: dict[str, dict] = {
    "threat":      {"file": "threat_blood.mp4",     "mode": "screen"},
    "cold":        {"file": "cold_snow.mp4",         "mode": "screen"},
    "mystery":     {"file": "mystery_fog.mp4",       "mode": "screen"},
    "dream":       {"file": "dream_bokeh.mp4",       "mode": "screen"},
    "warmth":      {"file": "warmth_rays.mp4",       "mode": "screen"},
    "revelation":  {"file": "revelation_flare.mp4",  "mode": "screen"},
    "noir":        {"file": "noir_rain.mp4",         "mode": "screen"},
    "sepia":       {"file": "sepia_grain.mp4",       "mode": "multiply"},
    "nature":      {"file": "nature_dust.mp4",       "mode": "screen"},
    "tech":        {"file": "tech_scanlines.mp4",    "mode": "screen"},
    "hacker_tech": {"file": "hacker_tech.mp4",       "mode": "screen"},
    "urgency":      {"file": "urgency.mp4",          "mode": "screen"},
    "euphoria":     {"file": "euphoria.mp4",         "mode": "screen"},
    "corporate":    {"file": "corporate.mp4",        "mode": "screen"},
    "glitch_soft":  {"file": "glitch_soft.mp4",      "mode": "screen"},
    "confusion":    {"file": "confusion.mp4",        "mode": "screen"},
    "network":      {"file": "polygon_grid.mp4",     "mode": "screen"},
    "royalty":      {"file": "royalty.mp4",          "mode": "screen"},
    "toxic":        {"file": "toxic.mp4",            "mode": "screen"},
    # static_dread is bright, full-frame grayscale noise (like sepia_grain,
    # not a sparse-on-black overlay) — screen at full opacity would whiteout
    # the footage under it; multiply darkens instead, keeping it legible.
    "static_dread": {"file": "static_dread.mp4",     "mode": "multiply"},
}

# Single knob for every overlay's blend strength (was 1.0 for all effects).
_GLOBAL_OVERLAY_OPACITY = 0.75

_OVERLAY_FADE_DUR = 0.5   # seconds — fade-in at start, fade-out at end

# Color grade presets (mirrors vidspeed's Grade type in src/theme.ts).
# "vintage" uses ffmpeg's own built-in curves preset; the others are plain
# well-known filter combos (classic sepia matrix, desaturate, cool-desaturated
# archival look) rather than anything calibrated against a reference.
_GRADE_FILTERS: dict[str, str] = {
    "sepia": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131:0",
    "bw": "hue=s=0",
    "vintage": "curves=preset=vintage",
    "aged": "eq=saturation=0.55:contrast=0.92:brightness=-0.02,colorbalance=rs=-0.05:gs=0.0:bs=0.08",
}


def _grade_chain_str(grade: str, grain: float, vignette: float) -> str:
    """Build a comma-joined ffmpeg -vf fragment for grade/grain/vignette.

    grain and vignette are 0..1 intensities; grade is a preset name from
    _GRADE_FILTERS (unknown/empty names are silently skipped, not errors —
    same "graceful no-op" convention as apply_visual_effect's unknown effect
    handling). Returns "" if nothing was requested.
    """
    parts: list[str] = []
    grade_filter = _GRADE_FILTERS.get(grade or "")
    if grade_filter:
        parts.append(grade_filter)
    if grain and grain > 0:
        strength = max(1, min(60, round(min(1.0, grain) * 40)))
        parts.append(f"noise=alls={strength}:allf=t")
    if vignette and vignette > 0:
        # ffmpeg's vignette filter has no direct 0..1 strength knob — angle
        # (radians) is the closest proxy: smaller angle = tighter/stronger
        # vignette. Default look (angle=PI/5) at intensity=1.0.
        angle = (3.14159265 / 5) / max(0.1, min(1.0, vignette))
        parts.append(f"vignette=angle={angle:.4f}")
    return ",".join(parts)


def _build_overlay_filter(
    blend_mode: str,
    opacity: float,
    width: int,
    height: int,
    accent: float,
    fade: float,
    clip_dur: float,
    post_chain: str = "",
) -> str:
    """Build the filter_complex for blending a motion overlay onto a clip.

    Keep everything in gbrp (planar RGB) so the blend operates in RGB colour
    space, matching what Filmora and other NLEs do. Blending in YUV applies
    the screen/multiply formula to offset chroma channels and introduces a
    colour cast (typically purple/teal).

    The overlay's fades and identity pad must use the blend's IDENTITY color:
    screen(clip, black) = clip but multiply(clip, black) = black — fading a
    multiply overlay (sepia grain) to black dragged the whole frame to black
    at both overlay edges, after which the white pad snapped it back.
    """
    identity = "white" if blend_mode == "multiply" else "black"
    fade_out_start = max(0.0, accent - fade)

    chains: list[str] = []

    # Scale, trim to accent duration, and fade in/out (to the identity color).
    chains.append(
        f"[0:v]scale={width}:{height},format=gbrp,"
        f"trim=0:{accent:.6f},setpts=PTS-STARTPTS,"
        f"fade=t=in:st=0:d={fade:.3f}:color={identity},"
        f"fade=t=out:st={fade_out_start:.6f}:d={fade:.3f}:color={identity}"
        f"[_ov_trimmed]"
    )

    # Pad the remainder of the clip with the identity color so the blend is a
    # mathematical identity after the accent window ends.
    if clip_dur > accent:
        pad_dur = clip_dur - accent
        chains.append(
            f"color=c={identity}:s={width}x{height}:r=30:d={pad_dur:.6f},"
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
    if post_chain:
        chains.append(
            f"[_clip][{ov_label}]"
            f"blend=all_mode={blend_mode}:all_opacity={opacity},"
            f"format=yuv420p"
            f"[_blended]"
        )
        chains.append(f"[_blended]{post_chain}[out]")
    else:
        chains.append(
            f"[_clip][{ov_label}]"
            f"blend=all_mode={blend_mode}:all_opacity={opacity},"
            f"format=yuv420p"
            f"[out]"
        )
    return ";".join(chains)

# Duration constants for lower_third compositing.
_LT_ANIM_IN  = 0.40   # seconds — fade-in
_LT_ANIM_OUT = 0.35   # seconds — fade-out


def _apply_grade_only(
    clip_path: str,
    output_path: str,
    post_chain: str,
    threads: int,
) -> str:
    """Grade/grain/vignette with no mood texture — single-input ffmpeg pass."""
    clip_dur = _probe_duration(clip_path)
    if not clip_dur:
        logger.warning("apply_visual_effect(grade-only): duration probe failed")
        return clip_path

    codec = _get_configured_video_codec()
    cmd = [
        utils.get_ffmpeg_binary(), "-y",
        "-i", clip_path,
        "-vf", post_chain,
        "-map", "0:v", "-map", "0:a?", "-c:a", "copy",
        "-c:v", codec, *_fast_preset_args(codec),
        "-pix_fmt", "yuv420p",
        "-threads", str(threads),
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            logger.error(
                f"apply_visual_effect(grade-only) failed: "
                f"{result.stderr.decode('utf-8', errors='replace')[-400:]}"
            )
            return clip_path
        return output_path
    except Exception as exc:
        logger.error(f"apply_visual_effect(grade-only) exception: {exc}")
        return clip_path


def apply_visual_effect(
    clip_path: str,
    effect: str,
    output_path: str,
    width: int = 1920,
    height: int = 1080,
    threads: int = 4,
    grade: str = "",
    grain: float = 0.0,
    vignette: float = 0.0,
) -> str:
    """Composite a motion overlay onto clip_path for its full natural duration,
    optionally fused with a color grade / grain / vignette post-process.

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

    grade/grain/vignette work independently of `effect` — a clip can be
    graded/grained/vignetted with no mood texture at all (single-input pass,
    see _apply_grade_only), or have both fused into the one ffmpeg call that
    already runs the texture blend, avoiding a second full transcode.
    """
    post_chain = _grade_chain_str(grade, grain, vignette)
    overlay_cfg = _EFFECT_OVERLAYS.get(effect) if effect in _VALID_VISUAL_EFFECTS else None

    if not overlay_cfg:
        if not post_chain:
            return clip_path
        return _apply_grade_only(clip_path, output_path, post_chain, threads)

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
    opacity    = _GLOBAL_OVERLAY_OPACITY

    # Play the full overlay once, trimmed to clip duration if the clip is shorter.
    accent = min(ov_dur, clip_dur)
    # Clamp fade so it never exceeds 25% of the overlay window.
    fade   = min(_OVERLAY_FADE_DUR, accent / 4)

    filter_complex = _build_overlay_filter(
        blend_mode, opacity, width, height, accent, fade, clip_dur, post_chain
    )

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

    Pure alpha-compositing strategy — no blend modes. The Revideo clip is
    white text on solid black with its own dynamically-sized dark backdrop;
    the black is keyed out with `colorkey`, giving the text clip a proper
    alpha channel, which is then overlaid onto the footage. Both fades run
    via FFmpeg `fade=alpha=1` on the alpha channel.
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

    # colorkey turns the solid black background of the Revideo clip transparent.
    # Convert to rgba first so the key operates in RGB space (not YUV), then fade
    # the alpha channel in/out for the lower-third animation.
    _text_chain = (
        f"scale={width}:{height},format=rgba,"
        f"colorkey=color=0x000000:similarity=0.01:blend=0.05,"
        f"colorchannelmixer=aa=0.75,"
        f"fade=t=in:st=0:d={fi:.3f}:alpha=1,"
        f"fade=t=out:st={fo_start:.3f}:d={fo:.3f}:alpha=1"
    )

    filter_complex = (
        f"[0:v]scale={width}:{height},format=rgba[footage];"
        f"[1:v]{_text_chain}[text];"
        f"[footage][text]overlay=0:0,format=yuv420p[out]"
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
