import glob
import itertools
import io
import json
import math
import os
import random
import gc
import shutil
import subprocess
from contextlib import redirect_stdout
from functools import lru_cache
from typing import List
from loguru import logger
import numpy as np
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
    vfx,
)
from moviepy.video.tools.subtitles import SubtitlesClip
from PIL import Image, ImageDraw, ImageFont

from app.config import config
from app.models.schema import (
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services.utils import video_effects
from app.utils import file_security, utils

class SubClippedVideoClip:
    def __init__(
        self,
        file_path,
        start_time=None,
        end_time=None,
        width=None,
        height=None,
        duration=None,
        source_file_path=None,
    ):
        self.file_path = file_path
        self.start_time = start_time
        self.end_time = end_time
        self.width = width
        self.height = height
        self.source_file_path = source_file_path or file_path
        if duration is None:
            self.duration = end_time - start_time
        else:
            self.duration = duration

    def __str__(self):
        return f"SubClippedVideoClip(file_path={self.file_path}, start_time={self.start_time}, end_time={self.end_time}, duration={self.duration}, width={self.width}, height={self.height})"


audio_codec = "aac"
# Docker 里的 ffmpeg/AAC 组合在默认配置下更容易出现音频质量波动，
# 这里显式抬高音频码率，避免成片阶段因为默认值过低而引入明显失真。
audio_bitrate = "192k"
fps = 30
# Above this clip count, ffmpeg xfade concat is skipped (CLI length limits /
# filter-graph size) and combine_videos falls back to plain concat with no
# crossfade overlap. pipeline.py uses the same value to decide whether to add
# crossfade trim-buffer padding to fetched clips.
XFADE_CLIP_LIMIT = 40
_BGM_EXTENSIONS = (".mp3",)
_DEFAULT_VIDEO_CODEC = "libx264"
_SUPPORTED_VIDEO_CODECS = (
    "libx264",
    "h264_nvenc",
    "h264_amf",
    "h264_qsv",
    "h264_mf",
    "h264_videotoolbox",
)
_runtime_disabled_video_codecs = set()


def _prioritize_unique_source_clips(
    subclipped_items: List[SubClippedVideoClip],
    concat_mode: VideoConcatMode,
) -> List[SubClippedVideoClip]:
    """
    优先让每个源素材只出现一次，降低成片里同一素材反复出现的概率。

    线上素材经常会遇到“一个长视频被切成多个短片段”的情况。旧逻辑在
    random 模式下直接打乱所有短片段，导致同一个源视频的多个切片可能
    分布在开头和中间，用户会感知为素材重复。本函数只调整片段顺序：
    先放每个源文件里最长的一个片段，剩余片段作为兜底；当素材总时长不足时，
    仍然允许后续片段补齐音频长度，避免破坏视频生成成功率。优先选择最长
    片段是为了避免随机选中视频尾部的零碎短片段，导致明明有足够素材却过早复用。
    """
    if not subclipped_items:
        return []

    concat_mode_value = getattr(concat_mode, "value", concat_mode)
    if concat_mode_value != VideoConcatMode.random.value:
        return subclipped_items

    grouped_items: dict[str, list[SubClippedVideoClip]] = {}
    for item in subclipped_items:
        grouped_items.setdefault(item.source_file_path, []).append(item)

    primary_items = []
    overflow_items = []
    for items in grouped_items.values():
        primary_item = max(items, key=lambda item: item.duration)
        primary_items.append(primary_item)
        overflow_items.extend(item for item in items if item is not primary_item)

    random.shuffle(primary_items)
    random.shuffle(overflow_items)
    logger.info(
        "prioritized unique video materials, "
        f"sources: {len(grouped_items)}, "
        f"primary clips: {len(primary_items)}, "
        f"fallback clips: {len(overflow_items)}"
    )
    return primary_items + overflow_items


def get_ffmpeg_binary():
    """
    兼容历史上直接从 video 服务读取 FFmpeg 路径的调用方。

    真正的解析逻辑已经抽到 `app.utils.utils.get_ffmpeg_binary()`，视频、语音
    和后续新增链路都应复用同一套优先级；这里保留薄包装，避免外部脚本或
    旧测试直接导入 `app.services.video.get_ffmpeg_binary` 时出现 AttributeError。
    """
    return utils.get_ffmpeg_binary()


def _get_configured_video_codec() -> str:
    """
    读取用户配置的视频编码器。

    该配置面向高级用户，用于尝试启用 NVENC/AMF/QSV/VideoToolbox 等硬件
    编码。这里刻意只允许固定白名单，避免开放任意 FFmpeg 参数后，用户填错
    参数导致输出格式不可控，甚至让生成任务在后续阶段才失败。
    """
    configured_codec = str(
        config.app.get("video_codec", _DEFAULT_VIDEO_CODEC) or _DEFAULT_VIDEO_CODEC
    ).strip()
    if configured_codec not in _SUPPORTED_VIDEO_CODECS:
        logger.warning(
            f"unsupported video codec configured: {configured_codec}, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC
    return configured_codec


@lru_cache(maxsize=16)
def _ffmpeg_encoder_exists(ffmpeg_binary: str, codec: str) -> bool:
    """
    检查当前 FFmpeg 是否声明支持指定编码器。

    这只能证明 FFmpeg 编译时包含该 encoder，不能证明当前机器硬件和驱动
    一定可用。因此实际编码失败时仍会再回退到 libx264。
    """
    try:
        result = subprocess.run(
            [ffmpeg_binary, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "failed to inspect ffmpeg encoders, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}: {str(exc)}"
        )
        return False

    if result.returncode != 0:
        logger.warning(
            "failed to inspect ffmpeg encoders, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}: {(result.stderr or result.stdout or '').strip()}"
        )
        return False
    return codec in result.stdout


def _get_effective_video_codec(preferred_codec: str | None = None) -> str:
    """
    返回本次实际使用的视频编码器。

    用户选择硬件编码器时，先做 FFmpeg encoder 列表检测；如果本进程里已经
    实际编码失败过，也直接回退，避免一个任务里每个片段都重复失败。
    """
    selected_codec = preferred_codec or _get_configured_video_codec()
    if selected_codec == _DEFAULT_VIDEO_CODEC:
        return _DEFAULT_VIDEO_CODEC

    if selected_codec in _runtime_disabled_video_codecs:
        logger.warning(
            f"video codec {selected_codec} was disabled after a runtime failure, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC

    ffmpeg_binary = utils.get_ffmpeg_binary()
    if not _ffmpeg_encoder_exists(ffmpeg_binary, selected_codec):
        logger.warning(
            f"ffmpeg encoder {selected_codec} is not available, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC

    return selected_codec


def _disable_runtime_video_codec(codec: str, reason: str):
    if codec == _DEFAULT_VIDEO_CODEC:
        return
    _runtime_disabled_video_codecs.add(codec)
    logger.warning(
        f"video codec {codec} failed, fallback to {_DEFAULT_VIDEO_CODEC}. "
        f"reason: {reason}"
    )


# Fastest preset per codec for throwaway intermediate encodes (concat output is
# re-encoded again in generate_video(), so encode quality here doesn't matter).
_FAST_PRESET_BY_CODEC = {
    "libx264": "ultrafast",
    "h264_nvenc": "p1",
    "h264_qsv": "veryfast",
    "h264_amf": "speed",
}


def _fast_preset_args(codec: str) -> List[str]:
    preset = _FAST_PRESET_BY_CODEC.get(codec)
    return ["-preset", preset] if preset else []


def _fallback_write_videofile(clip, output_file: str, failed_codec: str, reason: str, **kwargs):
    """
    硬件编码失败后用 libx264 重试，只有重试成功才禁用该硬件编码器。

    Windows 上 FFmpeg 失败原因比较复杂：可能是显卡/驱动不支持，也可能是输出
    文件被占用、目录权限、杀软拦截等通用 IO 问题。只有 libx264 能成功写出时，
    才能判断原始失败大概率来自硬件编码器本身，避免误伤后续任务。
    """
    clip.write_videofile(output_file, codec=_DEFAULT_VIDEO_CODEC, **kwargs)
    _disable_runtime_video_codec(failed_codec, reason)
    return _DEFAULT_VIDEO_CODEC


def _write_videofile_with_codec_fallback(clip, output_file: str, codec: str, **kwargs):
    """
    使用指定编码器写出视频，失败时自动用 libx264 重试一次。

    硬件编码器是否可用不仅取决于 FFmpeg，还取决于显卡、驱动和当前运行环境。
    生成任务不能因为高级编码器不可用而整体失败，所以这里把回退集中处理。
    """
    effective_codec = _get_effective_video_codec(codec)
    try:
        clip.write_videofile(output_file, codec=effective_codec, **kwargs)
        return effective_codec
    except Exception as exc:
        if effective_codec == _DEFAULT_VIDEO_CODEC:
            raise
        return _fallback_write_videofile(
            clip,
            output_file,
            failed_codec=effective_codec,
            reason=str(exc),
            **kwargs,
        )


def _escape_ffmpeg_concat_path(file_path: str) -> str:
    # concat demuxer 使用单引号包裹路径，路径中的单引号需要先转义。
    return file_path.replace("'", "'\\''")


def _format_ffmpeg_concat_path(file_path: str) -> str:
    """
    生成 concat demuxer 文件列表中的路径。

    FFmpeg 官方文档要求 concat list 中的特殊字符和空格需要转义；Windows
    绝对路径里的反斜杠也容易被解析成转义字符。这里统一转成正斜杠形式，
    让 `C:\\Users\\...` 变成 `C:/Users/...`，再处理单引号，兼容 macOS/Linux。
    """
    absolute_path = os.path.abspath(file_path)
    return _escape_ffmpeg_concat_path(absolute_path.replace("\\", "/"))


def _srt_time_to_seconds(ts: str) -> float:
    """Parse an SRT timestamp 'HH:MM:SS,mmm' into a float second value."""
    h, m, s_ms = ts.split(":")
    s, ms = s_ms.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _make_ducked_bgm(
    bgm_file: str,
    audio_duration: float,
    subtitle_path: str,
    bgm_volume: float,
    duck_to: float = 0.15,
    fade_secs: float = 0.25,
):
    """
    Return a looped BGM AudioClip with volume ducked during narration periods.

    duck_to: volume fraction applied while narration is active (0.15 = 15% of
             bgm_volume — clearly audible but not competing with speech).
    fade_secs: linear ramp duration at each duck boundary to avoid clicks.
    Narration periods are read from the SRT subtitle file.
    """
    from moviepy.audio.AudioClip import AudioClip as _AudioClip
    from app.services.subtitle import file_to_subtitles

    _SR = 44100
    n = int(math.ceil(audio_duration * _SR)) + 1

    duck_vol = bgm_volume * duck_to
    envelope = np.full(n, bgm_volume, dtype=np.float64)

    if subtitle_path and os.path.exists(subtitle_path):
        fade_n = max(1, int(fade_secs * _SR))
        for _, time_str, _ in file_to_subtitles(subtitle_path):
            parts = time_str.split(" --> ")
            if len(parts) != 2:
                continue
            t_start = _srt_time_to_seconds(parts[0].strip())
            t_end = _srt_time_to_seconds(parts[1].strip())
            s = max(0, int(t_start * _SR))
            e = min(n, int(t_end * _SR))
            if e <= s:
                continue
            envelope[s:e] = duck_vol
            half = (e - s) // 2
            fl = min(fade_n, half)
            if fl > 0:
                envelope[s:s + fl] = np.linspace(bgm_volume, duck_vol, fl)
                envelope[e - fl:e] = np.linspace(duck_vol, bgm_volume, fl)

    bgm_raw = AudioFileClip(bgm_file).with_effects([afx.AudioLoop(duration=audio_duration)])

    def make_frame(t):
        frame = bgm_raw.get_frame(t)
        if np.ndim(t) == 0:  # scalar
            idx = min(int(t * _SR), n - 1)
            return frame * float(envelope[idx])
        # array of time values — MoviePy batches audio rendering this way
        indices = np.clip((np.asarray(t) * _SR).astype(int), 0, n - 1)
        vols = envelope[indices]
        return frame * (vols[:, np.newaxis] if frame.ndim == 2 else vols)

    # MoviePy 2.x renamed make_frame= to frame_function= in AudioClip
    return _AudioClip(frame_function=make_frame, duration=audio_duration, fps=_SR)


def concat_video_clips_with_crossfade(
    clip_files: List[str],
    clip_durations: List[float],
    output_file: str,
    threads: int,
    output_dir: str,
    crossfade_duration: float = 0.2,
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
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        failed = result.returncode != 0
        err_msg = (result.stderr or result.stdout or "").strip()
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
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
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


def _sanitize_image_file(image_path: str) -> str:
    # 某些本地图片虽然能被 Pillow 打开，但会因为损坏的 EXIF/eXIf 元数据导致
    # ImageClip 在解析阶段直接抛异常。这里重新导出一份“干净图片”，把坏元数据剥离掉。
    image_root, _ = os.path.splitext(image_path)
    sanitized_path = f"{image_root}.sanitized.png"

    with Image.open(image_path) as image:
        image.load()
        # 统一导出为 PNG，避免 JPEG/PNG 不同元数据路径继续把坏块带过去。
        cleaned_image = Image.new(image.mode, image.size)
        cleaned_image.putdata(list(image.getdata()))
        cleaned_image.save(sanitized_path)

    return sanitized_path


def _open_image_clip_with_fallback(image_path: str):
    # 优先直接打开原始图片；如果因为损坏元数据失败，再尝试生成无元数据副本。
    try:
        return ImageClip(image_path), image_path
    except Exception as exc:
        logger.warning(
            f"failed to open image directly, trying sanitized copy: {image_path}, error: {str(exc)}"
        )
        sanitized_path = _sanitize_image_file(image_path)
        return ImageClip(sanitized_path), sanitized_path


_BG_BRIGHTNESS = 0.5
_BG_BLUR_FRACTION = 0.06  # downscale-then-upscale blur strength


def _blur_and_darken(frame: np.ndarray, brightness: float = _BG_BRIGHTNESS, blur_fraction: float = _BG_BLUR_FRACTION) -> np.ndarray:
    """Cheap blur (downscale + upscale) and brightness reduction for a background layer."""
    h, w = frame.shape[:2]
    small_w, small_h = max(1, int(w * blur_fraction)), max(1, int(h * blur_fraction))
    small = Image.fromarray(frame).resize((small_w, small_h), Image.BILINEAR)
    blurred = small.resize((w, h), Image.BICUBIC)
    arr = np.asarray(blurred, dtype=np.float32) * brightness
    return np.clip(arr, 0, 255).astype(np.uint8)


def _cover_crop_image(img: Image.Image, width: int, height: int) -> Image.Image:
    """Scale + center-crop a PIL image to exactly (width, height), covering the full frame."""
    scale = max(width / img.width, height / img.height)
    new_w = max(int(round(img.width * scale)), width)
    new_h = max(int(round(img.height * scale)), height)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    x0, y0 = (new_w - width) // 2, (new_h - height) // 2
    return resized.crop((x0, y0, x0 + width, y0 + height))


def _make_blurred_cover_background(clip, width: int, height: int, brightness: float = _BG_BRIGHTNESS):
    """For a video clip: cover-scale + center-crop to (width,height), then blur+darken every frame."""
    clip_w, clip_h = clip.size
    scale = max(width / clip_w, height / clip_h)
    cover_w = max(int(round(clip_w * scale)), width)
    cover_h = max(int(round(clip_h * scale)), height)
    bg = clip.resized(new_size=(cover_w, cover_h))
    bg = bg.with_effects([vfx.Crop(x_center=cover_w // 2, y_center=cover_h // 2, width=width, height=height)])
    bg = bg.image_transform(lambda frame: _blur_and_darken(frame, brightness=brightness))
    return bg.with_duration(clip.duration)


def apply_ken_burns(
    image_path: str,
    duration: float,
    width: int,
    height: int,
    zoom_start: float = 0.75,
    zoom_end: float = 0.825,
):
    """
    Create a VideoClip from a still image: the full image is shown as a centered
    inset (never cropped), slowly zooming from zoom_start to zoom_end of its
    "fit" size, over a blurred and darkened cover-fill copy of itself.

    Returns a VideoClip of size (width, height) and the given duration.
    """
    from moviepy.video.VideoClip import VideoClip as _VideoClip
    from PIL import Image as _PILImage

    with _PILImage.open(image_path) as f:
        img = f.convert("RGB")
        img.load()
        src_w, src_h = img.size
        img_arr = np.array(img)
        bg_arr = _blur_and_darken(np.array(_cover_crop_image(img, width, height)))

    fit_scale = min(width / src_w, height / src_h)
    fit_w, fit_h = src_w * fit_scale, src_h * fit_scale

    def make_frame(t: float) -> np.ndarray:
        progress = t / max(duration, 1e-6)
        scale_frac = zoom_start + (zoom_end - zoom_start) * progress
        fg_w = max(1, int(round(fit_w * scale_frac)))
        fg_h = max(1, int(round(fit_h * scale_frac)))
        fg = _PILImage.fromarray(img_arr).resize((fg_w, fg_h), _PILImage.LANCZOS)

        frame = bg_arr.copy()
        x0, y0 = (width - fg_w) // 2, (height - fg_h) // 2
        frame[y0:y0 + fg_h, x0:x0 + fg_w] = np.array(fg)
        return frame

    # MoviePy 2.x: positional make_frame (not make_frame=), fps set via .with_fps()
    clip = _VideoClip(make_frame, duration=duration)
    return clip.with_fps(fps)


def render_ken_burns_clip(
    image_path: str,
    duration: float,
    width: int,
    height: int,
    output_path: str,
    threads: int = 2,
) -> str:
    """
    Render a Ken Burns clip from image_path to an MP4 at output_path.
    Returns output_path on success, '' on failure.
    """
    clip = apply_ken_burns(image_path, duration, width, height)
    try:
        _write_videofile_with_codec_fallback(
            clip,
            output_file=output_path,
            codec=_get_configured_video_codec(),
            audio=False,
            fps=fps,
            threads=threads,
            logger=None,
        )
    finally:
        close_clip(clip)
    return output_path if os.path.exists(output_path) else ""


def _open_video_clip_quietly(video_path: str, audio: bool = False) -> VideoFileClip:
    """
    安静地打开视频文件，避免 MoviePy 2.1.x 把 ffmpeg 探测信息直接打印到 stdout。

    背景：
    当前依赖版本的 `FFMPEG_VideoReader` 内部存在 `print(self.infos)` 和
    `print(ffmpeg command)`，读取无音轨的中间视频时会输出
    `audio_found: False`。这只是输入素材 metadata，不代表最终成片没有音频，
    但会误导 WebUI/终端用户以为生成失败。

    实现：
    1. 只在打开 VideoFileClip 的短窗口内重定向 stdout；
    2. 默认 `audio=False`，因为项目视频素材阶段不需要保留素材原声，
       最终音频会在 `generate_video()` 阶段统一挂载；
    3. 如果依赖库确实输出了内容，降级为 debug 日志，便于必要时排查。
    """
    captured_stdout = io.StringIO()
    with redirect_stdout(captured_stdout):
        clip = VideoFileClip(video_path, audio=audio)

    moviepy_stdout = captured_stdout.getvalue().strip()
    if moviepy_stdout:
        logger.debug(
            "suppressed MoviePy video reader stdout for "
            f"{video_path}, chars: {len(moviepy_stdout)}"
        )

    return clip


def close_clip(clip):
    if clip is None:
        return
        
    try:
        # close main resources
        if hasattr(clip, 'reader') and clip.reader is not None:
            clip.reader.close()
            
        # close audio resources
        if hasattr(clip, 'audio') and clip.audio is not None:
            if hasattr(clip.audio, 'reader') and clip.audio.reader is not None:
                clip.audio.reader.close()
            del clip.audio
            
        # close mask resources
        if hasattr(clip, 'mask') and clip.mask is not None:
            if hasattr(clip.mask, 'reader') and clip.mask.reader is not None:
                clip.mask.reader.close()
            del clip.mask
            
        # handle child clips in composite clips
        if hasattr(clip, 'clips') and clip.clips:
            for child_clip in clip.clips:
                if child_clip is not clip:  # avoid possible circular references
                    close_clip(child_clip)
            
        # clear clip list
        if hasattr(clip, 'clips'):
            clip.clips = []
            
    except Exception as e:
        logger.error(f"failed to close clip: {str(e)}")
    
    del clip
    gc.collect()

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]

    for file in files:
        try:
            os.remove(file)
        except Exception as e:
            logger.debug(f"failed to delete file {file}: {str(e)}")


def _resolve_bgm_file_path(song_dir: str, bgm_file: str) -> str:
    # 背景音乐只允许读取 resource/songs 目录内的文件，避免用户输入任意路径后
    # 被 MoviePy 打开。这里兼容两种常见输入：
    # 1. output000.mp3：来自 BGM 列表或用户只填写文件名
    # 2. ./resource/songs/output000.mp3：用户按项目目录结构填写的相对路径
    # 两种写法最终都会再次通过 resource/songs 白名单校验，不能绕过目录限制。
    try:
        return file_security.resolve_path_within_directory(song_dir, bgm_file)
    except ValueError as song_dir_exc:
        if os.path.isabs(bgm_file):
            raise song_dir_exc

        project_relative_file = os.path.join(utils.root_dir(), bgm_file)
        try:
            return file_security.resolve_path_within_directory(
                song_dir, project_relative_file
            )
        except ValueError as root_dir_exc:
            raise ValueError(str(root_dir_exc)) from song_dir_exc


def get_bgm_file(bgm_type: str = "random", bgm_file: str = ""):
    if not bgm_type:
        return ""

    if bgm_file:
        # Allowed source directories: local song library + pipeline-downloaded cache
        allowed_dirs = [utils.song_dir(), utils.storage_dir("cache_bgm")]
        resolved_bgm_file = None
        for d in allowed_dirs:
            try:
                resolved_bgm_file = file_security.resolve_path_within_directory(d, bgm_file)
                break
            except ValueError:
                continue

        if resolved_bgm_file is None:
            logger.warning(f"reject unsafe bgm file (not in any allowed dir): {bgm_file}")
            return ""

        if not resolved_bgm_file.lower().endswith(_BGM_EXTENSIONS):
            logger.warning(f"reject unsupported bgm file extension: {resolved_bgm_file}")
            return ""

        return resolved_bgm_file

    if bgm_type == "random":
        suffix = "*.mp3"
        song_dir = utils.song_dir()
        files = glob.glob(os.path.join(song_dir, suffix))
        # 当背景音乐目录为空时，直接回退为“不使用 BGM”，避免 random.choice([]) 抛异常。
        if not files:
            logger.warning(f"no bgm files found in song directory: {song_dir}")
            return ""
        return random.choice(files)

    return ""


def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    video_transition_mode: VideoTransitionMode = None,
    max_clip_duration: int = 5,
    threads: int = 2,
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
    cf_overlap = 0.2 if crossfade_will_run else 0.0

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

            # 保留所有有效分段。
            # 这样既不会丢掉“整段视频本身就短于 max_clip_duration”的素材，
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

        try:
            clip = _open_video_clip_quietly(subclipped_item.file_path).subclipped(
                subclipped_item.start_time, subclipped_item.end_time
            )
            clip_duration = clip.duration
            # Not all videos are same size, so we need to resize them
            clip_w, clip_h = clip.size
            if clip_w != video_width or clip_h != video_height:
                clip_ratio = clip.w / clip.h
                video_ratio = video_width / video_height
                logger.debug(f"resizing clip, source: {clip_w}x{clip_h}, ratio: {clip_ratio:.2f}, target: {video_width}x{video_height}, ratio: {video_ratio:.2f}")

                if clip_ratio == video_ratio:
                    clip = clip.resized(new_size=(video_width, video_height))
                else:
                    if clip_ratio > video_ratio:
                        scale_factor = video_width / clip_w
                    else:
                        scale_factor = video_height / clip_h

                    new_width = int(clip_w * scale_factor)
                    new_height = int(clip_h * scale_factor)

                    background = _make_blurred_cover_background(clip, video_width, video_height)
                    clip_resized = clip.resized(new_size=(new_width, new_height)).with_position("center")
                    clip = CompositeVideoClip([background, clip_resized], size=(video_width, video_height)).with_duration(clip_duration)

            shuffle_side = random.choice(["left", "right", "top", "bottom"])
            if transition_value in (None, VideoTransitionMode.none.value):
                clip = clip
            elif transition_value == VideoTransitionMode.fade_in.value:
                clip = video_effects.fadein_transition(clip, 1)
            elif transition_value == VideoTransitionMode.fade_out.value:
                clip = video_effects.fadeout_transition(clip, 1)
            elif transition_value == VideoTransitionMode.slide_in.value:
                clip = video_effects.slidein_transition(clip, 1, shuffle_side)
            elif transition_value == VideoTransitionMode.slide_out.value:
                clip = video_effects.slideout_transition(clip, 1, shuffle_side)
            elif transition_value == VideoTransitionMode.shuffle.value:
                transition_funcs = [
                    lambda c: video_effects.fadein_transition(c, 1),
                    lambda c: video_effects.fadeout_transition(c, 1),
                    lambda c: video_effects.slidein_transition(c, 1, shuffle_side),
                    lambda c: video_effects.slideout_transition(c, 1, shuffle_side),
                ]
                shuffle_transition = random.choice(transition_funcs)
                clip = shuffle_transition(clip)

            if clip.duration > max_clip_duration:
                clip = clip.subclipped(0, max_clip_duration)

            # wirte clip to temp file
            clip_file = f"{output_dir}/temp-clip-{i+1}.mp4"
            _write_videofile_with_codec_fallback(
                clip,
                clip_file,
                codec=_get_configured_video_codec(),
                logger=None,
                fps=fps,
            )

            # Store clip duration before closing
            clip_duration_saved = clip.duration
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

    # loop processed clips until the effective output duration matches or exceeds the audio duration.
    n = len(processed_clips)
    effective_duration = video_duration - max(0, n - 1) * cf_overlap
    if effective_duration < audio_duration:
        logger.warning(f"effective duration ({effective_duration:.2f}s) is shorter than audio duration ({audio_duration:.2f}s), looping clips to match audio length.")
        base_clips = processed_clips.copy()
        for clip in itertools.cycle(base_clips):
            n = len(processed_clips)
            effective_duration = video_duration - max(0, n - 1) * cf_overlap
            if effective_duration >= audio_duration:
                break
            processed_clips.append(clip)
            video_duration += clip.duration
        n = len(processed_clips)
        effective_duration = video_duration - max(0, n - 1) * cf_overlap
        logger.info(f"effective duration: {effective_duration:.2f}s, audio duration: {audio_duration:.2f}s, looped {n - len(base_clips)} clips")
     
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


def wrap_text(text, max_width, font="Arial", fontsize=60):
    # 字幕换行必须在真正创建 TextClip 前完成，否则 MoviePy 只会按原始文本
    # 计算渲染区域。这里用 PIL 按当前字体和字号测量宽度，确保每一行都尽量
    # 控制在视频可用宽度内，避免大字号或中文长句直接溢出画面。
    font = ImageFont.truetype(font, fontsize)
    max_width = int(max_width)

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        if not inner_text:
            return 0, fontsize
        left, top, right, bottom = font.getbbox(inner_text)
        return right - left, bottom - top

    width, height = get_text_size(text)
    if width <= max_width:
        return text, height

    def split_long_token(token):
        # 当一个 token 本身就超宽时（常见于中文无空格长句，或英文超长单词），
        # 退化为字符级拆分。关键点是：检测到 candidate 超宽时，先提交上一个
        # 仍然合法的 current，再把当前字符放入下一行，不能把超宽字符塞回上一行。
        lines = []
        current = ""
        for char in token:
            candidate = f"{current}{char}"
            candidate_width, _ = get_text_size(candidate)
            if candidate_width <= max_width or not current:
                current = candidate
                continue
            lines.append(current)
            current = char
        if current:
            lines.append(current)
        return lines

    lines = []
    current = ""
    words = text.split(" ")
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)

        word_width, _ = get_text_size(word)
        if word_width <= max_width:
            current = word
        else:
            lines.extend(split_long_token(word))
            current = ""

    if current:
        lines.append(current)

    result = "\n".join(line.strip() for line in lines if line.strip()).strip()
    height = len(lines) * height
    return result, height


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    # 字幕背景色来自 API/WebUI 参数，可能为空或格式不规范。这里统一只接受
    # #RRGGBB 形式，非法值回退为黑色，避免 PIL 渲染阶段抛出异常中断任务。
    if isinstance(color, str) and color.startswith("#") and len(color) == 7:
        try:
            return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
        except ValueError:
            pass
    return (0, 0, 0)


def _rounded_subtitle_background_clip(
    width: int,
    height: int,
    color: str,
    alpha: int = 140,
    radius: int = 16,
) -> ImageClip:
    # 新字幕背景仅在用户显式开启时使用：通过 RGBA 图片绘制圆角半透明底板，
    # 再交给 MoviePy 作为透明 ImageClip 参与合成。这样默认路径完全不变，
    # 同时可以低成本试验更柔和的字幕视觉效果。
    rgb = _hex_to_rgb(color)
    safe_alpha = max(0, min(255, int(alpha)))
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [0, 0, max(0, width - 1), max(0, height - 1)],
        radius=max(0, int(radius)),
        fill=(rgb[0], rgb[1], rgb[2], safe_alpha),
    )
    return ImageClip(np.array(img), transparent=True)


def _build_word_highlight_clips(
    subtitle_path: str,
    params: VideoParams,
    video_width: int,
    video_height: int,
    font_path: str,
) -> list:
    """Return a list of yellow ColorClip highlight boxes, one per spoken word."""
    from app.services import subtitle as _subtitle_svc

    words_path = subtitle_path.replace(".srt", ".words.json")
    if not os.path.exists(words_path):
        logger.warning(f"word timings file not found, skipping highlights: {words_path}")
        return []

    try:
        with open(words_path, encoding="utf-8") as f:
            words = json.load(f)
    except Exception as exc:
        logger.warning(f"failed to load word timings: {exc}")
        return []

    srt_entries = _subtitle_svc.file_to_subtitles(subtitle_path)
    if not srt_entries:
        return []

    # Convert SRT entries to ((start_sec, end_sec), text) tuples.
    parsed = []
    for entry in srt_entries:
        # file_to_subtitles returns (index, "HH:MM:SS,mmm --> HH:MM:SS,mmm", text)
        _, time_str, text = entry
        parts = time_str.split(" --> ")
        def _ts(s):
            h, m, rest = s.strip().split(":")
            sec, ms = rest.split(",")
            return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000
        parsed.append((_ts(parts[0]), _ts(parts[1]), text.strip()))

    try:
        font = ImageFont.truetype(font_path, params.font_size)
    except Exception as exc:
        logger.warning(f"failed to load font for highlights: {exc}")
        return []

    max_width = int(video_width * 0.9)
    interline = int(params.font_size * 0.25)
    vertical_padding = int(params.font_size * 0.35)
    pad = max(2, int(params.font_size * 0.15))

    # Pre-compute subtitle base Y for each SRT entry (mirrors create_text_clip positioning).
    def _subtitle_base_y(clip_h):
        if params.subtitle_position == "bottom":
            return video_height * 0.95 - clip_h
        if params.subtitle_position == "top":
            return video_height * 0.05
        if params.subtitle_position == "custom":
            margin = 10
            custom_y = (video_height - clip_h) * (params.custom_position / 100)
            return max(margin, min(custom_y, video_height - clip_h - margin))
        return (video_height - clip_h) / 2  # center

    def _norm(s):
        return "".join(c for c in s.lower() if c.isalnum())

    # Tracks how many times we've highlighted each (srt_entry, word) pair so far,
    # allowing us to target the correct occurrence when the same word appears twice.
    occurrence_counter: dict = {}

    highlights = []
    for word_entry in words:
        w_text = word_entry.get("word", "").strip()
        w_start = float(word_entry.get("start", 0))
        w_end = float(word_entry.get("end", w_start + 0.1))
        if not w_text or w_start >= w_end:
            continue

        # Find which SRT entry this word belongs to.
        host = None
        for s_start, s_end, s_text in parsed:
            if s_start <= w_start < s_end or (s_start <= w_start and w_end <= s_end + 0.05):
                host = (s_start, s_end, s_text)
                break
        if host is None:
            continue

        phrase = host[2]
        try:
            wrapped_txt, txt_height = wrap_text(phrase, max_width=max_width, font=font_path, fontsize=params.font_size)
        except Exception:
            continue

        lines = wrapped_txt.split("\n")
        line_count = len(lines)
        clip_h = int(txt_height + vertical_padding + (interline * line_count))
        base_y = _subtitle_base_y(clip_h)

        w_norm = _norm(w_text)

        # Determine which occurrence of this word within this SRT entry we should target.
        occ_key = (host[0], host[1], w_norm)
        target_occurrence = occurrence_counter.get(occ_key, 0)
        occurrence_counter[occ_key] = target_occurrence + 1

        # Walk through lines to find the Nth occurrence of this word.
        placed = False
        seen_count = 0
        for line_idx, line in enumerate(lines):
            if placed:
                break
            line_words = line.split()
            char_offset = 0
            for lw in line_words:
                if _norm(lw) == w_norm:
                    if seen_count == target_occurrence:
                        # This is the correct occurrence — measure and create the highlight.
                        try:
                            before_w = font.getbbox(line[:char_offset].rstrip())[2] if char_offset > 0 else 0
                            word_w = max(1, font.getbbox(lw)[2] - font.getbbox(lw)[0])
                            line_w = max(1, font.getbbox(line)[2] - font.getbbox(line)[0])
                        except Exception:
                            break

                        # Center-aligned: clip starts at (video_width - max_width) / 2.
                        clip_x = (video_width - max_width) // 2
                        line_start_in_clip = (max_width - line_w) // 2
                        word_x = clip_x + line_start_in_clip + before_w - pad
                        word_y = base_y + vertical_padding // 2 + line_idx * (params.font_size + interline) - pad

                        try:
                            box = ColorClip(
                                size=(word_w + 2 * pad, params.font_size + 2 * pad),
                                color=(255, 220, 0),
                            )
                            box = box.with_opacity(0.55)
                            box = box.with_start(w_start).with_end(w_end)
                            box = box.with_position((int(word_x), int(word_y)))
                            highlights.append(box)
                        except Exception as exc:
                            logger.warning(f"failed to create highlight clip for '{w_text}': {exc}")

                        placed = True
                        break
                    else:
                        seen_count += 1
                char_offset += len(lw) + 1  # +1 for space

    logger.info(f"built {len(highlights)} word highlight clips")
    return highlights


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
):
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    # https://github.com/harry0703/MoneyPrinterTurbo/issues/217
    # PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'final-1.mp4.tempTEMP_MPY_wvf_snd.mp3'
    # write into the same directory as the output file
    output_dir = os.path.dirname(output_file)

    font_path = ""
    if params.subtitle_enabled:
        if not params.font_name:
            params.font_name = "Inter_18pt-SemiBold.ttf"
        font_path = os.path.join(utils.font_dir(), params.font_name)
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

        logger.info(f"  ⑤ font: {font_path}")

    def resolve_subtitle_background_color():
        # 兼容历史参数：API 里 `text_background_color` 既可能是布尔值，
        # 也可能是实际颜色字符串。统一在这里归一化，避免把 True/False
        # 直接传给 TextClip 后出现不可预期的渲染结果。
        if isinstance(params.text_background_color, bool):
            return "#000000" if params.text_background_color else None
        return params.text_background_color

    def create_text_clip(subtitle_item):
        params.font_size = int(params.font_size)
        params.stroke_width = int(params.stroke_width)
        phrase = subtitle_item[1]
        max_width = video_width * 0.9
        wrapped_txt, txt_height = wrap_text(
            phrase, max_width=max_width, font=font_path, fontsize=params.font_size
        )
        interline = int(params.font_size * 0.25)
        line_count = wrapped_txt.count("\n") + 1
        vertical_padding = int(params.font_size * 0.35)
        # MoviePy 在 `method=label` 下会自动收缩文本框高度，遇到多行字幕、
        # 描边或背景色时，容易把最后一行的下半部分裁掉。这里显式传入
        # 一个更保守的高度，把行间距和额外上下留白一并算进去，保证字幕
        # 背景框与文字本身都能完整渲染出来。
        clip_h = int(txt_height + vertical_padding + (interline * line_count))
        bg_color = resolve_subtitle_background_color()
        rounded_bg_enabled = bool(
            getattr(params, "rounded_subtitle_background", False) and bg_color
        )

        if rounded_bg_enabled:
            # 圆角背景需要贴合文字宽度，而不是沿用 90% 视频宽度。这里先用
            # PIL 测量最长一行文字，再加水平内边距，避免短字幕出现过宽底板。
            try:
                font = ImageFont.truetype(font_path, params.font_size)
                text_w = max(
                    int(font.getbbox(line)[2] - font.getbbox(line)[0])
                    for line in wrapped_txt.split("\n")
                )
            except Exception as exc:
                logger.warning(
                    f"failed to measure subtitle text width, fallback to max width: {str(exc)}"
                )
                text_w = int(max_width)

            pad_x = int(params.font_size * 0.6)
            box_w = max(1, min(int(max_width), text_w + 2 * pad_x))
            radius = max(8, int(params.font_size * 0.4))
            text_clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=(box_w, clip_h),
                text_align="center",
            )
            bg_clip = _rounded_subtitle_background_clip(
                width=box_w,
                height=clip_h,
                color=bg_color,
                alpha=140,
                radius=radius,
            )
            _clip = CompositeVideoClip(
                [bg_clip, text_clip.with_position("center")],
                size=(box_w, clip_h),
            )
        else:
            size = (
                int(max_width),
                clip_h,
            )
            _clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=bg_color,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=size,
                text_align="center",
            )
        duration = subtitle_item[0][1] - subtitle_item[0][0]
        _clip = _clip.with_start(subtitle_item[0][0])
        _clip = _clip.with_end(subtitle_item[0][1])
        _clip = _clip.with_duration(duration)
        if params.subtitle_position == "bottom":
            _clip = _clip.with_position(("center", video_height * 0.95 - _clip.h))
        elif params.subtitle_position == "top":
            _clip = _clip.with_position(("center", video_height * 0.05))
        elif params.subtitle_position == "custom":
            # Ensure the subtitle is fully within the screen bounds
            margin = 10  # Additional margin, in pixels
            max_y = video_height - _clip.h - margin
            min_y = margin
            custom_y = (video_height - _clip.h) * (params.custom_position / 100)
            custom_y = max(
                min_y, min(custom_y, max_y)
            )  # Constrain the y value within the valid range
            _clip = _clip.with_position(("center", custom_y))
        else:  # center
            _clip = _clip.with_position(("center", "center"))
        return _clip

    video_clip = _open_video_clip_quietly(video_path)
    audio_clip = AudioFileClip(audio_path).with_effects(
        [afx.MultiplyVolume(params.voice_volume)]
    )

    def make_textclip(text):
        return TextClip(
            text=text,
            font=font_path,
            font_size=params.font_size,
        )

    if subtitle_path and os.path.exists(subtitle_path):
        sub = SubtitlesClip(
            subtitles=subtitle_path, encoding="utf-8", make_textclip=make_textclip
        )
        text_clips = []
        for item in sub.subtitles:
            clip = create_text_clip(subtitle_item=item)
            text_clips.append(clip)

        if params.subtitle_highlight and font_path:
            highlight_clips = _build_word_highlight_clips(
                subtitle_path, params, video_width, video_height, font_path
            )
            # Highlights go before text so text renders on top of the boxes.
            video_clip = CompositeVideoClip([video_clip, *highlight_clips, *text_clips])
        else:
            video_clip = CompositeVideoClip([video_clip, *text_clips])

    # Fade the video to black over the last 1.5 s (covers the 2 s outro tail).
    video_clip = video_clip.with_effects([vfx.FadeOut(1.5)])

    bgm_file = get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)
    if bgm_file:
        try:
            duck_ratio = float(config.app.get("bgm_duck_ratio", 0.15))
            has_subtitles = bool(subtitle_path and os.path.exists(subtitle_path))
            if has_subtitles and duck_ratio < 1.0:
                logger.info(f"applying BGM ducking: {duck_ratio:.0%} during narration")
                bgm_clip = _make_ducked_bgm(
                    bgm_file=bgm_file,
                    audio_duration=video_clip.duration,
                    subtitle_path=subtitle_path,
                    bgm_volume=params.bgm_volume,
                    duck_to=duck_ratio,
                )
            else:
                bgm_clip = AudioFileClip(bgm_file).with_effects(
                    [
                        afx.MultiplyVolume(params.bgm_volume),
                        afx.AudioFadeOut(3),
                        afx.AudioLoop(duration=video_clip.duration),
                    ]
                )
            audio_clip = CompositeAudioClip([audio_clip, bgm_clip])
        except Exception as e:
            logger.error(f"failed to add bgm: {str(e)}")

    video_clip = video_clip.with_audio(audio_clip)
    # 显式沿用输入音频的采样率；如果取不到，再回退到 MoviePy 默认的 44100Hz。
    # 这样可以减少不同运行环境，尤其是 Docker 环境中再次重采样带来的音质波动。
    output_audio_fps = int(getattr(audio_clip, "fps", 0) or 44100)
    _write_videofile_with_codec_fallback(
        video_clip,
        output_file=output_file,
        codec=_get_configured_video_codec(),
        audio_codec=audio_codec,
        audio_fps=output_audio_fps,
        audio_bitrate=audio_bitrate,
        temp_audiofile_path=output_dir,
        threads=params.n_threads or 4,
        logger=None,
        fps=fps,
    )
    video_clip.close()
    del video_clip
