"""One-off probe: hit Segmind's InfiniteTalk v2 API once with a live key and
print the raw JSON at every stage (submit, each status poll, final result).

Segmind's public docs don't confirm the exact response field names -- whether
a completed "output" is a bare URL string or a nested object, or the full
status vocabulary beyond QUEUED/PROCESSING/COMPLETED/FAILED. This script
exists to observe a real response before trusting
app/services/avatar.py's _extract_segmind_video_url() in production, the
same corrective step the RunPod integration itself needed once (see its
"confirmed via a live probe" comments).

Usage:
    python scripts/segmind_avatar_probe.py
    python scripts/segmind_avatar_probe.py --api-key <key> --image path.png --audio path.mp3

Without --api-key, falls back to $SEGMIND_API_KEY, then config.toml's
segmind_api_key. Without --audio, synthesizes a short sine-tone MP3 via
ffmpeg (no committed audio fixture exists yet). Does not download or
normalize the resulting video -- this script only confirms JSON shapes;
real pipeline wiring happens in app/services/avatar.py.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import config  # noqa: E402
from app.services import avatar  # noqa: E402

_POLL_INTERVAL_SECONDS = 5.0
_DEFAULT_ENDPOINT = "https://api.segmind.com/v2/infinite-talk"


def _resolve_api_key(cli_key: str) -> str:
    return (
        cli_key
        or os.environ.get("SEGMIND_API_KEY", "")
        or str(config.app.get("segmind_api_key", ""))
    )


def _synthesize_tone(out_path: str, duration: float = 3.0) -> None:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame",
        out_path,
    ]
    subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--api-key", default="", help="Segmind API key (else $SEGMIND_API_KEY, else config.toml)")
    parser.add_argument(
        "--image",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests/fixtures/avatar-test.png"),
        help="Path to a test image (default: tests/fixtures/avatar-test.png)",
    )
    parser.add_argument("--audio", default="", help="Path to an audio file (else a tone is synthesized)")
    parser.add_argument("--resolution", default="480p", choices=["480p", "576p", "720p"])
    parser.add_argument(
        "--prompt",
        default=str(config.app.get(
            "avatar_prompt",
            "A person speaking directly to the camera, natural expression, subtle head movement",
        )),
    )
    parser.add_argument("--timeout", type=float, default=600.0, help="Overall deadline in seconds for submit+poll")
    args = parser.parse_args()

    api_key = _resolve_api_key(args.api_key)
    if not api_key:
        print(
            "No Segmind API key found (--api-key / $SEGMIND_API_KEY / config.toml's segmind_api_key).",
            file=sys.stderr,
        )
        sys.exit(1)

    tmp_audio_path = None
    audio_path = args.audio
    if not audio_path:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        tmp_audio_path = tmp.name
        print(f"No --audio given -- synthesizing a 3s test tone at {tmp_audio_path}")
        _synthesize_tone(tmp_audio_path)
        audio_path = tmp_audio_path

    local_dir, url_prefix = avatar.publish_dir("segmind-probe")
    if not (local_dir and url_prefix):
        print(
            "avatar.publish_dir() failed -- check avatar_assets_dir/avatar_public_base_url in config.toml.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        image_url = avatar.publish_asset(args.image, local_dir, url_prefix, "probe-image.png")
        audio_url = avatar.publish_asset(audio_path, local_dir, url_prefix, "probe-audio.mp3")
        if not (image_url and audio_url):
            print("Could not publish image/audio to a public URL.", file=sys.stderr)
            sys.exit(1)
        print(f"image_url: {image_url}")
        print(f"audio_url: {audio_url}")

        headers = {"x-api-key": api_key}
        body = {
            "image": image_url,
            "audio": audio_url,
            "prompt": args.prompt,
            "resolution": args.resolution,
        }
        endpoint = str(config.app.get("segmind_endpoint", _DEFAULT_ENDPOINT)).rstrip("/")

        print(f"\nPOST {endpoint}")
        resp = requests.post(endpoint, json=body, headers=headers, timeout=60)
        print(f"status_code={resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            print(f"non-JSON response body: {resp.text[:500]}")
            sys.exit(1)
        print(f"submit response: {payload}")

        status = str(payload.get("status", "")).upper()
        request_id = payload.get("request_id") or payload.get("id")

        deadline = time.monotonic() + args.timeout
        while status not in ("COMPLETED", "FAILED") and request_id and time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL_SECONDS)
            status_url = f"https://api.segmind.com/v2/requests/{request_id}/status"
            resp = requests.get(status_url, headers=headers, timeout=30)
            print(f"\nGET {status_url} -> status_code={resp.status_code}")
            try:
                poll_payload = resp.json()
            except ValueError:
                print(f"non-JSON poll body: {resp.text[:500]}")
                continue
            print(f"poll response: {poll_payload}")
            status = str(poll_payload.get("status", "")).upper()

        if status == "COMPLETED" and request_id:
            result_url = f"https://api.segmind.com/v2/requests/{request_id}"
            resp = requests.get(result_url, headers=headers, timeout=30)
            print(f"\nGET {result_url} -> status_code={resp.status_code}")
            print(f"final result: {resp.json()}")
        elif status == "COMPLETED":
            print("\nCompleted immediately on submit -- no separate result fetch needed.")
        else:
            print(f"\nGave up: final status={status!r}")
    finally:
        avatar.cleanup_assets(local_dir)
        if tmp_audio_path:
            try:
                os.unlink(tmp_audio_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
