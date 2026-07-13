"""TTS dispatcher — routes to the appropriate engine based on voice_name prefix."""
import re
from typing import Union

from edge_tts import SubMaker
from loguru import logger

from app.config import config
from app.services.tts._utils import (
    ensure_legacy_submaker_fields,
    populate_legacy_submaker_with_full_text,
    estimate_no_voice_duration,
    generate_silent_audio,
    get_audio_duration,
    normalize_narration_loudness,
)
from app.services.tts.edge import azure_tts_v1, _azure_tts_chunked
from app.services.tts.kokoro import kokoro_tts
from app.services.tts.supertonic import supertonic_tts
from app.services.tts.minimax import minimax_tts

_KOKORO_PREFIX = "kokoro:"
_SUPERTONIC_PREFIX = "supertonic:"
_MINIMAX_PREFIX = "minimax:"
_TTS_CHUNK_MAX_CHARS = 2500
NO_VOICE_NAME = "no-voice"
_NO_VOICE_ALIASES = {NO_VOICE_NAME, "none"}


def parse_voice_name(name: str) -> str:
    name = name.replace("-Female", "").replace("-Male", "").strip()
    return name


def is_no_voice(voice_name: Union[str, None]) -> bool:
    return str(voice_name or "").strip().lower() in _NO_VOICE_ALIASES


def resolve_tts_engine(voice_name: str) -> tuple[str, str]:
    """Return (engine, voice_name_with_prefix_stripped) for a job's voice_name.

    Shared by tts() and the post-render quality report (_quality.py) so the
    prefix->engine mapping lives in exactly one place.
    """
    engine = str(config.app.get("tts_engine", "edge")).lower()
    if voice_name.lower().startswith(_KOKORO_PREFIX):
        return "kokoro", voice_name[len(_KOKORO_PREFIX):]
    if voice_name.lower().startswith(_SUPERTONIC_PREFIX):
        return "supertonic", voice_name[len(_SUPERTONIC_PREFIX):]
    if voice_name.lower().startswith(_MINIMAX_PREFIX):
        return "minimax", voice_name[len(_MINIMAX_PREFIX):]
    return engine, voice_name


def tts(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    # Collapse mid-sentence newlines into spaces — edge-tts treats \n as a
    # paragraph break with an audible pause and prosody reset.
    text = re.sub(r'\s*\n\s*', ' ', text).strip()

    engine, voice_name = resolve_tts_engine(voice_name)

    if is_no_voice(voice_name):
        duration_seconds = estimate_no_voice_duration(text)
        if not generate_silent_audio(duration_seconds, voice_file):
            return None
        sub_maker = ensure_legacy_submaker_fields(SubMaker())
        return populate_legacy_submaker_with_full_text(
            sub_maker=sub_maker, text=text, audio_duration_seconds=duration_seconds,
        )

    if engine == "kokoro":
        return kokoro_tts(text, voice_name, voice_rate, voice_file)
    if engine == "supertonic":
        return supertonic_tts(text, voice_name, voice_rate, voice_file)
    if engine == "minimax":
        return minimax_tts(text, voice_name, voice_rate, voice_file)
    if len(text) > _TTS_CHUNK_MAX_CHARS:
        return _azure_tts_chunked(text, voice_name, voice_rate, voice_file)
    return azure_tts_v1(text, voice_name, voice_rate, voice_file)


__all__ = [
    "tts", "parse_voice_name", "is_no_voice", "get_audio_duration",
    "normalize_narration_loudness",
    "NO_VOICE_NAME", "kokoro_tts", "supertonic_tts", "minimax_tts",
    "azure_tts_v1",
]
