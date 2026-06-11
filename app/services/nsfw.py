"""
app/services/nsfw.py — pixel-level NSFW gate (NudeNet ONNX)

Independent, mandatory safety layer that hard-rejects images/video frames
containing exposed genitalia/breasts/buttocks, regardless of CLIP relevance
score or any tag/keyword denylist. CLIP must never be relied on for safety —
this module is the pixel-level backstop for content that is mislabeled or
has innocuous-looking tags/filenames (especially from DDG image search).

Uses the `nudenet` package, which bundles a small (~12MB) ONNX YOLO model —
no separate model download needed.

Disabled gracefully if `nudenet` isn't installed, the model fails to load,
or `nsfw_gate_enabled = false` in config — every function then returns None
and `passes()` treats that as "allow" (graceful degradation, mirroring
app/services/relevance.py).
"""

import io
from typing import List, Optional

from loguru import logger

from app.config import config

# Granular body-part labels NudeNet returns that constitute hard-reject
# content. FEET_EXPOSED, BELLY_EXPOSED, ARMPITS_EXPOSED, MALE_BREAST_EXPOSED,
# FACE_*, and *_COVERED variants are not rejected.
_REJECT_LABELS = {
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
}

_detector = None
_detector_load_attempted = False


def _get_detector():
    global _detector, _detector_load_attempted
    if _detector is not None or _detector_load_attempted:
        return _detector
    _detector_load_attempted = True

    if not config.app.get("nsfw_gate_enabled", True):
        logger.info("NSFW gate disabled (nsfw_gate_enabled=false)")
        return None

    try:
        from nudenet import NudeDetector

        _detector = NudeDetector()
        logger.info("NSFW gate: NudeNet model loaded")
    except Exception as exc:
        logger.warning(f"NSFW gate unavailable, continuing without it: {exc}")
        _detector = None
    return _detector


def is_available() -> bool:
    """True if the NSFW gate is enabled and the model loaded successfully."""
    return _get_detector() is not None


def _is_nsfw_detections(detections: list) -> bool:
    threshold = float(config.app.get("nsfw_threshold", 0.25))
    for det in detections:
        if det.get("class") in _REJECT_LABELS and det.get("score", 0.0) >= threshold:
            return True
    return False


def _normalize_image_bytes(image_bytes: bytes) -> Optional[bytes]:
    """Re-encode `image_bytes` to a format OpenCV's `cv2.imdecode` can read.

    NudeNet decodes raw bytes via `cv2.imdecode`, which -- unlike PIL --
    can't handle AVIF (a format several stock-photo/CDN sources serve by
    default). For those, `cv2.imdecode` silently returns None and NudeNet
    crashes on `mat.shape`, which previously surfaced as a caught exception
    that made the NSFW gate silently pass the image. Detect that case via
    PIL and re-encode to PNG so the gate actually scans it. Returns None if
    the bytes can't be decoded by either.
    """
    import cv2
    import numpy as np

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    if cv2.imdecode(arr, cv2.IMREAD_UNCHANGED) is not None:
        return image_bytes

    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        return None


def is_nsfw_image(image_bytes: bytes) -> Optional[bool]:
    """Return True if the image contains hard-reject NSFW content, False if
    clean, or None if the gate is unavailable or decoding failed."""
    detector = _get_detector()
    if detector is None or not image_bytes:
        return None
    try:
        normalized = _normalize_image_bytes(image_bytes)
        if normalized is None:
            return None
        detections = detector.detect(normalized)
        return _is_nsfw_detections(detections)
    except Exception as exc:
        logger.debug(f"NSFW image scan failed: {exc}")
        return None


def is_nsfw_video(video_path: str, num_frames: int = 4) -> Optional[bool]:
    """Sample `num_frames` evenly-spaced frames from the video and return
    True if ANY of them contains hard-reject NSFW content (short-circuits on
    first hit), False if all sampled frames are clean, or None if the gate
    is unavailable or the video couldn't be opened."""
    detector = _get_detector()
    if detector is None:
        return None

    from app.services.video import _open_video_clip_quietly

    clip = None
    try:
        clip = _open_video_clip_quietly(video_path)
        duration = clip.duration
        if not duration or duration <= 0:
            return None

        num_frames = max(1, num_frames)
        for i in range(num_frames):
            t = duration * (i + 0.5) / num_frames
            frame = clip.get_frame(min(t, max(duration - 0.01, 0.0)))
            frame_bytes = _frame_to_jpeg_bytes(frame)
            if frame_bytes and _is_nsfw_detections(detector.detect(frame_bytes)):
                return True
        return False
    except Exception as exc:
        logger.debug(f"NSFW video scan failed: {video_path} => {exc}")
        return None
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception:
                pass


def _frame_to_jpeg_bytes(frame) -> bytes:
    from PIL import Image

    try:
        img = Image.fromarray(frame)
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()
    except Exception as exc:
        logger.debug(f"frame encode failed: {exc}")
        return b""


def passes(result: Optional[bool]) -> bool:
    """A None or False result passes (graceful degradation / clean content);
    only a confirmed True (NSFW detected) rejects."""
    return result is not True


def sample_frame_bytes(video_path: str, num_frames: int = 4) -> List[bytes]:
    """Extract `num_frames` evenly-spaced JPEG-encoded frames from a video.

    Used by callers (pipeline.py) that need the same frames for both the
    NSFW gate and CLIP relevance scoring, avoiding opening the file twice.
    Returns an empty list if the video can't be opened.
    """
    from app.services.video import _open_video_clip_quietly

    clip = None
    frames: List[bytes] = []
    try:
        clip = _open_video_clip_quietly(video_path)
        duration = clip.duration
        if not duration or duration <= 0:
            return []
        num_frames = max(1, num_frames)
        for i in range(num_frames):
            t = duration * (i + 0.5) / num_frames
            frame = clip.get_frame(min(t, max(duration - 0.01, 0.0)))
            frame_bytes = _frame_to_jpeg_bytes(frame)
            if frame_bytes:
                frames.append(frame_bytes)
        return frames
    except Exception as exc:
        logger.debug(f"frame sampling failed: {video_path} => {exc}")
        return []
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception:
                pass


def is_nsfw_frames(frames_bytes: List[bytes]) -> Optional[bool]:
    """Like is_nsfw_video, but operates on already-extracted frame bytes
    (see sample_frame_bytes) so the NSFW gate and relevance scoring can
    share a single frame-extraction pass."""
    detector = _get_detector()
    if detector is None:
        return None
    if not frames_bytes:
        return None
    try:
        for frame_bytes in frames_bytes:
            if _is_nsfw_detections(detector.detect(frame_bytes)):
                return True
        return False
    except Exception as exc:
        logger.debug(f"NSFW frame scan failed: {exc}")
        return None
