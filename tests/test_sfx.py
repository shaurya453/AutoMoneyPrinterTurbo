"""Unit tests for the mood-triggered SFX library and bed renderer."""
import random
import subprocess
import wave

import numpy as np

from app.services.render import generate, sfx


def test_pick_sfx_for_mood_known_mood_stays_in_group():
    rng = random.Random(0)
    for _ in range(20):
        pick = sfx.pick_sfx_for_mood("threat", rng=rng)
        assert pick is not None
        assert pick["id"] in sfx._MOOD_SFX["threat"]


def test_pick_sfx_for_mood_unknown_mood_falls_back_to_universal():
    rng = random.Random(0)
    for mood in ("royalty", "confusion", "euphoria", "not-a-real-mood"):
        pick = sfx.pick_sfx_for_mood(mood, rng=rng)
        assert pick is not None
        assert pick["id"] in sfx._UNIVERSAL_SFX


def test_pick_sfx_for_mood_avoids_consecutive_repeat():
    rng = random.Random(0)
    sfx._last_sfx_id = ""
    seen = [sfx.pick_sfx_for_mood("threat", rng=rng)["id"] for _ in range(30)]
    for a, b in zip(seen, seen[1:]):
        assert a != b, "consecutive picks repeated the same SFX id"


def test_sfx_library_files_all_exist():
    import os
    for entry in sfx._SFX_LIBRARY.values():
        path = os.path.join(sfx.sfx_dir(), entry["file"])
        assert os.path.exists(path), f"missing SFX asset: {path}"


def test_render_sfx_bed_wav_places_cues_at_offsets(tmp_path):
    tone_a = tmp_path / "a.mp3"
    tone_b = tmp_path / "b.mp3"
    for path, freq in ((tone_a, 220), (tone_b, 440)):
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", f"sine=frequency={freq}:duration=1", "-b:a", "96k", str(path)],
            check=True,
        )

    out = tmp_path / "bed.wav"
    ok = generate._render_sfx_bed_wav(
        cues=[(2.0, str(tone_a), 1.0), (6.0, str(tone_b), 1.0)],
        duration=10.0,
        out_path=str(out),
    )
    assert ok and out.exists()

    with wave.open(str(out)) as wf:
        assert wf.getnchannels() == 2
        n = wf.getnframes()
        sr = wf.getframerate()
        assert abs(n / sr - 10.0) < 0.05
        pcm = np.frombuffer(wf.readframes(n), dtype=np.int16).reshape(-1, 2).astype(np.float64)

    def rms(t0, t1):
        seg = pcm[int(t0 * sr):int(t1 * sr)]
        return np.sqrt((seg ** 2).mean())

    silence = rms(4.2, 4.8)
    cue_a = rms(2.1, 2.9)
    cue_b = rms(6.1, 6.9)
    assert cue_a > silence * 5, "first cue not audible at its offset"
    assert cue_b > silence * 5, "second cue not audible at its offset"


def test_render_sfx_bed_wav_no_cues_returns_false(tmp_path):
    out = tmp_path / "bed.wav"
    assert generate._render_sfx_bed_wav([], duration=5.0, out_path=str(out)) is False
    assert not out.exists()


def test_render_sfx_bed_wav_caps_long_cue_duration(tmp_path):
    long_tone = tmp_path / "long.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=9", "-b:a", "96k", str(long_tone)],
        check=True,
    )

    out = tmp_path / "bed.wav"
    ok = generate._render_sfx_bed_wav(
        cues=[(0.0, str(long_tone), 1.0)],
        duration=12.0,
        out_path=str(out),
    )
    assert ok and out.exists()

    with wave.open(str(out)) as wf:
        n = wf.getnframes()
        sr = wf.getframerate()
        pcm = np.frombuffer(wf.readframes(n), dtype=np.int16).reshape(-1, 2).astype(np.float64)

    def rms(t0, t1):
        seg = pcm[int(t0 * sr):int(t1 * sr)]
        return np.sqrt((seg ** 2).mean())

    assert rms(0.5, 1.0) > 100, "cue should be audible near its start"
    assert rms(5.0, 6.0) < 50, "a 9s source should be silent well past the 3.5s cue cap"


def test_render_sfx_bed_wav_applies_volume_scale(tmp_path):
    tone = tmp_path / "tone.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-b:a", "96k", str(tone)],
        check=True,
    )

    def render_rms(volume_scale):
        out = tmp_path / f"bed_{volume_scale}.wav"
        generate._render_sfx_bed_wav(
            cues=[(1.0, str(tone), 1.0)],
            duration=3.0,
            out_path=str(out),
            volume_scale=volume_scale,
        )
        with wave.open(str(out)) as wf:
            n = wf.getnframes()
            sr = wf.getframerate()
            pcm = np.frombuffer(wf.readframes(n), dtype=np.int16).reshape(-1, 2).astype(np.float64)
        seg = pcm[int(1.1 * sr):int(1.8 * sr)]
        return np.sqrt((seg ** 2).mean())

    full = render_rms(1.0)
    half = render_rms(0.5)
    assert half < full * 0.6 and half > full * 0.4, "volume_scale=0.5 should roughly halve cue amplitude"
