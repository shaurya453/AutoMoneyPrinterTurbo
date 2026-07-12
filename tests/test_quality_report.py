"""Quality report + per-clip decisions ledger — no network."""
import json

from app.services.pipeline._quality import write_quality_report


def _report_fixture() -> dict:
    return {
        0: {
            "provider": "serper", "content_track": "named", "media_type": "image",
            "rejections": ["vlm", "dedup"], "used_dedup_fallback": False,
            "accepted": True, "sentence_idx": 0, "chosen_term": "Fossil leather tote",
            "url": "https://example.com/a.jpg", "clip_score": 0.31, "vlm_score": 0.8,
        },
        1: {
            "provider": "pexels", "content_track": "broll", "media_type": "image",
            "rejections": [], "used_dedup_fallback": False, "accepted": True,
            "sentence_idx": 1, "below_margin": True, "below_margin_score": 0.21,
        },
        2: {
            "provider": "", "content_track": "named", "media_type": "image",
            "rejections": ["vlm"], "used_dedup_fallback": False, "accepted": False,
            "sentence_idx": 2, "below_margin_blocked": True, "rescued": True,
        },
        3: {
            "provider": "pexels", "content_track": "broll", "media_type": "video",
            "rejections": [], "used_dedup_fallback": False, "accepted": True,
            "sentence_idx": 3, "chosen_term": "retail store aisle",
            "url": "https://example.com/v.mp4", "vlm_score": 0.6,
        },
    }


def test_writes_quality_and_decisions_files(tmp_path):
    agg = write_quality_report(
        work_dir=str(tmp_path), title="t", quality_report=_report_fixture(),
        sentences=[{}] * 4, sentence_got_clip={0, 1, 3},
        tts_char_count=100, tts_provider="edge",
    )
    assert (tmp_path / "t.quality.json").exists()
    decisions_file = tmp_path / "t.decisions.json"
    assert decisions_file.exists()

    decisions = json.loads(decisions_file.read_text(encoding="utf-8"))
    assert set(decisions["clips"]) == {"0", "1", "2", "3"}
    assert decisions["clips"]["0"]["sentence_idx"] == 0
    assert decisions["clips"]["0"]["url"] == "https://example.com/a.jpg"
    assert decisions["clips"]["1"]["below_margin"] is True

    assert agg["below_margin_accept_count"] == 1
    assert agg["below_margin_blocked_count"] == 1
    assert agg["image_clips"] == 2
    assert agg["video_clips"] == 1


def test_counts_zero_when_flags_absent(tmp_path):
    report = {0: {"provider": "pexels", "media_type": "image", "accepted": True,
                  "rejections": [], "used_dedup_fallback": False}}
    agg = write_quality_report(
        work_dir=str(tmp_path), title="t", quality_report=report,
        sentences=[{}], sentence_got_clip={0},
        tts_char_count=1, tts_provider="edge",
    )
    assert agg["below_margin_accept_count"] == 0
    assert agg["below_margin_blocked_count"] == 0
