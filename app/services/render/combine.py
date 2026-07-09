import os
import random
import shutil
import subprocess
from typing import List, Optional

from loguru import logger
from moviepy import AudioFileClip

from app.models.schema import (
    VideoAspect,
    VideoConcatMode,
    VideoTransitionMode,
)
from app.utils import utils

from ._common import (
    XFADE_CLIP_LIMIT,
    SubClippedVideoClip,
    _DEFAULT_CROSSFADE_SECONDS,
    _DEFAULT_VIDEO_CODEC,
    _FFMPEG_CONCAT_TIMEOUT_SECONDS,
    _CLIP_TRANSITION_SECONDS,
    _disable_runtime_video_codec,
    _fast_preset_args,
    _format_ffmpeg_concat_path,
    _get_configured_video_codec,
    _get_effective_video_codec,
    _open_video_clip_quietly,
    _prioritize_unique_source_clips,
    _write_videofile_with_codec_fallback,
    close_clip,
    delete_files,
    fps,
)
from .ken_burns import (
    _resize_clip_to_aspect,
    fadein_transition,
    fadeout_transition,
    slidein_transition,
    slideout_transition,
)

# Each entry maps a VideoTransitionMode value to a `(clip, shuffle_side) -> clip`
# transition function. VideoTransitionMode.shuffle resolves to a random pick
# from _SHUFFLE_TRANSITIONS instead of a single function.
_TRANSITION_DISPATCH = {
    VideoTransitionMode.fade_in.value: lambda clip, side: fadein_transition(clip, _CLIP_TRANSITION_SECONDS),
    VideoTransitionMode.fade_out.value: lambda clip, side: fadeout_transition(clip, _CLIP_TRANSITION_SECONDS),
    VideoTransitionMode.slide_in.value: lambda clip, side: slidein_transition(clip, _CLIP_TRANSITION_SECONDS, side),
    VideoTransitionMode.slide_out.value: lambda clip, side: slideout_transition(clip, _CLIP_TRANSITION_SECONDS, side),
    VideoTransitionMode.shuffle.value: "shuffle",
}

_SHUFFLE_TRANSITIONS = [
    lambda clip, side: fadein_transition(clip, _CLIP_TRANSITION_SECONDS),
    lambda clip, side: fadeout_transition(clip, _CLIP_TRANSITION_SECONDS),
    lambda clip, side: slidein_transition(clip, _CLIP_TRANSITION_SECONDS, side),
    lambda clip, side: slideout_transition(clip, _CLIP_TRANSITION_SECONDS, side),
]


def concat_video_clips_with_crossfade(
    clip_files: List[str],
    clip_durations: List[float],
    output_file: str,
    threads: int,
    output_dir: str,
    crossfade_duration: float = _DEFAULT_CROSSFADE_SECONDS,
):
    """
    Concatenate clips using ffmpeg's xfade filter for smooth dissolves between cuts.
    Falls back to regular concat if xfade fails (e.g. ffmpeg built without xfade).
    crossfade_duration is clamped to at most half of the shortest clip.
    """
    if len(clip_files) == 1:
        shutil.copy(clip_files[0], output_file)
        return

    # xfade builds one -i per clip and one filter per transition; impractical above ~40 clips
    # and hits Windows CreateProcess command-line length limits for large clip counts.
    if len(clip_files) > XFADE_CLIP_LIMIT:
        logger.info(
            f"clip count {len(clip_files)} > {XFADE_CLIP_LIMIT}, "
            "skipping xfade and using list-file concat"
        )
        concat_video_clips_with_ffmpeg(clip_files, output_file, threads, output_dir)
        return

    min_dur = min(clip_durations) if clip_durations else crossfade_duration * 2
    cf = min(crossfade_duration, min_dur / 2.0)
    if cf <= 0:
        concat_video_clips_with_ffmpeg(clip_files, output_file, threads, output_dir)
        return

    ffmpeg_bin = utils.get_ffmpeg_binary()
    codec = _get_effective_video_codec()

    inputs = []
    for f in clip_files:
        inputs += ["-i", f]

    # Build a chained xfade filter: [0:v][1:v]xfade=...:offset=O1[v1];[v1][2:v]xfade=...:offset=O2[v2];...
    n = len(clip_files)
    filter_parts = []
    offset = 0.0
    prev_label = "[0:v]"
    for i in range(1, n):
        offset += clip_durations[i - 1] - cf
        offset = max(0.0, offset)
        label_out = f"[v{i}]" if i < n - 1 else "[vout]"
        filter_parts.append(
            f"{prev_label}[{i}:v]xfade=transition=fade"
            f":duration={cf:.4f}:offset={offset:.4f}{label_out}"
        )
        prev_label = label_out

    cmd = [
        ffmpeg_bin, "-y",
        *inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]",
        "-c:v", codec,
        *_fast_preset_args(codec),
        "-threads", str(threads or os.cpu_count() or 4),
        "-pix_fmt", "yuv420p",
        output_file,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            timeout=_FFMPEG_CONCAT_TIMEOUT_SECONDS,
        )
        failed = result.returncode != 0
        err_msg = (result.stderr or result.stdout or "").strip()
    except subprocess.TimeoutExpired:
        failed = True
        err_msg = f"xfade concat timed out after {_FFMPEG_CONCAT_TIMEOUT_SECONDS}s"
    except OSError as exc:
        failed = True
        err_msg = str(exc)

    if failed:
        logger.warning(f"xfade concat failed ({err_msg[:200]}), falling back to regular concat")
        concat_video_clips_with_ffmpeg(clip_files, output_file, threads, output_dir)


def concat_video_clips_with_ffmpeg(
    clip_files: List[str], output_file: str, threads: int, output_dir: str
):
    concat_list_file = os.path.join(output_dir, "ffmpeg-concat-list.txt")
    with open(concat_list_file, "w", encoding="utf-8") as fp:
        for clip_file in clip_files:
            fp.write(f"file '{_format_ffmpeg_concat_path(clip_file)}'\n")

    def build_command(codec: str) -> list[str]:
        return [
            utils.get_ffmpeg_binary(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list_file,
            "-c:v",
            codec,
            *_fast_preset_args(codec),
            "-threads",
            str(threads or os.cpu_count() or 4),
            "-pix_fmt",
            "yuv420p",
            output_file,
        ]

    def run_concat(codec: str):
        command = build_command(codec)
        # 使用 ffmpeg 只做一次串联与编码，避免 MoviePy 逐段合并时反复重编码，
        # 从而降低画质劣化与颜色偏移风险。
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=_FFMPEG_CONCAT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"ffmpeg concat timed out after {_FFMPEG_CONCAT_TIMEOUT_SECONDS}s")
        if result.returncode != 0:
            error_message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(error_message or "ffmpeg concat failed")
        return codec

    try:
        effective_codec = _get_effective_video_codec()
        try:
            return run_concat(effective_codec)
        except Exception as exc:
            if effective_codec == _DEFAULT_VIDEO_CODEC:
                raise
            result_codec = run_concat(_DEFAULT_VIDEO_CODEC)
            _disable_runtime_video_codec(effective_codec, str(exc))
            return result_codec
    finally:
        delete_files(concat_list_file)


def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    video_transition_mode: VideoTransitionMode = None,
    max_clip_duration: int = 5,
    threads: int = 2,
    planned_clip_durations: Optional[List[float]] = None,
    rng: random.Random = random,
) -> str:
    audio_clip = AudioFileClip(audio_file)
    try:
        # 这里只需要读取旁白音频时长来决定素材视频拼接长度；后续不会再使用
        # audio_clip。读取完成后立即关闭，避免早退或异常路径泄漏文件句柄。
        audio_duration = audio_clip.duration
    finally:
        close_clip(audio_clip)
    logger.info(f"audio duration: {audio_duration} seconds")
    logger.info(f"maximum clip duration: {max_clip_duration} seconds")

    # 兼容 API 直接调用时未传转场模式的情况，避免后续访问 .value 时崩溃。
    transition_value = getattr(video_transition_mode, "value", video_transition_mode)
    output_dir = os.path.dirname(combined_video_path)

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()

    # When crossfade is used each transition overlaps adjacent clips by cf seconds,
    # so the effective output duration is shorter than the raw sum of clip durations.
    # We track the raw sum for book-keeping but use effective_duration for stop decisions.
    # Crossfade overlap is only actually consumed by the final concat when xfade
    # runs (see concat_video_clips_with_crossfade) — which is skipped above
    # XFADE_CLIP_LIMIT clips. Predict that here so the clip-collection loop below
    # doesn't assume overlap removal that will never happen.
    crossfade_will_run = (
        transition_value == VideoTransitionMode.crossfade.value
        and len(video_paths) <= XFADE_CLIP_LIMIT
    )
    cf_overlap = _DEFAULT_CROSSFADE_SECONDS if crossfade_will_run else 0.0

    processed_clips = []
    subclipped_items = []
    video_duration = 0
    for video_path in video_paths:
        clip = _open_video_clip_quietly(video_path)
        clip_duration = clip.duration
        clip_w, clip_h = clip.size
        close_clip(clip)

        start_time = 0

        while start_time < clip_duration:
            end_time = min(start_time + max_clip_duration, clip_duration)

            if end_time <= start_time:
                break  # max_clip_duration=0 or floating-point edge case — prevent infinite loop

            if end_time - start_time < 0.1:  # skip sub-100ms slivers — ffmpeg xfade can't handle them
                start_time = end_time
                continue

            # 保留所有有效分段。
            # 这样既不会丢掉"整段视频本身就短于 max_clip_duration"的素材，
            # 也不会吞掉长视频最后剩下的一小段尾部内容。
            if end_time > start_time:
                subclipped_items.append(
                    SubClippedVideoClip(
                        file_path=video_path,
                        start_time=start_time,
                        end_time=end_time,
                        width=clip_w,
                        height=clip_h,
                        source_file_path=video_path,
                    )
                )

            start_time = end_time
            if video_concat_mode.value == VideoConcatMode.sequential.value:
                break

    subclipped_items = _prioritize_unique_source_clips(
        subclipped_items=subclipped_items,
        concat_mode=video_concat_mode,
    )

    logger.debug(f"total subclipped items: {len(subclipped_items)}")

    # Add downloaded clips over and over until the duration of the audio (max_duration) has been reached
    for i, subclipped_item in enumerate(subclipped_items):
        n = len(processed_clips)
        effective_duration = video_duration - max(0, n - 1) * cf_overlap
        if effective_duration >= audio_duration:
            break

        logger.debug(
            f"processing clip {i+1}: {subclipped_item.width}x{subclipped_item.height}, "
            f"source: {os.path.basename(subclipped_item.source_file_path)}, "
            f"effective duration: {effective_duration:.2f}s, "
            f"remaining: {audio_duration - effective_duration:.2f}s"
        )

        clip_file = None
        clip_w = subclipped_item.width
        clip_h = subclipped_item.height
        src_start = subclipped_item.start_time
        src_end = subclipped_item.end_time

        shuffle_side = rng.choice(["left", "right", "top", "bottom"])
        transition_func = _TRANSITION_DISPATCH.get(transition_value)
        if transition_func == "shuffle":
            transition_func = rng.choice(_SHUFFLE_TRANSITIONS)

        # Compute snap duration (frame-aligned planned duration) for both paths.
        raw_dur = src_end - src_start
        if planned_clip_durations and i < len(planned_clip_durations):
            _planned = planned_clip_durations[i]
            _n_frames = max(1, round(_planned * fps))
            _snap_dur = _n_frames / fps
            if raw_dur > _snap_dur + 0.001:
                raw_dur = _snap_dur
        raw_dur = max(0.1, raw_dur)

        try:
            if transition_func is None:
                # FFmpeg-direct path: bypasses MoviePy's ffmpeg-pipe reader which
                # can produce a black first frame during decoder initialization.
                clip_file = f"{output_dir}/temp-clip-{i+1}.mp4"
                codec = _get_configured_video_codec()
                if clip_w != video_width or clip_h != video_height:
                    _scale = max(video_width / clip_w, video_height / clip_h)
                    _sw = int(round(clip_w * _scale / 2)) * 2
                    _sh = int(round(clip_h * _scale / 2)) * 2
                    _cx = (_sw - video_width) // 2
                    _cy = (_sh - video_height) // 2
                    vf = (f"scale={_sw}:{_sh}:flags=lanczos,"
                          f"crop={video_width}:{video_height}:{_cx}:{_cy},"
                          f"fps={fps}")
                else:
                    vf = f"fps={fps}"
                ff_cmd = [
                    utils.get_ffmpeg_binary(), "-y",
                    "-ss", str(src_start), "-i", subclipped_item.file_path,
                    "-t", f"{raw_dur:.6f}",
                    "-vf", vf,
                    "-c:v", codec, *_fast_preset_args(codec),
                    "-pix_fmt", "yuv420p",
                    "-threads", str(threads or os.cpu_count() or 4),
                    "-an", clip_file,
                ]
                result = subprocess.run(ff_cmd, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    logger.error(
                        f"FFmpeg direct clip failed (skipping clip): "
                        f"{result.stderr[-300:]}"
                    )
                    if os.path.exists(clip_file):
                        os.remove(clip_file)
                    clip_file = None
                    continue

                clip_duration_saved = raw_dur
                if clip_duration_saved < 0.1:
                    logger.warning(
                        f"skipping degenerate clip ({clip_duration_saved:.3f}s): "
                        f"{subclipped_item.file_path}"
                    )
                    continue

            else:
                # MoviePy path: per-clip transition requires a clip object.
                clip = _open_video_clip_quietly(subclipped_item.file_path).subclipped(
                    src_start, src_end
                )
                if raw_dur < (src_end - src_start) - 0.001:
                    clip = clip.subclipped(0, raw_dur)
                clip_w, clip_h = clip.size
                clip = _resize_clip_to_aspect(clip, video_width, video_height)
                clip = transition_func(clip, shuffle_side)
                if clip.duration > max_clip_duration:
                    clip = clip.subclipped(0, max_clip_duration)
                clip_file = f"{output_dir}/temp-clip-{i+1}.mp4"
                _write_videofile_with_codec_fallback(
                    clip,
                    clip_file,
                    codec=_get_configured_video_codec(),
                    logger=None,
                    fps=fps,
                )
                clip_duration_saved = clip.duration
                if clip_duration_saved < 0.1:
                    logger.warning(
                        f"skipping degenerate clip ({clip_duration_saved:.3f}s): "
                        f"{subclipped_item.file_path}"
                    )
                    close_clip(clip)
                    continue
                close_clip(clip)

            processed_clips.append(
                SubClippedVideoClip(
                    file_path=clip_file,
                    duration=clip_duration_saved,
                    width=clip_w,
                    height=clip_h,
                    source_file_path=subclipped_item.source_file_path,
                )
            )
            video_duration += clip_duration_saved

        except Exception as e:
            logger.error(f"failed to process clip: {str(e)}")
            if clip_file and os.path.exists(clip_file):
                try:
                    os.remove(clip_file)
                except OSError:
                    pass

    # NOTE: clips are intentionally NOT looped/repeated to cover any shortfall
    # against audio_duration -- repeating already-shown footage is exactly the
    # "same clip over and over" artifact this pipeline must avoid. Any gap is
    # left for pipeline.py's outro step, which extends the result with a
    # frozen last frame instead of replaying earlier clips.
    n = len(processed_clips)
    effective_duration = video_duration - max(0, n - 1) * cf_overlap
    if effective_duration < audio_duration:
        logger.warning(
            f"effective duration ({effective_duration:.2f}s) is shorter than audio "
            f"duration ({audio_duration:.2f}s); not looping clips — "
            f"the outro freeze-frame will cover the remainder"
        )

    # merge video clips progressively, avoid loading all videos at once to avoid memory overflow
    logger.info("starting clip merging process")
    if not processed_clips:
        logger.warning("no clips available for merging")
        return combined_video_path

    # if there is only one clip, use it directly
    if len(processed_clips) == 1:
        logger.info("using single clip directly")
        shutil.copy(processed_clips[0].file_path, combined_video_path)
        delete_files([processed_clips[0].file_path])
        logger.info("video combining completed")
        return combined_video_path

    clip_files = [clip.file_path for clip in processed_clips]
    logger.info(f"concatenating {len(clip_files)} clips with ffmpeg")
    if transition_value == VideoTransitionMode.crossfade.value:
        clip_durations_list = [clip.duration for clip in processed_clips]
        concat_video_clips_with_crossfade(
            clip_files=clip_files,
            clip_durations=clip_durations_list,
            output_file=combined_video_path,
            threads=threads,
            output_dir=output_dir,
        )
    else:
        concat_video_clips_with_ffmpeg(
            clip_files=clip_files,
            output_file=combined_video_path,
            threads=threads,
            output_dir=output_dir,
        )

    # clean temp files
    delete_files(clip_files)

    logger.info("video combining completed")
    return combined_video_path
