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
from difflib import SequenceMatcher
from typing import List, Optional, Tuple

from loguru import logger

from app.config import config
from app.models.schema import (
    VideoConcatMode,
    VideoAspect,
    VideoParams,
    VideoTransitionMode,
)
from app.services import material, nsfw, relevance, subtitle, video, voice
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
_CROSSFADE_DUR = 0.2
_TRIM_BUFFER = 0.2 + _CROSSFADE_DUR / 2   # 0.3 s total padding per clip


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
    search_terms = sentence.get("search_terms", [])
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

    # search_terms is a tiered list: term[0] is the most specific/niche ask
    # for this sentence, later terms progressively easier-to-find fallbacks
    # that still fit the sentence (see AGENT_GUIDE.md). Try one fresh
    # candidate per term, in order, round-robin -- so a niche term that's
    # empty or gets its candidate rejected falls through to the broader
    # terms instead of burning the whole attempt budget on one tier. Each
    # term's results (and thumbnail reranking) are only fetched lazily, the
    # first time that term is actually reached.
    term_results: List[Optional[list]] = [None] * len(search_terms)
    term_pos = [0] * len(search_terms)

    attempts = 0
    while attempts < max_attempts:
        progressed = False
        for t_idx, term in enumerate(search_terms):
            if attempts >= max_attempts:
                break
            if term_results[t_idx] is None:
                term_results[t_idx] = search_fn(
                    search_term=term,
                    minimum_duration=min_duration,
                    video_aspect=video_aspect,
                    prompt=caption_prompt,
                )
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

            return out_path

        if not progressed:
            break

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
) -> Optional[str]:
    """Download an image and render a Ken Burns clip. None if no image found.

    used_urls is mutated in-place on success (see material.download_image).
    Every downloaded candidate passes the NSFW gate and a CLIP relevance
    margin against `caption_prompt` inside material.download_image.
    """
    search_terms = sentence.get("search_terms", [])
    if not search_terms:
        return None

    width, height = VideoAspect(video_aspect).to_resolution()
    out_path = os.path.join(clips_dir, f"clip-{clip_idx:04d}.mp4")

    image_path = material.download_image(
        search_terms=search_terms,
        save_dir=utils.storage_dir("cache_images"),
        used_urls=used_urls,
        caption_prompt=caption_prompt,
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
) -> Optional[Tuple[str, bool]]:
    """
    Fetch a clip (stock video or Ken Burns image) for one sentence, preferring
    sentence['media_type'] (or `is_image_override` if given — used by the
    image-ratio cap to flip the preferred order without mutating the
    sentence). If the preferred type finds nothing, falls back to the other
    media type using the same search_terms before giving up.

    If both fail and `fallback_terms` is provided, retries both media types
    using a topic-wide pool of search terms (excluding this sentence's own
    terms) so the sentence can still get *different* footage instead of
    contributing nothing.

    used_urls is mutated in-place: the chosen clip's source is added so
    subsequent sentences won't reuse the same footage/image.

    CLIP relevance ranking (the NSFW gate is independent of this) scores
    candidates against `sentence['visual_caption']` (or `search_terms[0]` if
    missing) combined with `video_topic`, so even an on-topic-sounding
    caption is still anchored to the video's overall subject.

    Returns (clip_path, used_image) or None if nothing was found at all.
    """
    search_terms = sentence.get("search_terms", [])
    if not search_terms:
        logger.warning(f"clip {clip_idx}: no search terms provided")
        return None

    # Always anchor the relevance prompt to video_topic, even when
    # visual_caption is present -- a caption like "a person looking
    # surprised" is otherwise scored in isolation and will happily match
    # totally off-topic "surprised" stock footage (e.g. a pregnancy test
    # reveal) for a grocery-industry documentary.
    visual_caption = sentence.get("visual_caption", "")
    caption_prompt = relevance.build_prompt(visual_caption or search_terms[0], video_topic)

    is_image = (
        is_image_override
        if is_image_override is not None
        else sentence.get("media_type") == "image"
    )
    args_video = (sentence, sent_duration, trim_buffer, source, video_aspect, clip_idx, clips_dir, used_urls, caption_prompt)
    args_image = (sentence, sent_duration, trim_buffer, video_aspect, clip_idx, clips_dir, used_urls, caption_prompt)

    if is_image:
        result = _fetch_image_clip(*args_image)
        primary, fallback_name = "image", "video"
    else:
        result = _fetch_video_clip(*args_video)
        primary, fallback_name = "video", "image"

    if result:
        return result, is_image

    logger.warning(f"clip {clip_idx}: no {primary} found for terms {search_terms} — trying {fallback_name} fallback")
    result = _fetch_video_clip(*args_video) if fallback_name == "video" else _fetch_image_clip(*args_image)
    if result:
        return result, (fallback_name == "image")

    if fallback_terms:
        own = set(search_terms)
        extra_terms = [t for t in fallback_terms if t not in own]
        if extra_terms:
            fb_sentence = dict(sentence)
            fb_sentence["search_terms"] = extra_terms
            fb_caption_prompt = relevance.build_prompt(extra_terms[0], video_topic)
            args_video_fb = (fb_sentence, sent_duration, trim_buffer, source, video_aspect, clip_idx, clips_dir, used_urls, fb_caption_prompt)
            args_image_fb = (fb_sentence, sent_duration, trim_buffer, video_aspect, clip_idx, clips_dir, used_urls, fb_caption_prompt)
            video_fb = _fetch_video_clip(*args_video_fb)
            if video_fb:
                logger.info(f"clip {clip_idx}: used topic-wide fallback terms {extra_terms[:3]}")
                return video_fb, False
            image_fb = _fetch_image_clip(*args_image_fb)
            if image_fb:
                logger.info(f"clip {clip_idx}: used topic-wide fallback terms {extra_terms[:3]}")
                return image_fb, True

    logger.warning(f"clip {clip_idx}: no clip found for terms {search_terms} (tried both media types, plus fallback terms)")
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

    task_id = job.get("task_id") or utils.get_uuid()
    sentences: list = job.get("sentences", [])
    video_script: str = job.get("video_script", "").strip()
    video_topic: str = job.get("video_topic", "") or job.get("video_title", "")

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
    word_timings: List[Tuple[str, float, float]] = []
    if os.environ.get("SKIP_WHISPER") == "1":
        logger.info("SKIP_WHISPER=1 → using uniform distribution for timings")
        timings = _uniform_timestamps(sentences, audio_duration)
    else:
        try:
            timings, word_timings = _get_sentence_timestamps(audio_file, sentences)
        except Exception as exc:
            logger.warning(f"whisper failed ({exc}), falling back to uniform distribution")
            timings = _uniform_timestamps(sentences, audio_duration)

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
    # Video sentences: download multiple ~4-second clips to cover the sentence
    # duration without repeating footage.
    # Image sentences: one Ken Burns clip for the full sentence duration —
    # multiple clips would show the same cached image repeatedly.
    _CLIP_TARGET = 4.0
    _MIN_VISUAL_DUR = 3.0  # absolute floor for any single clip's duration

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
        if idx + 1 < len(rel_starts):
            target_end_k = max(rel_starts[idx + 1], start_k)
        else:
            target_end_k = max(audio_duration, start_k)
        raw_total = target_end_k - start_k

        if is_image or sent_audio_dur <= 0:
            num_clips = 1
        else:
            basis = raw_total if raw_total > 0 else sent_audio_dur
            ideal_clips = max(1, round(basis / _CLIP_TARGET))
            max_clips_by_min_dur = max(1, int(basis // _MIN_VISUAL_DUR))
            num_clips = max(1, min(ideal_clips, max_clips_by_min_dur))

        floor = num_clips * _MIN_VISUAL_DUR
        total = max(floor, raw_total)
        durations = [total / num_clips] * num_clips
        cum_end = start_k + total

        clip_plans.append({
            "sent": sent,
            "is_image": is_image,
            "durations": durations,
            "sent_audio_dur": sent_audio_dur,
        })

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

    # Topic-wide pool of search terms (deduped, order-preserving), used as a
    # last-resort fallback so a sentence whose own terms are exhausted can
    # still pull *different* footage instead of leaving a gap that
    # combine_videos would later fill by repeating clips.
    _all_search_terms: List[str] = []
    _seen_terms: set = set()
    for plan in clip_plans:
        for term in plan["sent"].get("search_terms", []):
            if term not in _seen_terms:
                _seen_terms.add(term)
                _all_search_terms.append(term)

    # ---- Pass 2: fetch clips according to the plan ----
    # Soft cap on the fraction of clips that may come from still images —
    # backstops the enrichment agent's media_type choices regardless of how
    # well it followed AGENT_GUIDE.md's image-ratio guidance.
    max_image_ratio = float(job.get("max_image_ratio", config.app.get("max_image_ratio", 0.25)))
    image_clip_count = 0
    video_clip_count = 0

    clip_counter = 0  # unique index for clip filenames across all sentences
    obtained_duration = 0.0  # sum of planned durations that yielded a clip
    for idx, plan in enumerate(clip_plans):
        sent = plan["sent"]
        durations = plan["durations"]
        is_image = plan["is_image"]
        preview = sent["text"][:60] + ("…" if len(sent["text"]) > 60 else "")
        logger.info(
            f"[{idx+1}/{total_sentences}] {plan['sent_audio_dur']:.2f}s audio → "
            f"{len(durations)} clip(s) {'(image)' if is_image else ''} — {preview}"
        )

        got_any = False
        for clip_duration in durations:
            is_image_override = None
            if is_image:
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
                fallback_terms=_all_search_terms,
                is_image_override=is_image_override,
                video_topic=video_topic,
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
    if obtained_duration < audio_duration - 0.5 and _all_search_terms:
        max_gap_fill_attempts = len(_all_search_terms) * 3 + 20
        attempts = 0
        logger.info(
            f"obtained {obtained_duration:.2f}s of {audio_duration:.2f}s — gap-filling with extra clips"
        )
        while obtained_duration < audio_duration - 0.5 and attempts < max_gap_fill_attempts:
            term = _all_search_terms[attempts % len(_all_search_terms)]
            attempts += 1
            filler_sentence = {
                "search_terms": [term],
                "media_type": "video",
                "visual_caption": relevance.build_prompt(term, video_topic),
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
            )
            clip_counter += 1
            if fetched:
                ordered_clips.append(fetched[0])
                obtained_duration += _CLIP_TARGET
        if obtained_duration < audio_duration - 0.5:
            logger.warning(
                f"gap-fill exhausted after {attempts} attempts — still "
                f"{audio_duration - obtained_duration:.2f}s short; the outro "
                f"freeze-frame will cover the remainder without repeating footage"
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

    # combine_videos() may overshoot audio_duration when clip count exceeds the
    # xfade limit (40 clips) and falls back to plain concat — the assumed crossfade
    # overlap never materialises so combined ends up longer than expected.
    # Trim it back to audio_duration before the outro step so the final video
    # does not play silent footage after the narration ends.
    if combined_duration > audio_duration + 0.5:
        logger.info(
            f"combined ({combined_duration:.2f}s) overshoots audio ({audio_duration:.2f}s); trimming"
        )
        _ENC_TRIM = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "30", "-threads", "4"]
        trimmed_path = os.path.join(temp_dir, "combined_trimmed.mp4")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", combined_path,
                    "-t", f"{audio_duration:.3f}",
                    *_ENC_TRIM, "-an", trimmed_path,
                ],
                check=True, capture_output=True, timeout=300,
            )
            if os.path.exists(trimmed_path):
                os.replace(trimmed_path, combined_path)
                combined_duration = audio_duration
        except Exception as exc:
            logger.warning(f"trim overshoot failed ({exc}); proceeding with overshooted combined")

    # Extend the combined video with a freeze of the last frame so the ending
    # has a clean hold before the FadeOut in generate_video.  The outro covers
    # any remaining gap to fill the full VO, plus a 2 s tail that will fade to
    # black in generate_video (FadeOut 1.5 s).
    outro_tail = 2.0
    needed_extra = max(outro_tail, audio_duration - combined_duration + outro_tail)
    extended_path = os.path.join(temp_dir, "extended.mp4")
    outro_path = os.path.join(temp_dir, "outro.mp4")
    _ENC = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "30", "-threads", "4"]
    # PRIMARY: freeze last frame via tpad — no loop restart, no stutter.
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", combined_path,
                "-vf", f"tpad=stop_mode=clone:stop_duration={needed_extra:.3f}",
                *_ENC, "-an", extended_path,
            ],
            check=True, capture_output=True, timeout=300,
        )
        logger.info(
            f"outro: freeze-frame {needed_extra:.2f}s → "
            f"extended={combined_duration + needed_extra:.2f}s (audio={audio_duration}s)"
        )
    except Exception as exc:
        logger.warning(f"outro tpad failed ({exc}), falling back to last-clip loop")
        # FALLBACK: loop the last clip.
        try:
            last_clip = ordered_clips[-1]
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
        except Exception:
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
        rounded_subtitle_background=False,
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
    }

    logger.success(f"pipeline complete → {output_file}")
    return result
