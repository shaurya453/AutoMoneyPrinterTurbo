"""Clip fetch: download + verify stock video or image for each sentence."""
import io
import os
import time
from typing import Any, List, Optional, Tuple

import numpy as np
from PIL import Image as _PILImage
from loguru import logger

from app.config import config
from app.models.schema import VideoAspect
from app.services import media as material
from app.services import render as video
from app.services.scoring import nsfw, relevance, vlm
from app.services.pipeline._planning import (
    _trim_clip,
    _get_visual_concepts,
    _build_query_ladder,
)
from app.utils import utils


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
    deadline: Optional[float] = None,
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
    margin = float(config.app.get("video_relevance_margin", config.app.get("relevance_margin", 0.04)))
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
        if deadline is not None and time.monotonic() > deadline:
            logger.warning(f"clip {clip_idx}: video search deadline reached — stopping early")
            break
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
    effect: str = "",
    video_topic: str = "",
    source_order: Optional[List[str]] = None,
    recent_embeddings: Optional[Any] = None,
    dedup_threshold: float = 0.92,
    deadline: Optional[float] = None,
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

    # For Serper (Google Images), long verbose concept strings return nothing —
    # use a short 3-word canonical entity name as the search query instead.
    visual_concepts_for_serper = _get_visual_concepts(sentence)
    serper_term = ""
    if source_order and "serper" in source_order and visual_concepts_for_serper:
        serper_term = " ".join(visual_concepts_for_serper[0].split()[:3])

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
        serper_term=serper_term,
        deadline=deadline,
    )
    if not image_path:
        return None

    result = video.render_ken_burns_clip(
        image_path=image_path,
        duration=sent_duration + trim_buffer,
        width=width,
        height=height,
        output_path=out_path,
        effect=effect,
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
    visual_effect: str = "",
    deadline: Optional[float] = None,
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
    dedup_kw = {"recent_embeddings": recent_embeddings, "dedup_threshold": dedup_threshold, "deadline": deadline}
    topic_kw = {"video_topic": video_topic}
    image_kw = {"effect": visual_effect}

    if content_track == "named":
        # Named/specific subjects are always served as images via Serper,
        # regardless of media_type or the image-ratio cap's preference flip.
        result = _fetch_image_clip(*args_image, source_order=named_source_order, **dedup_kw, **topic_kw, **image_kw)
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
            result = _fetch_image_clip(*args_image, source_order=named_source_order, **dedup_kw, **topic_kw, **image_kw)
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
            result = _fetch_image_clip(*args_image, source_order=None, **dedup_kw, **topic_kw, **image_kw)
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
            **image_kw,
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
            image_fb = _fetch_image_clip(*args_image_fb, source_order=fb_named_order, **dedup_kw, **topic_kw, **image_kw)
            if image_fb:
                logger.info(f"clip {clip_idx}: used topic-wide fallback concepts {extra_concepts[:3]}")
                return image_fb, True

    logger.warning(f"clip {clip_idx}: no clip found for {query_ladder} (tried both media types, plus fallback concepts)")
    return None
