"""Algrow TTS proxy — cheaper billing path for "minimax:" voices, and a
premade ElevenLabs voice catalog for "elevenlabs:" voices.

Algrow (https://algrow.online) proxies several providers behind one API.

MiniMax: its `provider=minimax` path only accepts voice_ids that were cloned
*inside Algrow's own account* (via `/api/voices/minimax/clone`) — it has no
knowledge of voices cloned directly with MiniMax's own API/GroupId, so
`minimax_algrow_voice_map` (config.toml, `[app]` table) maps our stable
"minimax:" voice_name (MiniMax's own moss_audio_* id) to the voice_id Algrow
issued for that same voice after `scripts/algrow_clone_from_minimax.py`
cloned it once.

ElevenLabs: its `provider=elevenlabs` path uses Algrow's own large premade
voice catalog (`GET /api/voices`, confirmed live 2026-07-14) — the catalog's
own `voice_id` is used directly with no cloning/mapping step, and each
catalog entry ships its own official `preview_url` (a public ElevenLabs CDN
link) that the portal uses directly for previews rather than generating one.

Async job API (shared by both providers): POST /api/generate-simple ->
{job_id, status: "pending"}, then poll GET /api/job-status/{job_id} until
status is "completed" (-> audio_url, a permanent CDN link) or "failed"
(-> error/error_message). Confirmed live (2026-07-14) via real end-to-end
generations for both providers.
"""
import os
import subprocess
import tempfile
import time
from typing import Callable, Optional, Union

import requests
from edge_tts import SubMaker
from loguru import logger

from app.config import config
from app.services.tts._utils import (
    ensure_file_path_exists,
    ensure_legacy_submaker_fields,
    populate_legacy_submaker_with_full_text,
    get_audio_duration,
)

_ALGROW_BASE_URL = "https://api.algrow.online"
_POLL_INTERVAL_SECONDS = 3.0
_MIN_SCRIPT_CHARS = 200  # Algrow: "minimum 200 characters" per /api/generate-simple call
_MAX_SCRIPT_CHARS = 9500  # mirrors minimax.py's direct-API chunk size; no documented Algrow limit


def _algrow_endpoint() -> str:
    return str(config.app.get("algrow_endpoint", _ALGROW_BASE_URL)).rstrip("/")


def _algrow_headers() -> dict:
    return {"Authorization": f"Bearer {config.app.get('algrow_api_key', '')}"}


def _algrow_timeout_seconds() -> float:
    return float(config.app.get("algrow_timeout_seconds", 300))


def _split_for_algrow(text: str, max_chars: int = _MAX_SCRIPT_CHARS, min_chars: int = _MIN_SCRIPT_CHARS) -> list:
    from app.services.tts.edge import _split_text_for_tts

    chunks = _split_text_for_tts(text, max_chars=max_chars)
    # Algrow rejects any single call under min_chars — merge a short trailing
    # chunk into its predecessor rather than sending it as its own request.
    if len(chunks) > 1 and len(chunks[-1]) < min_chars:
        last = chunks.pop()
        chunks[-1] = f"{chunks[-1]} {last}"
    return chunks


def _download_audio(url: str, out_file: str) -> bool:
    try:
        ensure_file_path_exists(out_file)
        with requests.get(url, stream=True, timeout=(10, 60)) as resp:
            resp.raise_for_status()
            with open(out_file, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    fh.write(chunk)
        return True
    except Exception as exc:
        logger.error(f"algrow tts: audio download failed: {exc}")
        return False


def _poll_job(job_id: str, headers: dict, deadline: float) -> Optional[dict]:
    while True:
        try:
            resp = requests.get(
                f"{_algrow_endpoint()}/api/job-status/{job_id}", headers=headers, timeout=30
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.error(f"algrow tts: status poll failed for job {job_id}: {exc}")
            return None

        status = payload.get("status")
        if status in ("completed", "failed"):
            return payload
        if time.monotonic() >= deadline:
            logger.warning(f"algrow tts: job {job_id} still not terminal at deadline — giving up")
            return None
        time.sleep(_POLL_INTERVAL_SECONDS)


def _algrow_submit_and_download(text: str, extra_fields: dict, out_file: str, deadline: float) -> bool:
    """Shared generate-simple -> poll -> download flow for any provider.

    extra_fields are sent verbatim as additional multipart form fields
    alongside `script` (e.g. voice_id/provider/speed/... for minimax,
    voice_id/provider/model_id/stability/... for elevenlabs).
    """
    headers = _algrow_headers()
    files = {"script": (None, text), **{k: (None, str(v)) for k, v in extra_fields.items()}}
    try:
        resp = requests.post(
            f"{_algrow_endpoint()}/api/generate-simple", headers=headers, files=files, timeout=30
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.error(f"algrow tts: submit failed: {exc}")
        return False

    status = payload.get("status")
    if status != "completed":
        job_id = payload.get("job_id")
        if status == "failed" or not job_id:
            logger.error(
                f"algrow tts: job failed to start: {payload.get('error_message') or payload.get('error') or payload}"
            )
            return False
        payload = _poll_job(job_id, headers, deadline)
        if not payload:
            return False

    if payload.get("status") == "failed":
        logger.error(f"algrow tts: job failed: {payload.get('error_message') or payload.get('error')}")
        return False

    audio_url = payload.get("audio_url")
    if not audio_url:
        logger.error(f"algrow tts: completed payload missing audio_url: {payload}")
        return False

    return _download_audio(audio_url, out_file)


def _algrow_minimax_tts_single(
    text: str, voice_id: str, speed: float, pitch: int, volume: float, out_file: str, deadline: float
) -> bool:
    return _algrow_submit_and_download(
        text,
        {"voice_id": voice_id, "provider": "minimax", "speed": speed, "pitch": pitch, "volume": volume},
        out_file,
        deadline,
    )


def _algrow_elevenlabs_tts_single(
    text: str, voice_id: str, model_id: str, stability: float, similarity_boost: float,
    speed: float, out_file: str, deadline: float,
) -> bool:
    return _algrow_submit_and_download(
        text,
        {
            "voice_id": voice_id, "provider": "elevenlabs", "model_id": model_id,
            "stability": stability, "similarity_boost": similarity_boost, "speed": speed,
        },
        out_file,
        deadline,
    )


def _run_chunked(text: str, voice_file: str, single_call: Callable[[str, str, float], bool]) -> Optional[float]:
    """Splits text, runs single_call(chunk, out_file, deadline) per chunk
    (each with its own full deadline — see algrow_timeout_seconds' comment
    in config.toml for why chunks can't share one), concatenates, and
    returns the final audio's duration, or None on any failure."""
    chunks = _split_for_algrow(text)

    if len(chunks) == 1:
        deadline = time.monotonic() + _algrow_timeout_seconds()
        if not single_call(text, voice_file, deadline):
            return None
    else:
        logger.info(f"algrow chunked TTS: {len(chunks)} chunks for {len(text)} chars")
        with tempfile.TemporaryDirectory() as tmpdir:
            chunk_files = []
            for i, chunk in enumerate(chunks):
                chunk_file = os.path.join(tmpdir, f"chunk_{i:04d}.mp3")
                deadline = time.monotonic() + _algrow_timeout_seconds()
                if not single_call(chunk, chunk_file, deadline):
                    logger.error(f"algrow TTS chunk {i+1}/{len(chunks)} failed")
                    return None
                chunk_files.append(chunk_file)
                logger.info(f"algrow TTS chunk {i+1}/{len(chunks)} done")

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
                logger.error("algrow audio chunk concatenation failed")
                return None

        logger.info(f"algrow chunked TTS complete → {voice_file}")

    return get_audio_duration(voice_file)


def algrow_minimax_tts(
    text: str, voice_name: str, voice_rate: float, voice_file: str
) -> Union[SubMaker, None]:
    if not config.app.get("algrow_api_key", ""):
        logger.error("algrow_minimax_tts: algrow_api_key not set in config")
        return None

    voice_map = config.app.get("minimax_algrow_voice_map", {}) or {}
    voice_id = voice_map.get(voice_name, voice_name)
    if voice_id == voice_name:
        logger.warning(
            f"algrow_minimax_tts: {voice_name!r} has no entry in minimax_algrow_voice_map — "
            "sending it as-is, which will fail unless it's already an Algrow-native voice_id"
        )

    speed, pitch, volume = 1.0, 0, 1.0
    if voice_rate and abs(float(voice_rate) - 1.0) > 1e-9:
        logger.warning(
            f"algrow_minimax_tts ignores voice_rate ({voice_rate}) — speed is hardcoded to 1.0"
        )

    duration = _run_chunked(
        text, voice_file,
        lambda chunk, out_file, deadline: _algrow_minimax_tts_single(
            chunk, voice_id, speed, pitch, volume, out_file, deadline
        ),
    )
    if duration is None:
        return None

    logger.info(f"algrow tts complete | voice={voice_name}")
    sub_maker = ensure_legacy_submaker_fields(SubMaker())
    return populate_legacy_submaker_with_full_text(
        sub_maker=sub_maker, text=text, audio_duration_seconds=duration,
    )


def algrow_elevenlabs_tts(
    text: str, voice_name: str, voice_rate: float, voice_file: str
) -> Union[SubMaker, None]:
    """voice_name is used directly as Algrow's catalog voice_id — ElevenLabs
    catalog voices need no cloning/mapping step, unlike minimax above."""
    if not config.app.get("algrow_api_key", ""):
        logger.error("algrow_elevenlabs_tts: algrow_api_key not set in config")
        return None

    voice_id = voice_name
    model_id = str(config.app.get("algrow_elevenlabs_model", "eleven_multilingual_v2"))
    stability, similarity_boost, speed = 0.5, 0.5, 1.0
    if voice_rate and abs(float(voice_rate) - 1.0) > 1e-9:
        logger.warning(
            f"algrow_elevenlabs_tts ignores voice_rate ({voice_rate}) — speed is hardcoded to 1.0"
        )

    duration = _run_chunked(
        text, voice_file,
        lambda chunk, out_file, deadline: _algrow_elevenlabs_tts_single(
            chunk, voice_id, model_id, stability, similarity_boost, speed, out_file, deadline
        ),
    )
    if duration is None:
        return None

    logger.info(f"algrow tts complete | voice={voice_name}")
    sub_maker = ensure_legacy_submaker_fields(SubMaker())
    return populate_legacy_submaker_with_full_text(
        sub_maker=sub_maker, text=text, audio_duration_seconds=duration,
    )


def clone_minimax_voice(voice_name: str, audio_path: str, language: str = "English") -> Optional[str]:
    """One-off helper for scripts/algrow_clone_from_minimax.py — clones a MiniMax
    voice into Algrow's own account from a local reference-audio file and returns
    the new Algrow voice_id, or None on failure."""
    headers = _algrow_headers()
    try:
        with open(audio_path, "rb") as fh:
            files = {
                "voice_name": (None, voice_name),
                "language": (None, language),
                "audio_file": (os.path.basename(audio_path), fh, "audio/mpeg"),
            }
            resp = requests.post(
                f"{_algrow_endpoint()}/api/voices/minimax/clone", headers=headers, files=files, timeout=120
            )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.error(f"algrow: clone failed for {voice_name!r}: {exc}")
        return None

    voice = payload.get("voice") or {}
    voice_id = voice.get("voice_id")
    if not voice_id:
        logger.error(f"algrow: clone response missing voice.voice_id: {payload}")
        return None
    return voice_id
