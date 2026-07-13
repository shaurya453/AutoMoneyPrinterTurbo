"""Avatar service: command builders, RunPod client (mocked), dry-run render."""
import shutil
import types

import pytest

from app.services import avatar


# ---------------------------------------------------------------------------
# Command builders (pure)
# ---------------------------------------------------------------------------

def test_slice_cmd_seeks_and_floors_min_length():
    cmd = avatar.build_slice_cmd("audio.mp3", 12.5, 12.8, "out.mp3")
    joined = " ".join(cmd)
    assert "-ss 12.500" in joined
    # 0.3s slice floored to 1.0s of audio.
    assert "-to 13.500" in joined
    assert "-ac 1" in joined and "libmp3lame" in joined


def test_slice_cmd_normal_range():
    cmd = avatar.build_slice_cmd("audio.mp3", 0.0, 12.0, "out.mp3")
    joined = " ".join(cmd)
    assert "-ss 0.000" in joined and "-to 12.000" in joined


def test_normalize_cmd_pads_trims_and_strips_audio():
    cmd = avatar.build_normalize_cmd("raw.mp4", "out.mp4", 1920, 1080, 13.4)
    joined = " ".join(cmd)
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in joined
    assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black" in joined
    assert "fps=30" in joined
    assert "tpad=stop_mode=clone" in joined
    assert "-t 13.400" in joined
    assert "-an" in cmd
    assert "yuv420p" in cmd


# ---------------------------------------------------------------------------
# RunPod client (requests monkeypatched — repo rule: no network in tests)
# ---------------------------------------------------------------------------

def _resp(payload, status_code=200):
    r = types.SimpleNamespace(status_code=status_code)
    r.json = lambda: payload
    r.raise_for_status = lambda: None
    return r


def test_runsync_completed_returns_url(monkeypatch):
    # "result" is the real key InfiniteTalk uses (confirmed via a live probe,
    # 2026-07-12) — output.video_url was an incorrect assumption from the
    # (undocumented) schema and silently dropped every successful generation.
    monkeypatch.setattr(avatar.requests, "post", lambda *a, **k: _resp(
        {"status": "COMPLETED", "output": {"cost": 0.25, "result": "https://x/video.mp4"}}
    ))
    url = avatar._submit_runsync("img", "aud", "720p", "p", deadline=avatar.time.monotonic() + 60)
    assert url == "https://x/video.mp4"


def test_extract_video_url_prefers_result_falls_back_to_video_url():
    assert avatar._extract_video_url({"output": {"result": "https://x/a.mp4"}}) == "https://x/a.mp4"
    assert avatar._extract_video_url({"output": {"video_url": "https://x/b.mp4"}}) == "https://x/b.mp4"
    assert avatar._extract_video_url({"output": {"result": "https://x/a.mp4", "video_url": "https://x/b.mp4"}}) == "https://x/a.mp4"
    assert avatar._extract_video_url({"output": {}}) is None
    assert avatar._extract_video_url({}) is None


def test_runsync_failed_returns_none(monkeypatch):
    monkeypatch.setattr(avatar.requests, "post", lambda *a, **k: _resp(
        {"status": "FAILED", "error": "boom"}
    ))
    assert avatar._submit_runsync("img", "aud", "720p", "p",
                                  deadline=avatar.time.monotonic() + 60) is None


def test_runsync_in_queue_falls_back_to_status_poll(monkeypatch):
    monkeypatch.setattr(avatar.requests, "post", lambda *a, **k: _resp(
        {"status": "IN_QUEUE", "id": "job-1"}
    ))
    polls = iter([
        {"status": "IN_PROGRESS", "id": "job-1"},
        {"status": "COMPLETED", "output": {"cost": 0.25, "result": "https://x/late.mp4"}},
    ])
    monkeypatch.setattr(avatar.requests, "get", lambda *a, **k: _resp(next(polls)))
    monkeypatch.setattr(avatar.time, "sleep", lambda s: None)
    url = avatar._submit_runsync("img", "aud", "720p", "p",
                                 deadline=avatar.time.monotonic() + 60)
    assert url == "https://x/late.mp4"


def test_runsync_deadline_expiry_returns_none(monkeypatch):
    monkeypatch.setattr(avatar.requests, "post", lambda *a, **k: _resp(
        {"status": "IN_QUEUE", "id": "job-1"}
    ))
    monkeypatch.setattr(avatar.requests, "get", lambda *a, **k: _resp(
        {"status": "IN_PROGRESS", "id": "job-1"}
    ))
    monkeypatch.setattr(avatar.time, "sleep", lambda s: None)
    # Deadline already passed → poll loop never runs.
    assert avatar._submit_runsync("img", "aud", "720p", "p",
                                  deadline=avatar.time.monotonic() - 1) is None


def test_runsync_submit_exception_returns_none(monkeypatch):
    def boom(*a, **k):
        raise avatar.requests.exceptions.ConnectionError("no route")
    monkeypatch.setattr(avatar.requests, "post", boom)
    assert avatar._submit_runsync("img", "aud", "720p", "p",
                                  deadline=avatar.time.monotonic() + 60) is None


# ---------------------------------------------------------------------------
# Dry-run path (local lavfi ffmpeg render — no network)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_generate_avatar_clip_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(avatar.config, "app", {
        **avatar.config.app,
        "avatar_enabled": True,
        "avatar_dry_run": True,
    })
    report = {}
    out = avatar.generate_avatar_clip(
        image_path="unused.png", audio_file="unused.mp3",
        t_start=0.0, t_end=2.0, planned_duration=2.0,
        clips_dir=str(tmp_path), clip_idx=0, width=192, height=108,
        image_url=None, local_dir=None, url_prefix=None, report=report,
    )
    assert out == str(tmp_path / "clip-0000.mp4")
    assert report[0]["avatar"] is True and report[0]["avatar_dry_run"] is True


def test_generate_avatar_clip_pads_audio_slice_tail(monkeypatch, tmp_path):
    """Whisper's end timestamp can land a touch early — the slice fed to the
    avatar API should get a tail buffer so the trailing word isn't clipped."""
    monkeypatch.setattr(avatar.config, "app", {
        **avatar.config.app,
        "avatar_enabled": True,
    })
    monkeypatch.setattr(avatar, "dry_run_enabled", lambda: False)

    seen = {}

    def fake_slice_audio(audio_file, t_start, t_end, out_path):
        seen["t_start"] = t_start
        seen["t_end"] = t_end
        return True

    monkeypatch.setattr(avatar, "slice_audio", fake_slice_audio)
    # Stop right after slicing — no need to hit the network for this test.
    monkeypatch.setattr(avatar, "_submit_runsync", lambda *a, **k: None)

    avatar.generate_avatar_clip(
        image_path="unused.png", audio_file="unused.mp3",
        t_start=5.0, t_end=12.0, planned_duration=7.0,
        clips_dir=str(tmp_path), clip_idx=0, width=192, height=108,
        image_url="https://example.com/img.png",
        local_dir=str(tmp_path), url_prefix="https://example.com/assets",
    )

    assert seen["t_start"] == 5.0
    assert seen["t_end"] == 12.0 + avatar._AUDIO_TAIL_BUFFER_SECONDS


def test_is_enabled_requires_key_or_dry_run(monkeypatch):
    monkeypatch.setattr(avatar.config, "app", {"avatar_enabled": True})
    assert avatar.is_enabled() is False
    monkeypatch.setattr(avatar.config, "app",
                        {"avatar_enabled": True, "runpod_api_key": "k"})
    assert avatar.is_enabled() is True
    monkeypatch.setattr(avatar.config, "app",
                        {"avatar_enabled": False, "runpod_api_key": "k"})
    assert avatar.is_enabled() is False
    monkeypatch.setattr(avatar.config, "app",
                        {"avatar_enabled": True, "avatar_dry_run": True})
    assert avatar.is_enabled() is True
