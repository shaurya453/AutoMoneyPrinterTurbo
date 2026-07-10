"""Minimax cloud TTS engine (speech-02-hd)."""
import os
import subprocess
import tempfile
from typing import Union

import requests as _requests
from edge_tts import SubMaker
from loguru import logger

from app.config import config
from app.services.tts._utils import (
    ensure_file_path_exists,
    ensure_legacy_submaker_fields,
    populate_legacy_submaker_with_full_text,
    get_audio_duration,
    _get_mp3_duration_seconds,
)
from app.services.tts.edge import _split_text_for_tts

_MINIMAX_CHUNK_MAX_CHARS = 9500  # sync API limit is 10,000; 9,500 gives safe headroom


def _minimax_tts_single(
    text: str, voice_name: str, url: str, headers: dict, model: str, speed: float, out_file: str
) -> bool:
    payload = {
        "model": model,
        "text": text,
        "stream": False,
        "voice_setting": {"voice_id": voice_name, "speed": speed},
        "audio_setting": {"format": "mp3", "sample_rate": 32000, "bitrate": 128000, "channel": 1},
        "output_format": "hex",
    }
    try:
        resp = _requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        if data.get("base_resp", {}).get("status_code", -1) != 0:
            logger.error(f"minimax tts API error: {data.get('base_resp')}")
            return False
        audio_bytes = bytes.fromhex(data["data"]["audio"])
        ensure_file_path_exists(out_file)
        with open(out_file, "wb") as fh:
            fh.write(audio_bytes)
        return True
    except Exception as exc:
        logger.error(f"minimax tts request failed: {exc}")
        return False


def minimax_tts(
    text: str, voice_name: str, voice_rate: float, voice_file: str
) -> Union[SubMaker, None]:
    api_key  = str(config.app.get("minimax_api_key", ""))
    group_id = str(config.app.get("minimax_group_id", ""))
    model    = str(config.app.get("minimax_model", "speech-02-hd"))
    speed    = 1.0  # hardcoded — not controlled by AI/voice_rate
    if voice_rate and abs(float(voice_rate) - 1.0) > 1e-9:
        logger.warning(
            f"minimax_tts ignores voice_rate ({voice_rate}) — speed is hardcoded to 1.0"
        )

    if not api_key or not group_id:
        logger.error("minimax_tts: minimax_api_key or minimax_group_id not set in config")
        return None

    url     = f"https://api.minimax.io/v1/t2a_v2?GroupId={group_id}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    chunks = _split_text_for_tts(text, max_chars=_MINIMAX_CHUNK_MAX_CHARS)

    if len(chunks) == 1:
        ok = _minimax_tts_single(text, voice_name, url, headers, model, speed, voice_file)
        if not ok:
            return None
        logger.info(f"minimax tts complete | voice={voice_name} model={model}")
    else:
        logger.info(f"minimax chunked TTS: {len(chunks)} chunks for {len(text)} chars")
        with tempfile.TemporaryDirectory() as tmpdir:
            chunk_files = []
            for i, chunk in enumerate(chunks):
                chunk_file = os.path.join(tmpdir, f"chunk_{i:04d}.mp3")
                ok = _minimax_tts_single(chunk, voice_name, url, headers, model, speed, chunk_file)
                if not ok:
                    logger.error(f"minimax TTS chunk {i+1}/{len(chunks)} failed")
                    return None
                chunk_files.append(chunk_file)
                logger.info(f"minimax TTS chunk {i+1}/{len(chunks)} done")

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
                logger.error("minimax audio chunk concatenation failed")
                return None

        logger.info(f"minimax chunked TTS complete → {voice_file}")

    duration_seconds = get_audio_duration(voice_file)
    sub_maker = ensure_legacy_submaker_fields(SubMaker())
    return populate_legacy_submaker_with_full_text(
        sub_maker=sub_maker, text=text, audio_duration_seconds=duration_seconds,
    )
