"""Render a short Minimax TTS preview to an MP3 file.

Always uses Minimax's direct API (bypassing the minimax_provider=algrow
switch) — the portal's preview route only allows a couple minutes before it
gives up, far shorter than Algrow's async queue (observed ~10 min per call).

Usage: python scripts/minimax_preview.py <voice_name> <output_path>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PREVIEW_TEXT = (
    "This is a short preview of the selected voice for your automated video."
)


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: minimax_preview.py <voice_name> <output_path>", file=sys.stderr)
        sys.exit(1)

    voice_name, output_path = sys.argv[1], sys.argv[2]

    from app.services.tts.minimax import direct_minimax_tts

    result = direct_minimax_tts(
        text=PREVIEW_TEXT,
        voice_name=voice_name,
        voice_rate=1.0,
        voice_file=output_path,
    )
    if result is None:
        print("minimax_preview: TTS call failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
