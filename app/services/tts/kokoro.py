"""Kokoro ONNX TTS engine."""
import os
import subprocess
from typing import Union

from edge_tts import SubMaker
from loguru import logger

from app.config import config
from app.utils import utils
from app.services.tts._utils import (
    ensure_file_path_exists,
    ensure_legacy_submaker_fields,
    populate_legacy_submaker_with_full_text,
    get_audio_duration,
)

_DEFAULT_KOKORO_LANG = "en-us"


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
        kokoro_bin, "-", wav_file,
        "--format", "wav",
        "--lang", voice_lang,
        "--voice", voice_name,
        "--speed", f"{speed}",
    ]

    logger.info(f"kokoro tts start | voice={voice_name} lang={voice_lang} speed={speed}")

    # Hard timeout so a hung kokoro binary can't stall the pipeline until the
    # portal's 8h Phase-2 zombie guard fires (every other subprocess in the
    # repo has one). Long scripts take minutes, so keep this generous.
    timeout_seconds = float(config.app.get("kokoro_timeout_seconds", 1800))
    try:
        proc = subprocess.run(
            cmd,
            input=text.encode("utf-8"),
            cwd=model_dir or None,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        if proc.returncode != 0:
            output = (proc.stderr or proc.stdout or b"").decode(errors="ignore")
            logger.error(f"kokoro tts failed: {output[:400]}")
            return None
    except subprocess.TimeoutExpired:
        logger.error(f"kokoro tts timed out after {timeout_seconds:g}s")
        return None
    except Exception as exc:
        logger.error(f"kokoro tts exception: {exc}")
        return None

    ffmpeg_binary = utils.get_ffmpeg_binary()
    mp3_cmd = [
        ffmpeg_binary, "-y", "-i", wav_file,
        "-codec:a", "libmp3lame", "-q:a", "4", voice_file,
    ]
    try:
        mp3_ok = subprocess.run(
            mp3_cmd, capture_output=True, text=True, check=False, timeout=300
        ).returncode == 0
    except subprocess.TimeoutExpired:
        logger.error("kokoro wav→mp3 conversion timed out after 300s")
        return None
    if not mp3_ok:
        logger.error("failed to convert kokoro wav to mp3")
        return None

    duration_seconds = get_audio_duration(voice_file)
    sub_maker = ensure_legacy_submaker_fields(SubMaker())
    sub_maker = populate_legacy_submaker_with_full_text(
        sub_maker=sub_maker, text=text, audio_duration_seconds=duration_seconds,
    )
    logger.info(f"kokoro tts complete → {voice_file}")
    return sub_maker
