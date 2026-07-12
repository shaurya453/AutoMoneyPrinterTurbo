"""Unit tests for Pass-1 slot planning and query-ladder building."""
from app.services.pipeline._planning import (
    _CLIP_TARGET,
    _IMAGE_CLIP_MAX,
    _MIN_VISUAL_DUR,
    _OUTRO_TAIL,
    _build_query_ladder,
    plan_clip_slots,
)


def _sent(text="A sentence.", **kw):
    s = {"text": text, "media_type": "video", "content_track": "broll"}
    s.update(kw)
    return s


# ---- plan_clip_slots ------------------------------------------------------ #

def test_basic_two_sentences_track_whisper_starts():
    timings = [(_sent(), 0.0, 4.0), (_sent(), 4.0, 8.0)]
    plans = plan_clip_slots(timings, audio_duration=8.0)
    assert len(plans) == 2
    # First slot runs exactly until the second sentence's narration begins.
    assert sum(plans[0]["durations"]) == 4.0
    # Last slot covers its narration plus the outro tail.
    assert sum(plans[1]["durations"]) == 4.0 + _OUTRO_TAIL


def test_total_planned_equals_audio_plus_outro():
    timings = [
        (_sent(), 0.0, 3.2),
        (_sent(), 3.2, 9.7),
        (_sent(), 9.7, 14.0),
    ]
    plans = plan_clip_slots(timings, audio_duration=14.0)
    total = sum(sum(p["durations"]) for p in plans)
    assert abs(total - (14.0 + _OUTRO_TAIL)) < 1e-9


def test_short_sentences_do_not_accumulate_drift():
    # Three consecutive sub-2s sentences: the _MIN_VISUAL_DUR floor must NOT
    # apply (each takes exactly its narration slot), so the final sentence's
    # footage still starts on its whisper timestamp.
    timings = [
        (_sent("Same money."), 0.0, 1.0),
        (_sent("None of the heartbreak."), 1.0, 2.2),
        (_sent("That's the point."), 2.2, 3.0),
        (_sent("A normal closing sentence follows this."), 3.0, 8.0),
    ]
    plans = plan_clip_slots(timings, audio_duration=8.0)
    # Sum of the first three slots == 3.0 → the fourth starts exactly at 3.0.
    lead_in = sum(sum(p["durations"]) for p in plans[:3])
    assert abs(lead_in - 3.0) < 1e-9


def test_min_visual_dur_floor_applies_to_normal_sentences():
    # A 3s sentence whose slot is squeezed to 0.5s by the next sentence's
    # start still gets floored (footage never shorter than the floor).
    timings = [(_sent(), 0.0, 3.0), (_sent(), 0.5, 6.0)]
    plans = plan_clip_slots(timings, audio_duration=6.0)
    assert sum(plans[0]["durations"]) >= _MIN_VISUAL_DUR


def test_long_image_sentence_splits_into_multiple_stills():
    timings = [
        (_sent(media_type="image"), 0.0, 16.0),
        (_sent(), 16.0, 20.0),
    ]
    plans = plan_clip_slots(timings, audio_duration=20.0)
    assert plans[0]["is_image"] is True
    # 16s > _IMAGE_CLIP_MAX → must split so one still never holds 16s.
    assert len(plans[0]["durations"]) >= 2
    assert sum(plans[0]["durations"]) == 16.0


def test_short_image_sentence_stays_single_clip():
    timings = [(_sent(media_type="image"), 0.0, 5.0), (_sent(), 5.0, 9.0)]
    plans = plan_clip_slots(timings, audio_duration=9.0)
    assert len(plans[0]["durations"]) == 1


def test_narrated_graphic_keeps_whisper_slot_with_1s_floor():
    # Graphic slot shorter than 1s gets the Revideo stability floor only —
    # never the full _MIN_VISUAL_DUR inflation.
    timings = [
        (_sent("Quick.", graphic_type="lower_third"), 0.0, 0.8),
        (_sent(), 0.8, 5.0),
    ]
    plans = plan_clip_slots(timings, audio_duration=5.0)
    assert sum(plans[0]["durations"]) == 1.0


def test_video_sentence_splits_by_clip_target():
    timings = [(_sent(), 0.0, 12.0), (_sent(), 12.0, 16.0)]
    plans = plan_clip_slots(timings, audio_duration=16.0)
    assert len(plans[0]["durations"]) == round(12.0 / _CLIP_TARGET)


def test_leading_silence_absorbed_by_first_slot():
    # Whisper's first word starts at 0.7s (leading silence). The first slot
    # must start at 0 and cover the lead-in, so sentence 1's footage lands on
    # its ABSOLUTE narration time — not 0.7s early (the muxed audio plays
    # from 0, so t0-relative scheduling shifts every visual early by t0).
    timings = [(_sent(), 0.7, 4.0), (_sent(), 4.0, 9.0)]
    plans = plan_clip_slots(timings, audio_duration=9.0)
    assert sum(plans[0]["durations"]) == 4.0  # 0 → 4.0, lead-in included
    total = sum(sum(p["durations"]) for p in plans)
    assert abs(total - (9.0 + _OUTRO_TAIL)) < 1e-9


def test_later_sentences_start_at_absolute_whisper_times():
    timings = [
        (_sent(), 0.5, 3.0),
        (_sent(), 3.0, 7.0),
        (_sent(), 7.0, 12.0),
    ]
    plans = plan_clip_slots(timings, audio_duration=12.0)
    # Footage is butt-joined, so each sentence's visual start is the cumsum of
    # the preceding slots — it must equal the absolute narration start.
    assert abs(sum(plans[0]["durations"]) - 3.0) < 1e-9
    assert abs(sum(sum(p["durations"]) for p in plans[:2]) - 7.0) < 1e-9


# ---- _build_query_ladder --------------------------------------------------- #

def test_ladder_thematic_bare_concepts_plus_topic_anchor():
    ladder = _build_query_ladder(
        "vanishing american brands",
        ["supermarket aisle", "empty shelves"],
        video_type="thematic",
    )
    assert ladder == [
        "supermarket aisle",
        "empty shelves",
        "vanishing american brands",
    ]


def test_ladder_named_entity_concept_then_topic_appended():
    ladder = _build_query_ladder(
        "vanishing brands",
        ["Corn Pops box"],
        video_type="named_entity",
    )
    assert ladder == ["Corn Pops box", "Corn Pops box vanishing brands", "vanishing brands"]


def test_ladder_deduplicates_in_order():
    ladder = _build_query_ladder("topic", ["a", "a", "b"], video_type="thematic")
    assert ladder == ["a", "b", "topic"]


def test_ladder_empty_topic_falls_back_to_bare_concepts():
    ladder = _build_query_ladder("", ["a", "b"], video_type="thematic")
    assert ladder == ["a", "b"]


def test_ladder_skips_blank_concepts():
    ladder = _build_query_ladder("topic", ["", "  ", "a"], video_type="thematic")
    assert ladder == ["a", "topic"]
