"""Clip fetch: download + verify stock video or image for each sentence."""
import io
import os
import random
import threading
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
    build_query_plan,
)
from app.utils import utils


class _ThreadSafeURLSet:
    """Thread-safe wrapper around a URL-tracking set.

    Exposes the standard set interface (``in``, ``.add()``) for read-only
    or single-threaded callers, plus an atomic ``try_claim(*items)`` method
    that checks *and* adds inside a single critical section.  This prevents
    two worker threads from both believing they are the first to claim the
    same clip URL between a bare ``in`` check and a subsequent ``.add()``.
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._set: set = set()

    def __contains__(self, item: object) -> bool:
        return item in self._set

    def add(self, item: str) -> None:
        self._set.add(item)

    def try_claim(self, *items: str) -> bool:
        """Atomically check-and-add.

        Returns True and adds all *items* when none are already present.
        Returns False (adding nothing) if any item is already in the set,
        meaning another thread already claimed this URL.
        """
        with self._lock:
            if any(i in self._set for i in items):
                return False
            self._set.update(items)
            return True


# ---------------------------------------------------------------------------
# Visual criticality — per-sentence effort dial (see AGENT_GUIDE.md)
# ---------------------------------------------------------------------------

_CRITICALITY_LEVELS = ("low", "medium", "high", "critical")

# How much harder each level tries, wired into existing budget knobs:
#   - per-clip search-deadline multiplier (applied in _orchestrate)
#   - extra stock-video download attempts (applied below)
#   - extra image candidates per term + VLM comparative pooling (images.py)
_CRITICALITY_BUDGET_MULT = {"low": 0.6, "medium": 1.0, "high": 1.25, "critical": 1.5}
_CRITICALITY_VIDEO_ATTEMPTS = {"low": -1, "medium": 0, "high": 2, "critical": 4}


def get_criticality(sentence: dict) -> str:
    """Normalized visual_criticality for a sentence; absent/unknown → medium."""
    level = str(sentence.get("visual_criticality") or "").strip().lower()
    return level if level in _CRITICALITY_LEVELS else "medium"


def criticality_budget_multiplier(sentence: dict) -> float:
    return _CRITICALITY_BUDGET_MULT[get_criticality(sentence)]


def _video_vlm_threshold(sentence: dict) -> float:
    """VLM gate for stock-video candidates, by sentence intent.

    The VLM scoring guide caps generic footage at <=0.4 whenever the visual
    intent sounds specific — correct for images, where Serper can actually
    find the named thing, but under a specific-sounding topic it blocks ALL
    stock video at the default 0.55 gate while images sail through at the
    0.30 image gate (one job shipped 0/375 videos and 137/137 stills). For
    generic broll — no must_show, low/medium criticality — generic footage
    IS the intended match, so gate it at vlm_threshold_broll instead."""
    if (
        sentence.get("content_track", "broll") == "broll"
        and not (sentence.get("must_show") or [])
        and get_criticality(sentence) in ("low", "medium")
    ):
        return float(config.app.get("vlm_threshold_broll", 0.35))
    return float(config.app.get("vlm_threshold", 0.55))


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
    report: Optional[dict] = None,
) -> Optional[str]:
    """Download + trim a stock-video clip, verifying each candidate against
    the NSFW pixel gate and a CLIP relevance margin before accepting it.

    Candidates are tried in order (cheap thumbnail-prefiltered by
    `caption_prompt`) for up to `max_video_download_attempts`. Every
    candidate that's downloaded -- whether accepted, NSFW-rejected, or
    relevance-rejected -- is marked in `used_urls` so it's never retried by
    this or a later sentence.

    report: optional dict keyed by clip_idx (see _orchestrate.py's
        quality_report). When provided, this call records
        {provider, content_track, media_type, rejections, used_dedup_fallback,
        accepted} for the post-render quality report. Different clip_idx keys
        never collide across ThreadPoolExecutor workers, so no lock is needed
        for this side-channel dict (same reasoning as used_urls/
        recent_embeddings elsewhere in this file). When None (the default),
        this parameter has zero effect on behavior.

    Returns None if no candidates are found at all, or none pass
    verification within the attempt budget.
    """
    _rejections: List[str] = []
    _content_track = sentence.get("content_track", "broll")

    def _record(accepted: bool, provider: str = "", used_dedup_fallback: bool = False,
                chosen_term: str = "", url: str = "", vlm_score: Optional[float] = None) -> None:
        if report is not None:
            entry = {
                "provider": provider,
                "content_track": _content_track,
                "media_type": "video",
                "rejections": list(_rejections),
                "used_dedup_fallback": used_dedup_fallback,
                "accepted": accepted,
            }
            if chosen_term:
                entry["chosen_term"] = chosen_term
            if url:
                entry["url"] = url
            if vlm_score is not None:
                entry["vlm_score"] = round(vlm_score, 3)
            report[clip_idx] = entry
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
    base_max_attempts = int(config.app.get("max_video_download_attempts", 3))
    if sentence.get("content_track", "broll") == "named":
        # Named/specific-entity sentences are always tried as images first
        # (see _fetch_clip) -- video is only a fallback for them -- but keep
        # the same extra rigor here for consistency and for named_entity
        # broll that explicitly requests video.
        max_attempts = int(config.app.get("max_video_download_attempts_named", base_max_attempts + 3))
    else:
        max_attempts = base_max_attempts
    max_attempts = max(1, max_attempts + _CRITICALITY_VIDEO_ATTEMPTS[get_criticality(sentence)])
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
    dedup_fallback_provider = ""

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
            # Atomically claim the URL before downloading.  If a concurrent
            # thread grabbed the same candidate between the `in used_urls`
            # scan above and this point, skip it; the outer while-loop will
            # try the next candidate on its next pass.
            if hasattr(used_urls, 'try_claim'):
                if not used_urls.try_claim(candidate.url):
                    continue
            else:
                used_urls.add(candidate.url)
            progressed = True
            attempts += 1

            downloaded = material.save_video(
                video_url=candidate.url,
                save_dir=utils.storage_dir("cache_videos"),
            )
            if not downloaded:
                logger.warning(f"clip {clip_idx}: video download failed for {candidate.url}")
                _rejections.append("download")
                continue

            ok = _trim_clip(downloaded, sent_duration + trim_buffer, out_path)
            if not ok:
                logger.warning(f"clip {clip_idx}: trim failed for {candidate.url}")
                _rejections.append("trim")
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
                    _rejections.append("frame_extract")
                    continue

            if nsfw.is_available() and not nsfw.passes(nsfw.is_nsfw_frames(frames)):
                logger.info(f"clip {clip_idx}: rejected NSFW video candidate: {candidate.url}")
                try:
                    os.remove(out_path)
                except Exception:
                    pass
                _rejections.append("nsfw")
                continue

            _vlm_score: Optional[float] = None
            if vlm.is_enabled() and frames:
                mid_frame = frames[len(frames) // 2]
                _vlm_score = vlm.verify_image(
                    mid_frame,
                    sentence.get("text", ""),
                    sentence.get("visual_caption", ""),
                    video_topic,
                    must_show,
                    avoid,
                )
                if not vlm.passes(_vlm_score, threshold=_video_vlm_threshold(sentence)):
                    logger.info(f"clip {clip_idx}: rejected by VLM: {candidate.url}")
                    try:
                        os.remove(out_path)
                    except Exception:
                        pass
                    _rejections.append("vlm")
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
                        _rejections.append("motion")
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
                    _rejections.append("relevance")
                    continue

            if recent_embeddings is not None and frames:
                dedup_emb = relevance.embed_image(frames[0])
                if dedup_emb is not None:
                    if not recent_embeddings.try_claim(dedup_emb, dedup_threshold):
                        logger.info(
                            f"clip {clip_idx}: rejected near-duplicate video candidate "
                            f"(or claimed by a concurrent thread first): {candidate.url}"
                        )
                        # Save first dedup-rejected clip as last-resort fallback.
                        if not os.path.exists(_dedup_fallback_tmp):
                            try:
                                os.rename(out_path, _dedup_fallback_tmp)
                                dedup_fallback_emb = dedup_emb
                                dedup_fallback_provider = getattr(candidate, "provider", "")
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
                        _rejections.append("dedup")
                        continue
                    # Claimed — clean up any saved fallback.
                    if os.path.exists(_dedup_fallback_tmp):
                        try:
                            os.remove(_dedup_fallback_tmp)
                        except Exception:
                            pass

            _record(accepted=True, provider=getattr(candidate, "provider", ""),
                    chosen_term=term, url=candidate.url, vlm_score=_vlm_score)
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
                recent_embeddings.force_add(dedup_fallback_emb)
            _record(accepted=True, provider=dedup_fallback_provider, used_dedup_fallback=True)
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
    _record(accepted=False)
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
    rng: random.Random = random,
    report: Optional[dict] = None,
    term_routing: Optional[dict] = None,
) -> Optional[str]:
    """Download an image and render a Ken Burns clip. None if no image found.

    used_urls is mutated in-place on success (see material.download_image).
    Every downloaded candidate passes the NSFW gate and a CLIP relevance
    margin against `caption_prompt` inside material.download_image.

    report: optional dict keyed by clip_idx -- see _fetch_video_clip's
        docstring for the shared convention. When None (the default), zero
        effect on behavior.
    """
    search_terms = query_ladder if query_ladder is not None else _get_visual_concepts(sentence)
    if not search_terms:
        return None
    _content_track = sentence.get("content_track", "broll")
    _download_report: dict = {} if report is not None else None

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
        content_track=_content_track,
        criticality=get_criticality(sentence),
        report=_download_report,
        term_routing=term_routing,
    )
    if report is not None:
        report[clip_idx] = {
            "provider": _download_report.get("provider", ""),
            "content_track": _content_track,
            "media_type": "image",
            "rejections": _download_report.get("rejections", []),
            "used_dedup_fallback": False,
            "accepted": bool(image_path),
            # Source image path -- lets the placeholder-fallback branch in
            # _orchestrate.py re-render this exact still at a longer duration
            # instead of looping the finished Ken Burns clip (which would
            # replay the baked-in animation from frame 0 on the loop).
            "image_path": image_path or "",
        }
        for _k in ("vlm_compare_used", "vlm_compare_pool_size", "chosen_term", "url",
                   "clip_score", "vlm_score", "below_margin", "below_margin_score",
                   "below_margin_blocked"):
            if _k in _download_report:
                report[clip_idx][_k] = _download_report[_k]
    if not image_path:
        return None

    result = video.render_ken_burns_clip(
        image_path=image_path,
        duration=sent_duration + trim_buffer,
        width=width,
        height=height,
        output_path=out_path,
        effect=effect,
        rng=rng,
    )
    if not result:
        logger.warning(f"clip {clip_idx}: Ken Burns render failed")
        if report is not None:
            report[clip_idx]["accepted"] = False
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
    rng: random.Random = random,
    report: Optional[dict] = None,
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

    # Referent-swap sentences (standalone_subject: true, see AGENT_GUIDE) are
    # about a different googleable entity than the video's overall subject —
    # an ingredient, chemical, component, standard. Appending video_topic to
    # their queries and scoring anchors drags every candidate back toward the
    # video's product category and rejects the exact diagram/evidence image
    # the sentence asked for (a niacinamide structure diagram scores terribly
    # against "... tinted sunscreen for mature skin"). The primary attempt
    # therefore runs un-anchored; the topic-wide fallback_terms rescue below
    # keeps the full topic anchor — if the specific subject can't be found,
    # on-topic generic footage is the right rescue.
    standalone_subject = bool(sentence.get("standalone_subject"))
    anchor_topic = "" if standalone_subject else video_topic
    if standalone_subject:
        logger.debug(f"clip {clip_idx}: standalone_subject — dropping video_topic anchor")

    query_plan = build_query_plan(
        anchor_topic, visual_concepts, video_type,
        entity_name=(sentence.get("entity_name") or ""),
    )
    query_ladder = [t for t, _ in query_plan]
    if not query_ladder:
        logger.warning(f"clip {clip_idx}: no visual concepts or video_topic provided")
        return None
    # Route entity-bearing rungs to web-image search only (see
    # build_query_plan): stock libraries never carry branded products, so a
    # "web" rung on Pexels can only ever return the wrong product.
    term_routing = {t: p for t, p in query_plan if p != "any"}
    # And keep those rungs out of the stock-VIDEO ladder entirely — each one
    # would burn a download attempt on a query stock video can't satisfy. If
    # everything is web-tagged, keep the last (broadest) rung so video
    # fallback still functions.
    video_ladder = [t for t, p in query_plan if p != "web"] or query_ladder[-1:]

    # Always anchor the relevance prompt to video_topic, even when
    # visual_caption is present -- a caption like "a person looking
    # surprised" is otherwise scored in isolation and will happily match
    # totally off-topic "surprised" stock footage (e.g. a pregnancy test
    # reveal) for a grocery-industry documentary.
    visual_caption = sentence.get("visual_caption", "")
    caption_prompt = relevance.build_prompt(
        visual_caption or (visual_concepts[0] if visual_concepts else anchor_topic),
        anchor_topic,
    )

    content_track = sentence.get("content_track", "broll")
    named_source_order = config.app.get(
        "named_track_image_source_order",
        ["serper", "duckduckgo", "wikimedia", "nasa", "archive_org", "smithsonian",
         "pexels", "pixabay", "unsplash"],
    )

    args_video = (sentence, sent_duration, trim_buffer, source, video_aspect, clip_idx, clips_dir, used_urls, caption_prompt, video_ladder)
    args_image = (sentence, sent_duration, trim_buffer, video_aspect, clip_idx, clips_dir, used_urls, caption_prompt, query_ladder)
    dedup_kw = {"recent_embeddings": recent_embeddings, "dedup_threshold": dedup_threshold, "deadline": deadline, "report": report}
    topic_kw = {"video_topic": anchor_topic}  # blank for standalone_subject — see above
    image_kw = {"effect": visual_effect, "rng": rng, "term_routing": term_routing}

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
            # The rescue pool is topic-wide generic concepts, so it always
            # keeps the full topic anchor — including for standalone_subject
            # sentences whose specific referent couldn't be found.
            fb_ladder = _build_query_ladder(video_topic, extra_concepts, video_type)
            fb_sentence = dict(sentence)
            fb_sentence["visual_concepts"] = extra_concepts
            fb_sentence.pop("standalone_subject", None)
            fb_caption_prompt = relevance.build_prompt(extra_concepts[0], video_topic)
            fb_topic_kw = {"video_topic": video_topic}
            args_video_fb = (fb_sentence, sent_duration, trim_buffer, source, video_aspect, clip_idx, clips_dir, used_urls, fb_caption_prompt, fb_ladder)
            args_image_fb = (fb_sentence, sent_duration, trim_buffer, video_aspect, clip_idx, clips_dir, used_urls, fb_caption_prompt, fb_ladder)
            video_fb = _fetch_video_clip(*args_video_fb, **dedup_kw, **fb_topic_kw)
            if video_fb:
                logger.info(f"clip {clip_idx}: used topic-wide fallback concepts {extra_concepts[:3]}")
                return video_fb, False
            fb_named_order = named_source_order if video_type == "named_entity" else None
            image_fb = _fetch_image_clip(*args_image_fb, source_order=fb_named_order, **dedup_kw, **fb_topic_kw, **image_kw)
            if image_fb:
                logger.info(f"clip {clip_idx}: used topic-wide fallback concepts {extra_concepts[:3]}")
                return image_fb, True

    logger.warning(f"clip {clip_idx}: no clip found for {query_ladder} (tried both media types, plus fallback concepts)")
    return None
