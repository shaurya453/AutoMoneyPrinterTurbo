"""Clip planning helpers: query building, duration estimation, ffmpeg trim."""
import os
import subprocess
from typing import List, Optional

from loguru import logger

from app.config import config

_TRIM_TIMEOUT_SECONDS = 120

# Crossfade constants — must match combine.py values in the render package.
_DEFAULT_CROSSFADE_SECONDS = 0.2   # kept local to avoid circular import
_CROSSFADE_DUR = _DEFAULT_CROSSFADE_SECONDS
# Half of the dissolve duration added to each clip so the crossfade overlap
# does not eat into the sentence's actual visual content.
_TRIM_BUFFER = _CROSSFADE_DUR


def _trim_clip(src_path: str, duration: float, out_path: str) -> bool:
    """Trim a video to `duration` seconds via ffmpeg. Returns True on success."""
    # Try fast stream-copy first
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", src_path,
        "-t", f"{duration:.3f}",
        "-c", "copy",
        out_path,
    ]
    try:
        if (
            subprocess.run(cmd, capture_output=True, timeout=_TRIM_TIMEOUT_SECONDS).returncode == 0
            and os.path.exists(out_path)
        ):
            return True
    except subprocess.TimeoutExpired:
        logger.warning(f"ffmpeg stream-copy trim timed out after {_TRIM_TIMEOUT_SECONDS}s: {src_path}")
    except Exception as e:
        logger.warning(f"ffmpeg stream-copy trim failed: {e}")
    # Re-encode fallback
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", src_path,
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        out_path,
    ]
    try:
        return (
            subprocess.run(cmd, capture_output=True, timeout=_TRIM_TIMEOUT_SECONDS).returncode == 0
            and os.path.exists(out_path)
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"ffmpeg re-encode trim timed out after {_TRIM_TIMEOUT_SECONDS}s: {src_path}")
        return False
    except Exception as e:
        logger.warning(f"ffmpeg re-encode trim failed: {e}")
        return False


def _get_visual_concepts(sentence: dict) -> List[str]:
    """Return the sentence's local visual ideas.

    Backward compat: older job.json files use "search_terms" (a 3-tier
    niche/medium/generic list) instead of "visual_concepts".
    """
    return sentence.get("visual_concepts") or sentence.get("search_terms", [])


def _build_query_ladder(
    video_topic: str,
    visual_concepts: List[str],
    video_type: str = "thematic",
) -> List[str]:
    """Build search queries from local visual concepts, with anchoring strategy
    determined by `video_type`.

    named_entity — every concept is combined with `video_topic` upfront
        ("{concept} {video_topic}"), preserving specificity for real-world
        subjects.  Variety comes from different aspects of the same subject.

    thematic (default) — bare concepts only, no topic appended per concept.
        Appending the topic produces overly long, precise phrases ("supermarket
        aisle empty shelves vanishing packaged food staples") that stock search
        engines handle poorly. The final bare-`video_topic` rung is the only
        anchor and acts as a last-resort safety net.

    Duplicates are removed in order.  When `video_topic` is empty, both modes
    fall back to bare concepts (legacy behavior).
    """
    video_topic = (video_topic or "").strip()
    ladder: List[str] = []

    for concept in visual_concepts:
        concept = (concept or "").strip()
        if not concept:
            continue
        if not video_topic or video_type == "named_entity":
            # Named-entity: bare concept first so image search engines (Serper/
            # Google Images) receive a clean product-name query without the full
            # video_topic appended (e.g. "Kellogg's Corn Pops yellow box" works
            # far better than "Kellogg's Corn Pops yellow box vanishing American
            # brands").  Topic-appended form is added as a second rung fallback.
            q_bare = concept if video_topic else concept
            q_topic = f"{concept} {video_topic}".strip() if video_topic else concept
            if q_bare not in ladder:
                ladder.append(q_bare)
            if q_topic not in ladder and q_topic != q_bare:
                ladder.append(q_topic)
        else:
            # Thematic: bare concept only. Appending the full topic string
            # produces long, over-specific queries ("supermarket aisle empty
            # shelves vanishing packaged food staples") that stock image search
            # handles poorly. The bare topic rung below is the safety net.
            if concept not in ladder:
                ladder.append(concept)

    if video_topic and video_topic not in ladder:
        ladder.append(video_topic)
    return ladder
