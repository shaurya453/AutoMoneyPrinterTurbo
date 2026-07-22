"""Pipeline orchestrator — the main start() entry point."""
import hashlib
import json
import math
import os
import random
import subprocess
import time
from collections import Counter as _Counter, deque as _deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional, Tuple

from loguru import logger

from app.config import config
from app.models.schema import (
    VideoConcatMode,
    VideoAspect,
    VideoParams,
    VideoTransitionMode,
)
from app.services import avatar
from app.services import media as material
from app.services import render as video
from app.services import tts as voice
from app.services.scoring import relevance, vlm
from app.services.pipeline._whisper import (
    _get_sentence_timestamps,
    _load_timings,
    _save_timings,
    _uniform_timestamps,
)
from app.services.pipeline._planning import (
    _trim_clip, _get_visual_concepts, _build_query_ladder, _TRIM_BUFFER,
    plan_clip_slots, plan_avatar_blocks, _CLIP_TARGET, _MIN_ANIM_DUR, _OUTRO_TAIL,
    _COMPOSITE_GRAPHIC_TYPES,
)
from app.services.pipeline._fetch import (
    _fetch_clip,
    _ThreadSafeURLSet,
    criticality_budget_multiplier,
)
from app.services.pipeline._quality import write_quality_report
from app.utils import subtitle, utils


def _resolve_bgm(job: dict) -> Tuple[str, str]:
    """
    Resolve BGM source for this job and return (bgm_type, bgm_file) for VideoParams.

    Priority:
      1. bgm_file = "none" / "" — no BGM (hard override, checked before bgm_search_term)
      2. bgm_search_term  — search Pixabay online and download; falls back to random local
      3. bgm_file = "random" — pick a random file from resource/songs/
      4. bgm_file = "/path/or/name" — explicit local file
    """
    raw = job.get("bgm_file", "random")
    if not raw or raw.lower() in ("", "none"):
        return "", ""

    search_term = job.get("bgm_search_term", "").strip()
    if search_term:
        logger.info(f"searching for BGM online: '{search_term}'")
        downloaded = material.download_bgm(
            search_term=search_term,
            save_dir=utils.storage_dir("cache_bgm"),
        )
        if downloaded:
            return "downloaded", downloaded
        logger.warning("online BGM search failed — falling back to random local file")
        return "random", ""

    if raw == "random":
        return "random", ""
    return "", raw  # explicit local path or filename


def _timings_to_srt(timings: list, subtitle_path: str) -> None:
    """Write sentence-level Whisper-aligned timestamps directly as an SRT file."""
    lines = []
    idx = 1
    for sent, start, end in timings:
        text = sent.get("text", "").strip()
        if not text:
            continue
        lines.append(utils.text_to_srt(idx, text, start, end))
        idx += 1
    content = "\n".join(lines)
    if content:
        with open(subtitle_path, "w", encoding="utf-8") as f:
            f.write(content + "\n")


# A clip tail whose mean luma is below this is treated as (near-)black —
# a baked Ken Burns fade-out or a black placeholder.
_DARK_TAIL_LUMA = 40.0


def _tail_mean_luma(clip_path: str, tail_seconds: float = 0.1) -> Optional[float]:
    """Mean Y (luma, 0–255) over the last `tail_seconds` of a clip.

    The window is deliberately short (~3 frames): a baked fade-out only
    reaches its darkest at the very end, and averaging over the whole fade
    ramp would sit above any usable threshold.

    Returns None when probing fails (caller should fail open to existing
    behavior). One ffmpeg pass over a fraction of a second of video.
    """
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nk=1:nw=1", clip_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        dur = float(probe.stdout.strip())
        start = max(0.0, dur - tail_seconds)
        r = subprocess.run(
            [
                utils.get_ffmpeg_binary(), "-v", "error",
                "-ss", f"{start:.3f}", "-i", clip_path,
                "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=60,
        )
        vals = [
            float(line.rsplit("=", 1)[1])
            for line in r.stdout.splitlines()
            if "YAVG" in line
        ]
        return sum(vals) / len(vals) if vals else None
    except Exception:
        return None


def start(job_path: str) -> Optional[dict]:
    """
    Run the full sentence-level documentary pipeline from a job JSON file.
    Returns a result dict with output paths on success, None on failure.
    """
    # Published avatar assets (audio slices + image copy) are world-readable
    # while the job runs — remove them on EVERY exit path, including raises.
    _avatar_state: dict = {"dir": None}
    try:
        return _start_impl(job_path, _avatar_state)
    finally:
        avatar.cleanup_assets(_avatar_state["dir"])


def _start_impl(job_path: str, _avatar_state: dict) -> Optional[dict]:
    with open(job_path, "r", encoding="utf-8") as fh:
        job = json.load(fh)

    vlm.reset_usage()
    # LRU-prune the download caches before any fetch — they otherwise grow
    # without bound (see cache_max_age_days / cache_max_total_gb in config).
    try:
        material.prune_cache_dirs()
    except Exception as exc:
        logger.warning(f"cache prune failed (continuing): {exc}")

    task_id = job.get("task_id") or utils.get_uuid()
    sentences: list = job.get("sentences", [])
    video_script: str = job.get("video_script", "").strip()
    video_topic: str = job.get("video_topic", "") or job.get("video_title", "")
    video_type: str = job.get("video_type", "thematic")

    # Deterministic per-job RNG: Ken Burns animations, graphic variants,
    # transitions, and BGM picks are reproducible across reruns of the same
    # job.json (same job_path + same script content -> same seed), while a
    # genuinely different job still gets its own independent sequence.
    # Falls back to the global `random` module (unseeded) wherever a
    # function's `rng` parameter isn't threaded through yet.
    _seed_material = f"{job_path}:{video_script}".encode("utf-8")
    job_seed = int(hashlib.sha256(_seed_material).hexdigest(), 16) & 0xFFFFFFFF
    job_rng = random.Random(job_seed)

    dedup_enabled: bool = bool(config.app.get("dedup_enabled", True))
    dedup_threshold: float = float(config.app.get("dedup_similarity_threshold", 0.92))
    dedup_lookback: int = int(config.app.get("dedup_lookback_window", 8))
    # Thread-safe: shared by every ThreadPoolExecutor worker in the main
    # clip-fetch pool below, not just the sequential gap-fill pass.
    # -1 (recommended) => unbounded, i.e. remembers every accepted clip for
    # the whole job. A bounded window forgets old entries once that many
    # newer clips are accepted, letting the same stock footage resurface
    # later in a long video once it ages out of the window.
    recent_embs: Optional[Any] = (
        relevance.ThreadSafeEmbeddingWindow(maxlen=None if dedup_lookback < 0 else dedup_lookback)
        if dedup_enabled else None
    )
    clip_budget_seconds: float = float(config.app.get("clip_fetch_budget_seconds", 180))
    rescue_budget_seconds: float = float(config.app.get("clip_rescue_budget_seconds", 25))
    rescue_max_attempts: int = int(config.app.get("clip_rescue_max_attempts", 3))

    if not video_script:
        logger.error("job.video_script is empty — nothing to do")
        return None
    if not sentences:
        logger.error("job.sentences is empty — run sentence_prep.py first")
        return None

    work_dir = os.path.dirname(os.path.abspath(job_path))
    os.makedirs(work_dir, exist_ok=True)

    # Intermediate files go here; final outputs stay in work_dir.
    temp_dir = os.path.join(work_dir, "temp")
    os.makedirs(temp_dir, exist_ok=True)

    logger.info(f"pipeline start | task={task_id} | folder={os.path.basename(work_dir)} | sentences={len(sentences)}")

    # ------------------------------------------------------------------ #
    # 1. TTS                                                               #
    # ------------------------------------------------------------------ #
    audio_file = os.path.join(work_dir, "audio.mp3")
    voice_name = voice.parse_voice_name(job.get("voice_name", "en-US-AriaNeural"))
    voice_rate = float(job.get("voice_rate", 1.0))

    # Allow reuse of an existing narration file to avoid re-running TTS when retrying.
    sub_maker = None
    if os.path.exists(audio_file):
        logger.info(f"reusing existing audio → {audio_file}")
        audio_duration = math.ceil(voice.get_audio_duration(audio_file))
    else:
        logger.info(f"TTS: voice={voice_name}, rate={voice_rate}")
        sub_maker = voice.tts(
            text=video_script,
            voice_name=voice_name,
            voice_rate=voice_rate,
            voice_file=audio_file,
        )
        if sub_maker is None:
            logger.error("TTS failed — check voice name and network connectivity")
            return None
        audio_duration = math.ceil(voice.get_audio_duration(sub_maker))
    logger.info(f"audio duration: {audio_duration}s")

    # ------------------------------------------------------------------ #
    # 1b. Normalize narration loudness                                    #
    # ------------------------------------------------------------------ #
    # TTS providers vary wildly in output level (some near-silent, some
    # clipping) — normalize before whisper alignment, since both extremes
    # hurt its word-level accuracy. Runs on every job (reused or freshly
    # generated audio) since it's cheap and safe to redo. Non-fatal: a
    # failed pass just leaves the original audio.mp3 in place.
    if not voice.normalize_narration_loudness(audio_file):
        logger.warning("narration loudness normalization failed — continuing with original audio")

    # ------------------------------------------------------------------ #
    # 2. Sentence timestamps via faster-whisper                           #
    # ------------------------------------------------------------------ #
    logger.info("aligning sentences with faster-whisper")

    word_timings: List[Tuple[str, float, float]] = []
    timings_path = os.path.join(work_dir, "timings.json")
    if os.environ.get("SKIP_WHISPER") == "1":
        # Prefer timings persisted by a previous full run of this task —
        # uniform text-length distribution is off by whole seconds against
        # real speech, which desyncs every visual on a re-render.
        timings = _load_timings(timings_path, sentences)
        if timings:
            logger.info("SKIP_WHISPER=1 → reusing persisted whisper timings (timings.json)")
        else:
            logger.info("SKIP_WHISPER=1 → using uniform distribution for timings")
            timings = _uniform_timestamps(sentences, audio_duration)
    else:
        try:
            timings, word_timings = _get_sentence_timestamps(audio_file, sentences)
            _save_timings(timings_path, timings)
        except Exception as exc:
            logger.warning(
                f"whisper failed ({exc}), falling back to uniform distribution "
                f"— word-level subtitle highlights will be disabled"
            )
            timings = _uniform_timestamps(sentences, audio_duration)

    # ------------------------------------------------------------------ #
    # 3. Per-sentence clip download + trim                                #
    # ------------------------------------------------------------------ #
    video_aspect = VideoAspect(job.get("video_aspect", "16:9"))
    video_source: str = job.get("video_source", "pexels")
    clips_dir = os.path.join(temp_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    ordered_clips: List[str] = []
    planned_clip_durations: List[float] = []  # parallel to ordered_clips; frame-snap target for combine_videos
    used_urls = _ThreadSafeURLSet()  # tracks clip URLs used this run to prevent reuse
    total_sentences = len(timings)

    # ---- Pass 1: plan per-sentence clip durations using absolute resync.
    # (See plan_clip_slots() in _planning.py for the full slot math.)
    clip_plans = plan_clip_slots(timings, audio_duration)

    # Crossfade trim-buffer padding is only consumed when combine_videos will
    # actually run ffmpeg xfade (see video.XFADE_CLIP_LIMIT) — otherwise it
    # would just inflate the final video's duration beyond the narration.
    # Avatar blocks (below) collapse member slots into one clip each, so this
    # slightly overcounts for avatar jobs — only making the xfade-vs-concat
    # decision more conservative, which is fine.
    total_planned_clips = sum(len(p["durations"]) for p in clip_plans)
    apply_trim_buffer = total_planned_clips <= video.XFADE_CLIP_LIMIT
    trim_buffer = _TRIM_BUFFER if apply_trim_buffer else 0.0
    logger.info(
        f"planned {total_planned_clips} clips total "
        f"({'with' if apply_trim_buffer else 'without'} crossfade trim buffer; "
        f"xfade limit={video.XFADE_CLIP_LIMIT})"
    )

    # ---- AI avatar blocks: contiguous avatar-marked sentences (portal's
    # graphics audit sets sent["avatar"]) become ONE externally generated
    # talking-head clip each, lip-synced to the block's slice of the master
    # narration (see app/services/avatar.py). Unconfigured/missing pieces
    # degrade the job to normal footage rather than failing it.
    avatar_blocks: List[dict] = []
    _avatar_image_path = str(job.get("avatar_image") or "")
    _avatar_image_url: Optional[str] = None
    _avatar_local_dir: Optional[str] = None
    _avatar_url_prefix: Optional[str] = None
    _avatar_requested = bool(_avatar_image_path) and any(s.get("avatar") for s in sentences)
    if _avatar_requested and not avatar.is_enabled():
        logger.warning(
            "job requests an avatar but avatar_enabled/the active provider's "
            "API key are not configured — demoting avatar sentences to normal footage"
        )
        for s in sentences:
            s.pop("avatar", None)
    elif _avatar_requested and not os.path.isfile(_avatar_image_path):
        logger.warning(
            f"avatar_image not found on disk ({_avatar_image_path}) — "
            "demoting avatar sentences to normal footage"
        )
        for s in sentences:
            s.pop("avatar", None)
    elif _avatar_requested:
        avatar_blocks, _ = plan_avatar_blocks(clip_plans)
        if avatar_blocks and not avatar.dry_run_enabled():
            _avatar_local_dir, _avatar_url_prefix = avatar.publish_dir(task_id)
            _avatar_state["dir"] = _avatar_local_dir
            if _avatar_local_dir:
                _ext = os.path.splitext(_avatar_image_path)[1] or ".png"
                _avatar_image_url = avatar.publish_asset(
                    _avatar_image_path, _avatar_local_dir, _avatar_url_prefix, f"avatar{_ext}"
                )
            if not _avatar_image_url:
                logger.warning("avatar: asset publish failed — demoting to normal footage")
                avatar_blocks = []
                for s in sentences:
                    s.pop("avatar", None)
        if avatar_blocks:
            logger.info(
                "avatar blocks: " + "; ".join(
                    f"sentences {b['member_idxs'][0]}–{b['member_idxs'][-1]} "
                    f"({b['planned_duration']:.1f}s from {b['slot_start']:.1f}s)"
                    for b in avatar_blocks
                )
            )
    _avatar_first = {b["first_idx"]: b for b in avatar_blocks}
    _avatar_members = {i for b in avatar_blocks for i in b["member_idxs"]}

    # Topic-wide pool of visual concepts (deduped, order-preserving), used as
    # a last-resort fallback so a sentence whose own concepts are exhausted
    # can still pull *different* footage instead of leaving a gap that
    # combine_videos would later fill by repeating clips.
    _all_concepts: List[str] = []
    _seen_concepts: set = set()
    for plan in clip_plans:
        for concept in _get_visual_concepts(plan["sent"]):
            if concept not in _seen_concepts:
                _seen_concepts.add(concept)
                _all_concepts.append(concept)

    # ---- Pass 2: fetch clips in parallel according to the plan ----
    # Soft cap on the fraction of clips that may come from still images.
    # In parallel mode this is best-effort — counts lag while futures are in-flight.
    max_image_ratio = float(job.get("max_image_ratio", config.app.get("max_image_ratio", 1.0)))
    image_clip_count = 0
    video_clip_count = 0

    # Concept-frequency guard: when the enrichment agent mode-collapses and
    # assigns the same visual_concepts to many sentences, the same search terms
    # exhaust their candidate pools quickly and produce visually monotone clips.
    concept_usage: _Counter = _Counter()
    max_concept_reuse = int(config.app.get("max_concept_reuse", 2))

    clip_counter = 0  # unique index for clip filenames across all sentences
    obtained_duration = 0.0
    _seen_lower_third_labels: set = set()
    # Planned submission counts — updated at submission time so the image-ratio cap
    # has real numbers during Phase A (Phase B counters lag until futures complete).
    _planned_image_count = 0
    _planned_video_count = 0

    # Fetch is network/API bound (search + download + VLM round-trips), not
    # CPU bound — 6 workers measurably shortens the fetch phase vs 4 without
    # hitting provider rate limits.
    max_fetch_workers = int(config.app.get("clip_fetch_workers", 6))

    def _fetch_clip_with_budget(budget_seconds: float, **kw):
        """Set deadline at execution time (inside worker thread), not at submission time."""
        kw["deadline"] = time.monotonic() + budget_seconds
        return _fetch_clip(**kw)

    # _task_queue: ordered list of (future | None, metadata_dict).
    # None future = standalone graphic, already rendered, no IO needed.
    _task_queue: List[tuple] = []
    # Populated in-place by _fetch_video_clip/_fetch_image_clip (side channel,
    # see their docstrings); aggregated into <title>.quality.json at the end
    # of this function. Different clip_idx keys per thread, so no lock needed.
    quality_report: dict = {}

    # Generic, job-vocabulary-independent terms -- used both as a last-resort
    # gap-fill source (below) and as the Phase B rescue fetch for a slot whose
    # primary fetch exhausted the job's own concept pool. Different from what
    # already failed, so we get fresh URLs instead of re-hitting the same
    # rejected/consumed candidates. Sourced from the enrichment agent's
    # per-job gapfill_terms (summarized from the whole script/topic, same
    # convention as motif_palette -- see AGENT_GUIDE.md) so filler footage
    # stays loosely on-theme instead of pulling from a single global list
    # shared by every video regardless of subject. Falls back to a generic
    # hardcoded safety net only when the agent didn't populate the field
    # (old job.json) or gave too few usable entries.
    _HARDCODED_GAPFILL_SAFETY_NET = [
        "retail store interior", "city street pedestrians", "office workers meeting",
        "nature landscape aerial", "documentary interview setting", "warehouse logistics",
        "business presentation", "urban architecture", "market stall vendor",
        "factory production line",
    ]
    _job_gapfill_terms = [
        t for t in (job.get("gapfill_terms") or []) if isinstance(t, str) and t.strip()
    ]
    if len(_job_gapfill_terms) < 3:
        logger.warning(
            f"job.gapfill_terms has only {len(_job_gapfill_terms)} usable entries "
            "(need >= 3) — falling back to generic hardcoded gap-fill terms. "
            "This job.json may predate the per-job gap-fill feature, or the "
            "enrichment agent didn't populate it."
        )
        _job_gapfill_terms = _HARDCODED_GAPFILL_SAFETY_NET
    _rescue_idx = 0

    # entity_name -> deduped, ordered list of every visual_concepts[0] seen
    # across sentences sharing that entity_name. AGENT_GUIDE mandates
    # visual_concepts[0] rotate within a named section (product, a feature,
    # the brand, a comparison product, ...), so a sentence's OWN concepts
    # aren't the only Serper-searchable terms for its product -- its section
    # siblings already have other real, on-product terms. When any named-
    # section sentence's primary fetch fails, these "cousin" terms are tried
    # before the fully generic _job_gapfill_terms, so a rescue fetch is far
    # less likely to land on a visually unrelated generic image under
    # product narration (or, worst case, a specific product-name label).
    _entity_concepts_map: dict = {}
    for _s in job.get("sentences", []):
        _ename = (_s.get("entity_name") or "").strip()
        if not _ename:
            continue
        _c0 = (_s.get("visual_concepts") or [None])[0]
        if not _c0:
            continue
        _bucket = _entity_concepts_map.setdefault(_ename, [])
        if _c0 not in _bucket:
            _bucket.append(_c0)

    # Last few successfully-fetched real (non-placeholder) clips, most recent
    # last. A single placeholder anchors on the OLDEST of these (the newest
    # is its immediate timeline neighbor — anchoring there produced a
    # side-by-side duplicate pair in a real job); runs of placeholders cycle
    # through the pool so consecutive failures don't loop the same footage
    # (see the placeholder branch below).
    _recent_real_clips: "_deque[str]" = _deque(maxlen=3)
    # clip_path -> source image_path, populated only for image-sourced (Ken
    # Burns) clips. Lets the placeholder branch re-render the same still at a
    # longer duration instead of looping the finished clip -- see below.
    _recent_real_image_sources: dict = {}
    _placeholder_run_len = 0
    # (timeline_position, duration_owed, clip_idx) for slots where BOTH the
    # real fetch AND every placeholder attempt failed -- a true hole in
    # ordered_clips. Left unfilled, everything after this position in the
    # final mux is offset earlier than the VO describing it. Filled
    # positionally after Phase B (see below) rather than by the tail-only
    # gap-fill pass, which only corrects the aggregate total duration, not
    # where the hole is.
    _catastrophic_gaps: List[Tuple[int, float, int]] = []

    _fetch_t0 = time.monotonic()

    with ThreadPoolExecutor(max_workers=max_fetch_workers) as executor:

        # ---- Phase A: render graphics sequentially + submit footage fetch tasks ----
        for idx, plan in enumerate(clip_plans):
            sent = plan["sent"]
            durations = plan["durations"]
            is_image = plan["is_image"]
            preview = (sent.get("text") or sent.get("graphic_type", "graphic"))[:60]
            logger.info(
                f"[{idx+1}/{total_sentences}] {plan['sent_audio_dur']:.2f}s audio → "
                f"{len(durations)} clip(s) {'(image)' if is_image else ''} — {preview}"
            )

            # Pending composite-over-footage graphic clip for this sentence
            # (lower_third or info_callout — see _COMPOSITE_GRAPHIC_TYPES).
            # Stored in every clip's metadata for the sentence so it can fall
            # through to the second clip if the first fetch fails.
            _pending_overlay_gfx: Optional[str] = None

            # AI avatar block — the block's FIRST sentence submits one
            # generation covering every member slot; the rest are skipped.
            # Submitting here (the intro block is first) lets the minutes-long
            # RunPod call overlap all the ordinary footage fetches.
            if idx in _avatar_members:
                if idx not in _avatar_first:
                    continue  # slot covered by the block's single clip
                _blk = _avatar_first[idx]
                _av_w, _av_h = video_aspect.to_resolution()
                future = executor.submit(
                    avatar.generate_avatar_clip,
                    image_path=_avatar_image_path,
                    audio_file=audio_file,
                    # Lip-sync to the block's slice of the master narration;
                    # the trim_buffer tail is frame-cloned in normalization,
                    # not narrated, so it stays out of the audio slice.
                    t_start=_blk["slot_start"],
                    t_end=_blk["slot_start"] + _blk["planned_duration"],
                    planned_duration=_blk["planned_duration"] + trim_buffer,
                    clips_dir=clips_dir,
                    clip_idx=clip_counter,
                    width=_av_w,
                    height=_av_h,
                    image_url=_avatar_image_url,
                    local_dir=_avatar_local_dir,
                    url_prefix=_avatar_url_prefix,
                    report=quality_report,
                )
                _task_queue.append((future, {
                    "type": "avatar",
                    "clip_idx": clip_counter,
                    "sent": sent,
                    "clip_duration": _blk["planned_duration"],
                    "is_image": False,
                    "visual_effect": "",
                    "overlay_gfx_clip": None,
                    "idx": idx,
                    "member_idxs": _blk["member_idxs"],
                }))
                clip_counter += 1
                _planned_video_count += 1
                continue

            # Narrated graphic — sentence has real VO text AND graphic_type set.
            # Revideo renders are synchronous subprocesses; keep them sequential.
            if sent.get("graphic_type") and sent.get("text"):
                from app.utils import graphics as _graphics
                whisper_dur = sum(durations)

                _GRAPHIC_MIN_DUR = 5.0  # list/infographic must be on-screen ≥5 s to be readable
                if sent.get("graphic_type") == "list":
                    n_items = len(sent.get("variables", {}).get("items", []))
                    _list_anim_in = 0.80 + n_items * 0.30
                    _trim_target  = max(whisper_dur, _list_anim_in + 0.50, _GRAPHIC_MIN_DUR)
                    render_dur    = _trim_target + 0.40
                elif sent.get("graphic_type") == "infographic":
                    _trim_target = max(whisper_dur, _GRAPHIC_MIN_DUR)
                    render_dur   = max(_trim_target, _MIN_ANIM_DUR)
                else:
                    # lower_third, info_callout — composite-over-footage types use
                    # whisper-driven duration, not a hardcoded floor like the
                    # footage-replacing types above (they're sized by narration
                    # length; their own reveal animations self-scale to fit).
                    _trim_target = whisper_dur
                    render_dur   = max(whisper_dur, _MIN_ANIM_DUR)

                gfx_path = os.path.join(clips_dir, f"clip-{clip_counter:04d}.mp4")
                clip_counter += 1
                w, h = video_aspect.to_resolution()
                rendered = _graphics.render_graphic_clip(
                    graphic_type=sent["graphic_type"],
                    out_path=gfx_path,
                    duration=render_dur,
                    width=w,
                    height=h,
                    fps=30,
                    variables=sent.get("variables", {}),
                    style=sent.get("variables", {}).get("style"),
                    rng=job_rng,
                )
                if rendered and render_dur > _trim_target:
                    trim_path = gfx_path.replace('.mp4', '-t.mp4')
                    if _trim_clip(rendered, _trim_target, trim_path):
                        os.replace(trim_path, gfx_path)
                    else:
                        logger.warning(
                            f"sentence {idx+1}: anim trim failed — using full {render_dur:.2f}s clip"
                        )
                if rendered:
                    gtype = sent.get("graphic_type")
                    if gtype in _COMPOSITE_GRAPHIC_TYPES:
                        if gtype == "lower_third":
                            _lt_label = str(sent.get("variables", {}).get("label", "")).strip().lower()
                            if _lt_label and _lt_label in _seen_lower_third_labels:
                                logger.info(
                                    f"sentence {idx+1}: lower_third label '{_lt_label}' already shown — skipping"
                                )
                                rendered = None  # discard; fall through to plain footage fetch
                            else:
                                if _lt_label:
                                    _seen_lower_third_labels.add(_lt_label)
                                _pending_overlay_gfx = rendered
                        else:
                            # info_callout has no single "label" to dedupe on —
                            # each occurrence is already a fresh, sentence-specific
                            # LLM judgment call, rate-limited by its own cap in
                            # validate_job.py instead.
                            _pending_overlay_gfx = rendered
                    else:
                        # Standalone graphic — enqueue directly; no fetch needed.
                        _task_queue.append((None, {
                            "type": "standalone_graphic",
                            "clip_path": rendered,
                            "clip_duration": _trim_target,
                            "idx": idx,
                        }))
                        continue  # skip footage fetch for this sentence
                else:
                    logger.warning(
                        f"sentence {idx+1}: narrated graphic render failed — falling back to footage"
                    )
                    # Fall through to footage fetch

            # Concept-frequency guard
            own_concepts = _get_visual_concepts(sent)
            if (own_concepts
                    and sent.get("content_track", "broll") != "named"
                    and all(concept_usage[c] >= max_concept_reuse for c in own_concepts)):
                fresh = [c for c in _all_concepts if concept_usage[c] < max_concept_reuse]
                if fresh:
                    sent = {**sent, "visual_concepts": fresh[:2]}
                    logger.info(
                        f"clip {clip_counter}: concepts {own_concepts} stale "
                        f"(used >={max_concept_reuse}x) — substituting {fresh[:2]}"
                    )
            for c in _get_visual_concepts(sent):
                concept_usage[c] += 1

            for clip_duration in durations:
                is_image_override = None
                if is_image and sent.get("content_track", "broll") != "named":
                    _total_planned = _planned_image_count + _planned_video_count
                    _projected = (_planned_image_count + 1) / (_total_planned + 1)
                    if _projected > max_image_ratio:
                        is_image_override = False
                        _planned_video_count += 1
                        logger.info(
                            f"clip {clip_counter}: image ratio cap "
                            f"({_planned_image_count}/{_total_planned or 1} planned) — trying video first"
                        )
                    else:
                        _planned_image_count += 1
                elif is_image:
                    _planned_image_count += 1
                else:
                    _planned_video_count += 1

                visual_effect = sent.get("visual_effect", "")
                # Consecutive-repeat guard is applied in Phase B (ordering is known there).

                # A single random.Random instance isn't safe to share across
                # ThreadPoolExecutor workers (concurrent mutation of its
                # internal state), and even with a lock, thread-scheduling
                # order would make the sequence depend on wall-clock timing,
                # not job content -- defeating reproducibility. Instead,
                # derive one independent, deterministic RNG per clip from
                # job_seed + clip_counter, both known at submission time.
                _clip_rng = random.Random(job_seed + clip_counter)

                future = executor.submit(
                    _fetch_clip_with_budget,
                    # visual_criticality scales the per-clip search deadline:
                    # low ×0.6, medium ×1.0, high ×1.25, critical ×1.5
                    budget_seconds=clip_budget_seconds * criticality_budget_multiplier(sent),
                    sentence=sent,
                    sent_duration=clip_duration,
                    trim_buffer=trim_buffer,
                    source=video_source,
                    video_aspect=video_aspect,
                    clip_idx=clip_counter,
                    clips_dir=clips_dir,
                    used_urls=used_urls,
                    fallback_terms=_all_concepts,
                    is_image_override=is_image_override,
                    video_topic=video_topic,
                    video_type=video_type,
                    recent_embeddings=recent_embs,  # ThreadSafeEmbeddingWindow -- safe to share
                    dedup_threshold=dedup_threshold,
                    visual_effect=visual_effect,
                    rng=_clip_rng,
                    report=quality_report,
                )
                _task_queue.append((future, {
                    "type": "broll",
                    "clip_idx": clip_counter,
                    "sent": sent,
                    "clip_duration": clip_duration,
                    "is_image": is_image,
                    "visual_effect": visual_effect,
                    "overlay_gfx_clip": _pending_overlay_gfx,  # same for all clips of sentence
                    "idx": idx,
                }))
                clip_counter += 1

        logger.info(
            f"submitted {len(_task_queue)} clip tasks — "
            f"{max_fetch_workers} workers fetching in parallel"
        )

        # ---- Phase B: collect results in original order + post-process ----
        _last_visual_effect: str = ""
        _overlay_composited_sentences: set = set()  # composite-overlay graphic applied for these sentence indices
        _sentence_got_clip: set = set()
        # (cue_start_seconds, sfx_path, volume) — cue_start is the clip's
        # position in planned_clip_durations' running sum *before* this clip
        # is appended, i.e. an approximation of its start in the final
        # timeline (pre-crossfade-overlap consumption — good enough for a
        # short mood accent, not claimed frame-exact).
        sfx_cues: list = []

        for future_or_none, meta in _task_queue:
            idx = meta["idx"]

            if meta["type"] == "standalone_graphic":
                ordered_clips.append(meta["clip_path"])
                planned_clip_durations.append(meta["clip_duration"])
                obtained_duration += meta["clip_duration"]
                video_clip_count += 1
                _sentence_got_clip.add(idx)
                continue

            # Block until this specific clip's fetch completes.
            try:
                fetched = future_or_none.result()
            except Exception as exc:
                logger.warning(f"clip {meta['clip_idx']}: fetch raised — {exc}")
                fetched = None
            if meta["type"] == "avatar":
                # generate_avatar_clip returns a bare path — adapt to
                # _fetch_clip's (path, used_image) contract so the shared
                # post-processing below applies unchanged. On failure the
                # block falls through to the normal rescue → placeholder
                # ladder using the first sentence's own visual_concepts.
                fetched = (fetched, False) if fetched else None
                if not fetched:
                    logger.warning(
                        f"clip {meta['clip_idx']}: avatar block failed — "
                        "falling back to footage for the whole block"
                    )
                    # Unlike a failed broll slot, this sentence's own concepts
                    # are still fresh (the primary fetch never ran for avatar
                    # slots) — try them before the generic rescue ladder below.
                    fetched = _fetch_clip(
                        sentence=meta["sent"],
                        sent_duration=meta["clip_duration"],
                        trim_buffer=trim_buffer,
                        source=video_source,
                        video_aspect=video_aspect,
                        clip_idx=meta["clip_idx"],
                        clips_dir=clips_dir,
                        used_urls=used_urls,
                        fallback_terms=_all_concepts,
                        video_topic=video_topic,
                        video_type=video_type,
                        recent_embeddings=recent_embs,
                        dedup_threshold=dedup_threshold,
                        deadline=time.monotonic() + rescue_budget_seconds,
                        rng=job_rng,
                        report=quality_report,
                    )
                    # Stamp AFTER the fetch — _fetch_clip's _record rewrites
                    # the whole report entry.
                    quality_report.setdefault(meta["clip_idx"], {})["avatar_failed"] = True
            # Stamp only after the fetch resolved: _fetch_*'s _record writes the
            # whole report entry, so stamping earlier would race and be lost.
            quality_report.setdefault(meta["clip_idx"], {})["sentence_idx"] = idx
            quality_report[meta["clip_idx"]]["planned_duration"] = meta["clip_duration"]

            clip_duration = meta["clip_duration"]

            # Rescue fetch: the primary attempt already exhausted this sentence's
            # own concept ladder AND the job-wide concept pool (fallback_terms=
            # _all_concepts in Phase A). Retrying with that same pool would just
            # re-hit already-rejected/consumed candidates, so use deliberately
            # different, generic vocabulary instead -- same reasoning as gap-fill
            # Phase 2 below. Without this, a failed slot falls straight through to
            # looping the previous clip (see the placeholder branch below), which
            # is the "same clip repeats over and over" artifact this must avoid.
            # Tries up to rescue_max_attempts distinct gapfill terms (not just
            # one) before giving up -- a single generic term can just as easily
            # turn up zero candidates as the sentence's own concepts did, and
            # every extra attempt meaningfully cuts how often we fall through
            # to the placeholder loop below.
            #
            # For ANY sentence inside a named section (entity_name set), a
            # generic gap-fill image is jarring -- product narration (worst
            # of all, a lower_third product label) over unrelated stock
            # footage. Other sentences in the same named section already
            # found real, Serper-searchable terms for this exact entity
            # (that's the whole point of the visual_concepts[0] rotation
            # rule), so try those "cousin" terms first, before falling back
            # to the fully generic _job_gapfill_terms pool.
            if not fetched:
                _rescue_pool: List[str] = []
                _ename = (meta["sent"].get("entity_name") or "").strip()
                if _ename:
                    _own_concepts = set(_get_visual_concepts(meta["sent"]))
                    _rescue_pool = [
                        c for c in _entity_concepts_map.get(_ename, [])
                        if c not in _own_concepts
                    ]
                _rescue_attempts_total = min(
                    rescue_max_attempts, len(_rescue_pool) + len(_job_gapfill_terms)
                )
                for _attempt in range(_rescue_attempts_total):
                    if _attempt < len(_rescue_pool):
                        _rescue_term = _rescue_pool[_attempt]
                        _rescue_kind = "entity-cousin"
                    else:
                        _rescue_term = _job_gapfill_terms[_rescue_idx % len(_job_gapfill_terms)]
                        _rescue_idx += 1
                        _rescue_kind = "generic"
                    logger.warning(
                        f"clip {meta['clip_idx']}: primary fetch failed — attempting "
                        f"{_rescue_kind} rescue fetch {_attempt + 1}/{_rescue_attempts_total} ('{_rescue_term}')"
                    )
                    fetched = _fetch_clip(
                        sentence={
                            "visual_concepts": [_rescue_term],
                            "media_type": "video",
                            # entity-cousin terms are real named-entity search
                            # phrases (same caliber as this sentence's own
                            # visual_concepts[0] would be) -- route them
                            # through the same named-entity search order
                            # (Serper/Google Images first) rather than generic
                            # stock-video search, for better product-relevance
                            # odds. Generic gapfill terms keep "broll" as before.
                            "content_track": "named" if _rescue_kind == "entity-cousin" else "broll",
                            "visual_caption": _rescue_term,
                        },
                        sent_duration=clip_duration,
                        trim_buffer=trim_buffer,
                        source=video_source,
                        video_aspect=video_aspect,
                        clip_idx=meta["clip_idx"],
                        clips_dir=clips_dir,
                        used_urls=used_urls,
                        video_topic=video_topic,
                        video_type=video_type,
                        recent_embeddings=recent_embs,
                        dedup_threshold=dedup_threshold,
                        deadline=time.monotonic() + rescue_budget_seconds,
                        rng=job_rng,
                        report=quality_report,
                    )
                    if fetched:
                        logger.info(
                            f"clip {meta['clip_idx']}: rescue fetch succeeded "
                            f"on attempt {_attempt + 1}"
                        )
                        quality_report.setdefault(meta["clip_idx"], {})["rescued"] = True
                        quality_report[meta["clip_idx"]]["rescue_attempts"] = _attempt + 1
                        quality_report[meta["clip_idx"]]["sentence_idx"] = idx  # rescue _record rewrote the entry
                        quality_report[meta["clip_idx"]]["planned_duration"] = meta["clip_duration"]
                        break

            # Consecutive-effect guard (deferred from Phase A where order was unknown).
            # AGENT_GUIDE's graphics-audit rule is "no two consecutive sentences may
            # have an effect (globally)" -- ANY effect back-to-back, not just a
            # repeated identical one. This is a render-time backstop: worker.mjs's
            # enforceVisualEffectSpacing() is the primary fix (cleans job.json
            # itself), but Section C's eligibility list is computed before the same
            # LLM call's own Section B sentence deletions, so adjacency can still
            # shift after the prompt was built -- this catches anything that slips
            # through. Only update _last_visual_effect when a clip is actually added
            # to the timeline; a failed fetch never played its effect so it
            # shouldn't suppress the next clip's.
            visual_effect = meta["visual_effect"]
            if visual_effect and _last_visual_effect:
                logger.info(
                    f"clip {meta['clip_idx']}: skipping consecutive overlay '{visual_effect}' "
                    f"(previous clip already had '{_last_visual_effect}')"
                )
                visual_effect = ""

            if fetched:
                clip_path, used_image = fetched
                content_track_sent = meta["sent"].get("content_track", "broll")
                visual_grade = meta["sent"].get("visual_grade", "")
                grain = float(meta["sent"].get("grain", 0.0) or 0.0)
                vignette = float(meta["sent"].get("vignette", 0.0) or 0.0)

                if (
                    (visual_effect or visual_grade or grain or vignette)
                    and content_track_sent in ("broll", "named")
                ):
                    effected_path = clip_path.replace(".mp4", f"_{visual_effect or 'graded'}.mp4")
                    eff_w, eff_h = video_aspect.to_resolution()
                    clip_path = video.apply_visual_effect(
                        clip_path, visual_effect, effected_path,
                        width=eff_w, height=eff_h,
                        threads=os.cpu_count() or 4,
                        grade=visual_grade, grain=grain, vignette=vignette,
                    )
                    if visual_effect and config.app.get("sfx_enabled", True):
                        sfx_pick = video.pick_sfx_for_mood(visual_effect, rng=job_rng)
                        if sfx_pick:
                            sfx_cues.append((
                                sum(planned_clip_durations),
                                sfx_pick["path"],
                                sfx_pick["volume"],
                            ))

                # Composite-over-footage graphic (lower_third or info_callout) —
                # apply on the first successful clip of the sentence. Consume the
                # slot regardless of success so a failed ffmpeg call doesn't cause
                # all remaining clips in the sentence to retry the same broken
                # composite. composite_lower_third() is fully generic (colorkey +
                # alpha-fade + overlay) despite its name — reused unchanged here.
                overlay_gfx_clip = meta.get("overlay_gfx_clip")
                if overlay_gfx_clip and idx not in _overlay_composited_sentences:
                    _overlay_composited_sentences.add(idx)
                    overlay_out = clip_path.replace(".mp4", "_overlay.mp4")
                    ov_w, ov_h = video_aspect.to_resolution()
                    overlay_result = video.composite_lower_third(
                        clip_path, overlay_gfx_clip, overlay_out,
                        ov_w, ov_h, threads=os.cpu_count() or 4,
                    )
                    if overlay_result:
                        clip_path = overlay_result
                    else:
                        logger.warning(
                            f"sentence {idx+1}: composite-over-footage graphic failed — "
                            "footage used without overlay"
                        )

                _last_visual_effect = visual_effect
                ordered_clips.append(clip_path)
                # A talking head is the worst possible anchor for a looped
                # placeholder — keep avatar clips out of the anchor pool.
                if meta["type"] != "avatar":
                    _recent_real_clips.append(clip_path)
                if used_image:
                    _img_src = (quality_report.get(meta["clip_idx"]) or {}).get("image_path", "")
                    if _img_src:
                        _recent_real_image_sources[clip_path] = _img_src
                _placeholder_run_len = 0
                planned_clip_durations.append(clip_duration + trim_buffer)
                obtained_duration += clip_duration
                if used_image:
                    image_clip_count += 1
                else:
                    video_clip_count += 1
                _sentence_got_clip.add(idx)
                # An avatar clip covers every sentence in its block.
                _sentence_got_clip.update(meta.get("member_idxs", ()))
            else:
                # Fetch failed — insert a placeholder clip of the correct duration so
                # the video timeline stays in sync with the audio narration.
                # Without this, each failed fetch leaves a "hole": the narration plays
                # for `clip_duration` seconds with no corresponding video, causing every
                # subsequent clip to appear progressively earlier than the VO that
                # describes it. Eight failures before a named-product sentence already
                # produces ~32 s of drift at that cut.
                ph_dur = clip_duration + trim_buffer
                ph_path = os.path.join(clips_dir, f"clip-{meta['clip_idx']:04d}-ph.mp4")
                ph_w, ph_h = video_aspect.to_resolution()
                ph_ok = False
                # Anchor on a REAL (non-placeholder) clip, never a placeholder --
                # looping a loop would compound the repeated-footage artifact.
                # OLDEST first: the most recent real clip is the placeholder's
                # immediate timeline neighbor, and anchoring on it produced a
                # frame-for-frame duplicate pair in a real job (same image,
                # same animation, side by side). Walking oldest→newest puts
                # the reused imagery as far as possible from its original
                # appearance; _placeholder_run_len advances through the pool
                # when several placeholders land back to back. If only one
                # real clip exists yet, there's nothing distinct to cycle to
                # -- mirror the 2nd+ one in a run so it's at least not a
                # frame-for-frame repeat.
                _anchor_pool = list(_recent_real_clips) or (
                    [ordered_clips[-1]] if ordered_clips else []
                )
                _prev = (
                    _anchor_pool[_placeholder_run_len % len(_anchor_pool)]
                    if _anchor_pool else None
                )
                _mirror_repeat = _placeholder_run_len > 0 and len(_anchor_pool) == 1

                # Prefer regenerating over looping: if the anchor is a Ken
                # Burns/3D still-image render, its source image is known
                # (_recent_real_image_sources), so re-render ONE continuous
                # animation at the exact fill duration instead of
                # stream_loop-ing the already-rendered clip. stream_loop
                # replays the baked-in animation from frame 0 whenever
                # ph_dur exceeds the anchor's own length -- the "clip
                # repeats including its animation" artifact this avoids.
                # Falls through to stream_loop when the anchor came from
                # stock video (no source image to re-animate) or regen fails.
                _prev_image_src = _recent_real_image_sources.get(_prev) if _prev else None
                if _prev_image_src:
                    # Mirror the source before re-rendering: the animation
                    # picker can legally choose the same animation the anchor
                    # clip used (its no-repeat state tracks fetch completion
                    # order, not timeline order), and an un-mirrored regen
                    # then reads as an exact repeat of the anchor.
                    _ph_src = _prev_image_src
                    try:
                        from PIL import Image as _PILImage
                        _flip = getattr(
                            getattr(_PILImage, "Transpose", _PILImage),
                            "FLIP_LEFT_RIGHT",
                        )
                        with _PILImage.open(_prev_image_src) as _im:
                            _mirrored = _im.transpose(_flip)
                            _ph_src = os.path.join(
                                clips_dir, f"clip-{meta['clip_idx']:04d}-ph-src.png"
                            )
                            _mirrored.save(_ph_src)
                    except Exception as _flip_exc:
                        logger.debug(f"placeholder mirror failed (using original): {_flip_exc}")
                        _ph_src = _prev_image_src
                    try:
                        _regen_result = video.render_ken_burns_clip(
                            image_path=_ph_src,
                            duration=ph_dur,
                            width=ph_w,
                            height=ph_h,
                            output_path=ph_path,
                            rng=job_rng,
                        )
                    except Exception as _regen_exc:
                        logger.warning(
                            f"clip {meta['clip_idx']}: placeholder animation regenerate "
                            f"raised — {_regen_exc}"
                        )
                        _regen_result = ""
                    if _regen_result:
                        ph_ok = True
                        logger.info(
                            f"clip {meta['clip_idx']}: placeholder filled by regenerating "
                            f"the anchor's animation continuously for {ph_dur:.2f}s "
                            "(no loop/repeat)"
                        )
                if not ph_ok and _prev:
                    _ph_vf = f"fps=30,scale={ph_w}:{ph_h}:flags=lanczos"
                    if _mirror_repeat:
                        _ph_vf = "hflip," + _ph_vf
                    _ph_cmd = [
                        utils.get_ffmpeg_binary(), "-y",
                        "-stream_loop", "-1", "-i", _prev,
                        "-t", f"{ph_dur:.6f}",
                        "-vf", _ph_vf,
                        "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-an", ph_path,
                    ]
                    _ph_r = subprocess.run(_ph_cmd, capture_output=True, timeout=60)
                    ph_ok = _ph_r.returncode == 0
                if not ph_ok:
                    # Fallback: solid black clip.
                    _ph_cmd = [
                        utils.get_ffmpeg_binary(), "-y",
                        "-f", "lavfi",
                        "-i", f"color=c=black:s={ph_w}x{ph_h}:r=30",
                        "-t", f"{ph_dur:.6f}",
                        "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-an", ph_path,
                    ]
                    _ph_r = subprocess.run(_ph_cmd, capture_output=True, timeout=60)
                    ph_ok = _ph_r.returncode == 0
                if not ph_ok:
                    # Both attempts above can fail transiently under CPU/IO
                    # contention (many concurrent fetch-worker threads racing
                    # ffmpeg calls) rather than a hard systemic failure. A
                    # solid-color render is about as cheap as ffmpeg gets, so
                    # one retry with a much longer timeout is nearly free and
                    # catches the transient case before conceding a real gap.
                    _ph_r = subprocess.run(_ph_cmd, capture_output=True, timeout=180)
                    ph_ok = _ph_r.returncode == 0
                if ph_ok:
                    ordered_clips.append(ph_path)
                    planned_clip_durations.append(ph_dur)
                    obtained_duration += clip_duration
                    video_clip_count += 1
                    _placeholder_run_len += 1
                    quality_report.setdefault(meta["clip_idx"], {})["placeholder_used"] = True
                    # A placeholder still covers the sentence(s) on the timeline —
                    # without this, every placeholder-filled sentence (and every
                    # member of a failed avatar block) wrongly logs as "no clip
                    # available" below and is misreported in quality.json.
                    _sentence_got_clip.add(idx)
                    if meta["type"] == "avatar":
                        _sentence_got_clip.update(meta.get("member_idxs", ()))
                    logger.warning(
                        f"clip {meta['clip_idx']}: fetch failed — inserted "
                        f"{clip_duration:.2f}s placeholder to maintain A/V sync"
                        + (" (mirrored, no distinct anchor available)" if _mirror_repeat else "")
                    )
                else:
                    quality_report.setdefault(meta["clip_idx"], {})["catastrophic_gap"] = True
                    quality_report[meta["clip_idx"]]["catastrophic_gap_duration"] = ph_dur
                    _catastrophic_gaps.append((len(ordered_clips), ph_dur, meta["clip_idx"]))
                    logger.error(
                        f"clip {meta['clip_idx']}: fetch failed AND placeholder generation "
                        f"failed after retries — {ph_dur:.2f}s gap recorded at timeline "
                        f"position {len(ordered_clips)} for position-aware gap-fill"
                    )

    logger.info(
        f"parallel clip fetch done in {time.monotonic() - _fetch_t0:.1f}s "
        f"({len(ordered_clips)} clips obtained, {max_fetch_workers} workers)"
    )
    for idx in range(len(clip_plans)):
        if idx not in _sentence_got_clip:
            logger.warning(f"sentence {idx+1}: skipping — no clip available")

    if not ordered_clips:
        logger.error("no clips obtained for any sentence — aborting")
        return None

    logger.info(f"obtained {len(ordered_clips)}/{total_sentences} clips")
    _total_clips = image_clip_count + video_clip_count
    if _total_clips:
        logger.info(
            f"clip mix: {video_clip_count} video / {image_clip_count} image "
            f"({image_clip_count / _total_clips:.0%} image)"
        )

    # ---- Position-aware gap-fill: patch catastrophic holes at their exact
    # timeline position, not the tail. A tail-only fix (the pass below) makes
    # the total video duration match the audio again, but everything between
    # the hole and the end stays shifted relative to the VO describing it --
    # this fixes that by inserting compensating footage exactly where the
    # hole is, before the tail pass ever runs.
    if _catastrophic_gaps:
        logger.warning(
            f"patching {len(_catastrophic_gaps)} catastrophic gap(s) at their "
            "recorded timeline position (not the tail)"
        )
        _gap_offset = 0
        for _gap_i, (_gap_pos, _gap_dur, _orig_clip_idx) in enumerate(_catastrophic_gaps):
            _filler_idx = clip_counter
            clip_counter += 1
            _gap_concept = None
            if _all_concepts:
                _gap_concept = _all_concepts[_gap_i % len(_all_concepts)]
            elif _job_gapfill_terms:
                _gap_concept = _job_gapfill_terms[_gap_i % len(_job_gapfill_terms)]

            _gap_fetched = None
            if _gap_concept:
                _gap_fetched = _fetch_clip(
                    sentence={
                        "visual_concepts": [_gap_concept],
                        "media_type": "video",
                        "content_track": "broll",
                        "visual_caption": _gap_concept,
                    },
                    sent_duration=_gap_dur,
                    trim_buffer=0.0,
                    source=video_source,
                    video_aspect=video_aspect,
                    clip_idx=_filler_idx,
                    clips_dir=clips_dir,
                    used_urls=used_urls,
                    video_topic=video_topic,
                    video_type=video_type,
                    recent_embeddings=recent_embs,
                    dedup_threshold=dedup_threshold,
                    rng=job_rng,
                    report=quality_report,
                )

            _gap_clip_path = _gap_fetched[0] if _gap_fetched else None
            if not _gap_clip_path:
                # Last resort: solid black, same as the per-sentence
                # placeholder fallback above -- guarantees SOMETHING occupies
                # this position rather than leaving a hole.
                _bw, _bh = video_aspect.to_resolution()
                _black_path = os.path.join(clips_dir, f"clip-{_filler_idx:04d}-gapfix.mp4")
                _black_cmd = [
                    utils.get_ffmpeg_binary(), "-y",
                    "-f", "lavfi",
                    "-i", f"color=c=black:s={_bw}x{_bh}:r=30",
                    "-t", f"{_gap_dur:.6f}",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-an", _black_path,
                ]
                _black_r = subprocess.run(_black_cmd, capture_output=True, timeout=180)
                if _black_r.returncode == 0:
                    _gap_clip_path = _black_path

            _insert_at = _gap_pos + _gap_offset
            if _gap_clip_path:
                ordered_clips.insert(_insert_at, _gap_clip_path)
                planned_clip_durations.insert(_insert_at, _gap_dur)
                obtained_duration += _gap_dur
                video_clip_count += 1
                _gap_offset += 1
                quality_report[_orig_clip_idx]["catastrophic_gap_recovered"] = True
                logger.info(
                    f"gap at position {_insert_at}: patched with {_gap_dur:.2f}s of "
                    "filler footage in place"
                )
            else:
                quality_report[_orig_clip_idx]["catastrophic_gap_recovered"] = False
                logger.error(
                    f"gap at position {_insert_at}: position-aware patch also failed — "
                    f"{_gap_dur:.2f}s of drift will remain in this job"
                )

    # ---- Gap-fill: cover any shortfall with extra unique clips. ----
    # Phase 1: cycle through the job's own concept pool (already assembled above).
    # Phase 2: when that pool is exhausted, try _job_gapfill_terms (defined
    # above, near quality_report -- also reused by the Phase B rescue fetch).
    if obtained_duration < audio_duration - 0.5 and (_all_concepts or _job_gapfill_terms):
        max_gap_fill_attempts = len(_all_concepts) * 3 + 20
        attempts = 0
        logger.info(
            f"obtained {obtained_duration:.2f}s of {audio_duration:.2f}s — gap-filling with extra clips"
        )
        # Phase 1: job concept pool
        while obtained_duration < audio_duration - 0.5 and attempts < max_gap_fill_attempts and _all_concepts:
            concept = _all_concepts[attempts % len(_all_concepts)]
            attempts += 1
            filler_sentence = {
                "visual_concepts": [concept],
                "media_type": "video",
                "content_track": "broll",
                "visual_caption": concept,
            }
            fetched = _fetch_clip(
                sentence=filler_sentence,
                sent_duration=_CLIP_TARGET,
                trim_buffer=trim_buffer,
                source=video_source,
                video_aspect=video_aspect,
                clip_idx=clip_counter,
                clips_dir=clips_dir,
                used_urls=used_urls,
                video_topic=video_topic,
                video_type=video_type,
                recent_embeddings=recent_embs,
                dedup_threshold=dedup_threshold,
                rng=job_rng,
                report=quality_report,
            )
            clip_counter += 1
            if fetched:
                ordered_clips.append(fetched[0])
                planned_clip_durations.append(_CLIP_TARGET + trim_buffer)
                obtained_duration += _CLIP_TARGET
        # Phase 2: generic fallback terms — different from the job's concept pool
        # so they produce fresh URLs even when the primary pool is completely dry.
        if obtained_duration < audio_duration - 0.5:
            logger.info("gap-fill phase 1 exhausted — trying generic fallback terms")
            for fb_concept in _job_gapfill_terms:
                if obtained_duration >= audio_duration - 0.5:
                    break
                filler_sentence = {
                    "visual_concepts": [fb_concept],
                    "media_type": "video",
                    "content_track": "broll",
                    "visual_caption": fb_concept,
                }
                fetched = _fetch_clip(
                    sentence=filler_sentence,
                    sent_duration=_CLIP_TARGET,
                    trim_buffer=trim_buffer,
                    source=video_source,
                    video_aspect=video_aspect,
                    clip_idx=clip_counter,
                    clips_dir=clips_dir,
                    used_urls=used_urls,
                    video_topic=video_topic,
                    video_type=video_type,
                    recent_embeddings=recent_embs,
                    dedup_threshold=dedup_threshold,
                    rng=job_rng,
                    report=quality_report,
                )
                clip_counter += 1
                if fetched:
                    ordered_clips.append(fetched[0])
                    planned_clip_durations.append(_CLIP_TARGET + trim_buffer)
                    obtained_duration += _CLIP_TARGET
        if obtained_duration < audio_duration - 0.5:
            repeat_secs = audio_duration - obtained_duration
            logger.error(
                f"FOOTAGE POOL EXHAUSTED: {repeat_secs:.0f}s of narration has no visual coverage. "
                f"The last clip will repeat for {repeat_secs:.0f}s. "
                f"Fix: provide more diverse visual_concepts in the job JSON."
            )
        else:
            logger.info(f"gap-fill complete: {obtained_duration:.2f}s obtained")

    # All VLM verdicts are in by now — persist any batched-but-unflushed ones.
    vlm.flush_cache()

    # ------------------------------------------------------------------ #
    # 4. Combine clips                                                     #
    # ------------------------------------------------------------------ #
    combined_path = os.path.join(temp_dir, "combined.mp4")
    logger.info("combining clips sequentially")
    try:
        video.combine_videos(
            combined_video_path=combined_path,
            video_paths=ordered_clips,
            audio_file=audio_file,
            video_aspect=video_aspect,
            video_concat_mode=VideoConcatMode.sequential,
            video_transition_mode=VideoTransitionMode.crossfade,
            # Clips are already pre-trimmed; use a large cap to avoid re-trimming.
            max_clip_duration=999,
            threads=os.cpu_count() or 4,
            planned_clip_durations=planned_clip_durations,
            rng=job_rng,
        )
    except Exception:
        logger.exception("combine_videos() raised — aborting")
        return None
    if not os.path.exists(combined_path):
        logger.error("combine_videos() produced no output — aborting")
        return None

    # Probe the actual combined clip duration — crossfade overlaps reduce it below the
    # raw sum of clip durations, so combine_videos may still fall a few seconds short.
    try:
        _probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nk=1:nw=1",
                combined_path,
            ],
            capture_output=True, text=True, check=True, timeout=30,
        )
        combined_duration = float(_probe.stdout.strip())
    except Exception:
        combined_duration = 0.0

    # Must match the tail already added to the last slot by plan_clip_slots().
    outro_tail = _OUTRO_TAIL
    target_duration = audio_duration + outro_tail
    _ENC = [
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-r", "30", "-threads", str(os.cpu_count() or 4),
    ]

    # Trim if the combined video overshoots audio + outro buffer.  Keep the
    # extra outro_tail seconds of real footage so the fade-out plays on live
    # content rather than a frozen frame.  Tolerance is 1.5s: a small trailing
    # overshoot is invisible (generate_video's fade-out covers it), and
    # trimming costs a full re-encode of the combined video.
    if combined_duration > target_duration + 1.5:
        logger.info(
            f"combined ({combined_duration:.2f}s) overshoots target ({target_duration:.2f}s); trimming"
        )
        trimmed_path = os.path.join(temp_dir, "combined_trimmed.mp4")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", combined_path,
                    "-t", f"{target_duration:.3f}",
                    *_ENC, "-an", trimmed_path,
                ],
                check=True, capture_output=True, timeout=300,
            )
            if os.path.exists(trimmed_path):
                os.replace(trimmed_path, combined_path)
                combined_duration = target_duration
        except Exception as exc:
            logger.warning(f"trim failed ({exc}); proceeding with overshooted combined")

    extended_path = os.path.join(temp_dir, "extended.mp4")
    needed_extra = target_duration - combined_duration

    if needed_extra <= 0.05:
        # Combined already covers audio + outro tail with real footage.
        extended_path = combined_path
        logger.info(
            f"outro: combined ({combined_duration:.2f}s) covers audio+tail ({target_duration:.2f}s) — no extension needed"
        )
    else:
        # Gap to fill: play the tail of the last clip in REVERSE so the outro
        # flows seamlessly from the last frame backwards — no visible loop seam,
        # and any Ken Burns animation continues moving through the fade-out.
        # Exception: when the last clip already ENDS on (near-)black (a baked
        # Ken Burns fade-out, or a black placeholder), reversing its tail
        # plays that fade BACKWARDS — the image visibly re-brightens right
        # before the final fade-out. Hold black instead: fade-to-black →
        # black hold → final fade-out reads as one clean ending.
        outro_path = os.path.join(temp_dir, "outro.mp4")
        last_clip = ordered_clips[-1]
        try:
            _tail_luma = _tail_mean_luma(last_clip)
            if _tail_luma is not None and _tail_luma < _DARK_TAIL_LUMA:
                _ow, _oh = video_aspect.to_resolution()
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-f", "lavfi",
                        "-i", f"color=c=black:s={_ow}x{_oh}:r=30",
                        "-t", f"{needed_extra:.3f}",
                        *_ENC, "-an", outro_path,
                    ],
                    check=True, capture_output=True, timeout=120,
                )
                logger.info(
                    f"outro: last clip tail is dark (YAVG {_tail_luma:.0f}) — "
                    f"holding black for {needed_extra:.2f}s instead of reversing"
                )
            else:
                # Probe the last clip's duration so we only seek within valid range.
                probe = subprocess.run(
                    [
                        "ffprobe", "-v", "error",
                        "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        last_clip,
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                last_clip_dur = float(probe.stdout.strip() or "5.0")
                # Seek to where we need to start reading backwards; clamp so we
                # never seek past the clip's own start.
                seek_back = min(needed_extra, last_clip_dur - 0.05)
                seek_pos = max(0.0, last_clip_dur - seek_back)
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-ss", f"{seek_pos:.3f}", "-i", last_clip,
                        "-vf", "reverse",
                        "-t", f"{needed_extra:.3f}",
                        *_ENC, "-an", outro_path,
                    ],
                    check=True, capture_output=True, timeout=120,
                )
            concat_list = os.path.join(temp_dir, "ext_concat.txt")
            with open(concat_list, "w") as _cf:
                _cf.write(f"file '{os.path.abspath(combined_path)}'\n")
                _cf.write(f"file '{os.path.abspath(outro_path)}'\n")
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", concat_list,
                    *_ENC, "-an", extended_path,
                ],
                check=True, capture_output=True, timeout=300,
            )
            logger.info(
                f"outro: extended by {needed_extra:.2f}s → "
                f"extended={combined_duration + needed_extra:.2f}s (audio={audio_duration}s)"
            )
        except Exception as exc:
            logger.warning(f"outro extension failed ({exc}), using combined as-is")
            extended_path = combined_path

    # ------------------------------------------------------------------ #
    # 5. Subtitle                                                          #
    # ------------------------------------------------------------------ #
    subtitle_path = ""
    if job.get("subtitle_enabled", True):
        subtitle_path = os.path.join(work_dir, "subtitle.srt")
        logger.info("generating subtitles from Whisper sentence timestamps")
        _timings_to_srt(timings, subtitle_path)
        if not subtitle.file_to_subtitles(subtitle_path):
            logger.warning("subtitle file is empty or invalid — subtitles disabled")
            subtitle_path = ""
        elif word_timings:
            words_path = subtitle_path.replace(".srt", ".words.json")
            with open(words_path, "w", encoding="utf-8") as f:
                json.dump(
                    [{"word": w, "start": s, "end": e} for w, s, e in word_timings],
                    f,
                )
            logger.info(f"word timings saved: {words_path}")

    # ------------------------------------------------------------------ #
    # 6. Final video                                                       #
    # ------------------------------------------------------------------ #
    bgm_type, bgm_file = _resolve_bgm(job)
    params = VideoParams(
        video_subject=video_script[:100],
        video_script=video_script,
        video_aspect=job.get("video_aspect", "16:9"),
        video_concat_mode=VideoConcatMode.sequential.value,
        voice_name=job.get("voice_name", "en-US-AriaNeural"),
        voice_rate=voice_rate,
        bgm_type=bgm_type,
        bgm_file=bgm_file,
        bgm_volume=float(job.get("bgm_volume", 0.15)),
        subtitle_enabled=bool(job.get("subtitle_enabled", True)),
        subtitle_position=job.get("subtitle_position", "bottom"),
        text_background_color=False,
        font_name=job.get("font_name", "Inter_18pt-SemiBold.ttf"),
        text_fore_color=job.get("text_fore_color", "#FFFFFF"),
        font_size=int(job.get("font_size", 30)),
        stroke_color=job.get("stroke_color", "#000000"),
        stroke_width=float(job.get("stroke_width", 1.5)),
        subtitle_highlight=bool(job.get("subtitle_highlight", False)),
    )

    output_file = os.path.join(work_dir, "final.mp4")
    logger.info(f"generating final video: {output_file}")
    try:
        video.generate_video(
            video_path=extended_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=output_file,
            params=params,
            rng=job_rng,
            sfx_cues=sfx_cues,
        )
    except Exception:
        logger.exception("generate_video() raised — aborting")
        return None

    if not os.path.exists(output_file):
        logger.error("final video not found after generate_video()")
        return None

    vlm_usage = vlm.get_usage()
    tts_provider, _ = voice.resolve_tts_engine(voice_name)
    quality = write_quality_report(
        work_dir=work_dir,
        title=os.path.basename(work_dir),
        quality_report=quality_report,
        sentences=sentences,
        sentence_got_clip=_sentence_got_clip,
        tts_char_count=len(video_script),
        tts_provider=tts_provider,
        vlm_usage=vlm_usage,
    )

    result = {
        "task_id": task_id,
        "video": output_file,
        "audio": audio_file,
        "subtitle": subtitle_path,
        "combined": combined_path,
        "clips": ordered_clips,
        "audio_duration": audio_duration,
        "vlm_usage": vlm_usage,
        "quality_report_path": quality.get("_path"),
    }

    logger.success(f"pipeline complete → {output_file}")
    return result
