import glob
import json
import math
import os
import random
import shutil
import subprocess
import tempfile
import wave
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
    _fast_preset_args,
    _get_configured_video_codec,
    _get_effective_video_codec,
    _open_video_clip_quietly,
    _probe_duration,
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

    # End-of-video fade-out, matching the AudioFadeOut the non-ducked BGM path
    # applies — without this the music cuts hard on the final frame.
    fade_out_n = min(n, max(1, int(_BGM_FADEOUT_SECONDS * _SR)))
    envelope[n - fade_out_n:] *= np.linspace(1.0, 0.0, fade_out_n)

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


_FFMPEG_FINAL_RENDER_TIMEOUT = 30 * 60  # seconds


def _render_bgm_wav(
    bgm_file: str,
    duration: float,
    subtitle_path: str,
    bgm_volume: float,
    duck_ratio: float,
    out_path: str,
) -> bool:
    """Pre-render looped + ducked + faded BGM to a 16-bit WAV.

    Same envelope math as _make_ducked_bgm(), but applied offline to raw PCM
    so the final render is a single ffmpeg pass instead of MoviePy pulling
    audio frames through a Python callback. Returns True on success.
    """
    from app.utils.subtitle import file_to_subtitles

    _SR = 44100
    decode = subprocess.run(
        [
            utils.get_ffmpeg_binary(), "-y", "-loglevel", "error",
            "-stream_loop", "-1", "-i", bgm_file,
            "-t", f"{duration:.3f}",
            "-ar", str(_SR), "-ac", "2",
            "-f", "s16le", "pipe:1",
        ],
        capture_output=True, timeout=300,
    )
    if decode.returncode != 0 or not decode.stdout:
        logger.warning(f"BGM decode failed: {decode.stderr[-200:] if decode.stderr else 'empty output'}")
        return False

    samples = np.frombuffer(decode.stdout, dtype=np.int16).reshape(-1, 2).astype(np.float64)
    # mp3 frame granularity / encoder delay make the looped decode land a few
    # hundredths off the requested duration in either direction — trim or
    # silence-pad to the exact sample count so the BGM always matches the
    # video (the pad falls inside the fade-out anyway).
    n_target = int(duration * _SR)
    samples = samples[:n_target]
    if len(samples) < n_target:
        samples = np.vstack(
            [samples, np.zeros((n_target - len(samples), 2), dtype=np.float64)]
        )
    n = len(samples)

    envelope = np.full(n, bgm_volume, dtype=np.float64)
    if subtitle_path and os.path.exists(subtitle_path) and duck_ratio < 1.0:
        duck_vol = bgm_volume * duck_ratio
        fade_n = max(1, int(0.25 * _SR))
        for _, time_str, _ in file_to_subtitles(subtitle_path):
            parts = time_str.split(" --> ")
            if len(parts) != 2:
                continue
            s = max(0, int(_srt_time_to_seconds(parts[0].strip()) * _SR))
            e = min(n, int(_srt_time_to_seconds(parts[1].strip()) * _SR))
            if e <= s:
                continue
            envelope[s:e] = duck_vol
            half = (e - s) // 2
            fl = min(fade_n, half)
            if fl > 0:
                envelope[s:s + fl] = np.linspace(bgm_volume, duck_vol, fl)
                envelope[e - fl:e] = np.linspace(duck_vol, bgm_volume, fl)

    fade_out_n = min(n, max(1, int(_BGM_FADEOUT_SECONDS * _SR)))
    envelope[n - fade_out_n:] *= np.linspace(1.0, 0.0, fade_out_n)

    mixed = np.clip(samples * envelope[:, np.newaxis], -32768, 32767).astype(np.int16)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(_SR)
        wf.writeframes(mixed.tobytes())
    return True


# Longer library assets (e.g. ticking-clock at ~9s, ambience loops like
# noir-rain at 90s) are one-shot *accents*, not beds — cap every cue to this
# many seconds so a long source file doesn't turn into background ambience.
_SFX_MAX_CUE_SECONDS = 3.5
_SFX_FADE_OUT_SECONDS = 0.25  # tail fade so a mid-file trim doesn't click


def _render_sfx_bed_wav(
    cues: list,
    duration: float,
    out_path: str,
    volume_scale: float = 1.0,
) -> bool:
    """Pre-render one-shot SFX cues into a single full-duration 16-bit WAV bed.

    Mirrors _render_bgm_wav()'s "decode offline, mix as PCM, single ffmpeg
    pass" approach rather than adding one raw -i input per cue to the final
    ffmpeg command. `cues` is a list of (start_seconds, file_path, volume)
    tuples — SFX are NOT ducked against narration (see sfx.py docstring: an
    accent is meant to cut through, not sit under the mix). Returns True if
    at least one cue rendered; False (and no file written) if the cue list
    is empty or every cue failed to decode.
    """
    _SR = 44100
    n_target = int(duration * _SR)
    if n_target <= 0 or not cues:
        return False

    bed = np.zeros((n_target, 2), dtype=np.float64)
    any_mixed = False
    max_cue_samples = int(_SFX_MAX_CUE_SECONDS * _SR)
    fade_samples = int(_SFX_FADE_OUT_SECONDS * _SR)

    for start_seconds, file_path, cue_volume in cues:
        decode = subprocess.run(
            [
                utils.get_ffmpeg_binary(), "-y", "-loglevel", "error",
                "-i", file_path,
                "-ar", str(_SR), "-ac", "2",
                "-f", "s16le", "pipe:1",
            ],
            capture_output=True, timeout=60,
        )
        if decode.returncode != 0 or not decode.stdout:
            logger.warning(
                f"SFX decode failed for {file_path}: "
                f"{decode.stderr[-200:] if decode.stderr else 'empty output'}"
            )
            continue

        samples = np.frombuffer(decode.stdout, dtype=np.int16).reshape(-1, 2).astype(np.float64)
        if len(samples) > max_cue_samples:
            samples = samples[:max_cue_samples].copy()
            fade_len = min(fade_samples, len(samples))
            if fade_len > 0:
                fade = np.linspace(1.0, 0.0, fade_len)[:, None]
                samples[-fade_len:] *= fade
        start_idx = max(0, int(start_seconds * _SR))
        if start_idx >= n_target:
            continue
        end_idx = min(n_target, start_idx + len(samples))
        clip_len = end_idx - start_idx
        if clip_len <= 0:
            continue
        bed[start_idx:end_idx] += samples[:clip_len] * (cue_volume * volume_scale)
        any_mixed = True

    if not any_mixed:
        return False

    mixed = np.clip(bed, -32768, 32767).astype(np.int16)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(_SR)
        wf.writeframes(mixed.tobytes())
    return True


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' → ASS '&HAABBGGRR' (ASS is BGR with inverted alpha)."""
    r, g, b = _hex_to_rgb(hex_color or "#FFFFFF")
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _ffmpeg_render_eligible(params: VideoParams, subtitle_path: str) -> bool:
    """The ffmpeg-native final render covers the common configurations; the
    remaining decorations still need the MoviePy compositor."""
    if not config.app.get("ffmpeg_final_render", True):
        return False
    if not (subtitle_path and os.path.exists(subtitle_path)):
        return True  # no subtitles at all — trivially supported
    if params.subtitle_enabled and params.subtitle_highlight:
        return False  # per-word highlight boxes
    if getattr(params, "rounded_subtitle_background", False):
        return False  # Pillow-drawn rounded boxes
    if params.subtitle_position == "custom":
        return False  # percentage positioning not mapped to ASS margins
    return True


def _generate_video_ffmpeg(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
    rng: random.Random = random,
    sfx_cues: Optional[list] = None,
) -> None:
    """Single-pass ffmpeg final render: subtitle burn-in (libass), fade-out,
    voice + pre-rendered ducked BGM mix. Replaces the MoviePy frame loop,
    which spent ~11 minutes on a 13-minute video even with subtitles off.
    Raises on any failure — generate_video() falls back to MoviePy.
    """
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()
    video_duration = _probe_duration(video_path)
    if not video_duration:
        raise RuntimeError(f"could not probe video duration: {video_path}")

    output_dir = os.path.dirname(output_file)
    tmp_files = []
    try:
        # ---- video filters ------------------------------------------------ #
        vf_parts = []
        burn_subs = bool(
            subtitle_path and os.path.exists(subtitle_path) and params.subtitle_enabled
        )
        if burn_subs:
            font_path = os.path.join(
                utils.font_dir(), params.font_name or "Inter_18pt-SemiBold.ttf"
            )
            try:
                font_family = ImageFont.truetype(font_path, 20).getname()[0]
            except Exception:
                font_family = "sans-serif"

            # libass converts SRT with PlayResY=288 — scale pixel sizes to it.
            ass_scale = 288.0 / video_height
            font_size = max(1, round(int(params.font_size) * ass_scale * 1.3))
            outline = max(0, round(float(params.stroke_width) * ass_scale * 1.3))
            margin_v = round(video_height * 0.05 * ass_scale)
            alignment = {"bottom": 2, "top": 8}.get(params.subtitle_position, 5)

            style_parts = [
                f"FontName={font_family}",
                f"FontSize={font_size}",
                f"PrimaryColour={_ass_color(params.text_fore_color)}",
                f"OutlineColour={_ass_color(params.stroke_color)}",
                f"Outline={outline}",
                "BorderStyle=1",
                f"Alignment={alignment}",
                f"MarginV={margin_v}",
            ]
            bg_color = (
                ("#000000" if params.text_background_color else None)
                if isinstance(params.text_background_color, bool)
                else params.text_background_color
            )
            if bg_color:
                # BorderStyle=3: libass draws an opaque box in BackColour.
                style_parts[5] = "BorderStyle=3"
                style_parts.append(f"BackColour={_ass_color(bg_color, alpha=0x50)}")

            # The subtitles filter chokes on quotes/commas in filenames and
            # task dirs contain both — hand it a copy at a safe temp path.
            fd, safe_srt = tempfile.mkstemp(suffix=".srt", prefix="ampt-subs-")
            os.close(fd)
            shutil.copyfile(subtitle_path, safe_srt)
            tmp_files.append(safe_srt)

            fonts_dir = utils.font_dir()
            vf_parts.append(
                f"subtitles={safe_srt}:fontsdir={fonts_dir}"
                f":force_style='{','.join(style_parts)}'"
            )

        fade_start = max(0.0, video_duration - _OUTRO_FADEOUT_SECONDS)
        vf_parts.append(f"fade=t=out:st={fade_start:.3f}:d={_OUTRO_FADEOUT_SECONDS}")

        # ---- audio -------------------------------------------------------- #
        bgm_file = get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file, rng=rng)
        bgm_wav = ""
        if bgm_file:
            duck_ratio = float(config.app.get("bgm_duck_ratio", 0.15))
            candidate = os.path.join(output_dir, "temp-bgm-ducked.wav")
            if _render_bgm_wav(
                bgm_file=bgm_file,
                duration=video_duration,
                subtitle_path=subtitle_path if burn_subs else "",
                bgm_volume=params.bgm_volume,
                duck_ratio=duck_ratio,
                out_path=candidate,
            ):
                bgm_wav = candidate
                tmp_files.append(candidate)
                logger.info(f"BGM pre-rendered with duck envelope: {duck_ratio:.0%} during narration")
            else:
                logger.warning("BGM pre-render failed — continuing without BGM")

        sfx_wav = ""
        if sfx_cues and config.app.get("sfx_enabled", True):
            sfx_candidate = os.path.join(output_dir, "temp-sfx-bed.wav")
            if _render_sfx_bed_wav(
                cues=sfx_cues,
                duration=video_duration,
                out_path=sfx_candidate,
                volume_scale=float(config.app.get("sfx_volume_scale", 1.0)),
            ):
                sfx_wav = sfx_candidate
                tmp_files.append(sfx_candidate)
                logger.info(f"SFX bed pre-rendered: {len(sfx_cues)} cue(s)")

        # Extra audio tracks beyond narration (bgm, sfx) are mixed in as
        # additional -i inputs on top of the always-present [1:a] narration —
        # order here must match the -i order built below.
        extra_audio_wavs = [w for w in (bgm_wav, sfx_wav) if w]

        voice_vol = float(params.voice_volume or 1.0)
        if extra_audio_wavs:
            n_inputs = 1 + len(extra_audio_wavs)
            extra_labels = "".join(f"[{2 + i}:a]" for i in range(len(extra_audio_wavs)))
            audio_graph = (
                f"[1:a]volume={voice_vol}[va];"
                f"[va]{extra_labels}amix=inputs={n_inputs}:duration=longest:normalize=0[a]"
            )
        else:
            audio_graph = f"[1:a]volume={voice_vol}[a]"

        filter_complex = f"[0:v]{','.join(vf_parts)}[v];{audio_graph}"

        codec = _get_effective_video_codec()
        cmd = [
            utils.get_ffmpeg_binary(), "-y",
            "-i", video_path,
            "-i", audio_path,
            *[arg for w in extra_audio_wavs for arg in ("-i", w)],
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            "-c:v", codec, *_fast_preset_args(codec),
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", audio_codec, "-b:a", audio_bitrate,
            "-t", f"{video_duration:.3f}",
            "-threads", str(params.n_threads or os.cpu_count() or 4),
            output_file,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_FFMPEG_FINAL_RENDER_TIMEOUT
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg final render failed: {(result.stderr or '')[-400:]}")
        if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
            raise RuntimeError("ffmpeg final render produced no output")
        logger.success("final render completed via ffmpeg-native path")
    finally:
        for f in tmp_files:
            try:
                os.remove(f)
            except OSError:
                pass


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
    rng: random.Random = random,
    sfx_cues: Optional[list] = None,
):
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    # Fast path: single-pass ffmpeg render (set ffmpeg_final_render=false in
    # config.toml to force the MoviePy compositor). Unsupported subtitle
    # decorations and any ffmpeg failure fall through to MoviePy below.
    if _ffmpeg_render_eligible(params, subtitle_path):
        try:
            return _generate_video_ffmpeg(
                video_path=video_path,
                audio_path=audio_path,
                subtitle_path=subtitle_path,
                output_file=output_file,
                params=params,
                rng=rng,
                sfx_cues=sfx_cues,
            )
        except Exception as exc:
            logger.warning(f"ffmpeg final render failed — falling back to MoviePy: {exc}")

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
        # Locals only — mutating params here would silently change the shared
        # VideoParams for every later subtitle (and 1.5 → 1 on stroke_width).
        font_size = int(params.font_size)
        stroke_width = int(params.stroke_width)
        phrase = subtitle_item[1]
        max_width = video_width * 0.9
        wrapped_txt, txt_height = wrap_text(
            phrase, max_width=max_width, font=font_path, fontsize=font_size
        )
        interline = int(font_size * 0.25)
        line_count = wrapped_txt.count("\n") + 1
        vertical_padding = int(font_size * 0.35)
        clip_h = int(txt_height + vertical_padding + interline * max(0, line_count - 1))
        bg_color = resolve_subtitle_background_color()
        rounded_bg_enabled = bool(
            getattr(params, "rounded_subtitle_background", False) and bg_color
        )

        if rounded_bg_enabled:
            try:
                font = ImageFont.truetype(font_path, font_size)
                text_w = max(
                    int(font.getbbox(line)[2] - font.getbbox(line)[0])
                    for line in wrapped_txt.split("\n")
                )
            except Exception as exc:
                logger.warning(
                    f"failed to measure subtitle text width, fallback to max width: {str(exc)}"
                )
                text_w = int(max_width)

            pad_x = int(font_size * 0.6)
            box_w = max(1, min(int(max_width), text_w + 2 * pad_x))
            radius = max(8, int(font_size * 0.4))
            text_clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=stroke_width,
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
                font_size=font_size,
                color=params.text_fore_color,
                bg_color=bg_color,
                stroke_color=params.stroke_color,
                stroke_width=stroke_width,
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

    sfx_audio_clips = []
    if sfx_cues and config.app.get("sfx_enabled", True):
        sfx_vol_scale = float(config.app.get("sfx_volume_scale", 1.0))
        for start_seconds, sfx_path, sfx_volume in sfx_cues:
            try:
                sfx_audio_clips.append(
                    AudioFileClip(sfx_path)
                    .with_effects([afx.MultiplyVolume(sfx_volume * sfx_vol_scale)])
                    .with_start(start_seconds)
                )
            except Exception as e:
                logger.warning(f"failed to load SFX cue {sfx_path}: {e}")
    if sfx_audio_clips:
        audio_clip = CompositeAudioClip([audio_clip, *sfx_audio_clips])

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
    for _sfx_clip in sfx_audio_clips:
        _sfx_clip.close()
    del video_clip
