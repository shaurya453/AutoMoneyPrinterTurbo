"""Post-render quality report: what happened during clip fetch, and what it cost.

Written once per job to storage/tasks/<title>/<title>.quality.json after
final.mp4 is produced. Aggregates the per-clip `report` dict that
_fetch_video_clip/_fetch_image_clip populate as a side channel (see their
docstrings) plus TTS/VLM cost fields, so a run can be audited without
re-reading logs.
"""
import json
import os
from collections import Counter
from typing import Optional

from loguru import logger


def write_quality_report(
    work_dir: str,
    title: str,
    quality_report: dict,
    sentences: list,
    sentence_got_clip: set,
    tts_char_count: int,
    tts_provider: str,
    vlm_usage: Optional[dict] = None,
) -> dict:
    """Aggregate per-clip data + cost fields and write <title>.quality.json.

    quality_report: {clip_idx: {provider, content_track, media_type,
        rejections, used_dedup_fallback, accepted}}, populated in-place by
        _fetch_video_clip/_fetch_image_clip during Phase B.
    sentence_got_clip: sentence indices that ended up with real footage
        (already tracked in _orchestrate.py's Phase B) -- the complement is
        the gap count (sentences that fell back to a placeholder clip).

    Returns the aggregate dict that was written, so the caller can fold its
    path/summary into the top-level pipeline result if useful.
    """
    entries = list(quality_report.values())
    rejection_totals: Counter = Counter()
    for entry in entries:
        rejection_totals.update(entry.get("rejections", []))

    image_count = sum(1 for e in entries if e.get("media_type") == "image" and e.get("accepted"))
    video_count = sum(1 for e in entries if e.get("media_type") == "video" and e.get("accepted"))
    accepted_count = image_count + video_count
    dedup_fallback_count = sum(1 for e in entries if e.get("used_dedup_fallback"))
    gap_count = max(0, len(sentences) - len(sentence_got_clip))

    provider_totals: Counter = Counter(
        e.get("provider", "") for e in entries if e.get("accepted") and e.get("provider")
    )
    vlm_compare_used_count = sum(1 for e in entries if e.get("vlm_compare_used"))
    rescued_count = sum(1 for e in entries if e.get("rescued"))
    placeholder_used_count = sum(1 for e in entries if e.get("placeholder_used"))
    below_margin_accept_count = sum(1 for e in entries if e.get("below_margin"))
    below_margin_blocked_count = sum(1 for e in entries if e.get("below_margin_blocked"))

    # Catastrophic gaps: slots where the real fetch AND every placeholder
    # attempt failed -- a true hole in the timeline (see _orchestrate.py's
    # position-aware gap-fill pass). "_recovered" is only present once that
    # pass has run, so absence means it never got a chance to try.
    catastrophic_gap_entries = [e for e in entries if e.get("catastrophic_gap")]
    catastrophic_gap_count = len(catastrophic_gap_entries)
    catastrophic_gap_unrecovered_count = sum(
        1 for e in catastrophic_gap_entries if not e.get("catastrophic_gap_recovered")
    )
    catastrophic_gap_seconds = round(
        sum(e.get("catastrophic_gap_duration", 0.0) for e in catastrophic_gap_entries), 2
    )

    # Paid AI-avatar (Segmind/RunPod InfiniteTalk) generations only -- avatar.py sets
    # "avatar_dry_run" instead of a real request when AVATAR_DRY_RUN is on,
    # and that path costs nothing, so it's excluded from the billable count.
    billable_avatar_entries = [
        e for e in entries if e.get("avatar") and not e.get("avatar_dry_run")
    ]
    avatar_request_count = len(billable_avatar_entries)
    # Real dollar cost as reported back by the provider itself (avatar.py's
    # _extract_runpod_cost/_extract_segmind_cost), not an estimate -- absent
    # when the provider's response didn't carry a cost field.
    avatar_cost_usd = round(
        sum(e.get("avatar_cost_usd", 0.0) for e in billable_avatar_entries), 6
    )

    aggregate = {
        "title": title,
        "total_clips": len(entries),
        "accepted_clips": accepted_count,
        "image_clips": image_count,
        "video_clips": video_count,
        "image_ratio": round(image_count / accepted_count, 3) if accepted_count else None,
        "gap_count": gap_count,
        "dedup_fallback_count": dedup_fallback_count,
        "vlm_compare_used_count": vlm_compare_used_count,
        "rescued_count": rescued_count,
        "placeholder_used_count": placeholder_used_count,
        "below_margin_accept_count": below_margin_accept_count,
        "below_margin_blocked_count": below_margin_blocked_count,
        "catastrophic_gap_count": catastrophic_gap_count,
        "catastrophic_gap_unrecovered_count": catastrophic_gap_unrecovered_count,
        "catastrophic_gap_seconds": catastrophic_gap_seconds,
        "rejection_totals": dict(rejection_totals),
        "provider_totals": dict(provider_totals),
        "cost": {
            "tts_char_count": tts_char_count,
            "tts_provider": tts_provider,
            "vlm_usage": vlm_usage or {},
            "avatar_request_count": avatar_request_count,
            "avatar_cost_usd": avatar_cost_usd,
        },
    }

    quality_path = os.path.join(work_dir, f"{title}.quality.json")
    try:
        with open(quality_path, "w", encoding="utf-8") as fh:
            json.dump(aggregate, fh, indent=2)
        logger.info(f"quality report written: {quality_path}")
    except Exception as exc:
        logger.warning(f"failed to write quality report: {exc}")

    # Per-clip decision ledger: the raw entries quality.json aggregates away.
    # Answers "which exact image/term/score ended up at clip N" without
    # grepping the run log (sentence_idx ties a clip back to its narration).
    decisions_path = os.path.join(work_dir, f"{title}.decisions.json")
    try:
        with open(decisions_path, "w", encoding="utf-8") as fh:
            json.dump(
                {"clips": {str(k): quality_report[k] for k in sorted(quality_report)}},
                fh, indent=2,
            )
        logger.info(f"decisions ledger written: {decisions_path}")
    except Exception as exc:
        logger.warning(f"failed to write decisions ledger: {exc}")

    aggregate["_path"] = quality_path
    return aggregate
