"""Clip planning helpers: query building, duration estimation, ffmpeg trim."""
import os
import subprocess
from typing import List, Optional

from loguru import logger

from app.config import config
from app.services.render._common import _DEFAULT_CROSSFADE_SECONDS

_TRIM_TIMEOUT_SECONDS = 120

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


# ---- Pass-1 slot-planning constants -------------------------------------- #
# Video sentences: download multiple ~_CLIP_TARGET-second clips to cover the
# sentence duration without repeating footage.
# Image sentences: capped at _IMAGE_CLIP_MAX seconds each — long sentences
# split into multiple clips so a single still never holds for the full
# narration. Each sub-clip fetches a different image (used_urls dedupes).
_CLIP_TARGET = 4.0
_MIN_VISUAL_DUR = 2.0  # absolute floor for any single clip's duration
_MIN_ANIM_DUR = 2.5  # conservative cover for all 4 title-card animation variants
_IMAGE_CLIP_MAX = 7.0  # max seconds per individual Ken Burns image clip
_OUTRO_TAIL = 2.0  # extra footage past audio end so the fade-out plays on live content


def plan_clip_slots(timings: List[tuple], audio_duration: float) -> List[dict]:
    """Pass 1: plan per-sentence clip durations using absolute resync.

    `timings` is a list of (sentence_dict, t_start, t_end) whisper spans.

    Each sentence's footage starts no earlier than max(when its narration
    begins, when the previous sentence's footage ends) -- guaranteeing
    footage never precedes the VO -- and runs until the next sentence's
    narration begins (or audio_duration for the last sentence), floored at
    num_clips * _MIN_VISUAL_DUR. This keeps the cumulative footage timeline
    tracking absolute whisper timestamps directly, instead of drifting via
    per-sentence duration sums.

    Returns a list of plan dicts: {"sent", "is_image", "durations",
    "sent_audio_dur"}, with the last slot already extended by _OUTRO_TAIL.
    """
    t0 = timings[0][1]
    rel_starts = [max(0.0, t_start - t0) for _, t_start, _ in timings]

    cum_end = 0.0
    clip_plans: List[dict] = []
    for idx, (sent, t_start, t_end) in enumerate(timings):
        sent_audio_dur = max(0.0, t_end - t_start)
        is_image = sent.get("media_type") == "image"

        start_k = max(rel_starts[idx], cum_end)
        if idx + 1 < len(timings):
            next_rel = rel_starts[idx + 1]
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
        # Two cases where applying _MIN_VISUAL_DUR would push cum_end past the
        # next sentence's Whisper timestamp, causing every subsequent clip to
        # start late (context drift — visuals lag behind the VO):
        #
        #  1. Narrated graphics: duration is fixed by Whisper; any floor inflation
        #     would desync the graphic from the narration it plays under.
        #  2. Short sentences (sent_audio_dur < _MIN_VISUAL_DUR): the floor would
        #     inflate the clip well beyond the sentence's audio slot.  Multiple
        #     consecutive short sentences compound the drift (e.g. "Same money."
        #     + "None of the heartbreak." can accumulate 3–4 s of visual lag).
        #     Use the exact available slot instead; 0.5 s minimum ensures a
        #     renderable clip without causing meaningful drift.
        is_narrated_graphic = bool(sent.get("graphic_type") and sent.get("text"))
        if is_narrated_graphic:
            total = max(raw_total, 1.0)    # 1s Revideo stability floor only
        elif sent_audio_dur < _MIN_VISUAL_DUR:
            total = max(raw_total, 0.5)    # exact slot, minimal clip floor
        else:
            total = max(floor, raw_total)
        durations = [total / num_clips] * num_clips
        cum_end = start_k + total

        clip_plans.append({
            "sent": sent,
            "is_image": is_image,
            "durations": durations,
            "sent_audio_dur": sent_audio_dur,
        })

    # Extend the last clip's slot by _OUTRO_TAIL so the combined video naturally
    # reaches audio_duration + _OUTRO_TAIL without the outro step having to loop
    # the last clip from the beginning (which caused visible repetition).
    if clip_plans:
        clip_plans[-1]["durations"][-1] += _OUTRO_TAIL

    return clip_plans


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
            q_topic = f"{concept} {video_topic}".strip() if video_topic else concept
            if concept not in ladder:
                ladder.append(concept)
            if q_topic not in ladder and q_topic != concept:
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
