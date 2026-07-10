"""
app/services/graphics.py — Revideo graphic segment renderer

Renders animated graphic clips (title cards, infographics, lists)
via a headless Node.js/Revideo subprocess and returns the path to the resulting
MP4, which slots directly into the existing clip pipeline.
"""

import json
import os
import random
import subprocess
from typing import Optional

from loguru import logger

_WORKER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "revideo-worker")
)
_RENDER_JS = os.path.join(_WORKER_DIR, "render.js")

_RENDER_TIMEOUT = 180  # seconds

# Variant metadata is single-sourced from revideo-worker/variants.json —
# the same file render.js maps variant indexes to project files with, so the
# two sides can't drift. Derived here:
#   _BG_VIDEOS     — gradient backgrounds served from revideo-worker/public/
#   _POOL_SIZES    — variants per type (rotation pool size)
#   _BG_VIDEO_TYPES — types whose scenes take an injected bgVideo
#   STYLE_MAP      — named style → variant index, per type (lets the agent
#                    request a specific aesthetic without knowing indexes)
_MANIFEST_PATH = os.path.join(_WORKER_DIR, "variants.json")
_BG_VIDEOS: list[str] = []
_BG_VIDEO_TYPES: set = set()
_POOL_SIZES: dict = {}
STYLE_MAP: dict = {}
try:
    with open(_MANIFEST_PATH, "r", encoding="utf-8") as _fh:
        _manifest = json.load(_fh)
    _BG_VIDEOS = list(_manifest["bg_videos"])
    for _type, _cfg in _manifest["types"].items():
        _POOL_SIZES[_type] = len(_cfg["variants"])
        if _cfg.get("uses_bg_video"):
            _BG_VIDEO_TYPES.add(_type)
        STYLE_MAP[_type] = {
            _style: _idx
            for _idx, _variant in enumerate(_cfg["variants"])
            for _style in _variant.get("styles", [])
        }
except Exception as _exc:  # missing/corrupt manifest — degrade, don't crash import
    logger.error(f"failed to load {_MANIFEST_PATH} ({_exc}) — graphic variants limited to defaults")

# Per-job last-used background (module-level, persists for one job run).
_last_bg_video: str = ""

# Per-type last-used variant index (module-level, persists for one job run).
# Used by the no-consecutive-repeat rotation logic.
_last_variant: dict = {}


def _pick_variant(
    graphic_type: str,
    style: Optional[str] = None,
    rng: random.Random = random,
) -> int:
    """
    Return a variant index for the given graphic type.

    If `style` is provided and matches a known style name for this type, that
    specific variant is returned (bypassing rotation).  Otherwise the pool is
    rotated to avoid repeating the last-used variant.
    """
    if style:
        type_styles = STYLE_MAP.get(graphic_type, {})
        if style in type_styles:
            variant = type_styles[style]
            _last_variant[graphic_type] = variant
            return variant
        # Unknown style name — log and fall through to rotation
        logger.warning(
            f"unknown style '{style}' for type '{graphic_type}' — using rotation"
        )

    pool_size = _POOL_SIZES.get(graphic_type, 1)
    last = _last_variant.get(graphic_type)
    choices = [i for i in range(pool_size) if i != last]
    if not choices:
        choices = list(range(pool_size))
    chosen = rng.choice(choices)
    _last_variant[graphic_type] = chosen
    return chosen


def render_graphic_clip(
    graphic_type: str,
    out_path: str,
    duration: float,
    width: int,
    height: int,
    fps: int = 30,
    variables: Optional[dict] = None,
    style: Optional[str] = None,
    rng: random.Random = random,
) -> Optional[str]:
    """
    Render a Revideo graphic segment. Returns the output MP4 path, or None on failure.

    Args:
        graphic_type: Scene selector — "lower_third", "infographic", "list".
        out_path:     Absolute path where the MP4 should be written.
        duration:     Clip length in seconds.
        width/height: Output dimensions (must match the pipeline's target resolution).
        fps:          Frame rate (default 30).
        variables:    Key/value pairs forwarded to the Revideo scene.
        style:        Optional named style (e.g. "editorial", "callouts") — maps to a
                      specific variant, bypassing the no-consecutive rotation.
        rng:          Source of randomness for variant/background rotation. Defaults
                      to the global `random` module; pass a seeded `random.Random`
                      for reproducible-per-job selection.
    """
    global _last_bg_video

    if os.environ.get("REVIDEO_ENABLED", "1") == "0":
        logger.info(f"Revideo disabled (REVIDEO_ENABLED=0) — skipping {graphic_type} render")
        return None

    if not os.path.isfile(_RENDER_JS):
        logger.error(f"revideo-worker not found at {_RENDER_JS} — skipping graphic clip")
        return None

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    variant = _pick_variant(graphic_type, style, rng=rng)
    merged_vars = dict(variables or {})

    # Inject background for scenes that take one, enforcing no-consecutive-repeat.
    if graphic_type in _BG_VIDEO_TYPES and _BG_VIDEOS:
        choices = [v for v in _BG_VIDEOS if v != _last_bg_video] or _BG_VIDEOS
        chosen_bg = rng.choice(choices)
        _last_bg_video = chosen_bg
        merged_vars["bgVideo"] = chosen_bg

    payload = {
        "type": graphic_type,
        "variant": variant,
        "outPath": out_path,
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "variables": merged_vars,
    }

    style_tag = f" style={style}" if style else ""
    logger.info(
        f"rendering graphic clip: type={graphic_type} variant={variant}{style_tag} "
        f"dur={duration:.1f}s → {os.path.basename(out_path)}"
    )

    try:
        result = subprocess.run(
            ["node", _RENDER_JS],
            input=json.dumps(payload).encode(),
            capture_output=True,
            timeout=_RENDER_TIMEOUT,
            cwd=_WORKER_DIR,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"revideo render timed out after {_RENDER_TIMEOUT}s")
        return None
    except Exception as exc:
        logger.warning(f"revideo subprocess error: {exc}")
        return None

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        logger.warning(f"revideo render failed (exit {result.returncode}): {stderr[:500]}")
        return None

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        logger.warning(f"revideo produced no output at {out_path}")
        return None

    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                out_path,
            ],
            capture_output=True,
            timeout=15,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            logger.warning(f"ffprobe validation failed for graphic clip: {out_path}")
            return None
    except Exception:
        pass  # ffprobe unavailable — trust the file exists

    logger.info(f"graphic clip ready: {out_path}")
    return out_path
