"""Narration loudness normalization (app/services/tts/_utils.py) — real
ffmpeg, no network. TTS providers vary in output level; this runs between
TTS and whisper alignment."""
import shutil
import subprocess

import pytest

from app.services.tts import _utils as tts_utils
from app.utils import utils

_NO_FFMPEG = shutil.which("ffmpeg") is None


def _make_tone(path: str, volume_db: float, duration: float = 3.0) -> None:
    """Generate a quiet/loud sine-wave mp3 fixture via ffmpeg lavfi (no network)."""
    cmd = [
        utils.get_ffmpeg_binary(), "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-af", f"volume={volume_db}dB",
        "-ar", "44100", "-c:a", "libmp3lame", path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_normalize_boosts_a_quiet_track(tmp_path):
    audio_path = str(tmp_path / "quiet.mp3")
    _make_tone(audio_path, volume_db=-35)

    before = tts_utils._measure_loudness(utils.get_ffmpeg_binary(), audio_path)
    assert before is not None
    assert float(before["input_i"]) < -25  # confirm the fixture really is quiet

    ok = tts_utils.normalize_narration_loudness(audio_path)
    assert ok is True

    after = tts_utils._measure_loudness(utils.get_ffmpeg_binary(), audio_path)
    assert after is not None
    # Should land much closer to the -16 LUFS target than the original -35ish.
    assert abs(float(after["input_i"]) - tts_utils._LOUDNORM_TARGET_I) < 3.0


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_normalize_preserves_duration(tmp_path):
    audio_path = str(tmp_path / "tone.mp3")
    _make_tone(audio_path, volume_db=-30, duration=4.0)

    assert tts_utils.normalize_narration_loudness(audio_path) is True
    assert abs(tts_utils.get_audio_duration(audio_path) - 4.0) < 0.5


def test_normalize_missing_file_returns_false(tmp_path):
    missing = str(tmp_path / "does-not-exist.mp3")
    assert tts_utils.normalize_narration_loudness(missing) is False


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_normalize_falls_back_to_single_pass_when_measurement_fails(tmp_path, monkeypatch):
    audio_path = str(tmp_path / "tone.mp3")
    _make_tone(audio_path, volume_db=-30)

    monkeypatch.setattr(tts_utils, "_measure_loudness", lambda *a, **k: None)
    assert tts_utils.normalize_narration_loudness(audio_path) is True
