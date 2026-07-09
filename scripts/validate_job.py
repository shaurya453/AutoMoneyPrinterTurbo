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
  - content_track='graphic' used (no longer supported; use broll + graphic_type)

Warnings (exit 0, logged by worker):
  - video_topic missing
  - video_type not thematic/named_entity
  - lower_third used on a sentence that is not content_track='named'
  - graphic sentence missing variables dict (would render blank)
  - Any broll/named sentence missing visual_concepts or visual_caption
  - Duplicate visual_caption across sentences
  - concept[0] used > 4 times total (AGENT_GUIDE hard limit)
  - Same concept[0] on > 2 consecutive enrichable sentences
  - Unique concept[0] count below max(15, ceil(N/4)) for videos ≥ 20 sentences
  - Unknown visual_effect value on any sentence (valid: threat/cold/warmth/mystery/sepia/tech/hacker_tech/dream/noir/nature/revelation)
  - visual_effect set on a graphic sentence
  - Effect-bearing sentences below 40% of broll/named sentences (target density)
  - Any two adjacent broll/named sentences both carry a visual_effect

Hook Zone warnings (sentences 0–4):
  - lower_third, infographic, or list graphic_type in sentences 0–4
  - media_type='image' on a broll sentence in sentences 0–4
  - Fewer than 2 visual_effects across sentences 0–4
  - Any visual_concepts slot left empty in sentences 0–4
"""

import json
import math
import sys

_HOOK_ZONE = 5  # first N sentences constitute the hook zone (~30 s at normal narration speed)

_VALID_GTYPES = frozenset({"lower_third", "infographic", "list"})
_HOOK_BANNED_GTYPES = _VALID_GTYPES  # all graphic types are banned from the hook zone

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

    _gapfill_terms = [t for t in (data.get("gapfill_terms") or []) if isinstance(t, str) and t.strip()]
    if len(_gapfill_terms) < 5:
        warnings.append(
            "gapfill_terms is missing or has fewer than 5 usable entries — "
            "gap-fill/rescue fetches will fall back to generic hardcoded terms"
        )

    if data.get("video_type") not in ("thematic", "named_entity"):
        warnings.append(
            f"video_type={data.get('video_type')!r} — expected 'thematic' or 'named_entity'"
        )

    seen_captions: dict = {}      # caption → first sentence index
    concept0_counts: dict = {}    # concept[0] → count
    enrichable: list = []         # (sentence_index, concept0) for non-graphic sentences
    non_lt_graphic_indices: list = []  # sentence indices with infographic/list graphic_type

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
            effect_on_graphic = (sent.get("visual_effect") or "").strip()
            if effect_on_graphic:
                warnings.append(
                    f"sentence {i}: visual_effect={effect_on_graphic!r} set on graphic sentence (ignored by pipeline)"
                )
            continue

        gtype = sent.get("graphic_type")
        if gtype:
            gtype_str = gtype.strip() if isinstance(gtype, str) else str(gtype)
            if gtype_str not in _VALID_GTYPES:
                text_errors.append(
                    f"sentence {i}: graphic_type={gtype!r} is not a valid type. "
                    f"Valid types: {sorted(_VALID_GTYPES)}"
                )
            if not isinstance(sent.get("variables"), dict):
                warnings.append(
                    f"sentence {i}: graphic_type={gtype!r} has no 'variables' dict — "
                    "graphic will render blank. All fields (title, label, items, style, etc.) "
                    "must be inside variables: {}"
                )
            if gtype_str in ("infographic", "list"):
                non_lt_graphic_indices.append(i)

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
        if effect and effect not in _VALID_VISUAL_EFFECTS:
            warnings.append(f"sentence {i}: unknown visual_effect={effect!r}")

        if track == "named" and not (sent.get("entity_name") or "").strip():
            warnings.append(
                f"sentence {i}: content_track='named' but entity_name is missing — "
                "the graphics pass will fall back to a brand-token heuristic, which can "
                "mislabel or duplicate lower-thirds and produce truncated on-screen text"
            )

    # ── Hook Zone checks (sentences 0–4) ────────────────────────────────────
    hook_effect_count  = 0
    hook_broll_named   = 0
    hook_sents = sentences[:_HOOK_ZONE]
    for i, sent in enumerate(hook_sents):
        track = sent.get("content_track", "broll")
        gtype = (sent.get("graphic_type") or "").strip()

        if gtype in _HOOK_BANNED_GTYPES:
            warnings.append(
                f"hook zone violation — sentence {i}: graphic_type={gtype!r} is banned "
                f"in sentences 0–{_HOOK_ZONE - 1}; save lower_thirds, infographics, and lists "
                "for after the viewer is hooked"
            )

        if track in ("broll", "named"):
            hook_broll_named += 1
            mtype = (sent.get("media_type") or "video").strip()
            if mtype == "image":
                warnings.append(
                    f"hook zone — sentence {i}: media_type='image' on broll sentence slows "
                    f"opening pace; use 'video' in sentences 0–{_HOOK_ZONE - 1}"
                )
            vc = sent.get("visual_concepts")
            if not vc or not isinstance(vc, list) or len(vc) < 3:
                filled = len(vc) if isinstance(vc, list) else 0
                warnings.append(
                    f"hook zone — sentence {i}: only {filled}/3 visual_concepts slots filled; "
                    f"all 3 are required in sentences 0–{_HOOK_ZONE - 1} to maximise footage variety"
                )
            eff = (sent.get("visual_effect") or "").strip()
            if eff and eff in _VALID_VISUAL_EFFECTS:
                hook_effect_count += 1

    if hook_broll_named >= 3 and hook_effect_count < 2:
        warnings.append(
            f"hook zone (sentences 0–{_HOOK_ZONE - 1}) has only {hook_effect_count} "
            f"visual_effect(s) across {hook_broll_named} broll/named sentences — "
            "at least 2 cinematic effects are required in the opening to hold viewers"
        )

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
        if pct < 0.40:
            warnings.append(
                f"visual_effect density too low: {len(effect_bearing)}/{len(enrichable_tracks)} "
                f"broll/named sentences have effects ({pct:.0%} < 40% target) — "
                "distribute effects more evenly; aim for at least 2 per every 5 sentences"
            )

    # Any two adjacent broll/named sentences both carrying an effect
    for j in range(1, len(enrichable_tracks)):
        if enrichable_tracks[j] and enrichable_tracks[j - 1]:
            warnings.append(
                f"visual_effect on adjacent sentences: {enrichable_tracks[j - 1]!r} then "
                f"{enrichable_tracks[j]!r} — separate effect sentences with ≥2 plain sentences"
            )
            break  # one warning is enough to flag the problem

    # ── Non-lower-third graphic cap and spacing ──────────────────────────────
    _NON_LT_CAP = 8
    if len(non_lt_graphic_indices) > _NON_LT_CAP:
        warnings.append(
            f"too many infographic/list graphics: {len(non_lt_graphic_indices)} "
            f"(hard limit is ≤{_NON_LT_CAP}); remove the least necessary ones"
        )
    for k in range(1, len(non_lt_graphic_indices)):
        gap = non_lt_graphic_indices[k] - non_lt_graphic_indices[k - 1]
        if gap < 4:  # gap < 4 means fewer than 3 sentences between the two graphics
            warnings.append(
                f"infographic/list graphics too close: sentence {non_lt_graphic_indices[k - 1]} "
                f"and sentence {non_lt_graphic_indices[k]} are only {gap - 1} sentence(s) apart "
                f"— separate them with ≥3 footage sentences so each graphic has room to land"
            )

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
