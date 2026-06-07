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
from typing import List, Optional, Tuple

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import (
    VideoConcatMode,
    VideoAspect,
    VideoParams,
    VideoTransitionMode,
)
from app.services import material, subtitle, video, voice
from app.services import state as sm
from app.utils import utils


# ---------------------------------------------------------------------------
# Whisper sentence-timestamp alignment
# ---------------------------------------------------------------------------

def _get_sentence_timestamps(
    audio_file: str, sentences: list
) -> List[Tuple[dict, float, float]]:
    """
    Transcribe audio with faster-whisper (word_timestamps=True) and map
    each sentence to a (start_sec, end_sec) span by counting words.
    Returns [(sentence_dict, start, end), ...].
    """
    from faster_whisper import WhisperModel

    model_size = config.whisper.get("model_size", "base")
    device = config.whisper.get("device", "cpu")
    compute_type = config.whisper.get("compute_type", "int8")

    logger.info(f"loading whisper model: {model_size} on {device}")
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments, _ = model.transcribe(audio_file, word_timestamps=True)

    all_words: List[Tuple[str, float, float]] = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                word = w.word.strip()
                if word:
                    all_words.append((word, w.start, w.end))

    logger.info(f"whisper found {len(all_words)} words across {len(sentences)} sentences")

    results: List[Tuple[dict, float, float]] = []
    word_idx = 0
    for sent in sentences:
        count = len(sent["text"].split())
        if word_idx >= len(all_words):
            last_end = all_words[-1][2] if all_words else 0.0
            results.append((sent, last_end, last_end + 2.0))
            continue
        start = all_words[word_idx][1]
        end_idx = min(word_idx + count - 1, len(all_words) - 1)
        end = all_words[end_idx][2]
        results.append((sent, start, end))
        word_idx = min(word_idx + count, len(all_words))

    return results


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
# does not eat into the sentence's actual visual content.
_CROSSFADE_DUR = 0.5
_TRIM_BUFFER = 0.2 + _CROSSFADE_DUR / 2   # 0.45 s total padding per clip


# ---------------------------------------------------------------------------
# Clip fetch + trim
# ---------------------------------------------------------------------------

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
    if subprocess.run(cmd, capture_output=True).returncode == 0 and os.path.exists(out_path):
        return True
    # Re-encode fallback
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", src_path,
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        out_path,
    ]
    return (
        subprocess.run(cmd, capture_output=True).returncode == 0
        and os.path.exists(out_path)
    )


def _fetch_clip(
    sentence: dict,
    sent_duration: float,
    source: str,
    video_aspect: VideoAspect,
    clip_idx: int,
    clips_dir: str,
    used_urls: set,
) -> Optional[str]:
    """
    Download and prepare a clip (stock video or Ken Burns image) for one sentence.
    Returns the path to the ready-to-assemble MP4, or None if nothing found.

    used_urls is mutated in-place: the chosen clip URL is added so subsequent
    sentences won't reuse the same source footage.
    """
    search_terms = sentence.get("search_terms", [])
    if not search_terms:
        logger.warning(f"clip {clip_idx}: no search terms provided")
        return None

    width, height = VideoAspect(video_aspect).to_resolution()
    out_path = os.path.join(clips_dir, f"clip-{clip_idx:04d}.mp4")

    # ---- image sentences: Ken Burns effect ----
    if sentence.get("media_type") == "image":
        image_path = material.download_image(
            search_terms=search_terms,
            save_dir=utils.storage_dir("cache_images"),
        )
        if not image_path:
            logger.warning(f"clip {clip_idx}: no image found for terms {search_terms}")
            return None
        result = video.render_ken_burns_clip(
            image_path=image_path,
            duration=sent_duration + _TRIM_BUFFER,
            width=width,
            height=height,
            pan_direction=sentence.get("pan_direction"),
            output_path=out_path,
            threads=2,
        )
        if not result:
            logger.warning(f"clip {clip_idx}: Ken Burns render failed")
        return result or None

    # ---- video sentences: download + trim ----
    search_fn = (
        material.search_videos_pixabay
        if source == "pixabay"
        else material.search_videos_pexels
    )

    min_duration = max(1, int(sent_duration))
    candidates = []
    for term in search_terms:
        items = search_fn(
            search_term=term,
            minimum_duration=min_duration,
            video_aspect=video_aspect,
        )
        candidates.extend(items)
        if candidates:
            break  # first term that yields usable results is enough

    if not candidates:
        logger.warning(f"clip {clip_idx}: no video results for terms {search_terms}")
        return None

    # Pick the first candidate whose URL hasn't been used in this run.
    chosen = next((c for c in candidates if c.url not in used_urls), candidates[0])
    if chosen.url in used_urls:
        logger.warning(f"clip {clip_idx}: all candidates already used — reusing {chosen.url}")
    used_urls.add(chosen.url)

    downloaded = material.save_video(
        video_url=chosen.url,
        save_dir=utils.storage_dir("cache_videos"),
    )
    if not downloaded:
        logger.warning(f"clip {clip_idx}: download failed")
        return None

    ok = _trim_clip(downloaded, sent_duration + _TRIM_BUFFER, out_path)
    if not ok:
        logger.warning(f"clip {clip_idx}: trim failed — skipping clip")
        return None

    return out_path


# ---------------------------------------------------------------------------
# BGM helpers
# ---------------------------------------------------------------------------

def _resolve_bgm(job: dict) -> Tuple[str, str]:
    """
    Resolve BGM source for this job and return (bgm_type, bgm_file) for VideoParams.

    Priority:
      1. bgm_search_term  — search Jamendo online and download; falls back to random local
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
# Work-directory resolution
# ---------------------------------------------------------------------------

def _slugify(title: str) -> str:
    """Return a filesystem-safe version of title, preserving readability."""
    # Strip characters illegal on Windows and Unix filesystems
    slug = re.sub(r'[\\/:*?"<>|]', "", title)
    # Collapse runs of whitespace/dots to a single space
    slug = re.sub(r"[\s.]+", " ", slug).strip()
    return slug or "untitled"


def _make_work_dir(title: str) -> str:
    """
    Return a unique path under storage/tasks/ named after the video title.
    If the folder already exists, append (2), (3), … until a free name is found.
    """
    slug = _slugify(title)
    base = utils.task_dir()  # ensures storage/tasks/ exists
    candidate = os.path.join(base, slug)
    if not os.path.exists(candidate):
        os.makedirs(candidate)
        return candidate
    counter = 2
    while True:
        candidate = os.path.join(base, f"{slug} ({counter})")
        if not os.path.exists(candidate):
            os.makedirs(candidate)
            return candidate
        counter += 1


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

    if not video_script:
        logger.error("job.video_script is empty — nothing to do")
        return None
    if not sentences:
        logger.error("job.sentences is empty — run sentence_prep.py first")
        return None

    video_title: str = job.get("video_title", "").strip()
    if video_title:
        work_dir = _make_work_dir(video_title)
    else:
        work_dir = utils.task_dir(task_id)
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5)
    logger.info(f"pipeline start | task={task_id} | folder={os.path.basename(work_dir)} | sentences={len(sentences)}")

    # ------------------------------------------------------------------ #
    # 1. TTS                                                               #
    # ------------------------------------------------------------------ #
    audio_file = os.path.join(work_dir, "audio.mp3")
    voice_name = voice.parse_voice_name(job.get("voice_name", "en-US-AriaNeural"))
    voice_rate = float(job.get("voice_rate", 1.0))

    logger.info(f"TTS: voice={voice_name}, rate={voice_rate}")
    sub_maker = voice.tts(
        text=video_script,
        voice_name=voice_name,
        voice_rate=voice_rate,
        voice_file=audio_file,
    )
    if sub_maker is None:
        logger.error("TTS failed — check voice name and network connectivity")
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return None

    audio_duration = math.ceil(voice.get_audio_duration(sub_maker))
    logger.info(f"audio duration: {audio_duration}s")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=15)

    # ------------------------------------------------------------------ #
    # 2. Sentence timestamps via faster-whisper                           #
    # ------------------------------------------------------------------ #
    logger.info("aligning sentences with faster-whisper")
    try:
        timings = _get_sentence_timestamps(audio_file, sentences)
    except Exception as exc:
        logger.warning(f"whisper failed ({exc}), falling back to uniform distribution")
        timings = _uniform_timestamps(sentences, audio_duration)

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=25)

    # ------------------------------------------------------------------ #
    # 3. Per-sentence clip download + trim                                #
    # ------------------------------------------------------------------ #
    video_aspect = VideoAspect(job.get("video_aspect", "16:9"))
    video_source: str = job.get("video_source", "pexels")
    clips_dir = os.path.join(work_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    ordered_clips: List[str] = []
    used_urls: set = set()  # tracks clip URLs used this run to prevent reuse
    total_sentences = len(timings)
    for idx, (sent, t_start, t_end) in enumerate(timings):
        sent_duration = max(t_end - t_start, 1.0)
        preview = sent["text"][:60] + ("…" if len(sent["text"]) > 60 else "")
        logger.info(f"[{idx+1}/{total_sentences}] {sent_duration:.2f}s — {preview}")

        clip_path = _fetch_clip(
            sentence=sent,
            sent_duration=sent_duration,
            source=video_source,
            video_aspect=video_aspect,
            clip_idx=idx,
            clips_dir=clips_dir,
            used_urls=used_urls,
        )
        if clip_path:
            ordered_clips.append(clip_path)
        else:
            logger.warning(f"sentence {idx+1}: skipping — no clip available")

        # Update progress proportionally across 25–55%
        progress = 25 + int((idx + 1) / total_sentences * 30)
        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=progress)

    if not ordered_clips:
        logger.error("no clips obtained for any sentence — aborting")
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return None

    logger.info(f"obtained {len(ordered_clips)}/{total_sentences} clips")

    # ------------------------------------------------------------------ #
    # 4. Combine clips                                                     #
    # ------------------------------------------------------------------ #
    combined_path = os.path.join(work_dir, "combined.mp4")
    logger.info("combining clips sequentially")
    video.combine_videos(
        combined_video_path=combined_path,
        video_paths=ordered_clips,
        audio_file=audio_file,
        video_aspect=video_aspect,
        video_concat_mode=VideoConcatMode.sequential,
        video_transition_mode=VideoTransitionMode.crossfade,
        # Clips are already pre-trimmed; use a large cap to avoid re-trimming.
        max_clip_duration=999,
        threads=2,
    )
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=65)

    # ------------------------------------------------------------------ #
    # 5. Subtitle                                                          #
    # ------------------------------------------------------------------ #
    subtitle_path = ""
    if job.get("subtitle_enabled", True):
        subtitle_path = os.path.join(work_dir, "subtitle.srt")
        provider = config.app.get("subtitle_provider", "edge").strip().lower()
        logger.info(f"generating subtitle via {provider}")

        if provider == "edge":
            voice.create_subtitle(
                text=video_script,
                sub_maker=sub_maker,
                subtitle_file=subtitle_path,
            )
            if not os.path.exists(subtitle_path):
                logger.warning("edge subtitle empty, falling back to whisper")
                provider = "whisper"

        if provider == "whisper":
            subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path)
            subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

        lines = subtitle.file_to_subtitles(subtitle_path)
        if not lines:
            logger.warning("subtitle file is empty or invalid — subtitles disabled")
            subtitle_path = ""

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=75)

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
        rounded_subtitle_background=True,
        font_name=job.get("font_name", "Charm-Bold.ttf"),
        text_fore_color=job.get("text_fore_color", "#FFFFFF"),
        font_size=int(job.get("font_size", 55)),
        stroke_color=job.get("stroke_color", "#000000"),
        stroke_width=float(job.get("stroke_width", 1.5)),
    )

    output_file = os.path.join(work_dir, "final.mp4")
    logger.info(f"generating final video: {output_file}")
    video.generate_video(
        video_path=combined_path,
        audio_path=audio_file,
        subtitle_path=subtitle_path,
        output_file=output_file,
        params=params,
    )

    if not os.path.exists(output_file):
        logger.error("final video not found after generate_video()")
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
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
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **result
    )
    logger.success(f"pipeline complete → {output_file}")
    return result
