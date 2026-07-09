import glob
import json
import math
import os
import random
from typing import Optional

import numpy as np
from loguru import logger
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    afx,
    vfx,
)
from moviepy.video.tools.subtitles import file_to_subtitles as _parse_srt
from PIL import Image, ImageDraw, ImageFont

from app.config import config
from app.models.schema import VideoAspect, VideoParams
from app.utils import file_security, utils

from ._common import (
    _BGM_EXTENSIONS,
    _BGM_FADEOUT_SECONDS,
    _OUTRO_FADEOUT_SECONDS,
    _get_configured_video_codec,
    _open_video_clip_quietly,
    _srt_time_to_seconds,
    _write_videofile_with_codec_fallback,
    audio_bitrate,
    audio_codec,
    close_clip,
    fps,
)


def get_bgm_file(bgm_type: str = "random", bgm_file: str = "", rng: random.Random = random):
    if not bgm_type:
        return ""

    if bgm_file:
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
        if not files:
            logger.warning(f"no bgm files found in song directory: {song_dir}")
            return ""
        return rng.choice(files)

    return ""


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
    """
    from moviepy.audio.AudioClip import AudioClip as _AudioClip
    from app.utils.subtitle import file_to_subtitles

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
        if np.ndim(t) == 0:
            idx = min(int(t * _SR), n - 1)
            return frame * float(envelope[idx])
        indices = np.clip((np.asarray(t) * _SR).astype(int), 0, n - 1)
        vols = envelope[indices]
        return frame * (vols[:, np.newaxis] if frame.ndim == 2 else vols)

    ducked = _AudioClip(frame_function=make_frame, duration=audio_duration, fps=_SR)
    return ducked, bgm_raw


def wrap_text(text, max_width, font="Arial", fontsize=60):
    font = ImageFont.truetype(font, fontsize)
    max_width = int(max_width)

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        if not inner_text:
            return 0, fontsize
        left, top, right, bottom = font.getbbox(inner_text)
        return right - left, bottom - top

    ascent, descent = font.getmetrics()
    true_line_h = ascent + descent

    width, _ = get_text_size(text)
    if width <= max_width:
        return text, true_line_h

    def split_long_token(token):
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
    from app.utils import subtitle as _subtitle_svc

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

    parsed = []
    for entry in srt_entries:
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

    def _subtitle_base_y(clip_h):
        if params.subtitle_position == "bottom":
            return video_height * 0.95 - clip_h
        if params.subtitle_position == "top":
            return video_height * 0.05
        if params.subtitle_position == "custom":
            margin = 10
            custom_y = (video_height - clip_h) * (params.custom_position / 100)
            return max(margin, min(custom_y, video_height - clip_h - margin))
        return (video_height - clip_h) / 2

    def _norm(s):
        return "".join(c for c in s.lower() if c.isalnum())

    occurrence_counter: dict = {}

    highlights = []
    for word_entry in words:
        w_text = word_entry.get("word", "").strip()
        w_start = float(word_entry.get("start", 0))
        w_end = float(word_entry.get("end", w_start + 0.1))
        if not w_text or w_start >= w_end:
            continue

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

        occ_key = (host[0], host[1], w_norm)
        target_occurrence = occurrence_counter.get(occ_key, 0)
        occurrence_counter[occ_key] = target_occurrence + 1

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
                        try:
                            before_w = font.getbbox(line[:char_offset].rstrip())[2] if char_offset > 0 else 0
                            word_w = max(1, font.getbbox(lw)[2] - font.getbbox(lw)[0])
                            line_w = max(1, font.getbbox(line)[2] - font.getbbox(line)[0])
                        except Exception:
                            break

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
                char_offset += len(lw) + 1

    logger.info(f"built {len(highlights)} word highlight clips")
    return highlights


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
    rng: random.Random = random,
):
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

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
        clip_h = int(txt_height + vertical_padding + interline * max(0, line_count - 1))
        bg_color = resolve_subtitle_background_color()
        rounded_bg_enabled = bool(
            getattr(params, "rounded_subtitle_background", False) and bg_color
        )

        if rounded_bg_enabled:
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
            size = (int(max_width), clip_h)
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
            margin = 10
            max_y = video_height - _clip.h - margin
            min_y = margin
            custom_y = (video_height - _clip.h) * (params.custom_position / 100)
            custom_y = max(min_y, min(custom_y, max_y))
            _clip = _clip.with_position(("center", custom_y))
        else:
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
            video_clip = CompositeVideoClip([video_clip, *highlight_clips, *text_clips])
        else:
            video_clip = CompositeVideoClip([video_clip, *text_clips])

    video_clip = video_clip.with_effects([vfx.FadeOut(_OUTRO_FADEOUT_SECONDS)])

    voice_audio_clip = audio_clip
    bgm_audio_clip = None
    bgm_file = get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file, rng=rng)
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
