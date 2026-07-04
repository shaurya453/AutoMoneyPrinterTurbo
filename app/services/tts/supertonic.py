"""Supertonic on-device TTS engine."""
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

# Module-level singleton so the 400MB ONNX model is loaded once per process.
_supertonic_instance = None


def supertonic_tts(
    text: str, voice_name: str, voice_rate: float, voice_file: str
) -> Union[SubMaker, None]:
    global _supertonic_instance
    try:
        if _supertonic_instance is None:
            from supertonic import TTS as _ST
            _supertonic_instance = _ST(auto_download=True)
        tts_obj = _supertonic_instance
    except Exception as exc:
        logger.error(f"supertonic: failed to initialise: {exc}")
        return None

    steps = int(config.app.get("supertonic_steps", 8))
    speed = 1.0  # hardcoded — not controlled by AI/voice_rate
    lang = str(config.app.get("supertonic_lang", "en"))

    wav_file = voice_file.replace(".mp3", ".wav")
    ensure_file_path_exists(wav_file)

    try:
        style = tts_obj.get_voice_style(voice_name=voice_name)
        wav, _ = tts_obj.synthesize(
            text=text,
            voice_style=style,
            total_steps=steps,
            speed=speed,
            lang=lang,
        )
        tts_obj.save_audio(wav, wav_file)
        logger.info(f"supertonic tts complete | voice={voice_name} steps={steps} speed={speed}")
    except Exception as exc:
        logger.error(f"supertonic tts failed: {exc}")
        return None

    ffmpeg_binary = utils.get_ffmpeg_binary()
    mp3_cmd = [
        ffmpeg_binary, "-y", "-i", wav_file,
        "-codec:a", "libmp3lame", "-q:a", "4", voice_file,
    ]
    if subprocess.run(mp3_cmd, capture_output=True, check=False).returncode != 0:
        logger.error("failed to convert supertonic wav to mp3")
        return None

    duration_seconds = get_audio_duration(voice_file)
    sub_maker = ensure_legacy_submaker_fields(SubMaker())
    return populate_legacy_submaker_with_full_text(
        sub_maker=sub_maker, text=text, audio_duration_seconds=duration_seconds,
    )
