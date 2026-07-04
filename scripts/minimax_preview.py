"""Render a short Minimax TTS preview to an MP3 file.

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

    from app.services.tts.minimax import minimax_tts

    result = minimax_tts(
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
