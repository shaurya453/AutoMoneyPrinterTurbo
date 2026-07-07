"""
app/services/relevance.py — CLIP-based image/footage relevance filtering

Scores candidate images and video thumbnails against a text prompt built
from a sentence's search term plus the video's overall topic, so off-topic
search results (e.g. a football player for "scale", sheet music for "score")
can be deprioritized/dropped before they reach the final video.

Uses the "Qdrant/clip-ViT-B-32-vision" and "Qdrant/clip-ViT-B-32-text" ONNX
ports of OpenAI CLIP ViT-B/32 (pure numpy/PIL + onnxruntime, no torch
dependency). Model files (~600MB total) are downloaded on first use and
cached under storage/models/clip-vit-b32/.

Disabled gracefully if onnxruntime/tokenizers aren't installed, the model
fails to load or download, or `relevance_filter_enabled = false` in config —
every function then becomes a no-op (or returns None/original order) so the
pipeline behaves exactly as it did before this module existed.

Calibration: set RELEVANCE_LOG_ONLY=1 to compute and log every candidate's
score (when relevance_debug_log=true) without changing selection order —
mirrors the SKIP_WHISPER=1 dry-run pattern in pipeline.py.
"""

import io
import os
import threading
from typing import Any, List, Optional, Sequence, Tuple

from loguru import logger

from app.config import config
from app.utils import utils

_LOG_ONLY = os.environ.get("RELEVANCE_LOG_ONLY") == "1"

# Qdrant's separate vision/text ONNX ports of openai/clip-vit-base-patch32 —
# the upstream onnx_clip package's own model source (lakera-clip S3 bucket)
# is dead (404), so we load these directly with onnxruntime instead.
_MODEL_BASE_URL = "https://huggingface.co/Qdrant"
_MODEL_FILES = {
    "vision.onnx": f"{_MODEL_BASE_URL}/clip-ViT-B-32-vision/resolve/main/model.onnx",
    "text.onnx": f"{_MODEL_BASE_URL}/clip-ViT-B-32-text/resolve/main/model.onnx",
    "tokenizer.json": f"{_MODEL_BASE_URL}/clip-ViT-B-32-text/resolve/main/tokenizer.json",
}

# Standard CLIP ViT-B/32 image preprocessing (see preprocessor_config.json).
_IMAGE_SIZE = 224
_IMAGE_MEAN = (0.48145466, 0.4578275, 0.40821073)
_IMAGE_STD = (0.26862954, 0.26130258, 0.27577711)

_MAX_TOKENS = 77
_EOT_TOKEN_ID = 49407

# "Junk" anchors used as a negative margin: a candidate must score higher
# against the real visual_caption/prompt than against the worst of these by
# at least `relevance_margin`, or it's rejected. This replaces a fixed
# global threshold -- some sentences simply have weaker candidate pools, and
# a margin against junk anchors adapts to that instead of a hard cutoff.
_JUNK_ANCHORS = [
    "text overlay watermark",
    "unrelated stock photo",
    "blurry low quality image",
    "explicit nudity content",
]

_model = None
_model_load_attempted = False
_model_load_lock = threading.Lock()
_text_embedding_cache: dict = {}


def _model_dir() -> str:
    return utils.storage_dir("models/clip-vit-b32", create=True)


def _download_file(url: str, path: str) -> None:
    import requests

    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    logger.info(f"relevance filter: downloading {os.path.basename(path)} from {url}")
    tmp_path = path + ".part"
    with requests.get(
        url, stream=True, proxies=config.proxy, verify=bool(tls_verify), timeout=(30, 600)
    ) as r:
        r.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
    os.replace(tmp_path, path)


def _ensure_model_files() -> str:
    """Download any missing model files, return the model directory."""
    model_dir = _model_dir()
    for filename, url in _MODEL_FILES.items():
        path = os.path.join(model_dir, filename)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            _download_file(url, path)
    return model_dir


def _get_model():
    global _model, _model_load_attempted
    if _model is not None or _model_load_attempted:
        return _model
    with _model_load_lock:
        # Double-checked: another thread may have loaded while we waited.
        if _model is not None or _model_load_attempted:
            return _model
        _model_load_attempted = True

    if not config.app.get("relevance_filter_enabled", True):
        logger.info("relevance filter disabled (relevance_filter_enabled=false)")
        return None

    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_dir = _ensure_model_files()

        vision_session = ort.InferenceSession(
            os.path.join(model_dir, "vision.onnx"), providers=["CPUExecutionProvider"]
        )
        text_session = ort.InferenceSession(
            os.path.join(model_dir, "text.onnx"), providers=["CPUExecutionProvider"]
        )
        tokenizer = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        tokenizer.enable_padding(
            length=_MAX_TOKENS, pad_id=_EOT_TOKEN_ID, pad_token="<|endoftext|>"
        )
        tokenizer.enable_truncation(max_length=_MAX_TOKENS)

        _model = (vision_session, text_session, tokenizer)
        logger.info("relevance filter: CLIP model loaded")
    except Exception as exc:
        logger.warning(f"relevance filter unavailable, continuing without it: {exc}")
        _model = None
    return _model


def is_available() -> bool:
    """True if the relevance model is enabled and loaded successfully."""
    return _get_model() is not None


def is_log_only() -> bool:
    """True if RELEVANCE_LOG_ONLY=1 -- score/log only, never change selection."""
    return _LOG_ONLY


def build_prompt(search_term: str, video_topic: str = "") -> str:
    """Combine a search term with the video's overall topic for scoring."""
    search_term = (search_term or "").strip()
    video_topic = (video_topic or "").strip()
    if video_topic:
        return f"{search_term}, {video_topic}"
    return search_term


def _embed_text(model, texts: List[str]):
    import numpy as np

    _, text_session, tokenizer = model
    encodings = tokenizer.encode_batch(texts)
    input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
    return text_session.run(
        ["text_embeds"], {"input_ids": input_ids, "attention_mask": attention_mask}
    )[0]


def _embed_image(model, image: "Any"):
    import numpy as np

    vision_session, _, _ = model
    img = image.convert("RGB")
    w, h = img.size
    short = min(w, h)
    scale = _IMAGE_SIZE / short
    new_w, new_h = round(w * scale), round(h * scale)
    img = img.resize((new_w, new_h), resample=3)  # BICUBIC

    left = (new_w - _IMAGE_SIZE) // 2
    top = (new_h - _IMAGE_SIZE) // 2
    img = img.crop((left, top, left + _IMAGE_SIZE, top + _IMAGE_SIZE))

    arr = np.asarray(img).astype(np.float32) / 255.0
    arr = (arr - np.array(_IMAGE_MEAN, dtype=np.float32)) / np.array(_IMAGE_STD, dtype=np.float32)
    arr = arr.transpose(2, 0, 1)[np.newaxis, ...].astype(np.float32)

    return vision_session.run(["image_embeds"], {"pixel_values": arr})[0]


def _text_embedding(model, prompt: str):
    cached = _text_embedding_cache.get(prompt)
    if cached is not None:
        return cached
    emb = _embed_text(model, [prompt])[0]
    _text_embedding_cache[prompt] = emb
    return emb


def _cosine(a, b) -> float:
    import numpy as np

    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def score(prompt: str, image_bytes: bytes) -> Optional[float]:
    """Cosine similarity between `prompt` and the image, or None if scoring
    is unavailable or the image/model failed."""
    model = _get_model()
    if model is None or not image_bytes:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            image_emb = _embed_image(model, img)[0]
        text_emb = _text_embedding(model, prompt)
        return _cosine(image_emb, text_emb)
    except Exception as exc:
        logger.debug(f"relevance scoring failed: {exc}")
        return None


def passes_margin(prompt: str, image_bytes: bytes, margin: float) -> Optional[bool]:
    """True if `prompt`'s score beats the worst _JUNK_ANCHORS score by at
    least `margin`. None if scoring is unavailable (graceful no-op).

    Extracts the image embedding once and reuses it for all comparisons
    (prompt + 4 junk anchors) so the image is decoded and ONNX-inferred
    only a single time instead of 5×.
    """
    model = _get_model()
    if model is None or not image_bytes:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            image_emb = _embed_image(model, img)[0]
    except Exception as exc:
        logger.debug(f"relevance scoring failed: {exc}")
        return None

    def _sim(text: str) -> Optional[float]:
        try:
            return _cosine(image_emb, _text_embedding(model, text))
        except Exception:
            return None

    caption_score = _sim(prompt)
    if caption_score is None:
        return None
    junk_scores = [s for s in (_sim(a) for a in _JUNK_ANCHORS) if s is not None]
    if not junk_scores:
        return None
    return (caption_score - max(junk_scores)) >= margin


def score_frames(prompt: str, frames_bytes: List[bytes], pool: str = "mean") -> Optional[float]:
    """Score each frame against `prompt` and mean- or max-pool the results.
    None if scoring is unavailable or no frame could be scored."""
    scores = [score(prompt, frame) for frame in frames_bytes]
    scores = [s for s in scores if s is not None]
    if not scores:
        return None
    if pool == "max":
        return max(scores)
    return sum(scores) / len(scores)


def passes_margin_frames(
    prompt: str, frames_bytes: List[bytes], margin: float, pool: str = "mean"
) -> Optional[bool]:
    """Frame-pooled analog of passes_margin: pools `prompt` and each junk
    anchor's per-frame scores with `pool`, then applies the margin.

    Embeds all frames once and reuses the embeddings for every text
    comparison (prompt + 4 junk anchors), so each frame is decoded and
    ONNX-inferred only a single time instead of 5×.
    """
    model = _get_model()
    if model is None or not frames_bytes:
        return None

    frame_embs = []
    try:
        from PIL import Image

        for fb in frames_bytes:
            try:
                with Image.open(io.BytesIO(fb)) as img:
                    frame_embs.append(_embed_image(model, img)[0])
            except Exception as exc:
                logger.debug(f"frame embed failed: {exc}")
    except ImportError:
        return None
    if not frame_embs:
        return None

    def _pool(text: str) -> Optional[float]:
        try:
            t_emb = _text_embedding(model, text)
        except Exception:
            return None
        sims = []
        for f_emb in frame_embs:
            try:
                sims.append(_cosine(f_emb, t_emb))
            except Exception:
                pass
        if not sims:
            return None
        return max(sims) if pool == "max" else sum(sims) / len(sims)

    caption_score = _pool(prompt)
    if caption_score is None:
        return None
    junk_pooled = [s for s in (_pool(a) for a in _JUNK_ANCHORS) if s is not None]
    if not junk_pooled:
        return None
    return (caption_score - max(junk_pooled)) >= margin


def rank(
    prompt: str,
    candidates: List[Tuple[Any, bytes]],
    kind: str = "image",
) -> List[Tuple[Any, Optional[float]]]:
    """
    Score each (key, image_bytes) candidate against `prompt`.

    Returns a list of (key, score) sorted descending by score (None scores
    sort last). If the relevance model is unavailable, returns the input
    keys with score=None in their original order. If RELEVANCE_LOG_ONLY is
    set, scores are computed and logged but the original order is preserved
    (for byte-identical calibration runs).
    """
    if not candidates:
        return []

    model = _get_model()
    if model is None:
        return [(key, None) for key, _ in candidates]

    debug = bool(config.app.get("relevance_debug_log", False))
    scored: List[Tuple[Any, Optional[float]]] = []
    for key, image_bytes in candidates:
        s = score(prompt, image_bytes)
        scored.append((key, s))
        if debug:
            logger.debug(f"relevance[{kind}] prompt={prompt!r} score={s} key={key}")

    if _LOG_ONLY:
        return scored

    scored.sort(key=lambda kv: kv[1] if kv[1] is not None else float("-inf"), reverse=True)
    return scored


def embed_image(image_bytes: bytes) -> "Optional[Any]":
    """Return the L2-normalised CLIP image embedding as a numpy array, or None if
    the model is unavailable or the image can't be decoded.

    The returned vector is unit-length so cosine similarity equals the dot product,
    matching the representation used internally by `score()`.
    """
    model = _get_model()
    if model is None or not image_bytes:
        return None
    try:
        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            emb = _embed_image(model, img)[0]
        norm = float(np.linalg.norm(emb))
        return (emb / norm) if norm > 0 else emb
    except Exception as exc:
        logger.debug(f"embed_image failed: {exc}")
        return None


def too_similar(
    embedding: "Any",
    recent: "Sequence[Any]",
    threshold: float,
) -> bool:
    """True if `embedding` has a cosine similarity >= `threshold` to ANY vector in
    `recent`.  Both vectors must be L2-normalised (as returned by `embed_image`)
    so the similarity equals the dot product.  Returns False when `recent` is
    empty or when numpy is unavailable.
    """
    if not recent:
        return False
    try:
        import numpy as np

        for prev in recent:
            if float(np.dot(embedding, prev)) >= threshold:
                return True
    except Exception as exc:
        logger.debug(f"too_similar check failed: {exc}")
    return False
