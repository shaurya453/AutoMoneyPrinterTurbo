"""One-off bootstrap: clone the existing "minimax:" voices into Algrow.

Algrow's provider=minimax path only accepts voice_ids cloned inside Algrow's
own account — it has no knowledge of voices cloned directly with Minimax's
own API/GroupId. This script produces a ~35s reference sample of each
existing voice via Minimax's *direct* API (bypassing the algrow-provider
dispatch in minimax_tts, since we need real Minimax audio as the clone
source), uploads it to Algrow's /api/voices/minimax/clone, and prints the
config.toml [app.minimax_algrow_voice_map] entries to fill in.

Usage: python scripts/algrow_clone_from_minimax.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ~45s of speech at a typical narration pace — comfortably over Algrow's
# documented 30s minimum reference-audio requirement (a first pass at ~23s
# of text came in under 30s of audio, so this is padded well past the floor).
_REFERENCE_TEXT = (
    "This is a reference recording used to clone this voice into a new text "
    "to speech provider. The quick brown fox jumps over the lazy dog while "
    "the sun sets slowly behind the distant mountains, painting the sky in "
    "warm shades of orange and violet. Every product deserves a voice that "
    "feels natural, confident, and easy to listen to for minutes at a time. "
    "Listeners tend to trust a narrator who speaks clearly, at a measured "
    "pace, without rushing through important details or trailing off at "
    "the end of a sentence. A good reference sample should sound relaxed "
    "and conversational, the same way this voice would sound in a normal "
    "video review, so the clone captures its natural tone and cadence."
)

_VOICES = [
    ("moss_audio_9e6fd163-789d-11f1-a909-feb3e5c18eb0", "Makeup Skincare Channel"),
    ("moss_audio_aafbfa2f-7905-11f1-8b87-ba0ad3e185a0", "Handbag Lady"),
    ("moss_audio_23b4150d-7ebd-11f1-99fb-96e792fde6a1", "Hifi Guy"),
]


def _direct_minimax_reference_sample(voice_id: str, out_file: str) -> bool:
    """Generates the reference sample via Minimax's own API directly,
    bypassing minimax_tts()'s algrow-provider dispatch."""
    from app.services.tts.minimax import direct_minimax_tts

    return direct_minimax_tts(_REFERENCE_TEXT, voice_id, 1.0, out_file) is not None


def main() -> None:
    from app.services.tts._utils import get_audio_duration
    from app.services.tts.algrow import clone_minimax_voice

    results = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        for voice_id, label in _VOICES:
            print(f"--- {label} ({voice_id}) ---")
            sample_path = os.path.join(tmpdir, f"{voice_id}.mp3")

            print("  generating reference sample via direct Minimax API...")
            if not _direct_minimax_reference_sample(voice_id, sample_path):
                print("  FAILED: could not generate reference sample", file=sys.stderr)
                continue

            duration = get_audio_duration(sample_path)
            print(f"  reference sample: {duration:.1f}s")
            if duration < 30:
                print(f"  FAILED: sample too short ({duration:.1f}s < 30s minimum)", file=sys.stderr)
                continue

            print("  cloning into Algrow...")
            new_voice_id = clone_minimax_voice(label, sample_path)
            if not new_voice_id:
                print("  FAILED: clone request failed", file=sys.stderr)
                continue

            print(f"  cloned -> {new_voice_id}")
            results[voice_id] = new_voice_id

    print("\n--- config.toml [app.minimax_algrow_voice_map] entries ---")
    for voice_id, new_voice_id in results.items():
        print(f'"{voice_id}" = "{new_voice_id}"')

    if len(results) != len(_VOICES):
        print(f"\n{len(results)}/{len(_VOICES)} voices cloned successfully.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
