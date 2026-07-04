"""Minimax cloud TTS engine (speech-02-hd)."""
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
)


def minimax_tts(
    text: str, voice_name: str, voice_rate: float, voice_file: str
) -> Union[SubMaker, None]:
    api_key  = str(config.app.get("minimax_api_key", ""))
    group_id = str(config.app.get("minimax_group_id", ""))
    model    = str(config.app.get("minimax_model", "speech-02-hd"))
    speed    = 1.0  # hardcoded — not controlled by AI/voice_rate

    if not api_key or not group_id:
        logger.error("minimax_tts: minimax_api_key or minimax_group_id not set in config")
        return None

    url = f"https://api.minimax.io/v1/t2a_v2?GroupId={group_id}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
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
            return None
        audio_bytes = bytes.fromhex(data["data"]["audio"])
        ensure_file_path_exists(voice_file)
        with open(voice_file, "wb") as fh:
            fh.write(audio_bytes)
        logger.info(f"minimax tts complete | voice={voice_name} model={model}")
    except Exception as exc:
        logger.error(f"minimax tts failed: {exc}")
        return None

    duration_seconds = get_audio_duration(voice_file)
    sub_maker = ensure_legacy_submaker_fields(SubMaker())
    return populate_legacy_submaker_with_full_text(
        sub_maker=sub_maker, text=text, audio_duration_seconds=duration_seconds,
    )
