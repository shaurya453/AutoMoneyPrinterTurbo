"""
app/services/vlm.py — VLM visual verification via Google Gemini

Checks downloaded footage against narration context. Any failure degrades
gracefully to None, which is treated as "pass" by passes() — mirroring the
nsfw.py and relevance.py fail-open pattern.

Enable by setting vlm_verify_enabled = true and vlm_api_key in config.toml.
Get a free Gemini API key at aistudio.google.com (1,500 req/day free).
"""

import json
from typing import List, Optional

from loguru import logger

from app.config import config

_client = None
_client_load_attempted = False


def _get_client():
    global _client, _client_load_attempted
    if _client is not None or _client_load_attempted:
        return _client
    _client_load_attempted = True

    if not config.app.get("vlm_verify_enabled", False):
        return None

    api_key = str(config.app.get("vlm_api_key", "")).strip()
    if not api_key:
        logger.warning("vlm_verify_enabled=true but vlm_api_key is empty — VLM disabled")
        return None

    try:
        from google import genai

        _client = genai.Client(api_key=api_key)
        logger.info("VLM: Google Gemini client initialized")
    except Exception as exc:
        logger.warning(f"VLM unavailable, continuing without it: {exc}")
        _client = None
    return _client


def is_enabled() -> bool:
    """True if VLM verification is enabled and the Gemini client loaded."""
    return _get_client() is not None


def passes(result: Optional[bool]) -> bool:
    """None or False passes (fail-open / accepted); only True rejects."""
    return result is not True


def verify_image(
    image_bytes: bytes,
    narration: str,
    visual_caption: str,
    video_topic: str,
    must_show: List[str] = None,
    avoid: List[str] = None,
) -> Optional[bool]:
    """Verify that image_bytes is a good visual match for the narration context.

    Returns True if rejected, False if accepted, None on any failure (fail-open).
    Use passes(verify_image(...)) to gate acceptance.

    For video clips, pass the midpoint frame bytes (from nsfw.sample_frame_bytes).
    """
    client = _get_client()
    if client is None or not image_bytes:
        return None

    model = str(config.app.get("vlm_model", "gemini-2.0-flash-lite"))
    threshold = float(config.app.get("vlm_threshold", 0.55))

    must_show_str = ", ".join(must_show) if must_show else "anything relevant"
    avoid_str = ", ".join(avoid) if avoid else "watermarks, text overlays, cartoons"

    prompt = (
        "You are a footage reviewer for a documentary video. Evaluate whether this "
        "image is a good visual match for the narration moment.\n\n"
        f'Narration: "{narration}"\n'
        f'Visual intent: "{visual_caption}"\n'
        f'Topic: "{video_topic}"\n'
        f"Must show: {must_show_str}\n"
        f"Avoid: {avoid_str}\n\n"
        'Return JSON only: {"accepted": true/false, "score": 0.0-1.0, "reason": "short string"}\n'
        "Reject if the image is clearly off-topic, contains watermarks, heavy text, "
        "cartoons, or does not support the narration."
    )

    try:
        from google.genai import types

        response = client.models.generate_content(
            model=model,
            contents=[
                prompt,
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        raw = (response.text or "").strip()
        data = json.loads(raw)
        accepted = bool(data.get("accepted", True))
        score = float(data.get("score", 0.5))
        reason = str(data.get("reason", ""))
        if config.app.get("relevance_debug_log", False):
            logger.debug(f"VLM: accepted={accepted} score={score:.2f} reason={reason!r}")
        return not accepted or score < threshold
    except Exception as exc:
        logger.warning(f"VLM verify failed (fail-open): {exc}")
        return None
