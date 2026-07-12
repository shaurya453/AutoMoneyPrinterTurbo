"""download_image acceptance-gate tests — no network.

Covers the CLIP-before-VLM ordering and the gated below-margin fallback
added after the 2026-07-11 handbags post-mortem (13 below-margin accepts,
incl. a wrong-brand bag under Michael Kors narration; 1,381 paid VLM
rejections on candidates local CLIP would have caught first).
"""
from unittest import mock

import pytest

from app.services.media import images
from app.services.scoring import nsfw, relevance, vlm


@pytest.fixture()
def stub_env(tmp_path, monkeypatch):
    """One stub provider with two candidates; NSFW passes; save_image → tmp files."""
    urls = ["https://stub.example.com/a.jpg", "https://stub.example.com/b.jpg"]

    def _provider(query, n=6, rank_tokens=None, avoid_tokens=None):
        return list(urls)

    saved = {}

    def _save(url, save_dir=""):
        p = tmp_path / (url.rsplit("/", 1)[-1])
        p.write_bytes(b"fakeimagebytes-" + url.encode())
        saved[url] = str(p)
        return str(p)

    monkeypatch.setattr(images, "_IMAGE_PROVIDERS", {"stub": _provider})
    monkeypatch.setattr(images, "save_image", _save)
    monkeypatch.setattr(nsfw, "is_nsfw_image", lambda b: 0.0)
    monkeypatch.setattr(nsfw, "passes", lambda r: True)
    monkeypatch.setattr(relevance, "is_available", lambda: True)
    monkeypatch.setattr(relevance, "is_log_only", lambda: False)
    monkeypatch.setattr(relevance, "embed_image", lambda b: None)
    return {"urls": urls, "saved": saved}


def _download(report=None, **kw):
    defaults = dict(
        search_terms=["some concept"],
        source_order=["stub"],
        caption_prompt="a caption, some topic",
        narration="Narration.",
        visual_caption="a caption",
        video_topic="some topic",
        report=report,
    )
    defaults.update(kw)
    return images.download_image(**defaults)


# ── gated below-margin fallback ──────────────────────────────────────────────

def _margin_fail(monkeypatch, score=0.25):
    monkeypatch.setattr(relevance, "score", lambda p, b: score)
    monkeypatch.setattr(relevance, "passes_margin", lambda p, b, m: False)


def test_named_track_never_below_margin_accepts(stub_env, monkeypatch):
    _margin_fail(monkeypatch)
    monkeypatch.setattr(vlm, "is_enabled", lambda: False)
    report = {}
    assert _download(report=report, content_track="named") == ""
    assert report.get("below_margin_blocked") is True
    assert "below_margin" not in report or report.get("below_margin") is not True


def test_high_criticality_broll_never_below_margin_accepts(stub_env, monkeypatch):
    _margin_fail(monkeypatch)
    monkeypatch.setattr(vlm, "is_enabled", lambda: False)
    report = {}
    assert _download(report=report, content_track="broll", criticality="high") == ""
    assert report.get("below_margin_blocked") is True


def test_broll_below_floor_blocked(stub_env, monkeypatch):
    _margin_fail(monkeypatch, score=0.169)  # < image_fallback_min_score 0.20
    monkeypatch.setattr(vlm, "is_enabled", lambda: False)
    report = {}
    assert _download(report=report, content_track="broll", criticality="medium") == ""
    assert report.get("below_margin_blocked") is True


def test_broll_above_floor_accepts_with_flag(stub_env, monkeypatch):
    _margin_fail(monkeypatch, score=0.25)
    monkeypatch.setattr(vlm, "is_enabled", lambda: False)
    report = {}
    result = _download(report=report, content_track="broll", criticality="medium")
    assert result  # accepted
    assert report.get("below_margin") is True
    assert report.get("below_margin_score") == 0.25


def test_broll_fallback_still_vlm_checked(stub_env, monkeypatch):
    _margin_fail(monkeypatch, score=0.25)
    monkeypatch.setattr(vlm, "is_enabled", lambda: True)
    monkeypatch.setattr(vlm, "verify_image", lambda *a, **k: 0.1)  # below 0.30 image bar
    report = {}
    assert _download(report=report, content_track="broll", criticality="medium") == ""
    assert report.get("below_margin_blocked") is True


# ── CLIP-before-VLM ordering ─────────────────────────────────────────────────

def test_vlm_never_called_when_margin_fails(stub_env, monkeypatch):
    _margin_fail(monkeypatch)
    monkeypatch.setattr(vlm, "is_enabled", lambda: True)
    verify = mock.Mock(return_value=0.9)
    monkeypatch.setattr(vlm, "verify_image", verify)
    # named track: fallback blocked, so NO VLM call should happen anywhere
    assert _download(content_track="named") == ""
    verify.assert_not_called()


def test_clip_runs_before_vlm_on_pass(stub_env, monkeypatch):
    calls = []
    monkeypatch.setattr(relevance, "score", lambda p, b: calls.append("clip") or 0.4)
    monkeypatch.setattr(relevance, "passes_margin", lambda p, b, m: True)
    monkeypatch.setattr(vlm, "is_enabled", lambda: True)
    monkeypatch.setattr(vlm, "verify_image", lambda *a, **k: calls.append("vlm") or 0.9)
    monkeypatch.setattr(vlm, "compare_candidates", lambda *a, **k: 0)
    result = _download(content_track="broll")
    assert result
    assert calls[0] == "clip"
    assert "vlm" in calls
    assert calls.index("clip") < calls.index("vlm")


def test_vlm_still_gates_when_clip_unavailable(stub_env, monkeypatch):
    # Degradation path: CLIP off → VLM-only gating, no relevance calls at all.
    monkeypatch.setattr(relevance, "is_available", lambda: False)
    monkeypatch.setattr(vlm, "is_enabled", lambda: True)
    monkeypatch.setattr(vlm, "verify_image", lambda *a, **k: 0.05)  # reject all
    report = {}
    assert _download(report=report, content_track="broll") == ""
    assert report.get("rejections") == ["vlm", "vlm"]


def test_accept_records_decision_metadata(stub_env, monkeypatch):
    monkeypatch.setattr(relevance, "score", lambda p, b: 0.42)
    monkeypatch.setattr(relevance, "passes_margin", lambda p, b, m: True)
    monkeypatch.setattr(vlm, "is_enabled", lambda: False)
    report = {}
    result = _download(report=report, content_track="broll")
    assert result
    assert report["provider"] == "stub"
    assert report["chosen_term"] == "some concept"
    assert report["url"].startswith("https://stub.example.com/")
    assert report["clip_score"] == 0.42
