"""Regression tests for the C1/C2 lazy-load races.

The NSFW gate and CLIP relevance model load lazily under concurrent fetch
workers. A past bug released the load lock before constructing the model, so
threads arriving mid-load saw "attempted" and silently failed open. These
tests hammer the loaders from 8 threads (barrier-synchronized) and assert the
model is constructed exactly once and every thread sees the same instance.
"""
import threading
import time


def _hammer(loader, n_threads=8):
    barrier = threading.Barrier(n_threads)
    results = [None] * n_threads

    def worker(i):
        barrier.wait()
        results[i] = loader()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_nsfw_detector_loads_once_under_concurrency(monkeypatch):
    import sys
    import types

    from app.services.scoring import nsfw

    construct_count = [0]

    class FakeDetector:
        def __init__(self):
            construct_count[0] += 1
            time.sleep(0.2)  # widen the race window

    fake_module = types.ModuleType("nudenet")
    fake_module.NudeDetector = FakeDetector
    monkeypatch.setitem(sys.modules, "nudenet", fake_module)
    monkeypatch.setattr(nsfw, "_detector", None)
    monkeypatch.setattr(nsfw, "_detector_load_attempted", False)

    results = _hammer(nsfw._get_detector)

    assert construct_count[0] == 1, "detector constructed more than once"
    assert all(r is results[0] for r in results), "threads saw different instances"
    assert results[0] is not None, "no thread may fail open during the load"


def test_relevance_model_loads_once_under_concurrency(monkeypatch):
    """Fakes the ONNX/tokenizer modules so the real _get_model locking runs a
    slow-but-successful load. Under the old released-lock-before-load bug,
    threads arriving mid-load returned None (fail-open) while the loader
    thread got the real model — the same-instance assertion catches that.
    """
    import sys
    import types

    from app.services.scoring import relevance

    ensure_count = [0]

    def fake_ensure_model_files():
        ensure_count[0] += 1
        time.sleep(0.2)  # widen the race window
        return "/nonexistent-model-dir"

    class FakeSession:
        def __init__(self, path, providers=None):
            pass

    class FakeTokenizer:
        @staticmethod
        def from_file(path):
            tok = FakeTokenizer()
            return tok

        def enable_padding(self, **kw):
            pass

        def enable_truncation(self, **kw):
            pass

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.InferenceSession = FakeSession
    fake_tokenizers = types.ModuleType("tokenizers")
    fake_tokenizers.Tokenizer = FakeTokenizer

    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "tokenizers", fake_tokenizers)
    monkeypatch.setattr(relevance, "_ensure_model_files", fake_ensure_model_files)
    monkeypatch.setattr(relevance, "_model", None)
    monkeypatch.setattr(relevance, "_model_load_attempted", False)

    results = _hammer(relevance._get_model)

    assert ensure_count[0] == 1, "model files fetched more than once"
    assert results[0] is not None, "load should have succeeded with fakes"
    assert all(r is results[0] for r in results), (
        "threads saw different results — a thread failed open mid-load"
    )
