"""Persisted whisper timings: save/load round-trip and mismatch rejection."""
import os

from app.services.pipeline._whisper import _load_timings, _save_timings


def _sents(n):
    return [{"text": f"Sentence {i}."} for i in range(n)]


def test_round_trip_reattaches_sentences(tmp_path):
    path = str(tmp_path / "timings.json")
    sents = _sents(3)
    timings = [(sents[0], 0.5, 3.0), (sents[1], 3.0, 7.25), (sents[2], 7.25, 12.0)]
    assert _save_timings(path, timings) is True

    loaded = _load_timings(path, sents)
    assert loaded is not None
    assert [(s["text"], a, b) for s, a, b in loaded] == [
        ("Sentence 0.", 0.5, 3.0),
        ("Sentence 1.", 3.0, 7.25),
        ("Sentence 2.", 7.25, 12.0),
    ]
    # Same dict objects re-attached, not copies.
    assert loaded[1][0] is sents[1]


def test_missing_file_returns_none(tmp_path):
    assert _load_timings(str(tmp_path / "nope.json"), _sents(2)) is None


def test_sentence_count_mismatch_returns_none(tmp_path):
    path = str(tmp_path / "timings.json")
    sents = _sents(3)
    _save_timings(path, [(s, i * 2.0, i * 2.0 + 2.0) for i, s in enumerate(sents)])
    assert _load_timings(path, _sents(4)) is None


def test_malformed_file_returns_none(tmp_path):
    path = str(tmp_path / "timings.json")
    with open(path, "w") as f:
        f.write("{not json")
    assert _load_timings(path, _sents(1)) is None


def test_save_failure_is_nonfatal(tmp_path):
    bad_path = os.path.join(str(tmp_path), "no-such-dir", "timings.json")
    assert _save_timings(bad_path, [({"text": "x"}, 0.0, 1.0)]) is False
