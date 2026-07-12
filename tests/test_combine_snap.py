"""Cumulative frame-grid snap — rounding error must not accumulate."""
import random

from app.services.render._common import fps
from app.services.render.combine import _cumulative_snap


def _simulate(planned, raw_extra, snap_fn):
    """Emit clips through a snap policy; return per-position |planned-emitted|.

    `raw_extra[i]` is how much longer the source file is than its planned
    duration (Ken Burns renders overshoot by up to a frame; some sources
    come up short). The snap can only TRIM (like combine_videos): a raw
    duration below the target is emitted as-is.
    """
    planned_cum = emitted = 0.0
    errors = []
    for p, extra in zip(planned, raw_extra):
        raw = p + extra
        snap = snap_fn(planned_cum, emitted, p)
        planned_cum += p
        emitted += snap if raw > snap + 0.001 else raw
        errors.append(abs(planned_cum - emitted))
    return errors


def test_cumulative_snap_error_bounded_by_one_frame():
    rng = random.Random(7)
    planned = [rng.uniform(2.0, 9.0) for _ in range(200)]
    # Overshoot up to +1 frame (ffmpeg -t includes the partial last frame).
    raw_extra = [rng.uniform(0.0, 1.0 / fps) for _ in planned]
    errors = _simulate(planned, raw_extra, _cumulative_snap)
    assert max(errors) < 1.0 / fps


def test_old_per_clip_snap_accumulates_error():
    # Regression guard: the pre-fix policy (snap each clip's own duration to
    # the nearest frame) drifts without bound on the same input.
    def per_clip_snap(_planned_cum, _emitted, planned_dur):
        return max(1, round(planned_dur * fps)) / fps

    # Planned durations just past a frame boundary: per-clip snap rounds each
    # one down and trims (one-sided bias, the production failure mode), while
    # cumulative snap alternates and stays bounded.
    planned = [4.0 + 0.4 / fps] * 200
    raw_extra = [1.0 / fps] * 200
    errors = _simulate(planned, raw_extra, per_clip_snap)
    assert max(errors) > 3.0 / fps  # old policy: error grows well past a frame
    errors_new = _simulate(planned, raw_extra, _cumulative_snap)
    assert max(errors_new) < 1.0 / fps


def test_short_source_self_corrects_on_next_clip():
    # Clip 0's file is 0.4s SHORTER than planned (can't be extended). The
    # next clip's snap target must grow to absorb the shortfall.
    planned = [4.0, 4.0]
    planned_cum = emitted = 0.0

    snap0 = _cumulative_snap(planned_cum, emitted, planned[0])
    planned_cum += planned[0]
    emitted += min(3.6, snap0)  # source came up short

    snap1 = _cumulative_snap(planned_cum, emitted, planned[1])
    planned_cum += planned[1]
    emitted += snap1  # long source, trimmed to target

    assert snap1 > 4.0  # inflated target
    assert abs(planned_cum - emitted) < 1.0 / fps
