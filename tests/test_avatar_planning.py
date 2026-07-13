"""plan_avatar_blocks grouping/cap logic + slot_start exposure."""
from app.services.pipeline._planning import plan_avatar_blocks, plan_clip_slots


def _plan(idx_start, dur, avatar=False, graphic=None):
    sent = {"text": f"s{idx_start}"}
    if avatar:
        sent["avatar"] = True
    if graphic:
        sent["graphic_type"] = graphic
    return {
        "sent": sent,
        "is_image": False,
        "durations": [dur],
        "sent_audio_dur": dur,
        "slot_start": idx_start,
    }


def _plans(specs):
    """specs: list of (dur, avatar, graphic); slot_start = cumulative sum."""
    plans, t = [], 0.0
    for dur, av, gr in specs:
        plans.append(_plan(t, dur, avatar=av, graphic=gr))
        t += dur
    return plans


def test_contiguous_grouping_single_block():
    plans = _plans([(4.0, True, None), (4.0, True, None), (4.0, False, None)])
    blocks, demoted = plan_avatar_blocks(plans)
    assert demoted == []
    assert len(blocks) == 1
    blk = blocks[0]
    assert blk["first_idx"] == 0
    assert blk["member_idxs"] == [0, 1]
    assert blk["slot_start"] == 0.0
    assert blk["planned_duration"] == 8.0


def test_two_separate_blocks():
    plans = _plans([
        (4.0, True, None), (4.0, False, None),
        (5.0, True, None), (5.0, True, None), (4.0, False, None),
    ])
    blocks, demoted = plan_avatar_blocks(plans)
    assert demoted == []
    assert [b["first_idx"] for b in blocks] == [0, 2]
    assert blocks[1]["member_idxs"] == [2, 3]
    assert blocks[1]["slot_start"] == 8.0
    assert blocks[1]["planned_duration"] == 10.0


def test_no_length_cap_long_block_kept_in_full():
    # No length cap — live RunPod testing (2026-07-12) found no failure mode
    # tied to generation duration, so long blocks are never trimmed/dropped.
    plans = _plans([(6.0, True, None), (6.0, True, None), (6.0, True, None)])
    blocks, demoted = plan_avatar_blocks(plans)
    assert demoted == []
    assert blocks[0]["member_idxs"] == [0, 1, 2]
    assert blocks[0]["planned_duration"] == 18.0


def test_single_very_long_member_kept_not_dropped():
    plans = _plans([(20.0, True, None), (4.0, False, None)])
    blocks, demoted = plan_avatar_blocks(plans)
    assert demoted == []
    assert blocks[0]["member_idxs"] == [0]
    assert blocks[0]["planned_duration"] == 20.0


def test_graphic_type_never_joins_a_block():
    plans = _plans([(4.0, True, None), (4.0, True, "lower_third"), (4.0, True, None)])
    blocks, demoted = plan_avatar_blocks(plans)
    # The graphic sentence splits the run into two one-member blocks.
    assert [b["member_idxs"] for b in blocks] == [[0], [2]]
    assert demoted == []


def test_multi_clip_sentence_duration_summed():
    plans = _plans([(4.0, True, None)])
    plans[0]["durations"] = [3.0, 3.0]
    blocks, _ = plan_avatar_blocks(plans)
    assert blocks[0]["planned_duration"] == 6.0


def test_no_avatar_sentences_no_blocks():
    plans = _plans([(4.0, False, None), (4.0, False, None)])
    assert plan_avatar_blocks(plans) == ([], [])


def test_plan_clip_slots_exposes_slot_start():
    timings = [
        ({"text": "a", "media_type": "video"}, 0.4, 3.0),
        ({"text": "b", "media_type": "video"}, 3.2, 7.0),
    ]
    plans = plan_clip_slots(timings, audio_duration=8.0)
    # First slot pinned to 0 (absorbs leading silence); second at its
    # absolute whisper start.
    assert plans[0]["slot_start"] == 0.0
    assert plans[1]["slot_start"] == 3.2
