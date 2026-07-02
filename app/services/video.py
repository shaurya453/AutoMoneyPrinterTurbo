import glob
import io
import json
import math
import os
import random
import shutil
import subprocess
from contextlib import redirect_stdout
from functools import lru_cache
from typing import List, Optional
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
from moviepy.video.tools.subtitles import file_to_subtitles as _parse_srt
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
# Default crossfade overlap between adjacent clips, in seconds. Must match
# concat_video_clips_with_crossfade's default `crossfade_duration` so
# combine_videos's effective-duration bookkeeping reflects the overlap that
# the xfade concat step will actually consume.
_DEFAULT_CROSSFADE_SECONDS = 0.2
# Duration of each per-clip transition effect (fade/slide in or out).
_CLIP_TRANSITION_SECONDS = 1
# Final video fade-to-black duration, covering the frozen-frame outro tail.
_OUTRO_FADEOUT_SECONDS = 1.5
# BGM fade-out duration at the end of the video.
_BGM_FADEOUT_SECONDS = 3
# ffmpeg concat/xfade subprocess timeout. These run a single local encode pass
# over already-downloaded clips (no network I/O), but a hung hardware encoder
# or a malformed input stream could otherwise block the pipeline forever.
_FFMPEG_CONCAT_TIMEOUT_SECONDS = 1800
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
    Return a looped BGM AudioClip with volume ducked during narration periods,
    plus the underlying AudioFileClip so the caller can close it after rendering.

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
    ducked = _AudioClip(frame_function=make_frame, duration=audio_duration, fps=_SR)
    # bgm_raw is captured by make_frame's closure and stays open for the life of
    # `ducked`; return it too so the caller can close it once rendering is done.
    return ducked, bgm_raw


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


_BG_BRIGHTNESS = 0.5
_BG_BLUR_FRACTION = 0.06  # downscale-then-upscale blur strength

_PAN_Z = 1.04                    # zoom factor: subtle 4% motion, minimal content crop at peak zoom

_KEN_BURNS_ANIMATIONS = ("pan_lr", "pan_rl", "zoom_in", "zoom_out", "pan_ud", "fade")
_last_ken_burns_animation: str | None = None
_3D_ANIM_DUR = 1.8  # seconds — tilt-to-flat transition; remaining duration holds flat

_VALID_VISUAL_EFFECTS = frozenset({
    "threat", "cold", "warmth", "mystery", "sepia",
    "tech", "hacker_tech", "dream", "noir", "nature", "revelation",
})

_OVERLAY_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "resource", "overlays")
)

# Per-effect overlay config: file basename + FFmpeg blend mode + opacity.
# All overlays use a black background; screen blend treats black as transparent.
_EFFECT_OVERLAYS: dict[str, dict] = {
    "threat":      {"file": "threat_blood.mp4",     "mode": "screen",   "opacity": 1.0},
    "cold":        {"file": "cold_snow.mp4",         "mode": "screen",   "opacity": 1.0},
    "mystery":     {"file": "mystery_fog.mp4",       "mode": "screen",   "opacity": 1.0},
    "dream":       {"file": "dream_bokeh.mp4",       "mode": "screen",   "opacity": 1.0},
    "warmth":      {"file": "warmth_rays.mp4",       "mode": "screen",   "opacity": 1.0},
    "revelation":  {"file": "revelation_flare.mp4",  "mode": "screen",   "opacity": 1.0},
    "noir":        {"file": "noir_rain.mp4",         "mode": "screen",   "opacity": 1.0},
    "sepia":       {"file": "sepia_grain.mp4",       "mode": "multiply", "opacity": 1.0},
    "nature":      {"file": "nature_dust.mp4",       "mode": "screen",   "opacity": 1.0},
    "tech":        {"file": "tech_scanlines.mp4",    "mode": "screen",   "opacity": 1.0},
    "hacker_tech": {"file": "hacker_tech.mp4",       "mode": "screen",   "opacity": 1.0},
}

# Soft animation bias per mood effect — entries within the allowed pool are
# duplicated by their weight, so the random pick naturally favours them without
# overriding geometry constraints or the no-consecutive-repeat rule.
_EFFECT_ANIM_WEIGHTS: dict[str, dict[str, int]] = {
    "threat":      {"zoom_in": 3, "screen_3d_ud": 2},
    "cold":        {"pan_ud": 2},
    "mystery":     {"pan_ud": 3, "screen_3d_lr": 1},
    "warmth":      {"zoom_in": 2},
    "dream":       {"zoom_in": 2},
    "revelation":  {"zoom_in": 3, "screen_3d_lr": 2},
    "sepia":       {"pan_lr": 3},
    "hacker_tech": {"zoom_in": 2},
    "noir":        {"zoom_in": 2},
}


def _pick_animation(allowed: list[str] | None = None, effect: str = "neutral") -> str:
    global _last_ken_burns_animation
    pool_source = list(allowed) if allowed else list(_KEN_BURNS_ANIMATIONS)
    weights = _EFFECT_ANIM_WEIGHTS.get(effect, {})
    weighted: list[str] = []
    for a in pool_source:
        if a != _last_ken_burns_animation:
            weighted.extend([a] * weights.get(a, 1))
    if not weighted:
        weighted = pool_source  # single-entry pool: allow repeat rather than crash
    choice = random.choice(weighted)
    _last_ken_burns_animation = choice
    return choice


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


def _perspective_coeffs(dst_pts, src_pts) -> list:
    """Compute 8 PIL PERSPECTIVE inverse-map coefficients from 4 (dst→src) point pairs."""
    A, b = [], []
    for (dx, dy), (sx, sy) in zip(dst_pts, src_pts):
        A.append([dx, dy, 1,  0,  0, 0, -sx * dx, -sx * dy])
        b.append(sx)
        A.append([ 0,  0, 0, dx, dy, 1, -sy * dx, -sy * dy])
        b.append(sy)
    coeffs = np.linalg.solve(np.array(A, dtype=np.float64), np.array(b, dtype=np.float64))
    return coeffs.tolist()


def _ffmpeg_persp_quad(tilt_pts: list, W: int, H: int) -> tuple:
    """
    Convert tilt_pts (where each image corner appears in the output at max tilt,
    order TL/TR/BR/BL) into the 8 source-space coordinates FFmpeg's perspective
    filter expects: which source pixel fills each output corner (TL TR BL BR).
    Returns (x0,y0, x1,y1, x2,y2, x3,y3) as floats.
    """
    src_pts = [(0, 0), (W, 0), (W, H), (0, H)]   # image corners, same TL/TR/BR/BL order
    a, bc, c, d, e, f, g, h = _perspective_coeffs(tilt_pts, src_pts)

    def src_at(dx: float, dy: float):
        denom = g * dx + h * dy + 1.0
        return (a * dx + bc * dy + c) / denom, (d * dx + e * dy + f) / denom

    tl = src_at(0, 0)
    tr = src_at(W, 0)
    bl = src_at(0, H)
    br = src_at(W, H)
    return (*tl, *tr, *bl, *br)  # x0,y0,x1,y1,x2,y2,x3,y3


def _render_3d_effect(
    image_path: str,
    duration: float,
    width: int,
    height: int,
    output_path: str,
    preset: str,
) -> bool:
    """
    Render a 3D screen-mockup animation: tilted perspective → flat full-frame.
    Re-implemented using MoviePy and PIL to bypass FFmpeg's perspective
    filter lacking `sendcmd` / timeline command support.
    """
    from moviepy.video.VideoClip import VideoClip as _VideoClip
    from PIL import Image as _PILImage, ImageFilter as _ImageFilter
    import numpy as np

    W, H = width, height
    D = duration * 0.50

    # Destination positions of each source corner at max tilt
    if preset == "screen_3d_lr":
        tilt_pts = [
            (W * 0.25, H * 0.18),  # TL
            (W * 0.94, H * 0.03),  # TR
            (W * 0.94, H * 0.97),  # BR
            (W * 0.25, H * 0.82),  # BL
        ]
        sdx, sdy = 48, 20
    else:  # screen_3d_ud
        tilt_pts = [
            (W * 0.16, H * 0.24),  # TL
            (W * 0.84, H * 0.24),  # TR
            (W * 0.96, H * 0.92),  # BR
            (W * 0.04, H * 0.92),  # BL
        ]
        sdx, sdy = 20, 48

    flat_pts = [(0, 0), (W, 0), (W, H), (0, H)]
    src_pts = [(0, 0), (W, 0), (W, H), (0, H)]

    # Fixed offset for the shadow
    sox, soy = sdx // 3, sdy // 3

    try:
        # 1. Load and cover-crop the base image to exactly WxH
        with _PILImage.open(image_path) as f:
            img_raw = f.convert("RGB")

        img = _cover_crop_image(img_raw, W, H)

        # Pre-compute blurred background once (heavy blur + darken, static across all frames)
        bg_arr = np.array(img, dtype=np.float32) * 0.45
        bg = _PILImage.fromarray(np.clip(bg_arr, 0, 255).astype(np.uint8))
        bg = bg.filter(_ImageFilter.GaussianBlur(radius=48))

        img = img.convert("RGBA")  # Requires Alpha for transparent bounds

        def make_frame(t):
            # ease-out cubic, 1→0
            ease = (1.0 - min(t, D) / D) ** 3

            # Zoom: progress 0→1 over the full clip duration.
            # screen_3d_lr zooms in (0.95→1.0); screen_3d_ud zooms out (1.05→1.0).
            zoom_t = t / duration
            if preset == "screen_3d_lr":
                scale = 0.95 + 0.05 * zoom_t
            else:
                scale = 1.05 - 0.05 * zoom_t

            # Interpolate the 4 corners towards the flat full-frame bounds
            current_dst = []
            for i in range(4):
                tx, ty = tilt_pts[i]
                fx, fy = flat_pts[i]
                current_dst.append((fx + (tx - fx) * ease, fy + (ty - fy) * ease))

            # Compute PIL PERSPECTIVE inverse-map coefficients (dst->src)
            coeffs = _perspective_coeffs(current_dst, src_pts)

            # Warp the image. fillcolor=(0,0,0,0) makes the outside transparent.
            warped = img.transform(
                (W, H),
                _PILImage.PERSPECTIVE,
                coeffs,
                _PILImage.BICUBIC,
                fillcolor=(0, 0, 0, 0)
            )

            # Apply zoom: scale the warped card and re-center on a WxH canvas.
            # For zoom-in (scale<1) the card is letterboxed; for zoom-out (scale>1)
            # the paste offset goes negative and PIL naturally center-crops it.
            new_w = max(1, round(W * scale))
            new_h = max(1, round(H * scale))
            scaled = warped.resize((new_w, new_h), _PILImage.BILINEAR)
            warped = _PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
            warped.paste(scaled, ((W - new_w) // 2, (H - new_h) // 2), mask=scaled.split()[3])

            # Blurred background base (same image, heavily blurred and darkened)
            frame = bg.copy()
            alpha = warped.split()[3]

            # Shadow creation (matches FFmpeg: gblur=sigma=20 + colorchannelmixer)
            shadow_mask = alpha.filter(_ImageFilter.GaussianBlur(radius=20))
            shadow_layer = _PILImage.new("RGB", (W, H), (38, 38, 38))  # 15% brightness

            # Composite shadow, then composite the warped image on top
            frame.paste(shadow_layer, (sox, soy), mask=shadow_mask)
            frame.paste(warped, (0, 0), mask=alpha)

            return np.array(frame)

        # 2. Render via MoviePy using the global fps variable
        clip = _VideoClip(make_frame, duration=duration).with_fps(fps)

        # 3. Write output leveraging existing codec fallback & hardware acceleration
        _write_videofile_with_codec_fallback(
            clip,
            output_file=output_path,
            codec="libx264",
            preset="fast",
            threads=os.cpu_count() or 4,
            logger=None,
        )
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0

    except Exception as exc:
        logger.warning(f"_render_3d_effect ({preset}) failed: {exc}")
        return False


def _resize_clip_to_aspect(clip, video_width: int, video_height: int):
    """Resize `clip` to exactly (video_width, video_height) via cover-crop.

    Scales to fill (no bars), then center-crops to the target resolution.
    """
    clip_w, clip_h = clip.size
    if clip_w == video_width and clip_h == video_height:
        return clip

    clip_ratio = clip_w / clip_h
    video_ratio = video_width / video_height
    logger.debug(
        f"resizing clip, source: {clip_w}x{clip_h}, ratio: {clip_ratio:.2f}, "
        f"target: {video_width}x{video_height}, ratio: {video_ratio:.2f}"
    )

    if clip_ratio == video_ratio:
        return clip.resized(new_size=(video_width, video_height))

    scale = max(video_width / clip_w, video_height / clip_h)
    new_w = int(round(clip_w * scale))
    new_h = int(round(clip_h * scale))
    return clip.resized(new_size=(new_w, new_h)).with_effects([
        vfx.Crop(x_center=new_w // 2, y_center=new_h // 2, width=video_width, height=video_height)
    ])


def apply_ken_burns(
    image_path: str,
    duration: float,
    width: int,
    height: int,
    frame_scale: float = 1.0,
    animation: str = "zoom_in",
):
    """MoviePy Ken Burns fallback with pan/zoom animations and ease-out timing.

    frame_scale >= 1.0 → cover mode (landscape): image fills the full frame via
    cover-crop; pan/zoom travels across the natural overflow area with no
    blurred background visible.

    frame_scale < 1.0 → FIT mode (portrait): image is letterboxed to
    frame_scale of the frame on a blurred+darkened background.
    """
    from moviepy.video.VideoClip import VideoClip as _VideoClip
    from PIL import Image as _PILImage

    with _PILImage.open(image_path) as f:
        img = f.convert("RGB")
        img.load()
        src_w, src_h = img.size

    # ── Cover mode (landscape, full-frame fill) ───────────────────────────────
    if frame_scale >= 1.0:
        cover_scale = max(width / src_w, height / src_h)
        cv_w = max(int(round(src_w * cover_scale)), width)
        cv_h = max(int(round(src_h * cover_scale)), height)
        cover_arr = np.array(img.resize((cv_w, cv_h), _PILImage.LANCZOS))
        h_excess = max(0, cv_w - width)
        v_excess = max(0, cv_h - height)

        def make_frame_cover(t: float) -> np.ndarray:
            p = t / max(duration, 1e-6)
            pe = 1.0 - (1.0 - p) ** 2  # ease-out

            if animation == "zoom_in":
                z = 1.0 + (_PAN_Z - 1.0) * pe
                cw = max(1, int(width / z))
                ch = max(1, int(height / z))
                x0 = (cv_w - cw) // 2
                y0 = (cv_h - ch) // 2
                crop = _PILImage.fromarray(cover_arr[y0:y0 + ch, x0:x0 + cw])
                return np.array(crop.resize((width, height), _PILImage.LANCZOS))
            elif animation == "pan_lr":
                x0 = min(int(h_excess * pe), max(0, cv_w - width))
                y0 = v_excess // 2
            elif animation == "pan_rl":
                x0 = min(int(h_excess * (1.0 - p) ** 2), max(0, cv_w - width))
                y0 = v_excess // 2
            else:  # pan_ud
                x0 = h_excess // 2
                y0 = min(int(v_excess * pe), max(0, cv_h - height))

            return cover_arr[y0:y0 + height, x0:x0 + width].copy()

        clip = _VideoClip(make_frame_cover, duration=duration)
        return clip.with_fps(fps)

    # ── FIT mode (portrait, blurred background) ───────────────────────────────
    bg_arr = _blur_and_darken(np.array(_cover_crop_image(img, width, height)))

    fg_budget_w = width * frame_scale
    fg_budget_h = height * frame_scale
    # Always fit inside the foreground budget — no cover-crop, no content clipping.
    fit_scale = min(fg_budget_w / src_w, fg_budget_h / src_h)
    fit_w = int(fit_scale * src_w)
    fit_h = int(fit_scale * src_h)
    fit_arr = np.array(img.resize((fit_w, fit_h), _PILImage.LANCZOS))

    x_off = (width - fit_w) // 2
    y_off = (height - fit_h) // 2

    def make_frame(t: float) -> np.ndarray:
        if animation == "static":
            frame = bg_arr.copy()
            frame[y_off:y_off + fit_h, x_off:x_off + fit_w] = fit_arr
            return frame

        p = t / max(duration, 1e-6)
        pe = 1.0 - (1.0 - p) ** 2  # ease-out: fast start, decelerates

        if animation == "fade":
            fade_d = min(0.4, duration * 0.15)
            if t < fade_d:
                alpha = t / fade_d
            elif t > duration - fade_d:
                alpha = (duration - t) / max(fade_d, 1e-6)
            else:
                alpha = 1.0
            alpha = max(0.0, min(1.0, alpha))
            z = 1.0 + (_PAN_Z - 1.0) * pe
            crop_w = max(1, int(fit_w / z))
            crop_h = max(1, int(fit_h / z))
            x0 = (fit_w - crop_w) // 2
            y0 = (fit_h - crop_h) // 2
            crop = _PILImage.fromarray(fit_arr[y0:y0 + crop_h, x0:x0 + crop_w])
            zoomed = np.array(crop.resize((fit_w, fit_h), _PILImage.LANCZOS))
            bg_region = bg_arr[y_off:y_off + fit_h, x_off:x_off + fit_w]
            blended = (zoomed.astype(np.float32) * alpha + bg_region.astype(np.float32) * (1.0 - alpha)).astype(np.uint8)
            frame = bg_arr.copy()
            frame[y_off:y_off + fit_h, x_off:x_off + fit_w] = blended
            return frame

        if animation == "zoom_in":
            z = 1.0 + (_PAN_Z - 1.0) * pe
            crop_w = max(1, int(fit_w / z))
            crop_h = max(1, int(fit_h / z))
            x0 = (fit_w - crop_w) // 2
            y0 = (fit_h - crop_h) // 2
        elif animation == "zoom_out":
            z = 1.0 + (_PAN_Z - 1.0) * (1.0 - pe)
            crop_w = max(1, int(fit_w / z))
            crop_h = max(1, int(fit_h / z))
            x0 = (fit_w - crop_w) // 2
            y0 = (fit_h - crop_h) // 2
        elif animation == "pan_lr":
            crop_w = max(1, int(fit_w / _PAN_Z))
            crop_h = max(1, int(fit_h / _PAN_Z))
            x0 = int((fit_w - crop_w) * pe)
            y0 = (fit_h - crop_h) // 2
        elif animation == "pan_rl":
            crop_w = max(1, int(fit_w / _PAN_Z))
            crop_h = max(1, int(fit_h / _PAN_Z))
            x0 = int((fit_w - crop_w) * (1.0 - p) ** 2)
            y0 = (fit_h - crop_h) // 2
        else:  # pan_ud
            crop_w = max(1, int(fit_w / _PAN_Z))
            crop_h = max(1, int(fit_h / _PAN_Z))
            x0 = (fit_w - crop_w) // 2
            y0 = int((fit_h - crop_h) * pe)

        crop = _PILImage.fromarray(fit_arr[y0:y0 + crop_h, x0:x0 + crop_w])
        fg = np.array(crop.resize((fit_w, fit_h), _PILImage.LANCZOS))

        frame = bg_arr.copy()
        frame[y_off:y_off + fit_h, x_off:x_off + fit_w] = fg
        return frame

    clip = _VideoClip(make_frame, duration=duration)
    return clip.with_fps(fps)


def _render_ken_burns_ffmpeg(
    image_path: str,
    duration: float,
    width: int,
    height: int,
    output_path: str,
    threads: int = 2,
    frame_scale: float = 1.0,
    animation: str = "zoom_in",
) -> str:
    """
    FFmpeg Ken Burns renderer.

    frame_scale < 1.0 (portrait): FIT-scales the image to frame_scale of the
    frame, centers it on a blurred+darkened background — no content is cropped.

    frame_scale >= 1.0 (landscape): COVER-CROPs to fill the full frame with no
    background visible. Pan/zoom travel uses the image's natural cover-crop
    overflow (pixels that extend past the frame edge) for content-aware motion.
    Pan animations at 2× PIL scale give sub-pixel smooth motion via 2:1 lanczos
    downscale. zoom_in uses 8× zoompan for the same reason.
    """
    import tempfile
    from PIL import Image as _PILImage

    ffmpeg_bin = utils.get_ffmpeg_binary()
    codec = _get_configured_video_codec()

    with _PILImage.open(image_path) as f:
        img = f.convert("RGB")
        img.load()
        src_w, src_h = img.size

    dur = max(duration, 0.001)
    eot = f"(1-pow(1-t/{dur:.6f},2))"  # ease-out quadratic: 0→1, fast start

    tmp_pre = tmp_bg = None
    try:
        # ── Cover mode: landscape fill, no background ─────────────────────────
        if frame_scale >= 1.0:
            cover_scale = max(width / src_w, height / src_h)

            if animation == "zoom_in":
                # Cover-crop to exact frame size, then 8× zoompan adds _PAN_Z extra zoom.
                canvas_img = _cover_crop_image(img, width, height)
                _uz = 8
                up_w, up_h = width * _uz, height * _uz
                total_frames = max(int(round(duration * fps)), 1)
                d_minus_1 = max(total_frames - 1, 1)
                z_expr = f"1.0+{_PAN_Z - 1.0:.4f}*(1-pow(1-on/{d_minus_1},2))"
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                    tmp_pre = fh.name
                canvas_img.save(tmp_pre)
                vf = (
                    f"scale={up_w}:{up_h}:flags=lanczos,"
                    f"zoompan=z='{z_expr}':x='iw/2-iw/(2*zoom)':y='ih/2-ih/(2*zoom)':"
                    f"d={total_frames}:s={width}x{height}:fps={fps}"
                )

            elif animation in ("pan_lr", "pan_rl", "pan_ud"):
                # Resize to cover dimensions at 2× scale; FFmpeg crop(t) + 2:1 lanczos
                # scale gives sub-pixel smooth travel at output resolution.
                cw2 = max(int(src_w * cover_scale * 2 / 2) * 2, width * 2)
                ch2 = max(int(src_h * cover_scale * 2 / 2) * 2, height * 2)
                h_excess2 = cw2 - width * 2
                v_excess2 = ch2 - height * 2
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                    tmp_pre = fh.name
                img.resize((cw2, ch2), _PILImage.LANCZOS).save(tmp_pre)

                if animation == "pan_lr":
                    vf = (
                        f"crop=w={width * 2}:h={height * 2}"
                        f":x='{h_excess2}*{eot}':y={v_excess2 // 2},"
                        f"scale={width}:{height}:flags=lanczos"
                    )
                elif animation == "pan_rl":
                    vf = (
                        f"crop=w={width * 2}:h={height * 2}"
                        f":x='{h_excess2}*(1-{eot})':y={v_excess2 // 2},"
                        f"scale={width}:{height}:flags=lanczos"
                    )
                else:  # pan_ud
                    vf = (
                        f"crop=w={width * 2}:h={height * 2}"
                        f":x={h_excess2 // 2}:y='{v_excess2}*{eot}',"
                        f"scale={width}:{height}:flags=lanczos"
                    )

            elif animation == "fade":
                canvas_img = _cover_crop_image(img, width, height)
                _uz = 8
                up_w, up_h = width * _uz, height * _uz
                total_frames = max(int(round(duration * fps)), 1)
                d_minus_1 = max(total_frames - 1, 1)
                z_expr = f"1.0+{_PAN_Z - 1.0:.4f}*(1-pow(1-on/{d_minus_1},2))"
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                    tmp_pre = fh.name
                canvas_img.save(tmp_pre)
                fade_d = min(0.5, duration * 0.15)
                fade_out_st = max(0.0, duration - fade_d)
                vf = (
                    f"scale={up_w}:{up_h}:flags=lanczos,"
                    f"zoompan=z='{z_expr}':x='iw/2-iw/(2*zoom)':y='ih/2-ih/(2*zoom)':"
                    f"d={total_frames}:s={width}x{height}:fps={fps},"
                    f"fade=t=in:st=0:d={fade_d:.3f},"
                    f"fade=t=out:st={fade_out_st:.3f}:d={fade_d:.3f}"
                )

            else:  # static (cover) — safety fallback; landscape path won't request this
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                    tmp_pre = fh.name
                _cover_crop_image(img, width, height).save(tmp_pre)
                vf = f"scale={width}:{height}:flags=lanczos"

            cmd = [
                ffmpeg_bin, "-y",
                "-sws_flags", "lanczos",
                "-f", "image2", "-loop", "1", "-t", str(duration), "-i", tmp_pre,
                "-vf", vf,
                "-t", str(duration), "-r", str(fps),
                "-c:v", codec, "-preset", "fast", "-an",
                "-threads", str(threads), output_path,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                logger.warning(f"FFmpeg Ken Burns cover ({animation}) stderr: {proc.stderr[-600:]}")
                return ""
            return output_path if os.path.exists(output_path) else ""

        # ── FIT mode: portrait, blurred background ────────────────────────────
        bg_arr = _blur_and_darken(np.array(_cover_crop_image(img, width, height)))

        # Fit image into the foreground budget (frame_scale of screen).
        fg_budget_w = width * frame_scale
        fg_budget_h = height * frame_scale
        # Always fit inside the foreground budget — no cover-crop, no content clipping.
        # Portrait images get blurred bars on the sides or top/bottom.
        fit_scale = min(fg_budget_w / src_w, fg_budget_h / src_h)
        # Round to even for libx264 compatibility.
        fit_w = int(fit_scale * src_w / 2) * 2
        fit_h = int(fit_scale * src_h / 2) * 2

        # Centered overlay offset; static because foreground size never changes.
        fg_x = (width - fit_w) // 2
        fg_y = (height - fit_h) // 2

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            tmp_bg = fh.name
        _PILImage.fromarray(bg_arr).save(tmp_bg)

        # Static path — portrait images displayed with no animation.
        if animation == "static":
            pre_resized = img.resize((fit_w, fit_h), _PILImage.LANCZOS)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                tmp_pre = fh.name
            pre_resized.save(tmp_pre)
            filter_complex = f"[1:v][0:v]overlay=x={fg_x}:y={fg_y}"
            cmd = [
                ffmpeg_bin, "-y",
                "-sws_flags", "lanczos",
                "-f", "image2", "-loop", "1", "-t", str(duration), "-i", tmp_pre,
                "-f", "image2", "-loop", "1", "-t", str(duration), "-i", tmp_bg,
                "-filter_complex", filter_complex,
                "-t", str(duration), "-r", str(fps),
                "-c:v", codec, "-preset", "fast", "-an",
                "-threads", str(threads), output_path,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                logger.warning(f"FFmpeg Ken Burns (static) stderr: {proc.stderr[-600:]}")
                return ""
            return output_path if os.path.exists(output_path) else ""

        # Fade path — fade in/out with slow zoom in.
        if animation == "fade":
            _uz = 8
            up_w = fit_w * _uz
            up_h = fit_h * _uz
            total_frames = max(int(round(duration * fps)), 1)
            d_minus_1 = max(total_frames - 1, 1)
            z_expr = f"1.0+{_PAN_Z - 1.0:.4f}*(1-pow(1-on/{d_minus_1},2))"
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                tmp_pre = fh.name
            img.resize((fit_w, fit_h), _PILImage.LANCZOS).save(tmp_pre)
            fade_d = min(0.4, duration * 0.15)
            fade_out_st = max(0.0, duration - fade_d)
            filter_complex = (
                f"[0:v]scale={up_w}:{up_h}:flags=lanczos,"
                f"zoompan=z='{z_expr}':x='iw/2-iw/(2*zoom)':y='ih/2-ih/(2*zoom)':"
                f"d={total_frames}:s={fit_w}x{fit_h}:fps={fps},"
                f"fade=t=in:st=0:d={fade_d:.3f},"
                f"fade=t=out:st={fade_out_st:.3f}:d={fade_d:.3f}"
                f"[fg];"
                f"[1:v][fg]overlay=x={fg_x}:y={fg_y}"
            )
            cmd = [
                ffmpeg_bin, "-y",
                "-sws_flags", "lanczos",
                "-f", "image2", "-loop", "1", "-t", str(duration), "-i", tmp_pre,
                "-f", "image2", "-loop", "1", "-t", str(duration), "-i", tmp_bg,
                "-filter_complex", filter_complex,
                "-t", str(duration), "-r", str(fps),
                "-c:v", codec, "-preset", "fast", "-an",
                "-threads", str(threads), output_path,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                logger.warning(f"FFmpeg Ken Burns (fade) stderr: {proc.stderr[-600:]}")
                return ""
            return output_path if os.path.exists(output_path) else ""

        # Pan/zoom FIT animations — 2× canvas + crop(t) + overlay on blurred background.
        if animation == "pan_lr":
            cw = int(fit_w * 2 * _PAN_Z / 2) * 2
            ch = fit_h * 2
            travel = cw - fit_w * 2
            canvas_img = _cover_crop_image(img, cw, ch)
            filter_fg = (
                f"[0:v]crop=w={fit_w * 2}:h={ch}:x='{travel}*{eot}':y=0,"
                f"scale={fit_w}:{fit_h}:flags=lanczos[fg]"
            )
        elif animation == "pan_rl":
            cw = int(fit_w * 2 * _PAN_Z / 2) * 2
            ch = fit_h * 2
            travel = cw - fit_w * 2
            canvas_img = _cover_crop_image(img, cw, ch)
            filter_fg = (
                f"[0:v]crop=w={fit_w * 2}:h={ch}:x='{travel}*(1-{eot})':y=0,"
                f"scale={fit_w}:{fit_h}:flags=lanczos[fg]"
            )
        elif animation == "pan_ud":
            cw = fit_w * 2
            ch = int(fit_h * 2 * _PAN_Z / 2) * 2
            travel = ch - fit_h * 2
            canvas_img = _cover_crop_image(img, cw, ch)
            filter_fg = (
                f"[0:v]crop=w={cw}:h={fit_h * 2}:x=0:y='{travel}*{eot}',"
                f"scale={fit_w}:{fit_h}:flags=lanczos[fg]"
            )
        elif animation == "zoom_out":
            _uz = 8
            up_w = fit_w * _uz
            up_h = fit_h * _uz
            total_frames = max(int(round(duration * fps)), 1)
            d_minus_1 = max(total_frames - 1, 1)
            z_expr = f"1.0+{_PAN_Z - 1.0:.4f}*pow(1-on/{d_minus_1},2)"
            canvas_img = img.resize((fit_w, fit_h), _PILImage.LANCZOS)
            filter_fg = (
                f"[0:v]scale={up_w}:{up_h}:flags=lanczos,"
                f"zoompan=z='{z_expr}':x='iw/2-iw/(2*zoom)':y='ih/2-ih/(2*zoom)':"
                f"d={total_frames}:s={fit_w}x{fit_h}:fps={fps}[fg]"
            )

        else:  # zoom_in — 8× zoompan for sub-pixel accuracy.
            _uz = 8
            up_w = fit_w * _uz
            up_h = fit_h * _uz
            total_frames = max(int(round(duration * fps)), 1)
            d_minus_1 = max(total_frames - 1, 1)
            z_expr = f"1.0+{_PAN_Z - 1.0:.4f}*(1-pow(1-on/{d_minus_1},2))"
            canvas_img = img.resize((fit_w, fit_h), _PILImage.LANCZOS)
            filter_fg = (
                f"[0:v]scale={up_w}:{up_h}:flags=lanczos,"
                f"zoompan=z='{z_expr}':x='iw/2-iw/(2*zoom)':y='ih/2-ih/(2*zoom)':"
                f"d={total_frames}:s={fit_w}x{fit_h}:fps={fps}[fg]"
            )

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            tmp_pre = fh.name
        canvas_img.save(tmp_pre)

        filter_complex = f"{filter_fg};[1:v][fg]overlay=x={fg_x}:y={fg_y}"
        cmd = [
            ffmpeg_bin, "-y",
            "-sws_flags", "lanczos",
            "-f", "image2", "-loop", "1", "-t", str(duration), "-i", tmp_pre,
            "-f", "image2", "-loop", "1", "-t", str(duration), "-i", tmp_bg,
            "-filter_complex", filter_complex,
            "-t", str(duration), "-r", str(fps),
            "-c:v", codec, "-preset", "fast", "-an",
            "-threads", str(threads), output_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            logger.warning(f"FFmpeg Ken Burns stderr: {proc.stderr[-600:]}")
            return ""

    finally:
        for p in (tmp_pre, tmp_bg):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass

    return output_path if os.path.exists(output_path) else ""


def render_ken_burns_clip(
    image_path: str,
    duration: float,
    width: int,
    height: int,
    output_path: str,
    threads: int = 2,
    effect: str = "",
) -> str:
    """
    Render a Ken Burns clip from image_path to an MP4 at output_path.
    Returns output_path on success, '' on failure.

    Portrait images (h ≥ w): FIT-scaled to 95% of frame, centered on a
    blurred background, no animation — unchanged from before.

    Landscape images (w > h): cover-crop fills the full frame; animation is
    chosen randomly from a pool derived from the image's aspect-ratio overflow:
      - zoom_in: always available (adds 4% extra zoom on the cover-cropped base)
      - pan_lr / pan_rl: when the image is wider than the frame (h_excess > 2%)
      - pan_ud: when the image is taller than the frame after cover-scale (v_excess > 2%)
    Consecutive landscape clips get different animation types (no-repeat tracking).
    """
    from PIL import Image as _PILImg
    with _PILImg.open(image_path) as _im:
        is_landscape = _im.width > _im.height
        img_w, img_h = _im.width, _im.height

    if not is_landscape:
        # Portrait: FIT at 95%, blurred background, fade/zoom.
        frame_scale = 0.95
        animation = _pick_animation(["fade", "zoom_in", "zoom_out", "static"])
    else:
        # Landscape: cover-crop + random animation from overflow-derived pool.
        cover_scale = max(width / img_w, height / img_h)
        h_excess = max(0.0, img_w * cover_scale - width)
        v_excess = max(0.0, img_h * cover_scale - height)

        allowed = ["zoom_in", "screen_3d_lr", "screen_3d_ud", "fade"]
        if h_excess > width * 0.02:
            allowed.extend(["pan_lr", "pan_rl"])
        if v_excess > height * 0.02:
            allowed.append("pan_ud")

        animation = _pick_animation(allowed, effect)
        frame_scale = 1.0
        logger.info(
            f"Ken Burns: landscape cover-mode, animation={animation}, "
            f"h_excess={h_excess:.0f}px, v_excess={v_excess:.0f}px, "
            f"pool={allowed}"
        )

    # 3D screen-mockup presets use PIL per-frame rendering, not FFmpeg filters.
    if animation in ("screen_3d_lr", "screen_3d_ud"):
        ok = _render_3d_effect(image_path, duration, width, height, output_path, animation)
        if ok:
            return output_path
        logger.warning(f"3D effect failed for {image_path}, falling back to pan/zoom")
        allowed_fallback = [a for a in allowed if "screen_3d" not in a]
        animation = _pick_animation(allowed_fallback or ["zoom_in"], effect)

    # Prefer FFmpeg (sub-pixel smooth motion) over MoviePy (integer rounding jitter).
    try:
        result = _render_ken_burns_ffmpeg(
            image_path, duration, width, height, output_path, threads,
            frame_scale=frame_scale,
            animation=animation,
        )
        if result:
            return result
        logger.warning(f"FFmpeg Ken Burns returned empty for {image_path}, falling back to MoviePy")
    except Exception as exc:
        logger.warning(f"FFmpeg Ken Burns exception for {image_path}: {exc} — falling back to MoviePy")

    clip = apply_ken_burns(image_path, duration, width, height, frame_scale=frame_scale, animation=animation)
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
    except Exception as exc:
        logger.warning(f"Ken Burns render failed for {image_path}: {str(exc)}")
    finally:
        close_clip(clip)
    return output_path if os.path.exists(output_path) else ""


_OVERLAY_FADE_DUR = 0.5   # seconds — fade-in at start, fade-out at end


def _probe_duration(path: str) -> float | None:
    """Return media file duration in seconds, or None on failure."""
    try:
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", path],
            capture_output=True, text=True, timeout=15,
        )
        return float(pr.stdout.strip())
    except Exception:
        return None


def apply_visual_effect(
    clip_path: str,
    effect: str,
    output_path: str,
    width: int = 1920,
    height: int = 1080,
    threads: int = 4,
) -> str:
    """Composite a motion overlay onto clip_path for its full natural duration.

    The overlay plays ONCE from start to finish (no looping). If the clip is
    shorter than the overlay, the overlay is trimmed to fit. If the clip is
    longer, the overlay plays for its natural duration, then the remainder of
    the clip plays clean via a neutral-color pad that makes the blend a
    mathematical identity:
      - screen blend: pad with black  (screen(clip, 0) = clip)
      - multiply blend: pad with white (multiply(clip, 1) = clip)

    A fade-in and fade-out of _OVERLAY_FADE_DUR seconds is applied.
    Returns output_path on success, clip_path unchanged on any failure.
    Audio is passed through unchanged.
    """
    if effect not in _VALID_VISUAL_EFFECTS:
        return clip_path

    overlay_cfg = _EFFECT_OVERLAYS.get(effect)
    if not overlay_cfg:
        return clip_path

    overlay_path = os.path.join(_OVERLAY_DIR, overlay_cfg["file"])
    if not os.path.exists(overlay_path):
        logger.warning(f"apply_visual_effect({effect}): overlay not found — {overlay_path}")
        return clip_path

    clip_dur = _probe_duration(clip_path)
    ov_dur   = _probe_duration(overlay_path)
    if not clip_dur or not ov_dur or clip_dur <= 0 or ov_dur <= 0:
        logger.warning(f"apply_visual_effect({effect}): duration probe failed")
        return clip_path

    blend_mode = overlay_cfg["mode"]
    opacity    = overlay_cfg["opacity"]

    # Play the full overlay once, trimmed to clip duration if the clip is shorter.
    accent = min(ov_dur, clip_dur)
    # Clamp fade so it never exceeds 25% of the overlay window.
    fade   = min(_OVERLAY_FADE_DUR, accent / 4)
    fade_out_start = max(0.0, accent - fade)

    # ── Build filter_complex ────────────────────────────────────────────────
    # Keep everything in gbrp (planar RGB) so the blend operates in RGB colour
    # space, matching what Filmora and other NLEs do. Blending in YUV applies
    # the screen/multiply formula to offset chroma channels and introduces a
    # colour cast (typically purple/teal).
    chains: list[str] = []

    # Scale, trim to accent duration, and fade in/out.
    chains.append(
        f"[0:v]scale={width}:{height},format=gbrp,"
        f"trim=0:{accent:.6f},setpts=PTS-STARTPTS,"
        f"fade=t=in:st=0:d={fade:.3f},"
        f"fade=t=out:st={fade_out_start:.6f}:d={fade:.3f}"
        f"[_ov_trimmed]"
    )

    # Pad the remainder of the clip with a neutral color so the blend is a
    # mathematical identity after the accent window ends.
    if clip_dur > accent:
        pad_dur   = clip_dur - accent
        # screen(clip, black)=clip; multiply(clip, white)=clip
        pad_color = "white" if blend_mode == "multiply" else "black"
        chains.append(
            f"color=c={pad_color}:s={width}x{height}:r=30:d={pad_dur:.6f},"
            f"format=gbrp[_ov_pad]"
        )
        chains.append(
            f"[_ov_trimmed][_ov_pad]concat=n=2:v=1:a=0,setpts=PTS-STARTPTS[_ov_final]"
        )
        ov_label = "_ov_final"
    else:
        ov_label = "_ov_trimmed"

    # Blend and convert back to yuv420p for the encoder.
    # Scale the clip to match the overlay — Pexels sometimes delivers non-standard
    # resolutions (e.g. 2048×1080) that would cause the blend to fail with -22.
    chains.append(f"[1:v]scale={width}:{height},format=gbrp[_clip]")
    chains.append(
        f"[_clip][{ov_label}]"
        f"blend=all_mode={blend_mode}:all_opacity={opacity},"
        f"format=yuv420p"
        f"[out]"
    )

    filter_complex = ";".join(chains)

    codec = _get_configured_video_codec()
    cmd = [
        utils.get_ffmpeg_binary(), "-y",
        "-i", overlay_path,
        "-i", clip_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "1:a?", "-c:a", "copy",
        "-t", f"{clip_dur:.6f}",
        "-c:v", codec, *_fast_preset_args(codec),
        "-pix_fmt", "yuv420p",
        "-threads", str(threads),
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-400:]
            # FFmpeg sometimes receives SIGTERM from the process manager after
            # encoding completes but before it exits cleanly.  If the output
            # file exists and its duration is within 10% of the target, treat
            # it as a success rather than discarding a valid encode.
            if os.path.exists(output_path):
                got_dur = _probe_duration(output_path)
                if got_dur and abs(got_dur - clip_dur) / clip_dur < 0.10:
                    logger.warning(
                        f"apply_visual_effect({effect}) non-zero exit but output is valid "
                        f"({got_dur:.2f}s ≈ {clip_dur:.2f}s) — using it"
                    )
                    return output_path
            logger.error(f"apply_visual_effect({effect}) failed: {stderr}")
            return clip_path
        return output_path
    except Exception as exc:
        logger.error(f"apply_visual_effect({effect}) exception: {exc}")
        return clip_path


# Duration constants for lower_third compositing.
_LT_ANIM_IN  = 0.40   # seconds — fade-in
_LT_ANIM_OUT = 0.35   # seconds — fade-out

# Optional user-supplied full-frame RGBA blob PNG for the lower_third backdrop.
_LT_BLOB_PNG = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "resource", "graphics", "lower_third_shadow.png")
)


def composite_lower_third(
    footage_path: str,
    graphic_path: str,
    output_path: str,
    width: int,
    height: int,
    threads: int = 4,
) -> Optional[str]:
    """
    Composite a lower_third Revideo clip (white text on black) over stock footage.

    Two-pass blend strategy — no chroma key needed:
      1. Blob PNG (RGBA): overlaid on footage with its native alpha channel.
         FFmpeg `overlay` respects the PNG's alpha, so the feathered blob appears
         correctly without any colour contamination.
      2. Text clip (white on black): composited via `screen` blend.
         screen(footage, black=0) = footage  → black bg disappears
         screen(footage, white=1) = white    → text stays white

    Both the blob and the text fade in/out via separate FFmpeg `fade` filters
    so they stay in sync.  Falls back to text-only (no blob) if the PNG is absent.
    """
    codec = _get_configured_video_codec()
    footage_dur = _probe_duration(footage_path)
    gfx_dur     = _probe_duration(graphic_path)
    if not footage_dur or not gfx_dur:
        logger.warning("composite_lower_third: could not probe clip durations")
        return None

    fi       = min(_LT_ANIM_IN,  gfx_dur / 4)
    fo       = min(_LT_ANIM_OUT, gfx_dur / 4)
    fo_start = max(0.0, gfx_dur - fo)

    has_blob = os.path.isfile(_LT_BLOB_PNG)

    if has_blob:
        # Input 0: footage | Input 1: Revideo text clip | Input 2: blob PNG (via -loop 1)
        # IMPORTANT: use gbrp (not rgba/rgb24) for screen blend inputs — blend=all_mode=screen
        # operates on ALL channels; rgba corrupts the alpha channel into the output, and rgb24
        # causes incorrect YUV↔RGB range conversion. gbrp is the correct planar RGB format.
        filter_complex = (
            # Footage: scale to target, gbrp (planar RGB, correct for screen blend)
            f"[0:v]scale={width}:{height},format=gbrp[footage];"
            # Blob PNG: RGBA is needed so overlay can use the native alpha channel
            f"[2:v]scale={width}:{height},format=rgba,"
            f"fade=t=in:st=0:d={fi:.3f}:alpha=1,"
            f"fade=t=out:st={fo_start:.3f}:d={fo:.3f}:alpha=1[blob];"
            # Overlay blob using its native alpha; convert result to gbrp (drop alpha)
            f"[footage][blob]overlay=0:0,format=gbrp[with_blob];"
            # Revideo text clip: gbrp (white text on black), fade RGB values for screen blend
            f"[1:v]scale={width}:{height},format=gbrp,"
            f"fade=t=in:st=0:d={fi:.3f},"
            f"fade=t=out:st={fo_start:.3f}:d={fo:.3f}[text];"
            # screen(footage_with_blob, black=0)=footage; screen(…, white=1)=white
            f"[with_blob][text]blend=all_mode=screen,format=yuv420p[out]"
        )
        cmd = [
            utils.get_ffmpeg_binary(), "-y",
            "-i", footage_path,
            "-i", graphic_path,
            "-loop", "1", "-t", f"{gfx_dur + 0.1:.3f}", "-i", _LT_BLOB_PNG,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "0:a?", "-c:a", "copy",
            "-t", f"{footage_dur:.6f}",
            "-c:v", codec, *_fast_preset_args(codec),
            "-pix_fmt", "yuv420p",
            "-threads", str(threads),
            output_path,
        ]
    else:
        # No blob PNG — just screen-blend the text over footage
        filter_complex = (
            f"[0:v]scale={width}:{height},format=gbrp[footage];"
            f"[1:v]scale={width}:{height},format=gbrp,"
            f"fade=t=in:st=0:d={fi:.3f},"
            f"fade=t=out:st={fo_start:.3f}:d={fo:.3f}[text];"
            f"[footage][text]blend=all_mode=screen,format=yuv420p[out]"
        )
        cmd = [
            utils.get_ffmpeg_binary(), "-y",
            "-i", footage_path,
            "-i", graphic_path,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "0:a?", "-c:a", "copy",
            "-t", f"{footage_dur:.6f}",
            "-c:v", codec, *_fast_preset_args(codec),
            "-pix_fmt", "yuv420p",
            "-threads", str(threads),
            output_path,
        ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            logger.warning(
                f"composite_lower_third failed (exit {result.returncode}): {stderr[-400:]}"
            )
            return None
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.warning("composite_lower_third: output file is empty")
            return None
        return output_path
    except Exception as exc:
        logger.warning(f"composite_lower_third exception: {exc}")
        return None


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

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]

    for file in files:
        try:
            os.remove(file)
        except Exception as e:
            logger.debug(f"failed to delete file {file}: {str(e)}")


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
        song_dir = utils.song_dir()
        files = glob.glob(os.path.join(song_dir, "*.mp3"))
        # 当背景音乐目录为空时，直接回退为“不使用 BGM”，避免 random.choice([]) 抛异常。
        if not files:
            logger.warning(f"no bgm files found in song directory: {song_dir}")
            return ""
        return random.choice(files)

    return ""


# Each entry maps a VideoTransitionMode value to a `(clip, shuffle_side) -> clip`
# transition function. VideoTransitionMode.shuffle resolves to a random pick
# from _SHUFFLE_TRANSITIONS instead of a single function.
_TRANSITION_DISPATCH = {
    VideoTransitionMode.fade_in.value: lambda clip, side: video_effects.fadein_transition(clip, _CLIP_TRANSITION_SECONDS),
    VideoTransitionMode.fade_out.value: lambda clip, side: video_effects.fadeout_transition(clip, _CLIP_TRANSITION_SECONDS),
    VideoTransitionMode.slide_in.value: lambda clip, side: video_effects.slidein_transition(clip, _CLIP_TRANSITION_SECONDS, side),
    VideoTransitionMode.slide_out.value: lambda clip, side: video_effects.slideout_transition(clip, _CLIP_TRANSITION_SECONDS, side),
    VideoTransitionMode.shuffle.value: "shuffle",
}

_SHUFFLE_TRANSITIONS = [
    lambda clip, side: video_effects.fadein_transition(clip, _CLIP_TRANSITION_SECONDS),
    lambda clip, side: video_effects.fadeout_transition(clip, _CLIP_TRANSITION_SECONDS),
    lambda clip, side: video_effects.slidein_transition(clip, _CLIP_TRANSITION_SECONDS, side),
    lambda clip, side: video_effects.slideout_transition(clip, _CLIP_TRANSITION_SECONDS, side),
]


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
            # 这样既不会丢掉”整段视频本身就短于 max_clip_duration”的素材，
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

        shuffle_side = random.choice(["left", "right", "top", "bottom"])
        transition_func = _TRANSITION_DISPATCH.get(transition_value)
        if transition_func == "shuffle":
            transition_func = random.choice(_SHUFFLE_TRANSITIONS)

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

    # Use typographic line height from font metrics rather than the ink
    # bounding box: getbbox() measures only ink pixels (~22 px for Inter 30 pt)
    # but PIL's renderer uses ascent+descent (~35 px).  For multi-line subtitles
    # the difference compounds per line and causes visible bottom cropping.
    ascent, descent = font.getmetrics()
    true_line_h = ascent + descent

    width, _ = get_text_size(text)
    if width <= max_width:
        return text, true_line_h

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
    return result, len(lines) * true_line_h


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
        parsed.append((_srt_time_to_seconds(parts[0]), _srt_time_to_seconds(parts[1]), text.strip()))

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
        clip_h = int(txt_height + vertical_padding + interline * max(0, line_count - 1))
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
        # interline is spacing between lines — N lines have N-1 gaps, not N.
        # txt_height already uses getmetrics() line height so no further fudge needed.
        clip_h = int(txt_height + vertical_padding + interline * max(0, line_count - 1))
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

    if subtitle_path and os.path.exists(subtitle_path):
        text_clips = [
            create_text_clip(item)
            for item in _parse_srt(subtitle_path, encoding="utf-8")
        ]

        if params.subtitle_highlight and font_path:
            highlight_clips = _build_word_highlight_clips(
                subtitle_path, params, video_width, video_height, font_path
            )
            # Highlights go before text so text renders on top of the boxes.
            video_clip = CompositeVideoClip([video_clip, *highlight_clips, *text_clips])
        else:
            video_clip = CompositeVideoClip([video_clip, *text_clips])

    # Fade the video to black over the last _OUTRO_FADEOUT_SECONDS of the 2 s outro tail.
    video_clip = video_clip.with_effects([vfx.FadeOut(_OUTRO_FADEOUT_SECONDS)])

    voice_audio_clip = audio_clip
    bgm_audio_clip = None
    bgm_file = get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)
    if bgm_file:
        try:
            duck_ratio = float(config.app.get("bgm_duck_ratio", 0.15))
            has_subtitles = bool(subtitle_path and os.path.exists(subtitle_path))
            if has_subtitles and duck_ratio < 1.0:
                logger.info(f"applying BGM ducking: {duck_ratio:.0%} during narration")
                bgm_clip, bgm_audio_clip = _make_ducked_bgm(
                    bgm_file=bgm_file,
                    audio_duration=video_clip.duration,
                    subtitle_path=subtitle_path,
                    bgm_volume=params.bgm_volume,
                    duck_to=duck_ratio,
                )
            else:
                bgm_audio_clip = AudioFileClip(bgm_file)
                bgm_clip = bgm_audio_clip.with_effects(
                    [
                        afx.MultiplyVolume(params.bgm_volume),
                        afx.AudioFadeOut(_BGM_FADEOUT_SECONDS),
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
    voice_audio_clip.close()
    if bgm_audio_clip is not None:
        bgm_audio_clip.close()
    del video_clip
