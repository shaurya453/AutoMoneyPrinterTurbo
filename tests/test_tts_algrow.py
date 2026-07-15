"""Algrow TTS proxy: minimax provider dispatch, generate/poll client (mocked), voice map."""
import types

import pytest

from app.config import config
from app.services.tts import algrow, minimax


def _resp(payload, status_code=200):
    r = types.SimpleNamespace(status_code=status_code)
    r.json = lambda: payload
    r.raise_for_status = lambda: None
    return r


class _FakeStreamResponse:
    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# minimax_tts() provider dispatch
# ---------------------------------------------------------------------------


def test_minimax_tts_dispatches_to_algrow_by_default(monkeypatch):
    monkeypatch.setitem(config.app, "minimax_provider", "algrow")
    called = {}

    def fake_algrow(text, voice_name, voice_rate, voice_file):
        called["args"] = (text, voice_name, voice_rate, voice_file)
        return "sentinel"

    monkeypatch.setattr(algrow, "algrow_minimax_tts", fake_algrow)
    result = minimax.minimax_tts("hello", "voice-1", 1.0, "/tmp/out.mp3")
    assert result == "sentinel"
    assert called["args"] == ("hello", "voice-1", 1.0, "/tmp/out.mp3")


def test_minimax_tts_direct_provider_skips_algrow(monkeypatch):
    monkeypatch.setitem(config.app, "minimax_provider", "direct")
    monkeypatch.setitem(config.app, "minimax_api_key", "")
    monkeypatch.setitem(config.app, "minimax_group_id", "")
    # No api_key/group_id set -> direct path returns None without touching algrow.
    result = minimax.minimax_tts("hello", "voice-1", 1.0, "/tmp/out.mp3")
    assert result is None


# ---------------------------------------------------------------------------
# _split_for_algrow — merges any trailing chunk under the 200-char minimum
# ---------------------------------------------------------------------------


def test_split_for_algrow_merges_short_trailing_chunk():
    long_sentence = "A" * 190 + "."
    short_sentence = "B" * 50 + "."
    text = f"{long_sentence} {short_sentence}"
    chunks = algrow._split_for_algrow(text, max_chars=200, min_chars=200)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_for_algrow_keeps_chunks_above_minimum():
    chunk_a = "A" * 250 + "."
    chunk_b = "B" * 250 + "."
    text = f"{chunk_a} {chunk_b}"
    chunks = algrow._split_for_algrow(text, max_chars=260, min_chars=200)
    assert len(chunks) == 2


# ---------------------------------------------------------------------------
# _algrow_minimax_tts_single (mocked requests)
# ---------------------------------------------------------------------------


def test_single_completed_immediately_downloads_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(algrow.requests, "post", lambda *a, **k: _resp(
        {"success": True, "status": "completed", "audio_url": "https://audio.algrow.online/x.mp3"}
    ))

    def fake_get(url, **kwargs):
        assert url == "https://audio.algrow.online/x.mp3"
        return _FakeStreamResponse([b"fake-audio-bytes"])

    monkeypatch.setattr(algrow.requests, "get", fake_get)
    out_file = str(tmp_path / "out.mp3")
    ok = algrow._algrow_minimax_tts_single(
        "x" * 200, "voice-1", 1.0, 0, 1.0, out_file, deadline=algrow.time.monotonic() + 60
    )
    assert ok is True
    with open(out_file, "rb") as fh:
        assert fh.read() == b"fake-audio-bytes"


def test_single_failed_immediately_returns_false(monkeypatch):
    monkeypatch.setattr(algrow.requests, "post", lambda *a, **k: _resp(
        {"success": False, "status": "failed", "error": "boom"}
    ))
    ok = algrow._algrow_minimax_tts_single(
        "x" * 200, "voice-1", 1.0, 0, 1.0, "/tmp/unused.mp3", deadline=algrow.time.monotonic() + 60
    )
    assert ok is False


def test_single_pending_polls_then_downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(algrow.requests, "post", lambda *a, **k: _resp(
        {"success": True, "job_id": "job-1", "status": "pending"}
    ))

    def fake_get(url, **kwargs):
        if url.endswith("/api/job-status/job-1"):
            return _resp({"success": True, "status": "completed", "audio_url": "https://audio.algrow.online/y.mp3"})
        return _FakeStreamResponse([b"more-audio-bytes"])

    monkeypatch.setattr(algrow.requests, "get", fake_get)
    monkeypatch.setattr(algrow.time, "sleep", lambda s: None)
    out_file = str(tmp_path / "out.mp3")
    ok = algrow._algrow_minimax_tts_single(
        "x" * 200, "voice-1", 1.0, 0, 1.0, out_file, deadline=algrow.time.monotonic() + 60
    )
    assert ok is True


def test_single_deadline_expiry_returns_false(monkeypatch):
    monkeypatch.setattr(algrow.requests, "post", lambda *a, **k: _resp(
        {"success": True, "job_id": "job-1", "status": "pending"}
    ))
    monkeypatch.setattr(algrow.requests, "get", lambda *a, **k: _resp(
        {"success": True, "status": "processing"}
    ))
    monkeypatch.setattr(algrow.time, "sleep", lambda s: None)
    ok = algrow._algrow_minimax_tts_single(
        "x" * 200, "voice-1", 1.0, 0, 1.0, "/tmp/unused.mp3", deadline=algrow.time.monotonic() - 1
    )
    assert ok is False


def test_single_submit_exception_returns_false(monkeypatch):
    def boom(*a, **k):
        raise algrow.requests.exceptions.ConnectionError("no route")
    monkeypatch.setattr(algrow.requests, "post", boom)
    ok = algrow._algrow_minimax_tts_single(
        "x" * 200, "voice-1", 1.0, 0, 1.0, "/tmp/unused.mp3", deadline=algrow.time.monotonic() + 60
    )
    assert ok is False


# ---------------------------------------------------------------------------
# algrow_minimax_tts — voice map lookup + api key gate
# ---------------------------------------------------------------------------


def test_algrow_minimax_tts_no_api_key_returns_none(monkeypatch):
    monkeypatch.setitem(config.app, "algrow_api_key", "")
    assert algrow.algrow_minimax_tts("x" * 200, "moss_audio_x", 1.0, "/tmp/out.mp3") is None


def test_algrow_minimax_tts_maps_voice_name_to_algrow_voice_id(monkeypatch, tmp_path):
    monkeypatch.setitem(config.app, "algrow_api_key", "algrow_test_key")
    monkeypatch.setitem(config.app, "minimax_algrow_voice_map", {"moss_audio_x": "algrow_cloned_id"})

    captured = {}

    def fake_single(text, voice_id, speed, pitch, volume, out_file, deadline):
        captured["voice_id"] = voice_id
        with open(out_file, "wb") as fh:
            fh.write(b"audio")
        return True

    monkeypatch.setattr(algrow, "_algrow_minimax_tts_single", fake_single)
    out_file = str(tmp_path / "out.mp3")
    result = algrow.algrow_minimax_tts("x" * 200, "moss_audio_x", 1.0, out_file)
    assert result is not None
    assert captured["voice_id"] == "algrow_cloned_id"


def test_algrow_minimax_tts_unmapped_voice_passes_through_with_warning(monkeypatch, tmp_path):
    monkeypatch.setitem(config.app, "algrow_api_key", "algrow_test_key")
    monkeypatch.setitem(config.app, "minimax_algrow_voice_map", {})

    captured = {}

    def fake_single(text, voice_id, speed, pitch, volume, out_file, deadline):
        captured["voice_id"] = voice_id
        with open(out_file, "wb") as fh:
            fh.write(b"audio")
        return True

    monkeypatch.setattr(algrow, "_algrow_minimax_tts_single", fake_single)
    out_file = str(tmp_path / "out.mp3")
    algrow.algrow_minimax_tts("x" * 200, "some_unmapped_voice", 1.0, out_file)
    assert captured["voice_id"] == "some_unmapped_voice"


# ---------------------------------------------------------------------------
# clone_minimax_voice (mocked requests)
# ---------------------------------------------------------------------------


def test_clone_minimax_voice_returns_new_voice_id(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.mp3"
    audio_path.write_bytes(b"fake-mp3-bytes")

    monkeypatch.setattr(algrow.requests, "post", lambda *a, **k: _resp(
        {"success": True, "voice": {"voice_id": "new_id_123", "name": "Test Voice", "is_cloned": True}}
    ))
    voice_id = algrow.clone_minimax_voice("Test Voice", str(audio_path))
    assert voice_id == "new_id_123"


def test_clone_minimax_voice_missing_voice_id_returns_none(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.mp3"
    audio_path.write_bytes(b"fake-mp3-bytes")

    monkeypatch.setattr(algrow.requests, "post", lambda *a, **k: _resp({"success": False}))
    assert algrow.clone_minimax_voice("Test Voice", str(audio_path)) is None


# ---------------------------------------------------------------------------
# algrow_elevenlabs_tts — no clone/map step, voice_name used as voice_id directly
# ---------------------------------------------------------------------------


def test_algrow_elevenlabs_tts_no_api_key_returns_none(monkeypatch):
    monkeypatch.setitem(config.app, "algrow_api_key", "")
    assert algrow.algrow_elevenlabs_tts("x" * 200, "Ix8C14HEHgIQkJswik2o", 1.0, "/tmp/out.mp3") is None


def test_algrow_elevenlabs_tts_uses_voice_name_as_voice_id(monkeypatch, tmp_path):
    monkeypatch.setitem(config.app, "algrow_api_key", "algrow_test_key")

    captured = {}

    def fake_single(text, voice_id, model_id, stability, similarity_boost, speed, out_file, deadline):
        captured["voice_id"] = voice_id
        captured["model_id"] = model_id
        with open(out_file, "wb") as fh:
            fh.write(b"audio")
        return True

    monkeypatch.setattr(algrow, "_algrow_elevenlabs_tts_single", fake_single)
    out_file = str(tmp_path / "out.mp3")
    result = algrow.algrow_elevenlabs_tts("x" * 200, "Ix8C14HEHgIQkJswik2o", 1.0, out_file)
    assert result is not None
    assert captured["voice_id"] == "Ix8C14HEHgIQkJswik2o"
    assert captured["model_id"] == "eleven_multilingual_v2"


def test_single_elevenlabs_completed_immediately_downloads_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(algrow.requests, "post", lambda *a, **k: _resp(
        {"success": True, "status": "completed", "audio_url": "https://audio.algrow.online/e.mp3"}
    ))
    monkeypatch.setattr(algrow.requests, "get", lambda url, **k: _FakeStreamResponse([b"eleven-bytes"]))
    out_file = str(tmp_path / "out.mp3")
    ok = algrow._algrow_elevenlabs_tts_single(
        "x" * 200, "voice-1", "eleven_multilingual_v2", 0.5, 0.5, 1.0, out_file,
        deadline=algrow.time.monotonic() + 60,
    )
    assert ok is True
    with open(out_file, "rb") as fh:
        assert fh.read() == b"eleven-bytes"


def test_single_elevenlabs_failed_immediately_returns_false(monkeypatch):
    monkeypatch.setattr(algrow.requests, "post", lambda *a, **k: _resp(
        {"success": False, "status": "failed", "error": "boom"}
    ))
    ok = algrow._algrow_elevenlabs_tts_single(
        "x" * 200, "voice-1", "eleven_multilingual_v2", 0.5, 0.5, 1.0, "/tmp/unused.mp3",
        deadline=algrow.time.monotonic() + 60,
    )
    assert ok is False


def test_dispatches_elevenlabs_prefix_to_algrow_elevenlabs_tts(monkeypatch):
    from app.services import tts as tts_module

    called = {}

    def fake_elevenlabs(text, voice_name, voice_rate, voice_file):
        called["args"] = (text, voice_name, voice_rate, voice_file)
        return "sentinel"

    monkeypatch.setattr(tts_module, "algrow_elevenlabs_tts", fake_elevenlabs)
    result = tts_module.tts("hello", "elevenlabs:Ix8C14HEHgIQkJswik2o", 1.0, "/tmp/out.mp3")
    assert result == "sentinel"
    assert called["args"] == ("hello", "Ix8C14HEHgIQkJswik2o", 1.0, "/tmp/out.mp3")
