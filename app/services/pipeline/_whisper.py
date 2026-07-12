"""Whisper sentence-timestamp alignment helpers."""
import concurrent.futures
import json
import os
import re
from difflib import SequenceMatcher
from typing import List, Optional, Tuple

from loguru import logger

from app.config import config


def _norm_token(w: str) -> str:
    return re.sub(r"[^\w]", "", w).lower()


def _get_sentence_timestamps(
    audio_file: str, sentences: list
) -> Tuple[List[Tuple[dict, float, float]], List[Tuple[str, float, float]]]:
    """
    Transcribe audio with faster-whisper and align each sentence to a
    (start_sec, end_sec) span using SequenceMatcher token alignment.

    Unlike word-count advancement, SequenceMatcher handles insertions and
    deletions without accumulating drift across 200+ sentences.

    Returns (sentence_timings, word_timings) where word_timings is the raw
    list of (word, start_sec, end_sec) tuples from Whisper, used to drive
    subtitle word-highlight animation.
    """
    from faster_whisper import WhisperModel

    model_size = config.whisper.get("model_size", "base")
    device = config.whisper.get("device", "cpu")
    compute_type = config.whisper.get("compute_type", "int8")
    timeout_seconds = float(config.whisper.get("timeout_seconds", 1800))

    logger.info(f"loading whisper model: {model_size} on {device}")
    model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def _transcribe() -> List[Tuple[str, float, float]]:
        segments, _ = model.transcribe(audio_file, word_timestamps=True)
        words: List[Tuple[str, float, float]] = []
        for seg in segments:
            if seg.words:
                for w in seg.words:
                    word = w.word.strip()
                    if word:
                        words.append((word, w.start, w.end))
        return words

    # faster-whisper has no native timeout; run it on a worker thread so a
    # pathological/huge audio file can't hang the pipeline forever. On
    # timeout the thread is left to finish in the background (daemon-ish via
    # shutdown(wait=False)) while we fall back to uniform timestamps.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_transcribe)
    try:
        all_words = future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError:
        raise TimeoutError(f"whisper transcription exceeded {timeout_seconds:g}s")
    finally:
        executor.shutdown(wait=False)

    logger.info(f"whisper found {len(all_words)} words across {len(sentences)} sentences")

    if not all_words:
        # Fail loudly: returning uniform timings over 0.0s here would give the
        # caller all-zero spans and a degenerate clip plan. The caller
        # (_orchestrate.start) catches this and falls back to
        # _uniform_timestamps with the REAL audio duration.
        raise RuntimeError("whisper transcription produced zero words")

    # Build expected token sequence: (normalized_token, sentence_idx)
    expected_tokens: List[tuple] = []
    for s_idx, sent in enumerate(sentences):
        for tok in sent["text"].split():
            n = _norm_token(tok)
            if n:
                expected_tokens.append((n, s_idx))

    expected_norm = [t[0] for t in expected_tokens]
    whisper_norm = [_norm_token(w[0]) for w in all_words]

    # Global sequence alignment — no drift, handles insertions/deletions
    matcher = SequenceMatcher(None, expected_norm, whisper_norm, autojunk=False)

    exp_to_whisper: List[Optional[int]] = [None] * len(expected_tokens)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            exp_to_whisper[a + k] = b + k

    # Fill forward: unmatched expected tokens inherit the previous whisper index
    last_w = 0
    for i in range(len(exp_to_whisper)):
        if exp_to_whisper[i] is not None:
            last_w = exp_to_whisper[i]
        else:
            exp_to_whisper[i] = last_w

    # Build per-sentence time spans
    sentence_spans: dict = {}
    for exp_idx, (_, s_idx) in enumerate(expected_tokens):
        w_idx = exp_to_whisper[exp_idx]
        if w_idx is None or w_idx >= len(all_words):
            continue
        _, w_start, w_end = all_words[w_idx]
        if s_idx not in sentence_spans:
            sentence_spans[s_idx] = [w_start, w_end]
        else:
            sentence_spans[s_idx][1] = w_end

    # Sentences whose tokens never matched (e.g. heavy transcription mangling)
    # are placed as zero-duration spans at the current timeline cursor — i.e.
    # exactly where they belong between their neighbours. The old fallback
    # parked them at the END of the audio, which made the planner's monotonic
    # cursor jump to the audio end and collapse every later sentence's slot.
    results: List[Tuple[dict, float, float]] = []
    cursor = 0.0
    unmatched = 0
    for s_idx, sent in enumerate(sentences):
        if s_idx in sentence_spans:
            start, end = sentence_spans[s_idx]
            end = max(end, start)
            results.append((sent, start, end))
            cursor = end
        else:
            unmatched += 1
            results.append((sent, cursor, cursor))
    if unmatched:
        logger.warning(
            f"whisper alignment: {unmatched} sentence(s) had no matching tokens — "
            "placed as zero-duration spans at their timeline position"
        )

    return results, all_words


def _uniform_timestamps(
    sentences: list, audio_duration: float
) -> List[Tuple[dict, float, float]]:
    """Fallback: divide audio duration evenly across all sentences."""
    per_sent = audio_duration / max(len(sentences), 1)
    return [
        (s, i * per_sent, (i + 1) * per_sent)
        for i, s in enumerate(sentences)
    ]


def _save_timings(path: str, timings: List[Tuple[dict, float, float]]) -> bool:
    """Persist per-sentence [t_start, t_end] whisper spans as JSON.

    Lets SKIP_WHISPER=1 re-runs of the same task reuse real alignment instead
    of falling back to uniform text-length timestamps (measured 3–50s off on a
    real job — any A/V-sync evaluation on such a re-run is meaningless).
    Non-fatal: returns False on any write error.
    """
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump([[t_start, t_end] for _, t_start, t_end in timings], f)
        return True
    except Exception as exc:
        logger.warning(f"could not persist whisper timings to {path}: {exc}")
        return False


def _load_timings(
    path: str, sentences: list
) -> Optional[List[Tuple[dict, float, float]]]:
    """Load spans persisted by _save_timings and re-attach the sentence dicts.

    Returns None (caller falls back to uniform) when the file is missing,
    malformed, or its span count doesn't match the current sentence list —
    a count mismatch means the job.json was re-enriched/split since the
    timings were captured, so they no longer line up.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            spans = json.load(f)
        if len(spans) != len(sentences):
            logger.warning(
                f"persisted timings at {path} cover {len(spans)} sentences, "
                f"job has {len(sentences)} — ignoring them"
            )
            return None
        return [
            (sent, float(start), float(end))
            for sent, (start, end) in zip(sentences, spans)
        ]
    except Exception as exc:
        logger.warning(f"could not load persisted whisper timings from {path}: {exc}")
        return None
