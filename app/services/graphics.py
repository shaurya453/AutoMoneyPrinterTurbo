"""
app/services/graphics.py — Revideo graphic segment renderer

Renders animated graphic clips (title cards, infographics, etc.) via a
headless Node.js/Revideo subprocess and returns the path to the resulting
MP4, which slots directly into the existing clip pipeline.
"""

import json
import os
import subprocess
from typing import Optional

from loguru import logger

_WORKER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "revideo-worker")
)
_RENDER_JS = os.path.join(_WORKER_DIR, "render.js")

_RENDER_TIMEOUT = 180  # seconds; a 5-second title card takes ~15-40s on CPU


def render_graphic_clip(
    graphic_type: str,
    out_path: str,
    duration: float,
    width: int,
    height: int,
    fps: int = 30,
    variables: Optional[dict] = None,
) -> Optional[str]:
    """
    Render a Revideo graphic segment. Returns the output MP4 path, or None on failure.

    Args:
        graphic_type: Scene selector — currently only "title_card".
        out_path:     Absolute path where the MP4 should be written.
        duration:     Clip length in seconds.
        width/height: Output dimensions (must match the pipeline's target resolution).
        fps:          Frame rate (default 30).
        variables:    Key/value pairs forwarded to the Revideo scene (title, subtitle, etc.).
    """
    if not os.path.isfile(_RENDER_JS):
        logger.error(f"revideo-worker not found at {_RENDER_JS} — skipping graphic clip")
        return None

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    payload = {
        "type": graphic_type,
        "outPath": out_path,
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "variables": variables or {},
    }

    logger.info(f"rendering graphic clip: type={graphic_type} dur={duration:.1f}s → {os.path.basename(out_path)}")

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

    # Validate the output file exists and has content
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        logger.warning(f"revideo produced no output at {out_path}")
        return None

    # Quick ffprobe sanity check
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
