"""
app/services/pipeline.py — sentence-level documentary pipeline

Orchestrates:
  1. TTS generation (edge_tts via voice.tts)
  2. faster-whisper word-timestamp extraction → per-sentence durations
  3. Per-sentence stock-footage download and trim to exact sentence duration
  4. Sequential clip assembly via video.combine_videos()
  5. Subtitle generation (edge word-timing or whisper fallback)
  6. Final video via video.generate_video()

Entry point: pipeline.start(job_path)
Job JSON is produced by sentence_prep.py.
"""

import json
import math
import os
import re
import subprocess
from collections import deque
from difflib import SequenceMatcher
from typing import Any, List, Optional, Tuple

from loguru import logger

from app.config import config
from app.models.schema import (
    VideoConcatMode,
    VideoAspect,
    VideoParams,
    VideoTransitionMode,
)
from app.services import material, nsfw, relevance, subtitle, video, voice, vlm
from app.utils import utils


# ---------------------------------------------------------------------------
# Whisper sentence-timestamp alignment
# ---------------------------------------------------------------------------

def _norm_token(w: str) -> str:
    return re.sub(r"[^\w]", "", w).lower()


def _get_sentence_timestamps(
    audio_file: str, sentences: list
) -> Tuple[List[Tuple[dict, float, float]], List[Tuple[str, float, float]]]:
    """
    Transcribe audio with faster-whisper and align each sentence to a
    (start_sec, end_sec) span using SequenceMatcher token alignment.

    Unlike word-count advancement, SequenceMatcher handles insertions and
    deletions without accumulating drift across 200+ sentences.

    Returns (sentence_timings, word_timings) where word_timings is the raw
    list of (word, start_sec, end_sec) tuples from Whisper, used to drive
    subtitle word-highlight animation.
    """
    import concurrent.futures

    from faster_whisper import WhisperModel

    model_size = config.whisper.get("model_size", "base")
    device = config.whisper.get("device", "cpu")
    compute_type = config.whisper.get("compute_type", "int8")
    timeout_seconds = float(config.whisper.get("timeout_seconds", 1800))

    logger.info(f"loading whisper model: {model_size} on {device}")
    model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def _transcribe() -> List[Tuple[str, float, float]]:
        segments, _ = model.transcribe(audio_file, word_timestamps=True)
        words: List[Tuple[str, float, float]] = []
        for seg in segments:
            if seg.words:
                for w in seg.words:
                    word = w.word.strip()
                    if word:
                        words.append((word, w.start, w.end))
        return words

    # faster-whisper has no native timeout; run it on a worker thread so a
    # pathological/huge audio file can't hang the pipeline forever. On
    # timeout the thread is left to finish in the background (daemon-ish via
    # shutdown(wait=False)) while we fall back to uniform timestamps.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_transcribe)
    try:
        all_words = future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError:
        executor.shutdown(wait=False)
        raise TimeoutError(f"whisper transcription exceeded {timeout_seconds:g}s")
    executor.shutdown(wait=False)

    logger.info(f"whisper found {len(all_words)} words across {len(sentences)} sentences")

    if not all_words:
        return _uniform_timestamps(sentences, 0.0), []

    # Build expected token sequence: (normalized_token, sentence_idx)
    expected_tokens: List[tuple] = []
    for s_idx, sent in enumerate(sentences):
        for tok in sent["text"].split():
            n = _norm_token(tok)
            if n:
                expected_tokens.append((n, s_idx))

    expected_norm = [t[0] for t in expected_tokens]
    whisper_norm = [_norm_token(w[0]) for w in all_words]

    # Global sequence alignment — no drift, handles insertions/deletions
    matcher = SequenceMatcher(None, expected_norm, whisper_norm, autojunk=False)

    exp_to_whisper: List[Optional[int]] = [None] * len(expected_tokens)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            exp_to_whisper[a + k] = b + k

    # Fill forward: unmatched expected tokens inherit the previous whisper index
    last_w = 0
    for i in range(len(exp_to_whisper)):
        if exp_to_whisper[i] is not None:
            last_w = exp_to_whisper[i]
        else:
            exp_to_whisper[i] = last_w

    # Build per-sentence time spans
    sentence_spans: dict = {}
    for exp_idx, (_, s_idx) in enumerate(expected_tokens):
        w_idx = exp_to_whisper[exp_idx]
        if w_idx is None or w_idx >= len(all_words):
            continue
        _, w_start, w_end = all_words[w_idx]
        if s_idx not in sentence_spans:
            sentence_spans[s_idx] = [w_start, w_end]
        else:
            sentence_spans[s_idx][1] = w_end

    last_end = all_words[-1][2]
    results: List[Tuple[dict, float, float]] = []
    for s_idx, sent in enumerate(sentences):
        if s_idx in sentence_spans:
            start, end = sentence_spans[s_idx]
            results.append((sent, start, end))
        else:
            results.append((sent, last_end, last_end + 2.0))

    return results, all_words


def _uniform_timestamps(
    sentences: list, audio_duration: float
) -> List[Tuple[dict, float, float]]:
    """Fallback: divide audio duration evenly across all sentences."""
    per_sent = audio_duration / max(len(sentences), 1)
    return [
        (s, i * per_sent, (i + 1) * per_sent)
        for i, s in enumerate(sentences)
    ]


# Half of the dissolve duration added to each clip so the crossfade overlap
# does not eat into the sentence's actual visual content. Only applied when
# the total clip count is small enough that combine_videos will actually run
# ffmpeg xfade (see video.XFADE_CLIP_LIMIT) — otherwise this padding would
# never be consumed and would just inflate the final video's duration.
_CROSSFADE_DUR = video._DEFAULT_CROSSFADE_SECONDS
_TRIM_BUFFER = _CROSSFADE_DUR   # exactly cancels the crossfade overlap → zero net drift


# ---------------------------------------------------------------------------
# Clip fetch + trim
# ---------------------------------------------------------------------------

_TRIM_TIMEOUT_SECONDS = 120


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
            # Named-entity: anchor every query; no topic: bare concept only.
            q = f"{concept} {video_topic}".strip() if video_topic else concept
            if q not in ladder:
                ladder.append(q)
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


def _fetch_video_clip(
    sentence: dict,
    sent_duration: float,
    trim_buffer: float,
    source: str,
    video_aspect: VideoAspect,
    clip_idx: int,
    clips_dir: str,
    used_urls: set,
    caption_prompt: str = "",
    query_ladder: Optional[List[str]] = None,
    video_topic: str = "",
    recent_embeddings: Optional[Any] = None,
    dedup_threshold: float = 0.92,
) -> Optional[str]:
    """Download + trim a stock-video clip, verifying each candidate against
    the NSFW pixel gate and a CLIP relevance margin before accepting it.

    Candidates are tried in order (cheap thumbnail-prefiltered by
    `caption_prompt`) for up to `max_video_download_attempts`. Every
    candidate that's downloaded -- whether accepted, NSFW-rejected, or
    relevance-rejected -- is marked in `used_urls` so it's never retried by
    this or a later sentence.

    Returns None if no candidates are found at all, or none pass
    verification within the attempt budget.
    """
    search_terms = query_ladder if query_ladder is not None else _get_visual_concepts(sentence)
    if not search_terms:
        return None

    out_path = os.path.join(clips_dir, f"clip-{clip_idx:04d}.mp4")
    search_fn = (
        material.search_videos_pixabay
        if source == "pixabay"
        else material.search_videos_pexels
    )

    min_duration = max(1, int(sent_duration))
    max_attempts = int(config.app.get("max_video_download_attempts", 3))
    nsfw_frame_samples = int(config.app.get("nsfw_frame_samples", 4))
    relevance_frame_samples = int(config.app.get("relevance_video_frame_samples", 4))
    relevance_pool = config.app.get("relevance_video_pool", "mean")
    margin = float(config.app.get("relevance_margin", 0.02))
    use_relevance = relevance.is_available() and not relevance.is_log_only()
    num_frames = max(nsfw_frame_samples, relevance_frame_samples)
    coverr_enabled = bool(config.app.get("coverr_enabled", True))
    motion_filter_enabled = bool(config.app.get("motion_filter_enabled", True))
    motion_min_score = float(config.app.get("motion_min_score", 0.04))
    must_show = sentence.get("must_show") or []
    avoid = sentence.get("avoid") or []

    def _get_rung_results(term: str) -> list:
        """Fetch candidates for one ladder rung from primary source + Coverr."""
        primary = search_fn(
            search_term=term,
            minimum_duration=min_duration,
            video_aspect=video_aspect,
            prompt=caption_prompt,
        )
        if coverr_enabled:
            coverr = material.search_videos_coverr(
                search_term=term,
                minimum_duration=min_duration,
                video_aspect=video_aspect,
                prompt=caption_prompt,
            )
            combined = primary + coverr
        else:
            combined = primary
        if must_show or avoid:
            combined = material.sort_by_metadata(combined, caption_prompt, must_show, avoid)
        return combined

    # search_terms is a subject-anchored query ladder: each entry is
    # "{visual_concept} {video_topic}", specific concept first and
    # progressively broader, with a final bare-video_topic rung as the
    # safety net (see _build_query_ladder / AGENT_GUIDE.md). Try one fresh
    # candidate per query, in order, round-robin -- so a query that's empty
    # or gets its candidate rejected falls through to the next rung instead
    # of burning the whole attempt budget on one query. Each query's results
    # (and thumbnail reranking) are only fetched lazily, the first time that
    # query is actually reached.
    term_results: List[Optional[list]] = [None] * len(search_terms)
    term_pos = [0] * len(search_terms)

    # Dedup fallback: if every candidate that passes NSFW+relevance is rejected
    # by the dedup gate, we save the first one rather than discarding it, so the
    # slot can still be filled instead of coming back empty.
    _dedup_fallback_tmp = os.path.join(clips_dir, f"clip-{clip_idx:04d}-dfb.mp4")
    dedup_fallback_emb = None

    attempts = 0
    while attempts < max_attempts:
        progressed = False
        for t_idx, term in enumerate(search_terms):
            if attempts >= max_attempts:
                break
            if term_results[t_idx] is None:
                term_results[t_idx] = _get_rung_results(term)
            items = term_results[t_idx]
            pos = term_pos[t_idx]
            while pos < len(items) and items[pos].url in used_urls:
                pos += 1
            term_pos[t_idx] = pos
            if pos >= len(items):
                continue
            candidate = items[pos]
            term_pos[t_idx] = pos + 1
            progressed = True
            attempts += 1
            used_urls.add(candidate.url)

            downloaded = material.save_video(
                video_url=candidate.url,
                save_dir=utils.storage_dir("cache_videos"),
            )
            if not downloaded:
                logger.warning(f"clip {clip_idx}: video download failed for {candidate.url}")
                continue

            ok = _trim_clip(downloaded, sent_duration + trim_buffer, out_path)
            if not ok:
                logger.warning(f"clip {clip_idx}: trim failed for {candidate.url}")
                continue

            frames = []
            if nsfw.is_available() or use_relevance:
                frames = nsfw.sample_frame_bytes(out_path, num_frames=num_frames)
                if not frames:
                    logger.warning(
                        f"clip {clip_idx}: could not extract frames for verification, "
                        f"skipping candidate: {candidate.url}"
                    )
                    try:
                        os.remove(out_path)
                    except Exception:
                        pass
                    continue

            if nsfw.is_available() and not nsfw.passes(nsfw.is_nsfw_frames(frames)):
                logger.info(f"clip {clip_idx}: rejected NSFW video candidate: {candidate.url}")
                try:
                    os.remove(out_path)
                except Exception:
                    pass
                continue

            if vlm.is_enabled() and frames:
                mid_frame = frames[len(frames) // 2]
                if not vlm.passes(vlm.verify_image(
                    mid_frame,
                    sentence.get("text", ""),
                    sentence.get("visual_caption", ""),
                    video_topic,
                    must_show,
                    avoid,
                )):
                    logger.info(f"clip {clip_idx}: rejected by VLM: {candidate.url}")
                    try:
                        os.remove(out_path)
                    except Exception:
                        pass
                    continue

            # Motion gate — reject static clips (photographs exported as MP4,
            # frozen zooms) before spending a relevance or dedup check on them.
            if motion_filter_enabled and len(frames) >= 2:
                try:
                    import io
                    import numpy as np
                    from PIL import Image as _PILImage
                    arrays = [
                        np.array(_PILImage.open(io.BytesIO(fb)).convert("RGB"), dtype=float)
                        for fb in frames
                    ]
                    max_diff = max(
                        float(np.mean(np.abs(arrays[i] - arrays[i - 1]))) / 255.0
                        for i in range(1, len(arrays))
                    )
                    if max_diff < motion_min_score:
                        logger.info(
                            f"clip {clip_idx}: rejected static clip "
                            f"(max_frame_diff={max_diff:.3f} < {motion_min_score}): {candidate.url}"
                        )
                        try:
                            os.remove(out_path)
                        except Exception:
                            pass
                        continue
                except Exception as _me:
                    logger.debug(f"motion check skipped: {_me}")

            if use_relevance:
                margin_ok = relevance.passes_margin_frames(
                    caption_prompt, frames, margin, pool=relevance_pool
                )
                if margin_ok is False:
                    logger.info(
                        f"clip {clip_idx}: rejected video candidate (relevance margin): {candidate.url}"
                    )
                    try:
                        os.remove(out_path)
                    except Exception:
                        pass
                    continue

            if recent_embeddings is not None and frames:
                dedup_emb = relevance.embed_image(frames[0])
                if dedup_emb is not None:
                    if relevance.too_similar(dedup_emb, recent_embeddings, dedup_threshold):
                        logger.info(
                            f"clip {clip_idx}: rejected near-duplicate video candidate: {candidate.url}"
                        )
                        # Save first dedup-rejected clip as last-resort fallback.
                        if not os.path.exists(_dedup_fallback_tmp):
                            try:
                                os.rename(out_path, _dedup_fallback_tmp)
                                dedup_fallback_emb = dedup_emb
                            except Exception:
                                try:
                                    os.remove(out_path)
                                except Exception:
                                    pass
                        else:
                            try:
                                os.remove(out_path)
                            except Exception:
                                pass
                        continue
                    # Accepted — clean up any saved fallback.
                    if os.path.exists(_dedup_fallback_tmp):
                        try:
                            os.remove(_dedup_fallback_tmp)
                        except Exception:
                            pass
                    recent_embeddings.append(dedup_emb)

            return out_path

        if not progressed:
            break

    # Use the dedup fallback if every candidate that passed NSFW+relevance
    # was rejected only because of visual similarity to recent shots.
    if os.path.exists(_dedup_fallback_tmp):
        try:
            os.rename(_dedup_fallback_tmp, out_path)
            logger.warning(
                f"clip {clip_idx}: all candidates were near-duplicates; "
                f"using best-passing dedup fallback"
            )
            if dedup_fallback_emb is not None and recent_embeddings is not None:
                recent_embeddings.append(dedup_fallback_emb)
            return out_path
        except Exception as _fe:
            logger.debug(f"dedup fallback rename failed: {_fe}")
            try:
                os.remove(_dedup_fallback_tmp)
            except Exception:
                pass

    logger.warning(
        f"clip {clip_idx}: no video candidate passed verification for "
        f"{search_terms} (tried {attempts}/{max_attempts} attempts)"
    )
    return None


def _fetch_image_clip(
    sentence: dict,
    sent_duration: float,
    trim_buffer: float,
    video_aspect: VideoAspect,
    clip_idx: int,
    clips_dir: str,
    used_urls: set,
    caption_prompt: str = "",
    query_ladder: Optional[List[str]] = None,
    video_topic: str = "",
    source_order: Optional[List[str]] = None,
    recent_embeddings: Optional[Any] = None,
    dedup_threshold: float = 0.92,
) -> Optional[str]:
    """Download an image and render a Ken Burns clip. None if no image found.

    used_urls is mutated in-place on success (see material.download_image).
    Every downloaded candidate passes the NSFW gate and a CLIP relevance
    margin against `caption_prompt` inside material.download_image.
    """
    search_terms = query_ladder if query_ladder is not None else _get_visual_concepts(sentence)
    if not search_terms:
        return None

    width, height = VideoAspect(video_aspect).to_resolution()
    out_path = os.path.join(clips_dir, f"clip-{clip_idx:04d}.mp4")

    image_path = material.download_image(
        search_terms=search_terms,
        source_order=source_order,
        save_dir=utils.storage_dir("cache_images"),
        used_urls=used_urls,
        caption_prompt=caption_prompt,
        recent_embeddings=recent_embeddings,
        dedup_threshold=dedup_threshold,
        narration=sentence.get("text", ""),
        visual_caption=sentence.get("visual_caption", ""),
        video_topic=video_topic,
        must_show=sentence.get("must_show") or [],
        avoid=sentence.get("avoid") or [],
    )
    if not image_path:
        return None

    result = video.render_ken_burns_clip(
        image_path=image_path,
        duration=sent_duration + trim_buffer,
        width=width,
        height=height,
        output_path=out_path,
    )
    if not result:
        logger.warning(f"clip {clip_idx}: Ken Burns render failed")
        return None

    return out_path


def _fetch_clip(
    sentence: dict,
    sent_duration: float,
    trim_buffer: float,
    source: str,
    video_aspect: VideoAspect,
    clip_idx: int,
    clips_dir: str,
    used_urls: set,
    fallback_terms: Optional[List[str]] = None,
    is_image_override: Optional[bool] = None,
    video_topic: str = "",
    video_type: str = "thematic",
    recent_embeddings: Optional[Any] = None,
    dedup_threshold: float = 0.92,
) -> Optional[Tuple[str, bool]]:
    """
    Fetch a clip (stock video or Ken Burns image) for one sentence.

    Every search query is built by `_build_query_ladder(video_topic,
    visual_concepts)`: each local visual concept combined with the
    persistent `video_topic` subject, specific-first, with a final
    bare-`video_topic` rung as the safety net -- broadening never drops the
    subject.

    Routing is governed by `sentence['content_track']`:
      - "named" (a specific product/person/place/event): always fetched as
        an image via a Serper-first `source_order` (named_track_image_source_order),
        regardless of `media_type` -- generic stock libraries rarely have
        named entities, but Google Images often does.
      - "broll" (default): existing `media_type` / `is_image_override`
        -driven primary choice, using the default image source order.

    If the primary attempt finds nothing, falls back to the other media type
    using the same query ladder. If that also fails and `fallback_terms` is
    provided, retries both media types using a topic-wide pool of *concepts*
    (excluding this sentence's own concepts), rebuilding the ladder from
    those, so the sentence can still get *different* footage instead of
    contributing nothing.

    used_urls is mutated in-place: the chosen clip's source is added so
    subsequent sentences won't reuse the same footage/image.

    CLIP relevance ranking (the NSFW gate is independent of this) scores
    candidates against `sentence['visual_caption']` (or the first visual
    concept if missing) combined with `video_topic`, so even an
    on-topic-sounding caption is still anchored to the video's overall
    subject. Serper results pass through the same NSFW gate, relevance
    margin, and denylist as every other provider.

    Returns (clip_path, used_image) or None if nothing was found at all.
    """
    visual_concepts = _get_visual_concepts(sentence)
    query_ladder = _build_query_ladder(video_topic, visual_concepts, video_type)
    if not query_ladder:
        logger.warning(f"clip {clip_idx}: no visual concepts or video_topic provided")
        return None

    # Always anchor the relevance prompt to video_topic, even when
    # visual_caption is present -- a caption like "a person looking
    # surprised" is otherwise scored in isolation and will happily match
    # totally off-topic "surprised" stock footage (e.g. a pregnancy test
    # reveal) for a grocery-industry documentary.
    visual_caption = sentence.get("visual_caption", "")
    caption_prompt = relevance.build_prompt(
        visual_caption or (visual_concepts[0] if visual_concepts else video_topic),
        video_topic,
    )

    content_track = sentence.get("content_track", "broll")
    named_source_order = config.app.get(
        "named_track_image_source_order",
        ["serper", "duckduckgo", "wikimedia", "pexels", "pixabay", "unsplash"],
    )

    args_video = (sentence, sent_duration, trim_buffer, source, video_aspect, clip_idx, clips_dir, used_urls, caption_prompt, query_ladder)
    args_image = (sentence, sent_duration, trim_buffer, video_aspect, clip_idx, clips_dir, used_urls, caption_prompt, query_ladder)
    dedup_kw = {"recent_embeddings": recent_embeddings, "dedup_threshold": dedup_threshold}
    topic_kw = {"video_topic": video_topic}

    if content_track == "named":
        # Named/specific subjects are always served as images via Serper,
        # regardless of media_type or the image-ratio cap's preference flip.
        result = _fetch_image_clip(*args_image, source_order=named_source_order, **dedup_kw, **topic_kw)
        primary, fallback_name, is_image = "image", "video", True
    elif video_type == "named_entity":
        # For named_entity broll, respect media_type but route image requests
        # through named_source_order (Serper first) — stock libraries can't
        # distinguish specific product models or persons. Video requests try
        # stock video first; image fallback already uses named_source_order below.
        is_image = (
            is_image_override
            if is_image_override is not None
            else sentence.get("media_type") == "image"
        )
        if is_image:
            result = _fetch_image_clip(*args_image, source_order=named_source_order, **dedup_kw, **topic_kw)
            primary, fallback_name = "image", "video"
        else:
            result = _fetch_video_clip(*args_video, **dedup_kw, **topic_kw)
            primary, fallback_name = "video", "image"
    else:
        is_image = (
            is_image_override
            if is_image_override is not None
            else sentence.get("media_type") == "image"
        )
        if is_image:
            result = _fetch_image_clip(*args_image, source_order=None, **dedup_kw, **topic_kw)
            primary, fallback_name = "image", "video"
        else:
            result = _fetch_video_clip(*args_video, **dedup_kw, **topic_kw)
            primary, fallback_name = "video", "image"

    if result:
        return result, is_image

    logger.warning(f"clip {clip_idx}: no {primary} found for {query_ladder} — trying {fallback_name} fallback")
    if fallback_name == "video":
        result = _fetch_video_clip(*args_video, **dedup_kw, **topic_kw)
    else:
        # For named_entity videos, broll image fallbacks also route through
        # the named-track source order (Serper first) — generic broll image
        # sources (DuckDuckGo, Unsplash, etc.) won't have the specific entity
        # and would return unrelated content just as badly as generic video did.
        use_named_order = content_track == "named" or video_type == "named_entity"
        result = _fetch_image_clip(
            *args_image,
            source_order=(named_source_order if use_named_order else None),
            **dedup_kw,
            **topic_kw,
        )
    if result:
        return result, (fallback_name == "image")

    if fallback_terms:
        own = set(visual_concepts)
        extra_concepts = [c for c in fallback_terms if c not in own]
        if extra_concepts:
            fb_ladder = _build_query_ladder(video_topic, extra_concepts, video_type)
            fb_sentence = dict(sentence)
            fb_sentence["visual_concepts"] = extra_concepts
            fb_caption_prompt = relevance.build_prompt(extra_concepts[0], video_topic)
            args_video_fb = (fb_sentence, sent_duration, trim_buffer, source, video_aspect, clip_idx, clips_dir, used_urls, fb_caption_prompt, fb_ladder)
            args_image_fb = (fb_sentence, sent_duration, trim_buffer, video_aspect, clip_idx, clips_dir, used_urls, fb_caption_prompt, fb_ladder)
            video_fb = _fetch_video_clip(*args_video_fb, **dedup_kw, **topic_kw)
            if video_fb:
                logger.info(f"clip {clip_idx}: used topic-wide fallback concepts {extra_concepts[:3]}")
                return video_fb, False
            fb_named_order = named_source_order if video_type == "named_entity" else None
            image_fb = _fetch_image_clip(*args_image_fb, source_order=fb_named_order, **dedup_kw, **topic_kw)
            if image_fb:
                logger.info(f"clip {clip_idx}: used topic-wide fallback concepts {extra_concepts[:3]}")
                return image_fb, True

    logger.warning(f"clip {clip_idx}: no clip found for {query_ladder} (tried both media types, plus fallback concepts)")
    return None


# ---------------------------------------------------------------------------
# BGM helpers
# ---------------------------------------------------------------------------

def _resolve_bgm(job: dict) -> Tuple[str, str]:
    """
    Resolve BGM source for this job and return (bgm_type, bgm_file) for VideoParams.

    Priority:
      1. bgm_search_term  — search Pixabay online and download; falls back to random local
      2. bgm_file = "random" — pick a random file from resource/songs/
      3. bgm_file = "none" / "" — no BGM
      4. bgm_file = "/path/or/name" — explicit local file
    """
    search_term = job.get("bgm_search_term", "").strip()
    if search_term:
        logger.info(f"searching for BGM online: '{search_term}'")
        downloaded = material.download_bgm(
            search_term=search_term,
            save_dir=utils.storage_dir("cache_bgm"),
        )
        if downloaded:
            return "downloaded", downloaded
        logger.warning("online BGM search failed — falling back to random local file")
        return "random", ""

    raw = job.get("bgm_file", "random")
    if not raw or raw.lower() in ("", "none"):
        return "", ""
    if raw == "random":
        return "random", ""
    return "", raw  # explicit local path or filename


# ---------------------------------------------------------------------------
# Subtitle writer
# ---------------------------------------------------------------------------

def _timings_to_srt(timings: list, subtitle_path: str) -> None:
    """Write sentence-level Whisper-aligned timestamps directly as an SRT file."""
    lines = []
    idx = 1
    for sent, start, end in timings:
        text = sent.get("text", "").strip()
        if not text:
            continue
        lines.append(utils.text_to_srt(idx, text, start, end))
        idx += 1
    content = "\n".join(lines)
    if content:
        with open(subtitle_path, "w", encoding="utf-8") as f:
            f.write(content + "\n")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def start(job_path: str) -> Optional[dict]:
    """
    Run the full sentence-level documentary pipeline from a job JSON file.
    Returns a result dict with output paths on success, None on failure.
    """
    with open(job_path, "r", encoding="utf-8") as fh:
        job = json.load(fh)

    vlm.reset_usage()

    task_id = job.get("task_id") or utils.get_uuid()
    sentences: list = job.get("sentences", [])
    video_script: str = job.get("video_script", "").strip()
    video_topic: str = job.get("video_topic", "") or job.get("video_title", "")
    video_type: str = job.get("video_type", "thematic")

    dedup_enabled: bool = bool(config.app.get("dedup_enabled", True))
    dedup_threshold: float = float(config.app.get("dedup_similarity_threshold", 0.92))
    dedup_lookback: int = int(config.app.get("dedup_lookback_window", 8))
    recent_embs: Optional[Any] = deque(maxlen=dedup_lookback) if dedup_enabled else None

    if not video_script:
        logger.error("job.video_script is empty — nothing to do")
        return None
    if not sentences:
        logger.error("job.sentences is empty — run sentence_prep.py first")
        return None

    work_dir = os.path.dirname(os.path.abspath(job_path))
    os.makedirs(work_dir, exist_ok=True)

    # Intermediate files go here; final outputs stay in work_dir.
    temp_dir = os.path.join(work_dir, "temp")
    os.makedirs(temp_dir, exist_ok=True)

    logger.info(f"pipeline start | task={task_id} | folder={os.path.basename(work_dir)} | sentences={len(sentences)}")

    # ------------------------------------------------------------------ #
    # 1. TTS                                                               #
    # ------------------------------------------------------------------ #
    audio_file = os.path.join(work_dir, "audio.mp3")
    voice_name = voice.parse_voice_name(job.get("voice_name", "en-US-AriaNeural"))
    voice_rate = float(job.get("voice_rate", 1.0))

    # Allow reuse of an existing narration file to avoid re-running TTS when retrying.
    sub_maker = None
    if os.path.exists(audio_file):
        logger.info(f"reusing existing audio → {audio_file}")
        audio_duration = math.ceil(voice.get_audio_duration(audio_file))
    else:
        logger.info(f"TTS: voice={voice_name}, rate={voice_rate}")
        sub_maker = voice.tts(
            text=video_script,
            voice_name=voice_name,
            voice_rate=voice_rate,
            voice_file=audio_file,
        )
        if sub_maker is None:
            logger.error("TTS failed — check voice name and network connectivity")
            return None
        audio_duration = math.ceil(voice.get_audio_duration(sub_maker))
    logger.info(f"audio duration: {audio_duration}s")

    # ------------------------------------------------------------------ #
    # 2. Sentence timestamps via faster-whisper                           #
    # ------------------------------------------------------------------ #
    logger.info("aligning sentences with faster-whisper")

    # Graphic sentences have no spoken text — exclude them from Whisper alignment,
    # then re-insert with placeholder timestamps so the rest of the pipeline is unaffected.
    _graphic_idxs: set = {i for i, s in enumerate(sentences) if s.get("content_track") == "graphic"}
    _narration_sents = [s for i, s in enumerate(sentences) if i not in _graphic_idxs]

    word_timings: List[Tuple[str, float, float]] = []
    if os.environ.get("SKIP_WHISPER") == "1":
        logger.info("SKIP_WHISPER=1 → using uniform distribution for timings")
        _narration_timings = _uniform_timestamps(_narration_sents, audio_duration)
    else:
        try:
            _narration_timings, word_timings = _get_sentence_timestamps(audio_file, _narration_sents)
        except Exception as exc:
            logger.warning(f"whisper failed ({exc}), falling back to uniform distribution")
            _narration_timings = _uniform_timestamps(_narration_sents, audio_duration)

    # Re-merge graphic sentences back at their original positions with (0.0, 0.0) placeholders.
    # Pass 1 below overrides their duration from sent["duration"].
    timings = []
    _narration_iter = iter(_narration_timings)
    for _i, _s in enumerate(sentences):
        if _i in _graphic_idxs:
            timings.append((_s, 0.0, 0.0))
        else:
            try:
                timings.append(next(_narration_iter))
            except StopIteration:
                timings.append((_s, audio_duration, audio_duration))

    # ------------------------------------------------------------------ #
    # 3. Per-sentence clip download + trim                                #
    # ------------------------------------------------------------------ #
    video_aspect = VideoAspect(job.get("video_aspect", "16:9"))
    video_source: str = job.get("video_source", "pexels")
    clips_dir = os.path.join(temp_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    ordered_clips: List[str] = []
    used_urls: set = set()  # tracks clip URLs used this run to prevent reuse
    total_sentences = len(timings)

    # Warn early if a graphic sentence is at the very start or end — combine_videos
    # fills only to audio_duration, so end-positioned graphics are silently dropped;
    # start-positioned graphics consume visual time before any narration begins.
    if timings and timings[0][0].get("content_track") == "graphic":
        logger.warning("graphic sentence at position 0 — it will consume visual time before narration begins")
    if timings and timings[-1][0].get("content_track") == "graphic":
        logger.warning(
            f"graphic sentence at position {len(timings)-1} (last) — "
            "combine_videos fills to audio_duration; this clip will likely be dropped"
        )
    # Video sentences: download multiple ~4-second clips to cover the sentence
    # duration without repeating footage.
    # Image sentences: capped at _IMAGE_CLIP_MAX seconds each — long sentences
    # split into multiple clips so a single still never holds for the full
    # narration. Each sub-clip fetches a different image (used_urls dedupes).
    _CLIP_TARGET = 4.0
    _MIN_VISUAL_DUR = 2.0  # absolute floor for any single clip's duration
    _MIN_ANIM_DUR = 2.5  # conservative cover for all 4 title-card animation variants
    _IMAGE_CLIP_MAX = 7.0  # max seconds per individual Ken Burns image clip

    # ---- Pass 1: plan per-sentence clip durations using absolute resync.
    #
    # Each sentence's footage starts no earlier than max(when its narration
    # begins, when the previous sentence's footage ends) -- guaranteeing
    # footage never precedes the VO -- and runs until the next sentence's
    # narration begins (or audio_duration for the last sentence), floored at
    # num_clips * _MIN_VISUAL_DUR. This keeps the cumulative footage timeline
    # tracking absolute whisper timestamps directly, instead of drifting via
    # per-sentence duration sums.
    t0 = timings[0][1]
    rel_starts = [max(0.0, t_start - t0) for _, t_start, _ in timings]

    cum_end = 0.0
    clip_plans = []
    for idx, (sent, t_start, t_end) in enumerate(timings):
        sent_audio_dur = max(0.0, t_end - t_start)
        is_image = sent.get("media_type") == "image"

        start_k = max(rel_starts[idx], cum_end)
        if idx + 1 < len(timings):
            next_rel = rel_starts[idx + 1]
            # Pure graphic sentences carry (0.0, 0.0) placeholder timestamps.
            # Using one as target_end_k collapses raw_total to zero, forcing the
            # _MIN_VISUAL_DUR floor and pushing cum_end ahead of the audio timeline.
            # Skip past any contiguous pure graphics to find the next narrated boundary.
            if timings[idx + 1][0].get("content_track") == "graphic":
                for j in range(idx + 2, len(timings)):
                    if rel_starts[j] > 0:
                        next_rel = rel_starts[j]
                        break
            target_end_k = max(next_rel, start_k)
        else:
            target_end_k = max(audio_duration, start_k)
        raw_total = target_end_k - start_k

        if sent_audio_dur <= 0:
            num_clips = 1
        elif is_image:
            # Cap each image slot so long sentences show multiple images
            # rather than freezing on a single still for the full duration.
            if raw_total > _IMAGE_CLIP_MAX:
                ideal = max(1, round(raw_total / _IMAGE_CLIP_MAX))
                max_by_min = max(1, int(raw_total // _MIN_VISUAL_DUR))
                num_clips = max(1, min(ideal, max_by_min))
            else:
                num_clips = 1
        else:
            basis = raw_total if raw_total > 0 else sent_audio_dur
            ideal_clips = max(1, round(basis / _CLIP_TARGET))
            max_clips_by_min_dur = max(1, int(basis // _MIN_VISUAL_DUR))
            num_clips = max(1, min(ideal_clips, max_clips_by_min_dur))

        floor = num_clips * _MIN_VISUAL_DUR
        # Narrated graphics (graphic_type + text, not pure graphic) must use the
        # Whisper-derived duration so cum_end tracks the audio timeline exactly.
        # Applying _MIN_VISUAL_DUR floor here would push cum_end past the audio
        # position and cause every subsequent clip to drift late.
        is_narrated_graphic = bool(sent.get("graphic_type") and sent.get("text"))
        if is_narrated_graphic:
            total = max(raw_total, 1.0)    # 1s Revideo stability floor only
        else:
            total = max(floor, raw_total)
        durations = [total / num_clips] * num_clips
        cum_end = start_k + total

        # Graphic clips use their explicit duration, not the Whisper-derived window.
        if sent.get("content_track") == "graphic":
            total = float(sent.get("duration", 5.0))
            durations = [total]
            is_image = False
            cum_end = start_k + total

        clip_plans.append({
            "sent": sent,
            "is_image": is_image,
            "durations": durations,
            "sent_audio_dur": sent_audio_dur,
        })

    # Extend the last clip's slot by outro_tail so the combined video naturally
    # reaches audio_duration + outro_tail without the outro step having to loop
    # the last clip from the beginning (which caused visible repetition).
    _OUTRO_TAIL = 2.0
    if clip_plans:
        clip_plans[-1]["durations"][-1] += _OUTRO_TAIL

    # Crossfade trim-buffer padding is only consumed when combine_videos will
    # actually run ffmpeg xfade (see video.XFADE_CLIP_LIMIT) — otherwise it
    # would just inflate the final video's duration beyond the narration.
    total_planned_clips = sum(len(p["durations"]) for p in clip_plans)
    apply_trim_buffer = total_planned_clips <= video.XFADE_CLIP_LIMIT
    trim_buffer = _TRIM_BUFFER if apply_trim_buffer else 0.0
    logger.info(
        f"planned {total_planned_clips} clips total "
        f"({'with' if apply_trim_buffer else 'without'} crossfade trim buffer; "
        f"xfade limit={video.XFADE_CLIP_LIMIT})"
    )

    # Topic-wide pool of visual concepts (deduped, order-preserving), used as
    # a last-resort fallback so a sentence whose own concepts are exhausted
    # can still pull *different* footage instead of leaving a gap that
    # combine_videos would later fill by repeating clips.
    _all_concepts: List[str] = []
    _seen_concepts: set = set()
    for plan in clip_plans:
        for concept in _get_visual_concepts(plan["sent"]):
            if concept not in _seen_concepts:
                _seen_concepts.add(concept)
                _all_concepts.append(concept)

    # ---- Pass 2: fetch clips according to the plan ----
    # Soft cap on the fraction of clips that may come from still images —
    # backstops the enrichment agent's media_type choices regardless of how
    # well it followed AGENT_GUIDE.md's image-ratio guidance.
    max_image_ratio = float(job.get("max_image_ratio", config.app.get("max_image_ratio", 1.0)))
    image_clip_count = 0
    video_clip_count = 0

    # Concept-frequency guard: when the enrichment agent mode-collapses and
    # assigns the same visual_concepts to many sentences, the same search terms
    # exhaust their candidate pools quickly and produce visually monotone clips.
    # Track per-concept usage and substitute fresh alternatives from the job's
    # full concept pool once a concept has been used max_concept_reuse times.
    from collections import Counter as _Counter
    concept_usage: _Counter = _Counter()
    max_concept_reuse = int(config.app.get("max_concept_reuse", 2))

    clip_counter = 0  # unique index for clip filenames across all sentences
    obtained_duration = 0.0  # sum of planned durations that yielded a clip
    for idx, plan in enumerate(clip_plans):
        sent = plan["sent"]
        durations = plan["durations"]
        is_image = plan["is_image"]
        preview = (sent.get("text") or sent.get("graphic_type", "graphic"))[:60]
        logger.info(
            f"[{idx+1}/{total_sentences}] {plan['sent_audio_dur']:.2f}s audio → "
            f"{len(durations)} clip(s) {'(image)' if is_image else ''} — {preview}"
        )

        got_any = False

        # Graphic sentences are rendered by Revideo, not fetched from stock sources.
        if sent.get("content_track") == "graphic":
            from app.services import graphics as _graphics
            gfx_dur = durations[0]
            gfx_path = os.path.join(clips_dir, f"clip-{clip_counter:04d}.mp4")
            clip_counter += 1
            w, h = video_aspect.to_resolution()
            rendered = _graphics.render_graphic_clip(
                graphic_type=sent.get("graphic_type", "title_card"),
                out_path=gfx_path,
                duration=gfx_dur,
                width=w,
                height=h,
                fps=30,
                variables=sent.get("variables", {}),
                style=sent.get("variables", {}).get("style"),
            )
            if rendered:
                ordered_clips.append(rendered)
                got_any = True
                obtained_duration += gfx_dur
                video_clip_count += 1
            else:
                logger.warning(f"sentence {idx+1}: graphic render failed — skipping")
            if not got_any:
                logger.warning(f"sentence {idx+1}: skipping — no clip available")
            continue

        # Narrated graphic — sentence has real VO text AND graphic_type set.
        # Render a Revideo clip sized to the full Whisper-derived sentence duration
        # instead of fetching stock footage.  If the render fails, falls through to
        # the normal footage fetch so the sentence is never left visually empty.
        if sent.get("graphic_type") and sent.get("text"):
            from app.services import graphics as _graphics
            whisper_dur = sum(durations)       # exact audio slot from Whisper
            render_dur = max(whisper_dur, _MIN_ANIM_DUR)  # guarantee animation completes
            gfx_path = os.path.join(clips_dir, f"clip-{clip_counter:04d}.mp4")
            clip_counter += 1
            w, h = video_aspect.to_resolution()
            rendered = _graphics.render_graphic_clip(
                graphic_type=sent["graphic_type"],
                out_path=gfx_path,
                duration=render_dur,
                width=w,
                height=h,
                fps=30,
                variables=sent.get("variables", {}),
                style=sent.get("variables", {}).get("style"),
            )
            if rendered and render_dur > whisper_dur:
                # Clip rendered longer than audio slot to let animation complete.
                # Trim back to the audio slot so the timeline stays in sync.
                trim_path = gfx_path.replace('.mp4', '-t.mp4')
                if _trim_clip(rendered, whisper_dur, trim_path):
                    os.replace(trim_path, gfx_path)
                else:
                    logger.warning(
                        f"sentence {idx+1}: anim trim failed — using full {render_dur:.2f}s clip"
                    )
            if rendered:
                ordered_clips.append(rendered)
                got_any = True
                obtained_duration += whisper_dur
                video_clip_count += 1
                continue  # graphic is the visual — skip footage fetch
            else:
                logger.warning(
                    f"sentence {idx+1}: narrated graphic render failed — falling back to footage"
                )
                # Fall through to footage fetch below

        # Concept-frequency guard: if all of this sentence's concepts are stale
        # (used >= max_concept_reuse times), substitute fresh ones from the
        # topic-wide pool so the search ladder broadens instead of cycling.
        own_concepts = _get_visual_concepts(sent)
        if (own_concepts
                and sent.get("content_track", "broll") != "named"
                and all(concept_usage[c] >= max_concept_reuse for c in own_concepts)):
            fresh = [c for c in _all_concepts if concept_usage[c] < max_concept_reuse]
            if fresh:
                sent = {**sent, "visual_concepts": fresh[:2]}
                logger.info(
                    f"clip {clip_counter}: concepts {own_concepts} stale "
                    f"(used >={max_concept_reuse}x) — substituting {fresh[:2]}"
                )
        for c in own_concepts:
            concept_usage[c] += 1

        for clip_duration in durations:
            is_image_override = None
            if is_image and sent.get("content_track", "broll") != "named":
                total_so_far = image_clip_count + video_clip_count
                projected_ratio = (image_clip_count + 1) / (total_so_far + 1)
                if projected_ratio > max_image_ratio:
                    is_image_override = False
                    logger.info(
                        f"clip {clip_counter}: image ratio cap reached "
                        f"({image_clip_count}/{total_so_far or 1} so far) — trying video first"
                    )

            fetched = _fetch_clip(
                sentence=sent,
                sent_duration=clip_duration,
                trim_buffer=trim_buffer,
                source=video_source,
                video_aspect=video_aspect,
                clip_idx=clip_counter,
                clips_dir=clips_dir,
                used_urls=used_urls,
                fallback_terms=_all_concepts,
                is_image_override=is_image_override,
                video_topic=video_topic,
                video_type=video_type,
                recent_embeddings=recent_embs,
                dedup_threshold=dedup_threshold,
            )
            clip_counter += 1
            if fetched:
                clip_path, used_image = fetched
                ordered_clips.append(clip_path)
                got_any = True
                obtained_duration += clip_duration
                if used_image:
                    image_clip_count += 1
                else:
                    video_clip_count += 1

        if not got_any:
            logger.warning(f"sentence {idx+1}: skipping — no clip available")

    if not ordered_clips:
        logger.error("no clips obtained for any sentence — aborting")
        return None

    logger.info(f"obtained {len(ordered_clips)}/{total_sentences} clips")
    _total_clips = image_clip_count + video_clip_count
    if _total_clips:
        logger.info(
            f"clip mix: {video_clip_count} video / {image_clip_count} image "
            f"({image_clip_count / _total_clips:.0%} image)"
        )

    # ---- Gap-fill: cover any shortfall with extra unique clips. combine_videos
    # never repeats footage, so anything still missing after this is covered by
    # the outro's frozen-last-frame extension instead. ----
    if obtained_duration < audio_duration - 0.5 and _all_concepts:
        max_gap_fill_attempts = len(_all_concepts) * 3 + 20
        attempts = 0
        logger.info(
            f"obtained {obtained_duration:.2f}s of {audio_duration:.2f}s — gap-filling with extra clips"
        )
        while obtained_duration < audio_duration - 0.5 and attempts < max_gap_fill_attempts:
            concept = _all_concepts[attempts % len(_all_concepts)]
            attempts += 1
            filler_sentence = {
                "visual_concepts": [concept],
                "media_type": "video",
                "content_track": "broll",
                "visual_caption": concept,
            }
            fetched = _fetch_clip(
                sentence=filler_sentence,
                sent_duration=_CLIP_TARGET,
                trim_buffer=trim_buffer,
                source=video_source,
                video_aspect=video_aspect,
                clip_idx=clip_counter,
                clips_dir=clips_dir,
                used_urls=used_urls,
                video_topic=video_topic,
                video_type=video_type,
                recent_embeddings=recent_embs,
                dedup_threshold=dedup_threshold,
            )
            clip_counter += 1
            if fetched:
                ordered_clips.append(fetched[0])
                obtained_duration += _CLIP_TARGET
        if obtained_duration < audio_duration - 0.5:
            logger.warning(
                f"gap-fill exhausted after {attempts} attempts — still "
                f"{audio_duration - obtained_duration:.2f}s short; the outro "
                f"outro loop will cover the remainder without repeating footage"
            )
        else:
            logger.info(f"gap-fill complete: {obtained_duration:.2f}s obtained")

    # ------------------------------------------------------------------ #
    # 4. Combine clips                                                     #
    # ------------------------------------------------------------------ #
    combined_path = os.path.join(temp_dir, "combined.mp4")
    logger.info("combining clips sequentially")
    try:
        video.combine_videos(
            combined_video_path=combined_path,
            video_paths=ordered_clips,
            audio_file=audio_file,
            video_aspect=video_aspect,
            video_concat_mode=VideoConcatMode.sequential,
            video_transition_mode=VideoTransitionMode.crossfade,
            # Clips are already pre-trimmed; use a large cap to avoid re-trimming.
            max_clip_duration=999,
            threads=os.cpu_count() or 4,
        )
    except Exception:
        logger.exception("combine_videos() raised — aborting")
        return None
    if not os.path.exists(combined_path):
        logger.error("combine_videos() produced no output — aborting")
        return None

    # Probe the actual combined clip duration — crossfade overlaps reduce it below the
    # raw sum of clip durations, so combine_videos may still fall a few seconds short.
    try:
        _probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nk=1:nw=1",
                combined_path,
            ],
            capture_output=True, text=True, check=True, timeout=30,
        )
        combined_duration = float(_probe.stdout.strip())
    except Exception:
        combined_duration = 0.0

    outro_tail = 2.0
    target_duration = audio_duration + outro_tail
    _ENC = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "30", "-threads", "4"]

    # Trim if the combined video overshoots audio + outro buffer.  Keep the
    # extra outro_tail seconds of real footage so the fade-out plays on live
    # content rather than a frozen frame.
    if combined_duration > target_duration + 0.5:
        logger.info(
            f"combined ({combined_duration:.2f}s) overshoots target ({target_duration:.2f}s); trimming"
        )
        trimmed_path = os.path.join(temp_dir, "combined_trimmed.mp4")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", combined_path,
                    "-t", f"{target_duration:.3f}",
                    *_ENC, "-an", trimmed_path,
                ],
                check=True, capture_output=True, timeout=300,
            )
            if os.path.exists(trimmed_path):
                os.replace(trimmed_path, combined_path)
                combined_duration = target_duration
        except Exception as exc:
            logger.warning(f"trim failed ({exc}); proceeding with overshooted combined")

    extended_path = os.path.join(temp_dir, "extended.mp4")
    needed_extra = target_duration - combined_duration

    if needed_extra <= 0.05:
        # Combined already covers audio + outro tail with real footage.
        extended_path = combined_path
        logger.info(
            f"outro: combined ({combined_duration:.2f}s) covers audio+tail ({target_duration:.2f}s) — no extension needed"
        )
    else:
        # Gap to fill: loop the last clip so the outro plays live footage
        # rather than a frozen frame, then fade to black in generate_video.
        outro_path = os.path.join(temp_dir, "outro.mp4")
        last_clip = ordered_clips[-1]
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-stream_loop", "-1", "-i", last_clip,
                    "-t", f"{needed_extra:.3f}",
                    *_ENC, "-an", outro_path,
                ],
                check=True, capture_output=True, timeout=120,
            )
            concat_list = os.path.join(temp_dir, "ext_concat.txt")
            with open(concat_list, "w") as _cf:
                _cf.write(f"file '{os.path.abspath(combined_path)}'\n")
                _cf.write(f"file '{os.path.abspath(outro_path)}'\n")
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", concat_list,
                    *_ENC, "-an", extended_path,
                ],
                check=True, capture_output=True, timeout=300,
            )
            logger.info(
                f"outro: looped last clip {needed_extra:.2f}s → "
                f"extended={combined_duration + needed_extra:.2f}s (audio={audio_duration}s)"
            )
        except Exception as exc:
            logger.warning(f"outro loop failed ({exc}), using combined as-is")
            extended_path = combined_path

    # ------------------------------------------------------------------ #
    # 5. Subtitle                                                          #
    # ------------------------------------------------------------------ #
    subtitle_path = ""
    if job.get("subtitle_enabled", True):
        subtitle_path = os.path.join(work_dir, "subtitle.srt")
        logger.info("generating subtitles from Whisper sentence timestamps")
        _timings_to_srt(timings, subtitle_path)
        if not subtitle.file_to_subtitles(subtitle_path):
            logger.warning("subtitle file is empty or invalid — subtitles disabled")
            subtitle_path = ""
        elif word_timings:
            words_path = subtitle_path.replace(".srt", ".words.json")
            with open(words_path, "w", encoding="utf-8") as f:
                json.dump(
                    [{"word": w, "start": s, "end": e} for w, s, e in word_timings],
                    f,
                )
            logger.info(f"word timings saved: {words_path}")

    # ------------------------------------------------------------------ #
    # 6. Final video                                                       #
    # ------------------------------------------------------------------ #
    bgm_type, bgm_file = _resolve_bgm(job)
    params = VideoParams(
        video_subject=video_script[:100],
        video_script=video_script,
        video_aspect=job.get("video_aspect", "16:9"),
        video_concat_mode=VideoConcatMode.sequential.value,
        voice_name=job.get("voice_name", "en-US-AriaNeural"),
        voice_rate=voice_rate,
        bgm_type=bgm_type,
        bgm_file=bgm_file,
        bgm_volume=float(job.get("bgm_volume", 0.15)),
        subtitle_enabled=bool(job.get("subtitle_enabled", True)),
        subtitle_position=job.get("subtitle_position", "bottom"),
        text_background_color=False,
        font_name=job.get("font_name", "Inter_18pt-SemiBold.ttf"),
        text_fore_color=job.get("text_fore_color", "#FFFFFF"),
        font_size=int(job.get("font_size", 30)),
        stroke_color=job.get("stroke_color", "#000000"),
        stroke_width=float(job.get("stroke_width", 1.5)),
        subtitle_highlight=bool(job.get("subtitle_highlight", False)),
    )

    output_file = os.path.join(work_dir, "final.mp4")
    logger.info(f"generating final video: {output_file}")
    try:
        video.generate_video(
            video_path=extended_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=output_file,
            params=params,
        )
    except Exception:
        logger.exception("generate_video() raised — aborting")
        return None

    if not os.path.exists(output_file):
        logger.error("final video not found after generate_video()")
        return None

    result = {
        "task_id": task_id,
        "video": output_file,
        "audio": audio_file,
        "subtitle": subtitle_path,
        "combined": combined_path,
        "clips": ordered_clips,
        "audio_duration": audio_duration,
        "vlm_usage": vlm.get_usage(),
    }

    logger.success(f"pipeline complete → {output_file}")
    return result
