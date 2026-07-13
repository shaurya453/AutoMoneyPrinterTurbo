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
  - any sentence has avatar=true but top-level avatar_image is missing/empty
  - literal <<PLUG>> or <</PLUG>> marker text found in a sentence or video_script
    (sentence_prep.py should have stripped it — would otherwise be spoken/shown)

Warnings (exit 0, logged by worker):
  - video_topic missing
  - video_topic shares no words with video_title (likely a section theme)
  - named_entity video_topic shares no words with any entity_name (phantom
    anchor, e.g. a retailer name used as the topic)
  - video_type not thematic/named_entity
  - Duplicate concept within one sentence's visual_concepts
  - One value repeated across too many sentences' visual_concepts[1] or [2]
  - video_type='thematic' with 3+ distinct entity_names (should be named_entity)
  - lower_third used on a sentence that is not content_track='named'
  - graphic sentence missing variables dict (would render blank)
  - Any broll/named sentence missing visual_concepts or visual_caption
  - Duplicate visual_caption across sentences
  - concept[0] used > 4 times total (AGENT_GUIDE hard limit)
  - Same concept[0] on > 2 consecutive enrichable sentences
  - Unique concept[0] count below max(15, ceil(N/4)) for videos ≥ 20 sentences
  - Unknown visual_effect value on any sentence (valid: threat/cold/warmth/mystery/sepia/tech/
    hacker_tech/dream/noir/nature/revelation/urgency/euphoria/corporate/glitch_soft/confusion/
    network/royalty/static_dread/toxic)
  - visual_effect set on a graphic sentence
  - Effect-bearing sentences below 50% of broll/named sentences (target density; adjacent
    effect-bearing sentences are fine — an overlay persisting across clips isn't jarring)
  - info_callout used on a sentence that is not content_track='named'
  - info_callout variables.labels is not a list of exactly 3 non-empty strings
  - More than 5 info_callout graphics total, or two info_callout graphics too close together
    (own cap/spacing, separate from the infographic/list pool below — info_callout composites
    over footage like lower_third rather than replacing it, so shares that cost class instead)

Avatar warnings (talking-head blocks — see app/services/avatar.py):
  - avatar_image path does not exist on disk
  - avatar_image set but sentence 0 lacks avatar=true (intro is FIXED avatar)
  - more than 2 contiguous avatar blocks
  - avatar sentence also carrying graphic_type or visual_effect

Hook Zone warnings (sentences 0–4):
  - lower_third, infographic, or list graphic_type in sentences 0–4
  - media_type='image' on a broll sentence in sentences 0–4
  - Fewer than 2 visual_effects across sentences 0–4
  - Any visual_concepts slot left empty in sentences 0–4
"""

import json
import math
import os
import sys

_HOOK_ZONE = 5  # first N sentences constitute the hook zone (~30 s at normal narration speed)

_VALID_GTYPES = frozenset({"lower_third", "infographic", "list", "info_callout"})
_HOOK_BANNED_GTYPES = _VALID_GTYPES  # all graphic types are banned from the hook zone

_VALID_VISUAL_EFFECTS = frozenset({
    "threat", "cold", "warmth", "mystery", "sepia",
    "tech", "hacker_tech", "dream", "noir", "nature", "revelation",
    "urgency", "euphoria", "corporate", "glitch_soft", "confusion",
    "network", "royalty", "static_dread", "toxic",
})

_STOPWORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "our", "so",
    "than", "that", "the", "this", "to", "we", "what", "when",
    "why", "with", "you", "your", "not", "one", "only",
}


def _tokens(s: str) -> set:
    """Meaningful lowercase words of a phrase (stopwords stripped)."""
    return {
        w.strip(".,!?:;\"'()[]-–—").lower()
        for w in (s or "").split()
    } - _STOPWORDS - {""}


def _stems(tokens: set) -> set:
    """Tokens plus naive singular forms, so 'handbags' matches 'handbag'."""
    return tokens | {t.rstrip("s") for t in tokens}


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
    video_topic = data.get("video_topic", "").strip()
    if not video_topic:
        warnings.append("video_topic is missing — concept anchoring will be weak")
    else:
        # video_topic anchors EVERY relevance check and search ladder in the
        # job. A topic that shares no meaningful words with the title is
        # almost always a section theme or hook (e.g. "midday grease test"
        # for a tinted-sunscreen review) — it poisons footage selection for
        # the entire video.
        title = (data.get("video_title") or "").strip()
        if title:
            topic_tokens = _tokens(video_topic)
            title_tokens = _tokens(title)
            # Also match on simple singular/plural stems so
            # "sunscreens" (title) matches "sunscreen" (topic).
            title_stems = _stems(title_tokens)
            topic_stems = _stems(topic_tokens)
            if topic_tokens and title_tokens and not (topic_stems & title_stems):
                warnings.append(
                    f"video_topic {video_topic!r} shares no words with the title "
                    f"{title!r} — it looks like a section theme, not the video's "
                    "subject. This anchors every search and relevance check; a "
                    "wrong topic degrades footage for the whole video"
                )

    _gapfill_terms = [t for t in (data.get("gapfill_terms") or []) if isinstance(t, str) and t.strip()]
    if len(_gapfill_terms) < 10:
        warnings.append(
            f"gapfill_terms has only {len(_gapfill_terms)} usable entries "
            "(AGENT_GUIDE asks for 15–20) — a long video can burn a dozen "
            "rescue fetches, and a thin pool recycles the same generic imagery"
        )

    if data.get("video_type") not in ("thematic", "named_entity"):
        warnings.append(
            f"video_type={data.get('video_type')!r} — expected 'thematic' or 'named_entity'"
        )

    seen_captions: dict = {}      # caption → first sentence index
    concept0_counts: dict = {}    # concept[0] → count
    concept_slot_counts: dict = {1: {}, 2: {}}  # slot → value → count (broad-slot collapse)
    enrichable: list = []         # (sentence_index, concept0) for non-graphic sentences
    non_lt_graphic_indices: list = []  # sentence indices with infographic/list graphic_type
    info_callout_indices: list = []  # sentence indices with info_callout graphic_type (own cap/spacing)
    named_entities: set = set()   # distinct entity_name values on named sentences

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
            variables = sent.get("variables")
            if not isinstance(variables, dict):
                warnings.append(
                    f"sentence {i}: graphic_type={gtype!r} has no 'variables' dict — "
                    "graphic will render blank. All fields (title, label, items, style, etc.) "
                    "must be inside variables: {}"
                )
            if gtype_str in ("infographic", "list"):
                non_lt_graphic_indices.append(i)
            elif gtype_str == "info_callout":
                info_callout_indices.append(i)
                if track != "named":
                    warnings.append(
                        f"sentence {i}: info_callout used on a sentence that is not "
                        f"content_track='named' (track={track!r}) — info_callout only makes "
                        "sense pointing at a named-entity product image"
                    )
                if isinstance(variables, dict):
                    labels = variables.get("labels")
                    valid_labels = (
                        isinstance(labels, list)
                        and len(labels) == 3
                        and all(isinstance(l, str) and l.strip() for l in labels)
                    )
                    if not valid_labels:
                        warnings.append(
                            f"sentence {i}: info_callout variables.labels must be a list of "
                            f"exactly 3 non-empty strings, got {labels!r}"
                        )

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
            for slot in (1, 2):
                if len(vc) > slot and isinstance(vc[slot], str) and vc[slot].strip():
                    val = vc[slot].strip()
                    concept_slot_counts[slot][val] = concept_slot_counts[slot].get(val, 0) + 1
            _vc_strs = [c for c in vc if isinstance(c, str)]
            if len(_vc_strs) != len(set(_vc_strs)):
                warnings.append(
                    f"sentence {i}: duplicate concept within visual_concepts {vc!r} — "
                    "a repeated concept wastes a search-ladder rung; make each slot distinct"
                )

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

        crit = str(sent.get("visual_criticality") or "").strip().lower()
        if crit and crit not in ("low", "medium", "high", "critical"):
            warnings.append(
                f"sentence {i}: unknown visual_criticality={crit!r} "
                "(expected low|medium|high|critical) — treated as 'medium'"
            )

        if sent.get("standalone_subject"):
            # Referent-swap sentences drop the video_topic anchor from search
            # and scoring — without the named track + must_show they lose all
            # subject grounding (AGENT_GUIDE: standalone_subject).
            if track != "named":
                warnings.append(
                    f"sentence {i}: standalone_subject=true but content_track is "
                    f"'{track}' — referent-swap sentences should use the named track "
                    "so the entity itself is Serper-searchable"
                )
            if not (sent.get("must_show") or []):
                warnings.append(
                    f"sentence {i}: standalone_subject=true but must_show is empty — "
                    "name the standalone entity so the VLM can verify it appears"
                )

        if track == "named":
            entity = (sent.get("entity_name") or "").strip()
            if entity:
                named_entities.add(entity.lower())
            else:
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
                f"in sentences 0–{_HOOK_ZONE - 1}; save all graphics (lower_third, infographic, "
                "list, info_callout) for after the viewer is hooked"
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

    # ── Leaked plug marker ───────────────────────────────────────────────────
    # sentence_prep.py strips <<PLUG>>/<</PLUG>> tokens before splitting; if
    # either survives into a sentence or video_script, it would be read aloud
    # by TTS or shown on-screen.
    plug_marker_hits = [
        i for i, s in enumerate(sentences)
        if "<<PLUG>>" in (s.get("text") or "") or "<</PLUG>>" in (s.get("text") or "")
    ]
    script_has_marker = "<<PLUG>>" in (data.get("video_script") or "") or \
        "<</PLUG>>" in (data.get("video_script") or "")
    if plug_marker_hits or script_has_marker:
        print(
            "VALIDATION_ERROR: literal <<PLUG>>/<</PLUG>> marker text found "
            f"in sentence(s) {plug_marker_hits} or video_script "
            f"({script_has_marker}) — sentence_prep.py should have stripped it",
            flush=True,
        )
        sys.exit(1)

    # ── Avatar checks ────────────────────────────────────────────────────────
    avatar_image = str(data.get("avatar_image") or "").strip()
    avatar_idxs = [i for i, s in enumerate(sentences) if s.get("avatar")]

    if avatar_idxs and not avatar_image:
        print(
            f"VALIDATION_ERROR: {len(avatar_idxs)} sentence(s) have avatar=true "
            "but top-level avatar_image is missing — the pipeline cannot generate "
            "a talking head without a source image",
            flush=True,
        )
        sys.exit(1)

    if avatar_image:
        if not os.path.isfile(avatar_image):
            warnings.append(
                f"avatar_image does not exist on disk ({avatar_image!r}) — "
                "the pipeline will demote every avatar sentence to normal footage"
            )
        if avatar_idxs and 0 not in avatar_idxs:
            warnings.append(
                "avatar_image is set but sentence 0 lacks avatar=true — the intro "
                "block is FIXED for the avatar whenever an image is selected"
            )

        # Group contiguous avatar sentences into blocks. No length cap — live
        # RunPod testing (2026-07-12) found no failure mode tied to
        # generation duration, so blocks are never demoted for being long.
        blocks: list = []
        for i in avatar_idxs:
            if blocks and i == blocks[-1][-1] + 1:
                blocks[-1].append(i)
            else:
                blocks.append([i])
        if len(blocks) > 2:
            warnings.append(
                f"{len(blocks)} avatar blocks found — expected at most 2 "
                "(the fixed intro plus one mid-video spot)"
            )
        for i in avatar_idxs:
            if sentences[i].get("graphic_type"):
                warnings.append(
                    f"sentence {i}: avatar=true AND graphic_type="
                    f"{sentences[i].get('graphic_type')!r} — mutually exclusive; "
                    "the pipeline will ignore the avatar flag on this sentence"
                )
            if (sentences[i].get("visual_effect") or "").strip():
                warnings.append(
                    f"sentence {i}: visual_effect on an avatar sentence is "
                    "meaningless (the avatar clip replaces footage) — remove it"
                )

    # ── video_type sanity ────────────────────────────────────────────────────
    # A review/ranking that names 3+ distinct entities should be named_entity:
    # thematic mode uses bare-concept ladders that stock libraries can't match
    # to specific products, so most named sections degrade to generic footage.
    if data.get("video_type") == "thematic" and len(named_entities) >= 3:
        warnings.append(
            f"video_type='thematic' but {len(named_entities)} distinct named "
            "entities are present — reviews/rankings naming 3+ products should "
            "use video_type='named_entity' (routes product sentences to Google "
            "Images and anchors ladders correctly)"
        )

    # ── Phantom topic anchor ─────────────────────────────────────────────────
    # In a named_entity job, video_topic is appended to every search rung and
    # to every CLIP/VLM prompt. A topic that shares no words with ANY of the
    # job's entity_names is usually not a searchable product subject at all —
    # the classic case is a retailer roundup ("Marshall handbags" for a video
    # about Kate Spade / Fossil / Brahmin bags sold at Marshalls), where the
    # retailer-as-topic poisons search results and rejects good candidates.
    if data.get("video_type") == "named_entity" and video_topic and named_entities:
        topic_stems = _stems(_tokens(video_topic))
        entity_stems = set()
        for _e in named_entities:
            entity_stems |= _stems(_tokens(_e))
        if topic_stems and entity_stems and not (topic_stems & entity_stems):
            warnings.append(
                f"video_topic {video_topic!r} shares no words with any entity_name "
                f"({len(named_entities)} distinct) — the topic anchors every search "
                "and relevance prompt, so it must be the product category the "
                "entities belong to (e.g. 'designer leather handbags'), never a "
                "retailer/store name; put the retailer only in gapfill_terms or "
                "broll scene concepts"
            )

    # ── Criticality inflation ────────────────────────────────────────────────
    # The effort dial is zero-sum: high/critical raise per-clip search budgets,
    # candidate counts, and VLM comparisons. Marking everything up just makes
    # the whole fetch phase slow without improving any single clip.
    _elevated = sum(
        1 for sent in sentences
        if str(sent.get("visual_criticality") or "").strip().lower() in ("high", "critical")
    )
    if sentences and _elevated / len(sentences) > 0.30:
        warnings.append(
            f"{_elevated}/{len(sentences)} sentences are visual_criticality high/critical "
            "(>30%) — reserve elevated criticality for the few sentences where a wrong "
            "visual is actually noticeable (AGENT_GUIDE: visual_criticality)"
        )

    # ── Effect aggregate checks ──────────────────────────────────────────────
    enrichable_tracks = [
        (sent.get("visual_effect") or "").strip()
        for sent in sentences
        if sent.get("content_track", "broll") in ("broll", "named")
    ]
    effect_bearing = [e for e in enrichable_tracks if e and e in _VALID_VISUAL_EFFECTS]
    if enrichable_tracks:
        pct = len(effect_bearing) / len(enrichable_tracks)
        if pct < 0.50:
            warnings.append(
                f"visual_effect density too low: {len(effect_bearing)}/{len(enrichable_tracks)} "
                f"broll/named sentences have effects ({pct:.0%} < 50% target) — "
                "distribute effects more evenly; aim for at least 1 per every 2 sentences"
            )

    # No adjacency restriction: an overlay persists across a clip rather than
    # flickering between adjacent clips, so back-to-back effect sentences are
    # intentional at this density and not warned on.

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

    # ── info_callout cap and spacing ─────────────────────────────────────────
    # Own counter, separate from the infographic/list pool above — info_callout
    # composites over footage (same cost class as lower_third) rather than
    # replacing it, so it doesn't share that pool's cap or its warning wording.
    _INFO_CALLOUT_CAP = 5
    if len(info_callout_indices) > _INFO_CALLOUT_CAP:
        warnings.append(
            f"too many info_callout graphics: {len(info_callout_indices)} "
            f"(hard limit is ≤{_INFO_CALLOUT_CAP}); remove the least necessary ones"
        )
    for k in range(1, len(info_callout_indices)):
        gap = info_callout_indices[k] - info_callout_indices[k - 1]
        if gap < 4:
            warnings.append(
                f"info_callout graphics too close: sentence {info_callout_indices[k - 1]} "
                f"and sentence {info_callout_indices[k]} are only {gap - 1} sentence(s) apart "
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

    # Rule: slots 1–2 collapse — the broad fallback slots may legitimately
    # repeat more than concept[0], but one value shared across a large chunk
    # of the video means every fallback search returns the same images (mass
    # dedup rejections, starved rescue variety).
    if n:
        slot_cap = max(6, math.ceil(n / 6))
        for slot in (1, 2):
            collapsed = [
                (v, cnt) for v, cnt in concept_slot_counts[slot].items() if cnt > slot_cap
            ]
            if collapsed:
                detail = ", ".join(f"{v!r}×{cnt}" for v, cnt in sorted(
                    collapsed, key=lambda p: -p[1])[:4])
                warnings.append(
                    f"visual_concepts[{slot}] collapse (max {slot_cap} repeats "
                    f"for {n} sentences): {detail} — vary the broad fallback "
                    "concepts too, or fallback searches all return the same images"
                )

    if warnings:
        print(f"VALIDATION_WARNINGS: {'; '.join(warnings)}", flush=True)
    else:
        print("VALIDATION_OK", flush=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
