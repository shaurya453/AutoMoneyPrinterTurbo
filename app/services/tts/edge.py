"""Edge-TTS (Microsoft Azure via edge-tts) engine."""
import asyncio
import inspect
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from typing import Union

import edge_tts
from edge_tts import SubMaker
from loguru import logger

from app.config import config
from app.services.tts._utils import (
    convert_rate_to_percent,
    ensure_file_path_exists,
    ensure_legacy_submaker_fields,
    _get_mp3_duration_seconds,
)

_DEFAULT_EDGE_TTS_TIMEOUT_SECONDS = 120.0
_TTS_CHUNK_MAX_CHARS = 2500


def create_edge_tts_communicate(text: str, voice_name: str, rate_str: str) -> edge_tts.Communicate:
    communicate_kwargs = {"rate": rate_str}
    communicate_signature = inspect.signature(edge_tts.Communicate)
    if "boundary" in communicate_signature.parameters:
        communicate_kwargs["boundary"] = "WordBoundary"
    return edge_tts.Communicate(text, voice_name, **communicate_kwargs)


def get_edge_tts_timeout_seconds() -> Union[float, None]:
    raw_timeout = config.app.get("edge_tts_timeout", _DEFAULT_EDGE_TTS_TIMEOUT_SECONDS)
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError):
        logger.warning(f"invalid edge_tts_timeout: {raw_timeout}, using default {_DEFAULT_EDGE_TTS_TIMEOUT_SECONDS}s")
        timeout_seconds = _DEFAULT_EDGE_TTS_TIMEOUT_SECONDS
    if timeout_seconds <= 0:
        return None
    return timeout_seconds


def _stream_edge_tts_sync_with_timeout(communicate, on_chunk, timeout_seconds: float) -> None:
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
            raise TimeoutError(f"edge_tts stream timed out after {timeout_seconds:g}s")
        try:
            item_type, payload = stream_queue.get(timeout=min(0.5, remaining_seconds))
        except queue.Empty:
            continue
        if item_type == "chunk":
            on_chunk(payload)
        elif item_type == "error":
            raise payload
        elif item_type == "done":
            return


def stream_edge_tts_chunks(communicate, on_chunk, timeout_seconds: Union[float, None] = None) -> None:
    if hasattr(communicate, "stream_sync"):
        if timeout_seconds:
            _stream_edge_tts_sync_with_timeout(communicate, on_chunk, timeout_seconds)
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
            loop.run_until_complete(asyncio.wait_for(_consume_async_stream(), timeout=timeout_seconds))
        else:
            loop.run_until_complete(_consume_async_stream())
    finally:
        loop.close()


def _split_text_for_tts(text: str, max_chars: int = _TTS_CHUNK_MAX_CHARS) -> list:
    text = re.sub(r'\s*\n\s*', ' ', text).strip()
    sentences = re.split(r'(?<=[.!?])\s+', text)
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


def _merge_submakers(makers: list, offsets_seconds: list) -> SubMaker:
    import datetime
    merged = SubMaker()
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


def _azure_tts_chunked(
    text: str, voice_name: str, voice_rate: float, voice_file: str
) -> Union[SubMaker, None]:
    chunks = _split_text_for_tts(text)
    if len(chunks) == 1:
        return azure_tts_v1(text, voice_name, voice_rate, voice_file)

    logger.info(f"chunked TTS: {len(chunks)} chunks for {len(text)} chars")

    with tempfile.TemporaryDirectory() as tmpdir:
        chunk_files, sub_makers = [], []
        for i, chunk in enumerate(chunks):
            chunk_file = os.path.join(tmpdir, f"chunk_{i:04d}.mp3")
            maker = None
            for chunk_attempt in range(3):
                if chunk_attempt > 0:
                    sleep_sec = 30 * chunk_attempt
                    logger.warning(f"chunk {i+1} attempt {chunk_attempt+1}/3 — sleeping {sleep_sec}s")
                    time.sleep(sleep_sec)
                maker = azure_tts_v1(chunk, voice_name, voice_rate, chunk_file)
                if maker is not None:
                    break
            if maker is None:
                logger.error(f"TTS chunk {i+1}/{len(chunks)} failed after all retries")
                return None
            chunk_files.append(chunk_file)
            sub_makers.append(maker)
            logger.info(f"TTS chunk {i+1}/{len(chunks)} done")

        offsets = [0.0]
        for cf in chunk_files[:-1]:
            offsets.append(offsets[-1] + _get_mp3_duration_seconds(cf))

        ensure_file_path_exists(voice_file)
        list_file = os.path.join(tmpdir, "concat_list.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for cf in chunk_files:
                escaped = cf.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", voice_file,
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
    from app.services.tts._utils import convert_rate_to_percent as _crtop
    text = text.strip()
    rate_str = _crtop(voice_rate)
    for i in range(3):
        if i > 0:
            time.sleep(5 * i)
        try:
            logger.info(f"start, voice name: {voice_name}, try: {i+1}")
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

                stream_edge_tts_chunks(communicate, _handle_chunk, timeout_seconds=timeout_seconds)

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
                    logger.warning(f"failed to remove empty tts file: {voice_file}, error: {str(remove_error)}")
    return None
