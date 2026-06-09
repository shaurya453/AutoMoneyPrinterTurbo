import asyncio
import inspect
import json
import math
import os
import queue
import re
import subprocess
import threading
import time
import unicodedata
from typing import Union
from xml.sax.saxutils import unescape

import edge_tts
from edge_tts import SubMaker
from loguru import logger
from moviepy.video.tools import subtitles
from moviepy.audio.io.AudioFileClip import AudioFileClip

from app.config import config
from app.utils import utils

_DEFAULT_EDGE_TTS_TIMEOUT_SECONDS = 120.0
_DEFAULT_KOKORO_LANG = "en-us"
_KOKORO_PREFIX = "kokoro:"
_TTS_CHUNK_MAX_CHARS = 2500
NO_VOICE_NAME = "no-voice"
_NO_VOICE_ALIASES = {NO_VOICE_NAME, "none"}

_AZURE_VOICES_DATA_FILE = os.path.join(
    os.path.dirname(__file__), "data", "azure_voices.json"
)
_azure_voices_cache = None


def _load_azure_voices() -> list[dict]:
    global _azure_voices_cache
    if _azure_voices_cache is None:
        with open(_AZURE_VOICES_DATA_FILE, "r", encoding="utf-8") as f:
            _azure_voices_cache = json.load(f)
    return _azure_voices_cache


def get_all_azure_voices(filter_locals=None) -> list[str]:
    voices = []
    for item in _load_azure_voices():
        name = item["name"]
        gender = item["gender"]
        if filter_locals and any(
            name.lower().startswith(fl.lower()) for fl in filter_locals
        ):
            voices.append(f"{name}-{gender}")
        elif not filter_locals:
            voices.append(f"{name}-{gender}")
    voices.sort()
    return voices


def parse_voice_name(name: str):
    name = name.replace("-Female", "").replace("-Male", "").strip()
    return name


def is_no_voice(voice_name: str | None) -> bool:
    return str(voice_name or "").strip().lower() in _NO_VOICE_ALIASES


def estimate_no_voice_duration(text: str) -> float:
    normalized_text = (text or "").strip()
    if not normalized_text:
        return 3.0

    cjk_chars = len(re.findall(r"[一-鿿]", normalized_text))
    words = len(re.findall(r"[A-Za-z0-9]+", normalized_text))
    ascii_word_chars = sum(len(word) for word in re.findall(r"[A-Za-z0-9]+", normalized_text))
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
        ffmpeg_binary,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=mono",
        "-t",
        f"{duration_seconds:.3f}",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "4",
        output_file,
    ]

    logger.info(
        f"generating silent audio for no-voice mode, duration: {duration_seconds:.2f}s"
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.error(
            "failed to generate silent audio: "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
        return False
    if not os.path.exists(output_file) or os.path.getsize(output_file) <= 0:
        logger.error(
            "silent audio output file is missing or empty, "
            f"file: {output_file}, duration: {duration_seconds:.2f}s"
        )
        return False
    return True


def tts(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    engine = str(config.app.get("tts_engine", "edge")).lower()

    if voice_name.lower().startswith(_KOKORO_PREFIX):
        engine = "kokoro"
        voice_name = voice_name[len(_KOKORO_PREFIX) :]

    if is_no_voice(voice_name):
        duration_seconds = estimate_no_voice_duration(text)
        if not generate_silent_audio(duration_seconds, voice_file):
            return None
        sub_maker = ensure_legacy_submaker_fields(SubMaker())
        return populate_legacy_submaker_with_full_text(
            sub_maker=sub_maker,
            text=text,
            audio_duration_seconds=duration_seconds,
        )
    if engine == "kokoro":
        return kokoro_tts(text, voice_name, voice_rate, voice_file)
    if len(text) > _TTS_CHUNK_MAX_CHARS:
        return _azure_tts_chunked(text, voice_name, voice_rate, voice_file)
    return azure_tts_v1(text, voice_name, voice_rate, voice_file)


def convert_rate_to_percent(rate: float) -> str:
    # edge-tts requires a sign-prefixed percentage string
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

    total_chars = sum(len(sentence) for sentence in sentences)
    if total_chars <= 0:
        sub_maker.subs.append(normalized_text)
        sub_maker.offset.append((0, audio_duration_100ns))
        return sub_maker

    current_offset = 0
    for index, sentence in enumerate(sentences):
        cleaned_sentence = sentence.strip()
        if not cleaned_sentence:
            continue

        if index == len(sentences) - 1:
            sentence_end = audio_duration_100ns
        else:
            sentence_chars = len(cleaned_sentence)
            sentence_duration = max(
                int(audio_duration_100ns * (sentence_chars / total_chars)),
                1,
            )
            sentence_end = min(current_offset + sentence_duration, audio_duration_100ns)

        sub_maker.subs.append(cleaned_sentence)
        sub_maker.offset.append((current_offset, sentence_end))
        current_offset = sentence_end

    return sub_maker


def create_edge_tts_communicate(
    text: str, voice_name: str, rate_str: str
) -> edge_tts.Communicate:
    communicate_kwargs = {"rate": rate_str}
    communicate_signature = inspect.signature(edge_tts.Communicate)

    if "boundary" in communicate_signature.parameters:
        communicate_kwargs["boundary"] = "WordBoundary"

    return edge_tts.Communicate(text, voice_name, **communicate_kwargs)


def get_edge_tts_timeout_seconds() -> Union[float, None]:
    raw_timeout = config.app.get(
        "edge_tts_timeout", _DEFAULT_EDGE_TTS_TIMEOUT_SECONDS
    )
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError):
        logger.warning(
            "invalid edge_tts_timeout: "
            f"{raw_timeout}, fallback to {_DEFAULT_EDGE_TTS_TIMEOUT_SECONDS}s"
        )
        timeout_seconds = _DEFAULT_EDGE_TTS_TIMEOUT_SECONDS

    if timeout_seconds <= 0:
        return None

    return timeout_seconds


def _stream_edge_tts_sync_with_timeout(
    communicate, on_chunk, timeout_seconds: float
) -> None:
    stream_queue = queue.Queue()
    done_marker = object()

    def _produce_chunks():
        try:
            for chunk in communicate.stream_sync():
                stream_queue.put(("chunk", chunk))
            stream_queue.put(("done", done_marker))
        except Exception as e:
            stream_queue.put(("error", e))

    thread = threading.Thread(target=_produce_chunks, daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise TimeoutError(
                f"edge_tts stream timed out after {timeout_seconds:g}s"
            )

        try:
            item_type, payload = stream_queue.get(
                timeout=min(0.5, remaining_seconds)
            )
        except queue.Empty:
            continue

        if item_type == "chunk":
            on_chunk(payload)
        elif item_type == "error":
            raise payload
        elif item_type == "done":
            return


def stream_edge_tts_chunks(
    communicate, on_chunk, timeout_seconds: Union[float, None] = None
) -> None:
    if hasattr(communicate, "stream_sync"):
        if timeout_seconds:
            _stream_edge_tts_sync_with_timeout(
                communicate, on_chunk, timeout_seconds
            )
            return

        for chunk in communicate.stream_sync():
            on_chunk(chunk)
        return

    if not hasattr(communicate, "stream"):
        raise AttributeError("edge_tts communicate object has no stream method")

    async def _consume_async_stream():
        async for chunk in communicate.stream():
            on_chunk(chunk)

    loop = asyncio.new_event_loop()
    try:
        if timeout_seconds:
            loop.run_until_complete(
                asyncio.wait_for(_consume_async_stream(), timeout=timeout_seconds)
            )
        else:
            loop.run_until_complete(_consume_async_stream())
    finally:
        loop.close()


def _split_text_for_tts(text: str, max_chars: int = _TTS_CHUNK_MAX_CHARS) -> list:
    """Split text into chunks ≤ max_chars at sentence boundaries."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks, current = [], ""
    for sentence in sentences:
        candidate = (current + " " + sentence).strip() if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks or [text]


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


def _merge_submakers(makers: list, offsets_seconds: list) -> SubMaker:
    """Merge SubMakers, shifting each by the given cumulative offset."""
    import datetime

    merged = SubMaker()

    # Prefer cues-based API (newer edge_tts)
    if all(hasattr(m, "cues") and m.cues for m in makers):
        all_cues = []
        for maker, offset_sec in zip(makers, offsets_seconds):
            delta = datetime.timedelta(seconds=offset_sec)
            for cue in maker.cues:
                shifted = type(cue).__new__(type(cue))
                shifted.__dict__.update(cue.__dict__)
                shifted.start = cue.start + delta
                shifted.end = cue.end + delta
                all_cues.append(shifted)
        merged.cues = all_cues
        return merged

    # Legacy subs/offset API — edge_tts stores timestamps in 100-nanosecond units
    merged = ensure_legacy_submaker_fields(merged)
    for maker, offset_sec in zip(makers, offsets_seconds):
        offset_100ns = int(offset_sec * 10_000_000)  # seconds → 100ns ticks
        for sub, (start, end) in zip(
            getattr(maker, "subs", []), getattr(maker, "offset", [])
        ):
            merged.subs.append(sub)
            merged.offset.append((start + offset_100ns, end + offset_100ns))
    return merged


def _azure_tts_chunked(
    text: str, voice_name: str, voice_rate: float, voice_file: str
) -> Union[SubMaker, None]:
    """TTS for long text: chunk → generate → concatenate audio → merge SubMakers."""
    import tempfile

    chunks = _split_text_for_tts(text)
    if len(chunks) == 1:
        return None  # caller should use the direct path

    logger.info(f"chunked TTS: {len(chunks)} chunks for {len(text)} chars")

    with tempfile.TemporaryDirectory() as tmpdir:
        chunk_files, sub_makers = [], []
        for i, chunk in enumerate(chunks):
            chunk_file = os.path.join(tmpdir, f"chunk_{i:04d}.mp3")
            maker = None
            # Retry the whole chunk up to 3 times with backoff if edge_tts is flaky
            for chunk_attempt in range(3):
                if chunk_attempt > 0:
                    sleep_sec = 30 * chunk_attempt
                    logger.warning(f"chunk {i + 1} attempt {chunk_attempt + 1}/3 — sleeping {sleep_sec}s before retry")
                    time.sleep(sleep_sec)
                maker = azure_tts_v1(chunk, voice_name, voice_rate, chunk_file)
                if maker is not None:
                    break
            if maker is None:
                logger.error(f"TTS chunk {i + 1}/{len(chunks)} failed after all retries")
                return None
            chunk_files.append(chunk_file)
            sub_makers.append(maker)
            logger.info(f"TTS chunk {i + 1}/{len(chunks)} done")

        # Cumulative offsets for SubMaker merging
        offsets = [0.0]
        for cf in chunk_files[:-1]:
            offsets.append(offsets[-1] + _get_mp3_duration_seconds(cf))

        # Concatenate audio
        ensure_file_path_exists(voice_file)
        list_file = os.path.join(tmpdir, "concat_list.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for cf in chunk_files:
                f.write(f"file '{cf}'\n")

        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            voice_file,
        ]
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            logger.error("audio chunk concatenation failed")
            return None

    merged = _merge_submakers(sub_makers, offsets)
    logger.info(f"chunked TTS complete → {voice_file}")
    return merged


def azure_tts_v1(
    text: str, voice_name: str, voice_rate: float, voice_file: str
) -> Union[SubMaker, None]:
    voice_name = parse_voice_name(voice_name)
    text = text.strip()
    rate_str = convert_rate_to_percent(voice_rate)
    for i in range(3):
        if i > 0:
            time.sleep(5 * i)  # 5s, 10s between retries
        try:
            logger.info(f"start, voice name: {voice_name}, try: {i + 1}")

            ensure_file_path_exists(voice_file)
            communicate = create_edge_tts_communicate(text, voice_name, rate_str)
            sub_maker = edge_tts.SubMaker()
            timeout_seconds = get_edge_tts_timeout_seconds()

            with open(voice_file, "wb") as file:
                def _handle_chunk(chunk):
                    chunk_type = chunk["type"]
                    if chunk_type == "audio":
                        file.write(chunk["data"])
                    elif chunk_type in ["WordBoundary", "SentenceBoundary"]:
                        sub_maker.feed(chunk)

                stream_edge_tts_chunks(
                    communicate, _handle_chunk, timeout_seconds=timeout_seconds
                )

            if not sub_maker.get_srt():
                logger.warning("failed, sub_maker.get_srt() is empty")
                continue

            logger.info(f"completed, output file: {voice_file}")
            return sub_maker
        except Exception as e:
            logger.error(f"failed, error: {str(e)}")
            if os.path.exists(voice_file) and os.path.getsize(voice_file) == 0:
                try:
                    os.remove(voice_file)
                except Exception as remove_error:
                    logger.warning(
                        "failed to remove empty tts file: "
                        f"{voice_file}, error: {str(remove_error)}"
                    )
    return None


def kokoro_tts(
    text: str, voice_name: str, voice_rate: float, voice_file: str
) -> Union[SubMaker, None]:
    kokoro_venv = config.app.get("kokoro_venv", "kokoro-venv")
    if not os.path.isabs(kokoro_venv):
        kokoro_venv = os.path.join(config.root_dir, kokoro_venv)

    kokoro_bin = os.path.join(kokoro_venv, "bin", "kokoro-tts")
    if not os.path.exists(kokoro_bin):
        logger.error(f"kokoro-tts binary not found: {kokoro_bin}")
        return None

    model_path = config.app.get(
        "kokoro_model_path",
        os.path.join(config.root_dir, "resource/kokoro/kokoro-v1.0.onnx"),
    )
    if not os.path.isabs(model_path):
        model_path = os.path.join(config.root_dir, model_path)
    voices_path = config.app.get(
        "kokoro_voices_path",
        os.path.join(config.root_dir, "resource/kokoro/voices-v1.0.bin"),
    )
    if not os.path.isabs(voices_path):
        voices_path = os.path.join(config.root_dir, voices_path)
    model_dir = os.path.dirname(model_path)
    if not os.path.exists(model_path) or not os.path.exists(voices_path):
        logger.error("kokoro model or voices file missing")
        return None

    voice_lang = str(config.app.get("kokoro_lang", _DEFAULT_KOKORO_LANG))
    speed = max(0.5, min(float(voice_rate or 1.0), 2.0))

    wav_file = voice_file.replace(".mp3", ".wav")
    ensure_file_path_exists(wav_file)

    cmd = [
        kokoro_bin,
        "-",
        wav_file,
        "--format",
        "wav",
        "--lang",
        voice_lang,
        "--voice",
        voice_name,
        "--speed",
        f"{speed}",
    ]

    logger.info(
        f"kokoro tts start | voice={voice_name} lang={voice_lang} speed={speed}"
    )

    try:
        proc = subprocess.run(
            cmd,
            input=text.encode("utf-8"),
            cwd=model_dir or None,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            output = (proc.stderr or proc.stdout or b"").decode(errors="ignore")
            logger.error(f"kokoro tts failed: {output[:400]}")
            return None
    except Exception as exc:
        logger.error(f"kokoro tts exception: {exc}")
        return None

    ffmpeg_binary = utils.get_ffmpeg_binary()
    mp3_cmd = [
        ffmpeg_binary,
        "-y",
        "-i",
        wav_file,
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "4",
        voice_file,
    ]
    if subprocess.run(mp3_cmd, capture_output=True, text=True, check=False).returncode != 0:
        logger.error("failed to convert kokoro wav to mp3")
        return None

    duration_seconds = get_audio_duration(voice_file)
    sub_maker = ensure_legacy_submaker_fields(SubMaker())
    sub_maker = populate_legacy_submaker_with_full_text(
        sub_maker=sub_maker,
        text=text,
        audio_duration_seconds=duration_seconds,
    )
    logger.info(f"kokoro tts complete → {voice_file}")
    return sub_maker


def mktimestamp(time_unit: float) -> str:
    hour = math.floor(time_unit / 10**7 / 3600)
    minute = math.floor((time_unit / 10**7 / 60) % 60)
    seconds = (time_unit / 10**7) % 60
    return f"{hour:02d}:{minute:02d}:{seconds:06.3f}"


def _format_text(text: str) -> str:
    text = text.replace("[", " ")
    text = text.replace("]", " ")
    text = text.replace("(", " ")
    text = text.replace(")", " ")
    text = text.replace("{", " ")
    text = text.replace("}", " ")
    return utils.normalize_script_for_subtitle_matching(text)


def _build_subtitle_formatter():
    def formatter(idx: int, start_time: float, end_time: float, sub_text: str) -> str:
        start_t = mktimestamp(start_time).replace(".", ",")
        end_t = mktimestamp(end_time).replace(".", ",")
        return f"{idx}\n{start_t} --> {end_t}\n{sub_text}\n"
    return formatter


_ARABIC_DIACRITICS = re.compile("[ؐ-ًؚ-ٰٟـۖ-ۭ]")


def _normalize_arabic(text: str) -> str:
    text = _ARABIC_DIACRITICS.sub("", text)
    for src, dst in (
        ("أإآٱ", "ا"),
        ("ىئ", "ي"),
        ("ة", "ه"),
        ("ؤ", "و"),
    ):
        for ch in src:
            text = text.replace(ch, dst)
    return text


def _match_script_line(script_lines: list[str], current_text: str, sub_index: int) -> str:
    if len(script_lines) <= sub_index:
        return ""

    target_line = script_lines[sub_index]
    if current_text == target_line:
        return target_line.strip()

    current_text_normalized = re.sub(r"[_\W]+", "", current_text)
    target_line_normalized = re.sub(r"[_\W]+", "", target_line)
    if current_text_normalized == target_line_normalized:
        return target_line.strip()

    current_ar = re.sub(r"[_\W]+", "", _normalize_arabic(current_text))
    target_ar = re.sub(r"[_\W]+", "", _normalize_arabic(target_line))
    if current_ar and current_ar == target_ar:
        return target_line.strip()

    return ""


def _write_subtitle_items(sub_items: list[str], subtitle_file: str) -> bool:
    try:
        ensure_file_path_exists(subtitle_file)
        with open(subtitle_file, "w", encoding="utf-8") as file:
            file.write("\n".join(sub_items) + "\n")

        sbs = subtitles.file_to_subtitles(subtitle_file, encoding="utf-8")
        duration = max([tb for ((ta, tb), txt) in sbs]) if sbs else 0
        logger.info(
            f"completed, subtitle file created: {subtitle_file}, duration: {duration}"
        )
        return True
    except Exception as e:
        logger.error(f"failed, error: {str(e)}")
        if os.path.exists(subtitle_file):
            os.remove(subtitle_file)
        return False


def _build_subtitle_items_from_edge_cues(
    sub_maker: SubMaker, script_lines: list[str]
) -> list[str]:
    formatter = _build_subtitle_formatter()
    sub_items = []
    sub_index = 0
    current_text = ""
    current_start_time = None

    for cue in sub_maker.cues:
        cue_text = unescape(cue.content)
        if current_start_time is None:
            current_start_time = int(cue.start.total_seconds() * 10000000)

        current_end_time = int(cue.end.total_seconds() * 10000000)
        current_text += cue_text

        matched_text = _match_script_line(script_lines, current_text, sub_index)
        if not matched_text:
            continue

        sub_index += 1
        sub_items.append(
            formatter(
                idx=sub_index,
                start_time=current_start_time,
                end_time=current_end_time,
                sub_text=matched_text,
            )
        )
        current_text = ""
        current_start_time = None

    if current_text.strip():
        logger.warning(
            f"edge cues still have unmatched text after aggregation: {current_text}"
        )

    return sub_items


def _build_subtitle_items_from_legacy_submaker(
    sub_maker: SubMaker, script_lines: list[str]
) -> list[str]:
    formatter = _build_subtitle_formatter()
    start_time = -1.0
    sub_items = []
    sub_index = 0
    sub_line = ""

    legacy_offsets = getattr(sub_maker, "offset", [])
    legacy_subs = getattr(sub_maker, "subs", [])
    for _, (offset, sub) in enumerate(zip(legacy_offsets, legacy_subs)):
        current_start_time, current_end_time = offset
        if start_time < 0:
            start_time = current_start_time

        sub_line += unescape(sub)
        matched_text = _match_script_line(script_lines, sub_line, sub_index)
        if not matched_text:
            continue

        sub_index += 1
        sub_items.append(
            formatter(
                idx=sub_index,
                start_time=start_time,
                end_time=current_end_time,
                sub_text=matched_text,
            )
        )
        start_time = -1.0
        sub_line = ""

    if sub_line.strip():
        logger.warning(
            f"legacy subtitle items still have unmatched text after aggregation: {sub_line}"
        )

    return sub_items


def create_subtitle(sub_maker: SubMaker, text: str, subtitle_file: str):
    text = _format_text(text)
    script_lines = utils.split_string_by_punctuations(text)
    try:
        if hasattr(sub_maker, "cues") and sub_maker.cues:
            sub_items = _build_subtitle_items_from_edge_cues(sub_maker, script_lines)
        else:
            sub_items = _build_subtitle_items_from_legacy_submaker(
                sub_maker, script_lines
            )

        if len(sub_items) != len(script_lines):
            logger.warning(
                f"failed, sub_items len: {len(sub_items)}, script_lines len: {len(script_lines)}"
            )
            return

        _write_subtitle_items(sub_items, subtitle_file)
    except Exception as e:
        logger.error(f"failed, error: {str(e)}")


def create_word_timings(sub_maker: SubMaker) -> list:
    """Return [{word, start, end}] in seconds from SubMaker cues or legacy offsets."""
    if hasattr(sub_maker, "cues") and sub_maker.cues:
        result = []
        for cue in sub_maker.cues:
            w = unescape(cue.content).strip()
            if w:
                result.append({
                    "word": w,
                    "start": cue.start.total_seconds(),
                    "end": cue.end.total_seconds(),
                })
        return result

    legacy_offsets = getattr(sub_maker, "offset", [])
    legacy_subs = getattr(sub_maker, "subs", [])
    result = []
    for (start_100ns, end_100ns), sub in zip(legacy_offsets, legacy_subs):
        w = unescape(sub).strip()
        if w:
            result.append({
                "word": w,
                "start": start_100ns / 10_000_000,
                "end": end_100ns / 10_000_000,
            })
    return result


def _get_audio_duration_from_submaker(sub_maker: SubMaker):
    if hasattr(sub_maker, "cues") and sub_maker.cues:
        return sub_maker.cues[-1].end.total_seconds()

    legacy_offsets = getattr(sub_maker, "offset", [])
    if not legacy_offsets:
        return 0.0
    return legacy_offsets[-1][1] / 10000000


def _get_audio_duration_from_mp3(mp3_file: str) -> float:
    if not os.path.exists(mp3_file):
        logger.error(f"MP3 file does not exist: {mp3_file}")
        return 0.0

    try:
        with AudioFileClip(mp3_file) as audio:
            return audio.duration
    except Exception as e:
        logger.error(f"Failed to get audio duration from MP3: {str(e)}")
        return 0.0


def get_audio_duration(target: Union[str, SubMaker]) -> float:
    if isinstance(target, SubMaker):
        return _get_audio_duration_from_submaker(target)
    elif isinstance(target, str) and target.endswith(".mp3"):
        return _get_audio_duration_from_mp3(target)
    else:
        logger.error(f"Invalid target type: {type(target)}")
        return 0.0
