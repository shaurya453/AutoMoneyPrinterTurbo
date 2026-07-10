"""
app/services/vlm.py — VLM visual verification via OpenAI

Checks downloaded footage against narration context. Any failure degrades
gracefully to None, which is treated as "pass" by passes() — mirroring the
nsfw.py and relevance.py fail-open pattern.

Enable by setting vlm_verify_enabled = true and vlm_api_key in config.toml.
Uses gpt-4.1-nano by default (~$0.002 per video at 10 clips).
"""

import base64
import hashlib
import json
import os
import threading
from typing import List, Optional

from loguru import logger

from app.config import config

_client = None
_client_load_attempted = False
_client_load_lock = threading.Lock()

# Circuit breaker: after this many consecutive 429/quota errors, stop calling
# the API for the rest of the process (fail-open silently).
_CIRCUIT_BREAKER_THRESHOLD = 3
_consecutive_rate_errors = 0
_circuit_open = False

# On-disk verdict cache: "v2:" + md5(image_md5|narration|caption|topic) → score.
# Prevents re-screening the same image in the same prompt context across
# re-runs of the same job. Context is part of the key on purpose — a verdict
# is only reusable when the narration/caption/topic it was scored against
# match too.
_vlm_cache: dict = {}
_vlm_cache_loaded = False
_vlm_cache_lock = threading.Lock()
# Unflushed verdicts. The full JSON is only rewritten every
# _VLM_CACHE_FLUSH_EVERY new entries; the pipeline calls flush_cache() at the
# end of the run to persist the remainder (atexit is useless here — cli.py
# exits via os._exit()). A crash mid-run loses at most the last few verdicts.
_vlm_cache_dirty = 0
_VLM_CACHE_FLUSH_EVERY = 5

# Per-run usage accumulators and circuit-breaker state; all guarded by one lock
# so concurrent workers can't corrupt the counts or bypass the threshold.
_vlm_stats_lock = threading.Lock()
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


def _get_cache_path() -> str:
    return os.path.join(config.root_dir, "storage", "vlm_verdicts.json")


def _load_vlm_cache() -> None:
    global _vlm_cache, _vlm_cache_loaded
    if _vlm_cache_loaded:
        return
    try:
        with open(_get_cache_path(), "r", encoding="utf-8") as fh:
            _vlm_cache.update(json.load(fh))
        logger.debug(f"VLM cache: {len(_vlm_cache)} entries loaded")
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug(f"VLM cache load error (starting fresh): {exc}")
    _vlm_cache_loaded = True


def _save_vlm_cache() -> None:
    """Write the cache to disk. Caller must hold _vlm_cache_lock."""
    global _vlm_cache_dirty
    try:
        path = _get_cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(_vlm_cache, fh)
        os.replace(tmp_path, path)
        _vlm_cache_dirty = 0
    except Exception as exc:
        logger.debug(f"VLM cache save failed: {exc}")


def flush_cache() -> None:
    """Persist any unflushed verdicts. Called at the end of a pipeline run."""
    with _vlm_cache_lock:
        if _vlm_cache_dirty > 0:
            _save_vlm_cache()


def _get_client():
    """Thread-safe lazy init (same load-inside-lock pattern as nsfw/relevance)."""
    global _client, _client_load_attempted
    if _client_load_attempted:  # fast path, no lock once init finished
        return _client
    with _client_load_lock:
        if _client_load_attempted:
            return _client

        try:
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
        finally:
            _client_load_attempted = True


def is_enabled() -> bool:
    """True if VLM verification is enabled, client loaded, and circuit not open."""
    return _get_client() is not None and not _circuit_open


def passes(result: Optional[float]) -> bool:
    """None → fail-open (accept). Float → accept if score >= vlm_threshold."""
    if result is None:
        return True
    threshold = float(config.app.get("vlm_threshold", 0.55))
    return result >= threshold


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
) -> Optional[float]:
    """Score image_bytes against the narration context. Returns 0.0–1.0 or None (fail-open).

    Two different thresholds gate this score depending on the caller:
      - video candidates: passes(verify_image(...)) → `vlm_threshold` (default 0.55)
      - image candidates: images.py compares against `vlm_image_threshold`
        (default 0.30) directly — images tolerate a looser cut because the
        comparative VLM pool pass re-ranks the survivors anyway.
    None means the VLM call failed; passes(None) returns True (fail-open),
    so failures never block.

    For video clips, pass the midpoint frame bytes (from nsfw.sample_frame_bytes).
    """
    client = _get_client()
    if client is None or not image_bytes:
        return None

    # Cache lookup. The verdict depends on the PROMPT CONTEXT, not just the
    # pixels — the same stock image can be a perfect match for one narration
    # and irrelevant to another — so the key covers image bytes + narration +
    # visual_caption + video_topic. The "v2:" prefix versions the format;
    # legacy pixel-only keys (plain md5 hex) simply never match again.
    img_md5 = hashlib.md5(image_bytes).hexdigest()
    _ctx = f"{img_md5}|{narration}|{visual_caption}|{video_topic}"
    cache_key = "v2:" + hashlib.md5(_ctx.encode("utf-8")).hexdigest()
    with _vlm_cache_lock:
        _load_vlm_cache()
        if cache_key in _vlm_cache:
            cached = _vlm_cache[cache_key]
            logger.debug(f"VLM cache hit: {cache_key[:11]} score={cached:.2f}")
            return cached

    model = str(config.app.get("vlm_model", "gpt-4.1-nano"))
    threshold = float(config.app.get("vlm_threshold", 0.55))

    must_show_str = ", ".join(must_show) if must_show else "anything relevant"
    avoid_str = ", ".join(avoid) if avoid else "watermarks, text overlays, cartoons"

    prompt = (
        "You are a footage curator for a documentary video. Decide whether this image "
        "is acceptable b-roll for the narration moment shown below.\n\n"
        f'Narration: "{narration}"\n'
        f'Visual intent: "{visual_caption}"\n'
        f'Overall topic: "{video_topic}"\n'
        f"Should show: {must_show_str}\n"
        f"Avoid: {avoid_str}\n\n"
        "Scoring guide:\n"
        "  0.8–1.0 — clearly matches the visual intent and narration; exactly what was asked for\n"
        "  0.6–0.7 — close match; right subject, minor framing or context difference\n"
        "  0.4–0.5 — same broad category but wrong specific subject (e.g. generic speaker when a named model was asked for)\n"
        "  0.2–0.3 — loosely related or too generic; could belong to hundreds of different videos\n"
        "  0.0–0.1 — clearly wrong, unrelated, offensive, watermarked, cartoon, or heavy text overlay\n\n"
        "Be strict about specificity: if the visual intent names a specific product, person, or place, "
        "generic category footage scores 0.4 or below — not acceptable as a substitute. "
        "Generic footage is only acceptable when the narration itself is generic.\n\n"
        'Return JSON only: {"accepted": true/false, "score": 0.0-1.0, "reason": "short string"}'
    )

    global _consecutive_rate_errors, _circuit_open, _vlm_cache_dirty
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
        score = float(data.get("score", 0.5))
        reason = str(data.get("reason", ""))
        if config.app.get("relevance_debug_log", False):
            logger.debug(f"VLM: score={score:.2f} reason={reason!r}")
        with _vlm_cache_lock:
            _vlm_cache[cache_key] = score
            _vlm_cache_dirty += 1
            if _vlm_cache_dirty >= _VLM_CACHE_FLUSH_EVERY:
                _save_vlm_cache()
        global _total_calls, _total_input_tokens, _total_output_tokens
        with _vlm_stats_lock:
            _consecutive_rate_errors = 0
            _total_calls += 1
            if response.usage:
                _total_input_tokens += response.usage.prompt_tokens or 0
                _total_output_tokens += response.usage.completion_tokens or 0
        return score
    except Exception as exc:
        exc_str = str(exc)
        if "429" in exc_str or "rate_limit" in exc_str.lower() or "quota" in exc_str.lower():
            with _vlm_stats_lock:
                _consecutive_rate_errors += 1
                _current_errors = _consecutive_rate_errors
                if _current_errors >= _CIRCUIT_BREAKER_THRESHOLD:
                    _circuit_open = True
            if _current_errors >= _CIRCUIT_BREAKER_THRESHOLD:
                logger.warning(
                    f"VLM circuit breaker tripped after {_current_errors} "
                    f"consecutive rate errors — VLM disabled for this run (fail-open)"
                )
            else:
                logger.warning(f"VLM rate error ({_current_errors}/{_CIRCUIT_BREAKER_THRESHOLD}), fail-open")
        else:
            logger.warning(f"VLM verify failed (fail-open): {exc}")
        return None


_COMPARE_MAX_CANDIDATES = 5


def compare_candidates(
    candidates: List[bytes],
    narration: str,
    visual_caption: str,
    video_topic: str,
    must_show: List[str] = None,
    avoid: List[str] = None,
) -> Optional[int]:
    """Show several already-individually-accepted candidates to the VLM in
    ONE request and return the 0-based index of the best match, or None on
    any failure (fail-open — caller should default to candidates[0]).

    Unlike verify_image(), this scores a *set* of images relative to each
    other rather than one image in isolation, so it isn't cached (a
    comparative verdict depends on the whole set shown, not any single
    image's bytes) and shares verify_image()'s circuit breaker / usage
    counters rather than keeping its own.

    candidates is capped at _COMPARE_MAX_CANDIDATES regardless of how many
    are passed in, so a misconfigured pool size can't produce a runaway
    single request.
    """
    global _consecutive_rate_errors, _circuit_open, _total_calls, _total_input_tokens, _total_output_tokens
    client = _get_client()
    if client is None or not candidates:
        return None
    if _circuit_open:
        return None

    candidates = candidates[:_COMPARE_MAX_CANDIDATES]
    jpegs = [_to_jpeg(c) for c in candidates]
    valid = [(i, jb) for i, jb in enumerate(jpegs) if jb]
    if len(valid) < 2:
        # Fewer than 2 decodable images — nothing meaningful to compare.
        return None

    model = str(config.app.get("vlm_model", "gpt-4.1-nano"))
    must_show_str = ", ".join(must_show) if must_show else "anything relevant"
    avoid_str = ", ".join(avoid) if avoid else "watermarks, text overlays, cartoons"

    prompt = (
        "You are a footage curator for a documentary video. Below are several "
        "candidate images (numbered starting at 0, in the order shown), all "
        "already individually screened as acceptable. Pick the SINGLE best match "
        "for the narration moment described below.\n\n"
        f'Narration: "{narration}"\n'
        f'Visual intent: "{visual_caption}"\n'
        f'Overall topic: "{video_topic}"\n'
        f"Should show: {must_show_str}\n"
        f"Avoid: {avoid_str}\n\n"
        "Be strict about specificity: if the visual intent names a specific product, "
        "person, or place, prefer the candidate that most precisely matches it over one "
        "that is merely a plausible generic substitute.\n\n"
        'Return JSON only: {"choice": <0-based index of the best image>, "reason": "short string"}'
    )

    content = [{"type": "text", "text": prompt}]
    for i, jb in valid:
        b64 = base64.standard_b64encode(jb).decode("utf-8")
        content.append({"type": "text", "text": f"Image {i}:"})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
        })

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
            max_tokens=100,
        )
        raw = (response.choices[0].message.content or "").strip()
        data = json.loads(raw)
        choice = int(data.get("choice", 0))
        reason = str(data.get("reason", ""))
        if config.app.get("relevance_debug_log", False):
            logger.debug(f"VLM compare: choice={choice} reason={reason!r}")
        with _vlm_stats_lock:
            _consecutive_rate_errors = 0
            _total_calls += 1
            if response.usage:
                _total_input_tokens += response.usage.prompt_tokens or 0
                _total_output_tokens += response.usage.completion_tokens or 0
        valid_indices = {i for i, _ in valid}
        if choice not in valid_indices:
            logger.debug(f"VLM compare returned out-of-range choice {choice}, ignoring")
            return None
        return choice
    except Exception as exc:
        exc_str = str(exc)
        if "429" in exc_str or "rate_limit" in exc_str.lower() or "quota" in exc_str.lower():
            with _vlm_stats_lock:
                _consecutive_rate_errors += 1
                _current_errors = _consecutive_rate_errors
                if _current_errors >= _CIRCUIT_BREAKER_THRESHOLD:
                    _circuit_open = True
            if _current_errors >= _CIRCUIT_BREAKER_THRESHOLD:
                logger.warning(
                    f"VLM circuit breaker tripped after {_current_errors} "
                    f"consecutive rate errors — VLM disabled for this run (fail-open)"
                )
            else:
                logger.warning(f"VLM rate error ({_current_errors}/{_CIRCUIT_BREAKER_THRESHOLD}), fail-open")
        else:
            logger.warning(f"VLM compare failed (fail-open): {exc}")
        return None
