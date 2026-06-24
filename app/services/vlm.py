"""
app/services/vlm.py — VLM visual verification via OpenAI

Checks downloaded footage against narration context. Any failure degrades
gracefully to None, which is treated as "pass" by passes() — mirroring the
nsfw.py and relevance.py fail-open pattern.

Enable by setting vlm_verify_enabled = true and vlm_api_key in config.toml.
Uses gpt-4.1-nano by default (~$0.002 per video at 10 clips).
"""

import base64
import json
from typing import List, Optional

from loguru import logger

from app.config import config

_client = None
_client_load_attempted = False

# Circuit breaker: after this many consecutive 429/quota errors, stop calling
# the API for the rest of the process (fail-open silently).
_CIRCUIT_BREAKER_THRESHOLD = 3
_consecutive_rate_errors = 0
_circuit_open = False

# Per-run usage accumulators (reset at pipeline start via reset_usage()).
_total_calls = 0
_total_input_tokens = 0
_total_output_tokens = 0


def reset_usage() -> None:
    global _total_calls, _total_input_tokens, _total_output_tokens
    _total_calls = _total_input_tokens = _total_output_tokens = 0


def get_usage() -> dict:
    return {
        "calls": _total_calls,
        "input_tokens": _total_input_tokens,
        "output_tokens": _total_output_tokens,
    }


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
        from openai import OpenAI

        _client = OpenAI(api_key=api_key)
        logger.info("VLM: OpenAI client initialized")
    except Exception as exc:
        logger.warning(f"VLM unavailable, continuing without it: {exc}")
        _client = None
    return _client


def is_enabled() -> bool:
    """True if VLM verification is enabled, client loaded, and circuit not open."""
    return _get_client() is not None and not _circuit_open


def passes(result: Optional[bool]) -> bool:
    """None or False passes (fail-open / accepted); only True rejects."""
    return result is not True


def _to_jpeg(image_bytes: bytes) -> bytes:
    """Re-encode image_bytes to JPEG via PIL. Returns b'' if decoding fails."""
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(image_bytes)) as img:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            return buf.getvalue()
    except Exception:
        return b""


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

    model = str(config.app.get("vlm_model", "gpt-4.1-nano"))
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

    global _consecutive_rate_errors, _circuit_open
    try:
        # Normalize to JPEG — OpenAI only accepts png/jpeg/gif/webp and some
        # CDNs serve AVIF or other formats that will cause a 400.
        image_bytes = _to_jpeg(image_bytes)
        if not image_bytes:
            return None
        b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
                        },
                    ],
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=100,
        )
        raw = (response.choices[0].message.content or "").strip()
        data = json.loads(raw)
        accepted = bool(data.get("accepted", True))
        score = float(data.get("score", 0.5))
        reason = str(data.get("reason", ""))
        if config.app.get("relevance_debug_log", False):
            logger.debug(f"VLM: accepted={accepted} score={score:.2f} reason={reason!r}")
        _consecutive_rate_errors = 0
        global _total_calls, _total_input_tokens, _total_output_tokens
        _total_calls += 1
        if response.usage:
            _total_input_tokens += response.usage.prompt_tokens or 0
            _total_output_tokens += response.usage.completion_tokens or 0
        return not accepted or score < threshold
    except Exception as exc:
        exc_str = str(exc)
        if "429" in exc_str or "rate_limit" in exc_str.lower() or "quota" in exc_str.lower():
            _consecutive_rate_errors += 1
            if _consecutive_rate_errors >= _CIRCUIT_BREAKER_THRESHOLD:
                _circuit_open = True
                logger.warning(
                    f"VLM circuit breaker tripped after {_consecutive_rate_errors} "
                    f"consecutive rate errors — VLM disabled for this run (fail-open)"
                )
            else:
                logger.warning(f"VLM rate error ({_consecutive_rate_errors}/{_CIRCUIT_BREAKER_THRESHOLD}), fail-open")
        else:
            logger.warning(f"VLM verify failed (fail-open): {exc}")
        return None
