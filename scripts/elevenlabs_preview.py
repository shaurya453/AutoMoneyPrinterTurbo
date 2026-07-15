"""Render a short ElevenLabs (via Algrow) TTS preview to an MP3 file.

Unlike minimax_preview.py, this goes through the real Algrow async API — a
live test on 2026-07-14 completed in ~16s for a 272-char clip, comfortably
inside the portal preview route's timeout. PREVIEW_TEXT must still clear
Algrow's documented 200-character minimum per /api/generate-simple call.

Usage: python scripts/elevenlabs_preview.py <voice_id> <output_path>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PREVIEW_TEXT = (
    "This is a short preview of the selected voice for your automated video. "
    "It gives you a sense of the tone and pacing this narrator will use "
    "across a full script, so you can pick the voice that fits your content."
)


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: elevenlabs_preview.py <voice_id> <output_path>", file=sys.stderr)
        sys.exit(1)

    voice_id, output_path = sys.argv[1], sys.argv[2]

    from app.services.tts.algrow import algrow_elevenlabs_tts

    result = algrow_elevenlabs_tts(
        text=PREVIEW_TEXT,
        voice_name=voice_id,
        voice_rate=1.0,
        voice_file=output_path,
    )
    if result is None:
        print("elevenlabs_preview: TTS call failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
