"""Shared TTS utilities used by all engine modules."""
import os
import re
import subprocess
import unicodedata
from typing import Union

from edge_tts import SubMaker
from loguru import logger

from app.config import config
from app.utils import utils


def convert_rate_to_percent(rate: float) -> str:
    percent = round((rate - 1.0) * 100)
    if percent >= 0:
        return f"+{percent}%"
    return f"{percent}%"


def ensure_file_path_exists(file_path: str) -> None:
    dir_path = os.path.dirname(file_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)


def ensure_legacy_submaker_fields(sub_maker: SubMaker) -> SubMaker:
    if not hasattr(sub_maker, "subs"):
        sub_maker.subs = []
    if not hasattr(sub_maker, "offset"):
        sub_maker.offset = []
    return sub_maker


def populate_legacy_submaker_with_full_text(
    sub_maker: SubMaker, text: str, audio_duration_seconds: float
) -> SubMaker:
    sub_maker = ensure_legacy_submaker_fields(sub_maker)
    sub_maker.subs = []
    sub_maker.offset = []

    normalized_text = (text or "").strip()
    if not normalized_text:
        return sub_maker

    audio_duration_100ns = max(int(audio_duration_seconds * 10000000), 1)
    sentences = utils.split_string_by_punctuations(normalized_text)
    if not sentences:
        sentences = [normalized_text]

    total_chars = sum(len(s) for s in sentences)
    if total_chars <= 0:
        sub_maker.subs.append(normalized_text)
        sub_maker.offset.append((0, audio_duration_100ns))
        return sub_maker

    current_offset = 0
    for index, sentence in enumerate(sentences):
        cleaned = sentence.strip()
        if not cleaned:
            continue
        if index == len(sentences) - 1:
            sentence_end = audio_duration_100ns
        else:
            sentence_duration = max(
                int(audio_duration_100ns * (len(cleaned) / total_chars)), 1
            )
            sentence_end = min(current_offset + sentence_duration, audio_duration_100ns)
        sub_maker.subs.append(cleaned)
        sub_maker.offset.append((current_offset, sentence_end))
        current_offset = sentence_end

    return sub_maker


def estimate_no_voice_duration(text: str) -> float:
    normalized_text = (text or "").strip()
    if not normalized_text:
        return 3.0

    cjk_chars = len(re.findall(r"[一-鿿]", normalized_text))
    _ascii_words = re.findall(r"[A-Za-z0-9]+", normalized_text)
    words = len(_ascii_words)
    ascii_word_chars = sum(len(w) for w in _ascii_words)
    other_text_chars = 0
    for char in normalized_text:
        category = unicodedata.category(char)
        if category.startswith(("L", "N")):
            other_text_chars += 1
    other_text_chars = max(other_text_chars - cjk_chars - ascii_word_chars, 0)
    sentence_count = max(len(utils.split_string_by_punctuations(normalized_text)), 1)

    cjk_duration = cjk_chars / 4.2
    word_duration = words / 2.7
    other_text_duration = other_text_chars / 4.0
    pause_duration = max(sentence_count - 1, 0) * 0.35
    return max(3.0, cjk_duration + word_duration + other_text_duration + pause_duration)


def generate_silent_audio(duration_seconds: float, output_file: str) -> bool:
    ensure_file_path_exists(output_file)
    duration_seconds = max(float(duration_seconds or 0), 0.1)
    ffmpeg_binary = utils.get_ffmpeg_binary()
    command = [
        ffmpeg_binary, "-y", "-f", "lavfi", "-i",
        "anullsrc=r=44100:cl=mono", "-t", f"{duration_seconds:.3f}",
        "-codec:a", "libmp3lame", "-q:a", "4", output_file,
    ]
    logger.info(f"generating silent audio, duration: {duration_seconds:.2f}s")
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
    except subprocess.TimeoutExpired:
        logger.error("generating silent audio timed out after 60s")
        return False
    if result.returncode != 0:
        logger.error(f"failed to generate silent audio: {(result.stderr or result.stdout or '').strip()}")
        return False
    if not os.path.exists(output_file) or os.path.getsize(output_file) <= 0:
        logger.error(f"silent audio output missing or empty: {output_file}")
        return False
    return True


def _get_mp3_duration_seconds(mp3_file: str) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", mp3_file],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _get_audio_duration_from_submaker(sub_maker: SubMaker) -> float:
    if hasattr(sub_maker, "cues") and sub_maker.cues:
        return sub_maker.cues[-1].end.total_seconds()
    legacy_offsets = getattr(sub_maker, "offset", [])
    if not legacy_offsets:
        return 0.0
    return legacy_offsets[-1][1] / 10000000


def get_audio_duration(target: Union[str, SubMaker]) -> float:
    if isinstance(target, SubMaker):
        return _get_audio_duration_from_submaker(target)
    elif isinstance(target, str) and target.endswith(".mp3"):
        if not os.path.exists(target):
            logger.error(f"MP3 file does not exist: {target}")
            return 0.0
        return _get_mp3_duration_seconds(target)
    else:
        logger.error(f"Invalid target type: {type(target)}")
        return 0.0
