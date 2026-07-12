"""Unit tests for the ffmpeg-native final render helpers (E7)."""
import os
import subprocess
import wave

from app.models.schema import VideoParams
from app.services.render import generate


def _params(**kw):
    base = dict(
        video_subject="t",
        video_aspect="16:9",
        subtitle_enabled=True,
        subtitle_highlight=False,
        subtitle_position="bottom",
        font_name="Inter_18pt-SemiBold.ttf",
    )
    base.update(kw)
    return VideoParams(**base)


def test_ass_color_converts_rgb_to_bgr():
    assert generate._ass_color("#FFFFFF") == "&H00FFFFFF"
    assert generate._ass_color("#FF0000") == "&H000000FF"  # red → BGR
    assert generate._ass_color("#000000", alpha=0x50) == "&H50000000"
    assert generate._ass_color("") == "&H00FFFFFF"  # bad input → white


def test_eligibility_gates(tmp_path):
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n\n")

    assert generate._ffmpeg_render_eligible(_params(), str(srt)) is True
    # no subtitles at all → trivially supported
    assert generate._ffmpeg_render_eligible(_params(), "") is True
    # decorations still need MoviePy
    assert generate._ffmpeg_render_eligible(
        _params(subtitle_highlight=True), str(srt)) is False
    assert generate._ffmpeg_render_eligible(
        _params(subtitle_position="custom"), str(srt)) is False


def test_render_bgm_wav_duck_and_fade(tmp_path):
    bgm = tmp_path / "bgm.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=200:duration=3", "-b:a", "96k", str(bgm)],
        check=True,
    )
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:02,000 --> 00:00:06,000\nnarration\n\n")
    out = tmp_path / "ducked.wav"

    ok = generate._render_bgm_wav(
        bgm_file=str(bgm), duration=10.0, subtitle_path=str(srt),
        bgm_volume=0.5, duck_ratio=0.2, out_path=str(out),
    )
    assert ok and out.exists()

    with wave.open(str(out)) as wf:
        assert wf.getnchannels() == 2
        n = wf.getnframes()
        sr = wf.getframerate()
        assert abs(n / sr - 10.0) < 0.05, "loop+trim must hit requested duration"
        import numpy as np
        pcm = np.frombuffer(wf.readframes(n), dtype=np.int16).reshape(-1, 2)

    import numpy as np
    def rms(t0, t1):
        seg = pcm[int(t0 * sr):int(t1 * sr)].astype(np.float64)
        return np.sqrt((seg ** 2).mean())

    loud = rms(0.5, 1.5)      # before narration
    ducked = rms(3.0, 5.0)    # during narration
    tail = rms(9.8, 10.0)     # inside the 3s fade-out
    assert ducked < loud * 0.5, f"duck not applied: {ducked:.0f} vs {loud:.0f}"
    assert tail < loud * 0.2, f"fade-out not applied: {tail:.0f} vs {loud:.0f}"
