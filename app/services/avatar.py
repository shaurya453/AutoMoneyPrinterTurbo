"""
app/services/avatar.py — AI avatar (talking-head) clip generation, backed by
either Segmind's or RunPod's InfiniteTalk model (`avatar_provider` config,
see _provider()). Segmind is the active default; RunPod is a dormant
fallback kept intact for easy reversion — this provider switch is meant to
be temporary.

Flow per avatar block (one contiguous run of avatar-marked sentences):
  1. Slice the block's span out of the master TTS audio (ffmpeg).
  2. Publish the slice (and, once per job, the avatar image) into a
     publicly served assets dir — InfiniteTalk only accepts URL inputs.
  3. Submit to the active provider; if generation outlives the sync/first
     response, poll its status endpoint until COMPLETED/FAILED or the
     deadline (RunPod: GET /status/{id}; Segmind: GET /requests/{id}/status
     then /requests/{id} for the final payload).
  4. Download the completed video URL and normalize to the timeline
     geometry (scale+pad to WxH, 30fps, exact planned duration, audio
     stripped — narration comes from the master audio at final mux).

Every step degrades to None on failure — the caller (Phase B) routes a
failed avatar block into the normal rescue → placeholder path, so this
module must never raise out of the executor task.

Feature is dark unless config.toml sets avatar_enabled = true and either
the active provider's API key (segmind_api_key / runpod_api_key) or
avatar_dry_run. Never log API keys.
"""

import os
import shutil
import subprocess
import time
import uuid
from typing import List, Optional, Tuple

import requests
from loguru import logger

from app.config import config
from app.utils import utils

_POLL_INTERVAL_SECONDS = 5.0
# requests-level cap for the runsync call itself; the overall monotonic
# deadline (avatar_timeout_seconds) still governs the poll loop after it.
_RUNSYNC_HTTP_TIMEOUT = 300
# Whisper's end-of-block timestamp occasionally lands a touch early, clipping
# the trailing word/consonant when sliced for the avatar API. Padding the
# tail gives the model room to finish naturally; normalize_clip() still trims
# the resulting video back to planned_duration, so the timeline is unaffected.
_AUDIO_TAIL_BUFFER_SECONDS = 1.0


def _env_flag(name: str) -> Optional[bool]:
    """Tri-state env override: unset → None, "1" → True, else False."""
    value = os.environ.get(name)
    if value is None:
        return None
    return value == "1"


def dry_run_enabled() -> bool:
    """AVATAR_DRY_RUN env wins over config (smoke tests force it on)."""
    env = _env_flag("AVATAR_DRY_RUN")
    if env is not None:
        return env
    return bool(config.app.get("avatar_dry_run", False))


def _provider() -> str:
    """Active avatar backend: "segmind" (default) or "runpod" (dormant)."""
    return str(config.app.get("avatar_provider", "segmind")).strip().lower()


def is_enabled() -> bool:
    """Feature gate: explicit opt-in AND a way to actually generate."""
    env = _env_flag("AVATAR_ENABLED")
    enabled = env if env is not None else bool(config.app.get("avatar_enabled", False))
    if not enabled:
        return False
    if dry_run_enabled():
        return True
    key_name = "segmind_api_key" if _provider() == "segmind" else "runpod_api_key"
    return bool(config.app.get(key_name, ""))


# ---------------------------------------------------------------------------
# Pure command builders (unit-testable without running ffmpeg)
# ---------------------------------------------------------------------------

def build_slice_cmd(
    audio_file: str, t_start: float, t_end: float, out_path: str
) -> List[str]:
    """ffmpeg argv slicing [t_start, t_end) out of the master narration.

    Output-side seek (-ss after -i) for sample accuracy; mono 44.1k mp3 is
    plenty for lip-sync driving. The slice is floored at 1.0s — InfiniteTalk
    needs non-trivial audio to animate.
    """
    t_end = max(t_end, t_start + 1.0)
    return [
        utils.get_ffmpeg_binary(), "-y", "-loglevel", "error",
        "-i", audio_file,
        "-ss", f"{t_start:.3f}",
        "-to", f"{t_end:.3f}",
        "-ac", "1", "-ar", "44100",
        "-c:a", "libmp3lame",
        out_path,
    ]


def build_normalize_cmd(
    src: str,
    out_path: str,
    width: int,
    height: int,
    duration: float,
    fps: int = 30,
) -> List[str]:
    """ffmpeg argv normalizing a generated avatar video for the timeline.

    One pass does everything: fit + pillarbox to the timeline geometry,
    30fps, exact planned duration (trim if long via -t, clone the last
    frame via tpad if short), and strips the audio track — the narration
    is muxed from the master audio.mp3, so any audio here would double-voice.
    """
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},"
        # Clone the final frame far past the target; -t trims to exact length.
        f"tpad=stop_mode=clone:stop_duration={duration:.3f}"
    )
    return [
        utils.get_ffmpeg_binary(), "-y", "-loglevel", "error",
        "-i", src,
        "-vf", vf,
        "-t", f"{duration:.3f}",
        "-an",
        "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        out_path,
    ]


def slice_audio(
    audio_file: str, t_start: float, t_end: float, out_path: str
) -> bool:
    cmd = build_slice_cmd(audio_file, t_start, t_end, out_path)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0 or not os.path.exists(out_path):
            stderr = result.stderr.decode("utf-8", errors="replace")[-300:]
            logger.warning(f"avatar: audio slice failed — {stderr}")
            return False
        return True
    except Exception as exc:
        logger.warning(f"avatar: audio slice exception — {exc}")
        return False


def normalize_clip(
    src: str, out_path: str, width: int, height: int, duration: float
) -> Optional[str]:
    cmd = build_normalize_cmd(src, out_path, width, height, duration)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        if result.returncode != 0 or not os.path.exists(out_path):
            stderr = result.stderr.decode("utf-8", errors="replace")[-300:]
            logger.warning(f"avatar: normalize failed — {stderr}")
            return None
        return out_path
    except Exception as exc:
        logger.warning(f"avatar: normalize exception — {exc}")
        return None


# ---------------------------------------------------------------------------
# Publishing — InfiniteTalk inputs must be publicly reachable URLs
# ---------------------------------------------------------------------------

def publish_dir(task_id: str) -> Tuple[Optional[str], Optional[str]]:
    """Create a per-job public assets dir; return (local_dir, url_prefix).

    The random prefix makes the URL unguessable; the whole dir is removed by
    cleanup_assets() when the job finishes, so concurrent jobs never collide
    and nothing stays public longer than the job runs.
    """
    assets_dir = config.app.get("avatar_assets_dir", "")
    base_url = str(config.app.get("avatar_public_base_url", "")).rstrip("/")
    if not assets_dir or not base_url:
        logger.warning("avatar: avatar_assets_dir/avatar_public_base_url not configured")
        return None, None
    name = f"{uuid.uuid4().hex[:8]}-{task_id}"
    local_dir = os.path.join(assets_dir, name)
    try:
        os.makedirs(local_dir, exist_ok=True)
    except Exception as exc:
        logger.warning(f"avatar: cannot create assets dir {local_dir} — {exc}")
        return None, None
    return local_dir, f"{base_url}/{name}"


def publish_asset(
    src_path: str, local_dir: str, url_prefix: str, name: str
) -> Optional[str]:
    try:
        shutil.copyfile(src_path, os.path.join(local_dir, name))
        return f"{url_prefix}/{name}"
    except Exception as exc:
        logger.warning(f"avatar: publish {name} failed — {exc}")
        return None


def cleanup_assets(local_dir: Optional[str]) -> None:
    if local_dir:
        shutil.rmtree(local_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# RunPod client (dormant fallback — see _provider())
# ---------------------------------------------------------------------------

def _runpod_endpoint() -> str:
    return str(
        config.app.get("runpod_endpoint", "https://api.runpod.ai/v2/infinitetalk")
    ).rstrip("/")


def _runpod_headers() -> dict:
    return {"Authorization": f"Bearer {config.app.get('runpod_api_key', '')}"}


def _extract_runpod_video_url(payload: dict) -> Optional[str]:
    # Confirmed via a live probe (2026-07-12): InfiniteTalk's actual completed
    # payload is {"output": {"cost": ..., "result": "<url>"}} — "result", not
    # "video_url" as the (undocumented) schema might suggest. Both keys are
    # checked so this doesn't silently break again if a future model version
    # changes the field name back.
    output = payload.get("output") or {}
    if isinstance(output, dict):
        return output.get("result") or output.get("video_url") or None
    return None


def _extract_runpod_cost(payload: dict) -> Optional[float]:
    output = payload.get("output") or {}
    cost = output.get("cost") if isinstance(output, dict) else None
    return float(cost) if isinstance(cost, (int, float)) else None


def _submit_runpod(
    image_url: str, audio_url: str, size: str, prompt: str, deadline: float
) -> Tuple[Optional[str], Optional[float]]:
    """Submit a generation; return (video_url, cost_usd), either None on failure.

    runsync blocks while it can, but returns IN_QUEUE/IN_PROGRESS with an id
    when generation outlives the sync window — the /status poll below is
    mandatory, all under the caller's single monotonic deadline so Phase B
    can never hang on a wedged endpoint.
    """
    body = {
        "input": {
            "prompt": prompt,
            "image": image_url,
            "audio": audio_url,
            "size": size,
        }
    }
    try:
        http_timeout = min(_RUNSYNC_HTTP_TIMEOUT, max(10.0, deadline - time.monotonic()))
        resp = requests.post(
            f"{_runpod_endpoint()}/runsync", json=body, headers=_runpod_headers(), timeout=http_timeout
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning(f"avatar: runpod submit failed — {exc}")
        return None, None

    status = payload.get("status", "")
    if status == "COMPLETED":
        url = _extract_runpod_video_url(payload)
        if not url:
            logger.warning(f"avatar: COMPLETED without a usable video URL — {payload.get('output')}")
        return url, _extract_runpod_cost(payload)
    if status == "FAILED":
        logger.warning(f"avatar: generation FAILED — {payload.get('error') or payload.get('output')}")
        return None, None

    job_id = payload.get("id")
    if not job_id:
        logger.warning(f"avatar: unexpected runpod response status={status!r}, no id")
        return None, None

    # Sync window expired server-side — poll until terminal or our deadline.
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
        try:
            resp = requests.get(
                f"{_runpod_endpoint()}/status/{job_id}", headers=_runpod_headers(), timeout=30
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning(f"avatar: status poll failed — {exc}")
            continue
        status = payload.get("status", "")
        if status == "COMPLETED":
            url = _extract_runpod_video_url(payload)
            if not url:
                logger.warning(f"avatar: COMPLETED without a usable video URL — {payload.get('output')}")
            return url, _extract_runpod_cost(payload)
        if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
            logger.warning(f"avatar: generation {status} — {payload.get('error')}")
            return None, None
    logger.warning(f"avatar: job {job_id} still not terminal at deadline — giving up")
    return None, None


# ---------------------------------------------------------------------------
# Segmind client (active default — see _provider())
# ---------------------------------------------------------------------------

def _segmind_endpoint() -> str:
    return str(
        config.app.get("segmind_endpoint", "https://api.segmind.com/v2/infinite-talk")
    ).rstrip("/")


def _segmind_headers() -> dict:
    return {"x-api-key": config.app.get("segmind_api_key", "")}


def _extract_segmind_video_url(payload: dict) -> Optional[str]:
    # Confirmed via a live probe (2026-07-14, scripts/segmind_avatar_probe.py):
    # the completed GET /requests/{id} payload is
    # {"status": "COMPLETED", "output": "<url>", "video": {"url": "<url>", ...}, ...}
    # -- "output" is a bare URL string, not nested. The dict/fallback branches
    # below are defensive for a future response-shape change, same posture as
    # the RunPod client's _extract_runpod_video_url.
    output = payload.get("output")
    if isinstance(output, str) and output:
        return output
    if isinstance(output, dict):
        return output.get("url") or output.get("result") or output.get("video_url") or None
    return payload.get("video_url") or payload.get("result") or None


def _extract_segmind_cost(payload: dict) -> Optional[float]:
    # Confirmed via a live probe (2026-07-14): the completed payload carries
    # {"metrics": {"cost": <float>, "inference_time": ..., "queue_time": ...,
    # "remaining_credits": ..., "total_time": ...}, ...}.
    metrics = payload.get("metrics") or {}
    cost = metrics.get("cost") if isinstance(metrics, dict) else None
    return float(cost) if isinstance(cost, (int, float)) else None


def _submit_segmind(
    image_url: str, audio_url: str, size: str, prompt: str, deadline: float
) -> Tuple[Optional[str], Optional[float]]:
    """Submit a generation to Segmind's async v2 endpoint; return
    (video_url, cost_usd), either None on failure.

    Unlike RunPod, the request body is NOT wrapped in an "input" key, and the
    resolution field is called "resolution" rather than "size". A COMPLETED/
    FAILED status can come back immediately; otherwise the response carries a
    request_id that must be polled at GET /requests/{id}/status until
    terminal, then the final payload fetched from GET /requests/{id} — all
    under the caller's single monotonic deadline.
    """
    body = {
        "image": image_url,
        "audio": audio_url,
        "prompt": prompt,
        "resolution": size,
    }
    try:
        http_timeout = min(_RUNSYNC_HTTP_TIMEOUT, max(10.0, deadline - time.monotonic()))
        resp = requests.post(
            _segmind_endpoint(), json=body, headers=_segmind_headers(), timeout=http_timeout
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning(f"avatar: segmind submit failed — {exc}")
        return None, None

    status = str(payload.get("status", "")).upper()
    if status == "COMPLETED":
        url = _extract_segmind_video_url(payload)
        if not url:
            logger.warning(f"avatar: COMPLETED without a usable video URL — {payload}")
        return url, _extract_segmind_cost(payload)
    if status == "FAILED":
        logger.warning(f"avatar: segmind generation FAILED — {payload.get('error')}")
        return None, None

    request_id = payload.get("request_id") or payload.get("id")
    if not request_id:
        logger.warning(f"avatar: unexpected segmind submit response, no request_id — {payload}")
        return None, None

    status_url = f"https://api.segmind.com/v2/requests/{request_id}/status"
    result_url = f"https://api.segmind.com/v2/requests/{request_id}"
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
        try:
            resp = requests.get(status_url, headers=_segmind_headers(), timeout=30)
            resp.raise_for_status()
            status = str(resp.json().get("status", "")).upper()
        except Exception as exc:
            logger.warning(f"avatar: segmind status poll failed — {exc}")
            continue
        if status == "FAILED":
            logger.warning("avatar: segmind generation FAILED")
            return None, None
        if status == "COMPLETED":
            try:
                resp = requests.get(result_url, headers=_segmind_headers(), timeout=30)
                resp.raise_for_status()
                result_payload = resp.json()
                url = _extract_segmind_video_url(result_payload)
            except Exception as exc:
                logger.warning(f"avatar: segmind result fetch failed — {exc}")
                return None, None
            if not url:
                logger.warning("avatar: segmind COMPLETED without a usable video URL")
            return url, _extract_segmind_cost(result_payload)
    logger.warning(f"avatar: segmind request {request_id} still not terminal at deadline — giving up")
    return None, None


def _download_video(url: str, out_path: str, deadline: float) -> bool:
    for attempt in (1, 2):
        if time.monotonic() > deadline:
            logger.warning("avatar: download deadline reached")
            return False
        try:
            with requests.get(url, stream=True, timeout=(10, 60)) as resp:
                resp.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
            if os.path.getsize(out_path) > 0:
                return True
            logger.warning("avatar: downloaded video is empty")
        except Exception as exc:
            logger.warning(f"avatar: video download attempt {attempt} failed — {exc}")
    return False


# ---------------------------------------------------------------------------
# The ThreadPoolExecutor task
# ---------------------------------------------------------------------------

def _render_dry_run_clip(
    out_path: str, width: int, height: int, duration: float
) -> Optional[str]:
    """Labeled slate standing in for a real generation (avatar_dry_run)."""
    src = f"color=c=0x1f2937:s={width}x{height}:r=30:d={duration:.3f}"
    base = [utils.get_ffmpeg_binary(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", src]
    tail = ["-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-an", out_path]
    label = ["-vf", f"drawtext=text='AVATAR (dry run)':fontcolor=white:fontsize={height // 12}"
                    ":x=(w-text_w)/2:y=(h-text_h)/2"]
    for cmd in (base + label + tail, base + tail):  # drawtext needs fontconfig; plain slate fallback
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=120)
            if result.returncode == 0 and os.path.exists(out_path):
                return out_path
        except Exception:
            pass
    logger.warning("avatar: dry-run slate render failed")
    return None


def generate_avatar_clip(
    *,
    image_path: str,
    audio_file: str,
    t_start: float,
    t_end: float,
    planned_duration: float,
    clips_dir: str,
    clip_idx: int,
    width: int,
    height: int,
    image_url: Optional[str],
    local_dir: Optional[str],
    url_prefix: Optional[str],
    report: Optional[dict] = None,
) -> Optional[str]:
    """Generate one avatar block clip; returns the clip path or None.

    Runs inside the Phase A ThreadPoolExecutor — never raises.
    """
    out_path = os.path.join(clips_dir, f"clip-{clip_idx:04d}.mp4")
    try:
        size = str(config.app.get("avatar_size", "720p"))
        if dry_run_enabled():
            result = _render_dry_run_clip(out_path, width, height, planned_duration)
            if result and report is not None:
                report.setdefault(clip_idx, {})["avatar"] = True
                report[clip_idx]["avatar_dry_run"] = True
            return result

        if not (image_url and local_dir and url_prefix):
            logger.warning(f"clip {clip_idx}: avatar assets not published — skipping")
            return None

        deadline = time.monotonic() + float(config.app.get("avatar_timeout_seconds", 600))

        # Slice straight into the published dir — no separate copy step needed.
        slice_name = f"audio-{clip_idx:04d}.mp3"
        buffered_t_end = t_end + _AUDIO_TAIL_BUFFER_SECONDS
        if not slice_audio(audio_file, t_start, buffered_t_end, os.path.join(local_dir, slice_name)):
            return None
        audio_url = f"{url_prefix}/{slice_name}"

        prompt = str(config.app.get(
            "avatar_prompt",
            "A person speaking directly to the camera, natural expression, subtle head movement",
        ))
        submit = _submit_segmind if _provider() == "segmind" else _submit_runpod
        video_url, cost_usd = submit(image_url, audio_url, size, prompt, deadline)
        if not video_url:
            return None

        raw_path = os.path.join(clips_dir, f"clip-{clip_idx:04d}-avatar-raw.mp4")
        if not _download_video(video_url, raw_path, deadline):
            return None

        normalized = normalize_clip(raw_path, out_path, width, height, planned_duration)
        if normalized and report is not None:
            report.setdefault(clip_idx, {})["avatar"] = True
            report[clip_idx]["avatar_size"] = size
            if cost_usd is not None:
                report[clip_idx]["avatar_cost_usd"] = cost_usd
        return normalized
    except Exception as exc:
        logger.warning(f"clip {clip_idx}: avatar generation exception — {exc}")
        return None
