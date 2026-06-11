"""
cli.py — command-line entry point for the documentary pipeline

Typical workflow:
    1. Write your script to script.txt
    2. python sentence_prep.py --script script.txt --out job.json
    3. Review / edit job.json (override search_terms, set media_type, etc.)
    4. python cli.py --job job.json

Usage:
    python cli.py --job job.json
    python cli.py --job job.json --log-level DEBUG
"""

import argparse
import json
import os
import sys

from loguru import logger


def _configure_logging(level: str):
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        colorize=True,
        format="{time:HH:mm:ss} | <level>{level:<7}</level> | {message}",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the MoneyPrinterTurbo documentary pipeline from a job JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--job", required=True,
        help="Path to job JSON produced by sentence_prep.py",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity (default: INFO)",
    )
    return p


def main():
    # Windows cp1252 consoles can't print Unicode preview text cleanly
    if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("cp"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = _build_parser()
    args = parser.parse_args()

    _configure_logging(args.log_level)

    if not os.path.exists(args.job):
        logger.error(f"job file not found: {args.job}")
        sys.exit(1)

    from app.services import pipeline

    result = pipeline.start(args.job)

    if result is None:
        logger.error("pipeline failed — check logs above for details")
        sys.stderr.flush()
        # Optional ML deps (e.g. onnxruntime/tokenizers, used by the CLIP
        # relevance filter) can leave background threads running that hang
        # normal interpreter shutdown. All work is done, so exit immediately
        # at the OS level rather than risk the process never returning.
        os._exit(1)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
