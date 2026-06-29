#!/usr/bin/env python3
"""
validate_job.py — pre-Phase 2 job.json quality gate (zero API cost)

Usage: venv/bin/python validate_job.py <path/to/job.json>

Exit 0 → VALIDATION_OK or VALIDATION_WARNINGS: <list>
Exit 1 → VALIDATION_ERROR: <reason>   (blocks Phase 2)

Hard errors (exit 1):
  - JSON can't be parsed
  - video_script missing or empty
  - sentences missing or empty

Warnings (exit 0, logged by worker):
  - video_topic missing
  - video_type not thematic/named_entity
  - content_track='graphic' used (Pattern 1 removed; use broll + graphic_type instead)
  - Any broll/named sentence missing visual_concepts or visual_caption
  - Duplicate visual_caption across sentences
  - concept[0] used > 4 times total (AGENT_GUIDE hard limit)
  - Same concept[0] on > 2 consecutive enrichable sentences
  - Unique concept[0] count below max(15, ceil(N/4)) for videos ≥ 20 sentences
  - Unknown visual_effect value on any sentence (valid: threat/cold/warmth/mystery/sepia/tech/hacker_tech/dream/noir/nature/revelation)
  - visual_effect set on a graphic sentence
  - Effect-bearing sentences exceed 30% of broll/named sentences
  - Same visual_effect on > 3 consecutive broll/named sentences
  - Both 'sepia' and 'noir' used in the same video
"""

import json
import math
import sys

_VALID_VISUAL_EFFECTS = frozenset({
    "threat", "cold", "warmth", "mystery", "sepia",
    "tech", "hacker_tech", "dream", "noir", "nature", "revelation",
})


def main():
    if len(sys.argv) < 2:
        print("VALIDATION_ERROR: no job.json path provided", flush=True)
        sys.exit(1)

    job_path = sys.argv[1]

    try:
        with open(job_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        print(f"VALIDATION_ERROR: JSON parse failed — {exc}", flush=True)
        sys.exit(1)
    except FileNotFoundError:
        print(f"VALIDATION_ERROR: file not found — {job_path}", flush=True)
        sys.exit(1)

    errors = []
    warnings = []

    # ── Hard checks ──────────────────────────────────────────────────────────
    if not data.get("video_script", "").strip():
        errors.append("video_script is missing or empty")

    sentences = data.get("sentences")
    if not sentences or not isinstance(sentences, list):
        errors.append("sentences is missing or empty")

    if errors:
        print(f"VALIDATION_ERROR: {'; '.join(errors)}", flush=True)
        sys.exit(1)

    # ── Soft checks ──────────────────────────────────────────────────────────
    if not data.get("video_topic", "").strip():
        warnings.append("video_topic is missing — concept anchoring will be weak")

    if data.get("video_type") not in ("thematic", "named_entity"):
        warnings.append(
            f"video_type={data.get('video_type')!r} — expected 'thematic' or 'named_entity'"
        )

    seen_captions: dict = {}      # caption → first sentence index
    concept0_counts: dict = {}    # concept[0] → count
    enrichable: list = []         # (sentence_index, concept0) for non-graphic sentences

    text_errors = []

    for i, sent in enumerate(sentences):
        track = sent.get("content_track", "broll")
        text = (sent.get("text") or "").strip()

        if track == "graphic":
            text_errors.append(
                f"sentence {i}: content_track='graphic' is no longer supported. "
                "All graphics must be narrated (Pattern 2): keep content_track as 'broll', "
                "set graphic_type and variables, leave text as the narration."
            )
            continue

        if track in ("broll", "named") and not text:
            text_errors.append(
                f"sentence {i} (track={track!r}): text field is empty — "
                "Whisper cannot align; pipeline falls back to 2 s placeholder. "
                "The agent must never clear the text field during enrichment."
            )

        vc = sent.get("visual_concepts")
        if not vc or not isinstance(vc, list) or len(vc) == 0:
            if track in ("broll", "named"):
                warnings.append(f"sentence {i}: visual_concepts is empty (track={track!r})")
            enrichable.append((i, None))
        else:
            c0 = vc[0]
            enrichable.append((i, c0))
            concept0_counts[c0] = concept0_counts.get(c0, 0) + 1

        caption = (sent.get("visual_caption") or "").strip()
        if not caption and track in ("broll", "named"):
            warnings.append(f"sentence {i}: visual_caption is missing")
        elif caption:
            if caption in seen_captions:
                warnings.append(
                    f"sentence {i}: duplicate visual_caption (same as sentence {seen_captions[caption]})"
                )
            else:
                seen_captions[caption] = i

        effect = (sent.get("visual_effect") or "").strip()
        if effect:
            if track == "graphic":
                warnings.append(f"sentence {i}: visual_effect set on graphic sentence (ignored by pipeline)")
            elif effect not in _VALID_VISUAL_EFFECTS:
                warnings.append(f"sentence {i}: unknown visual_effect={effect!r}")

    # ── Text-field hard errors ───────────────────────────────────────────────
    if text_errors:
        print(
            f"VALIDATION_ERROR: {len(text_errors)} sentence(s) have empty text; "
            f"first: {text_errors[0]}",
            flush=True,
        )
        sys.exit(1)

    # ── Effect aggregate checks ──────────────────────────────────────────────
    enrichable_tracks = [
        (sent.get("visual_effect") or "").strip()
        for sent in sentences
        if sent.get("content_track", "broll") in ("broll", "named")
    ]
    effect_bearing = [e for e in enrichable_tracks if e and e in _VALID_VISUAL_EFFECTS]
    if enrichable_tracks:
        pct = len(effect_bearing) / len(enrichable_tracks)
        if pct > 0.30:
            warnings.append(
                f"visual_effect overuse: {len(effect_bearing)}/{len(enrichable_tracks)} "
                f"broll/named sentences have effects ({pct:.0%} > 30% limit)"
            )

    # Same effect on > 3 consecutive broll/named sentences
    eff_run, eff_run_val, eff_run_start = 1, "", 0
    for j in range(1, len(enrichable_tracks)):
        if enrichable_tracks[j] and enrichable_tracks[j] == enrichable_tracks[j - 1]:
            eff_run += 1
            if eff_run > 3 and eff_run == 4:
                warnings.append(
                    f"visual_effect consecutive run >3: {enrichable_tracks[j]!r} "
                    f"starting near broll/named sentence index {j - 2}"
                )
        else:
            eff_run = 1

    used_effects = set(effect_bearing)
    if "sepia" in used_effects and "noir" in used_effects:
        warnings.append("both 'sepia' and 'noir' effects used — pick at most one per video")
    if "tech" in used_effects and "hacker_tech" in used_effects:
        warnings.append("both 'tech' and 'hacker_tech' effects used — pick at most one per video")

    # Only consider enrichable slots that actually have a concept[0]
    enrichable_with_c0 = [(idx, c0) for idx, c0 in enrichable if c0 is not None]
    n = len(enrichable_with_c0)

    # Rule: concept[0] used > 4 times total (AGENT_GUIDE hard limit)
    overused = [(c, cnt) for c, cnt in concept0_counts.items() if cnt > 4]
    if overused:
        detail = ", ".join(f"{c!r}×{cnt}" for c, cnt in overused[:6])
        warnings.append(f"concept[0] overuse (max 4 allowed): {detail}")

    # Rule: same concept[0] on > 2 consecutive enrichable sentences
    run = 1
    consecutive_violations = []
    for j in range(1, len(enrichable_with_c0)):
        prev_c0 = enrichable_with_c0[j - 1][1]
        curr_c0 = enrichable_with_c0[j][1]
        if curr_c0 == prev_c0:
            run += 1
            if run > 2:
                consecutive_violations.append(
                    f"sentence {enrichable_with_c0[j][0]} ({curr_c0!r}, run={run})"
                )
        else:
            run = 1
    if consecutive_violations:
        warnings.append(
            f"concept[0] consecutive repetition >2 (AGENT_GUIDE max is 2): "
            + "; ".join(consecutive_violations[:4])
        )

    # Rule: variety audit — unique concept[0] count ≥ max(15, ceil(N/4))
    # Only applies to videos with ≥ 20 enrichable sentences (shorter videos
    # can't physically reach 15 unique concepts without padding).
    if n >= 20:
        unique_c0 = len(concept0_counts)
        required = max(15, math.ceil(n / 4))
        if unique_c0 < required:
            warnings.append(
                f"concept variety low: {unique_c0} unique concept[0] values for {n} enriched "
                f"sentences (AGENT_GUIDE minimum: {required})"
            )

    if warnings:
        print(f"VALIDATION_WARNINGS: {'; '.join(warnings)}", flush=True)
    else:
        print("VALIDATION_OK", flush=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
