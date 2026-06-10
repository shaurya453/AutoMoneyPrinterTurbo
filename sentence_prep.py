"""
sentence_prep.py — sentence splitter and search-term scaffolder

Splits a plain-text script into sentences and writes a job JSON template.
Search terms are auto-extracted as a rough scaffold only — the agent running
this pipeline must rewrite them (and set media_type) in Step 2
before calling cli.py.

Usage:
    python sentence_prep.py --script script.txt --out job.json
    python sentence_prep.py --script script.txt --out job.json \\
        --voice en-US-AriaNeural --aspect 16:9 --source pexels --terms 2
"""

import argparse
import json
import os
import re
import sys
import uuid


# ---------------------------------------------------------------------------
# NLTK bootstrap — download required data on first run, silently
# ---------------------------------------------------------------------------

def _ensure_nltk_data():
    import nltk
    for resource, kind in (("punkt_tab", "tokenizers"), ("stopwords", "corpora")):
        try:
            nltk.data.find(f"{kind}/{resource}")
        except LookupError:
            nltk.download(resource, quiet=True)


# ---------------------------------------------------------------------------
# Sentence splitting
# ---------------------------------------------------------------------------

def split_sentences(text: str) -> list[str]:
    from nltk.tokenize import sent_tokenize

    text = re.sub(r"\r\n|\r", "\n", text)
    paragraphs = re.split(r"\n{2,}", text.strip())

    sentences = []
    for para in paragraphs:
        para = re.sub(r"\s+", " ", para).strip()
        if not para:
            continue
        sentences.extend(sent_tokenize(para))

    return [s.strip() for s in sentences if s.strip()]


# ---------------------------------------------------------------------------
# Search-term scaffold (rough — agent must improve these in Step 2)
# ---------------------------------------------------------------------------

def _proper_noun_phrases(words_raw: list[str], stopwords_set: set) -> list[str]:
    phrases = []
    i = 1  # skip sentence-initial word (always capitalised)
    while i < len(words_raw):
        word = re.sub(r"[^\w]", "", words_raw[i])
        if (
            word
            and word[0].isupper()
            and word.lower() not in stopwords_set
            and len(word) > 2
        ):
            phrase_parts = [word]
            j = i + 1
            while j < len(words_raw):
                nw = re.sub(r"[^\w]", "", words_raw[j])
                if nw and nw[0].isupper() and nw.lower() not in stopwords_set:
                    phrase_parts.append(nw)
                    j += 1
                else:
                    break
            if len(phrase_parts) >= 2:
                phrases.append(" ".join(phrase_parts))
                i = j
                continue
            else:
                phrases.append(word)
        i += 1
    return phrases


def extract_search_terms(sentence: str, stopwords_set: set, n: int = 2) -> list[str]:
    words_raw = sentence.split()
    proper = _proper_noun_phrases(words_raw, stopwords_set)
    all_words = re.findall(r"\b[a-zA-Z]{4,}\b", sentence)
    content = [w.lower() for w in all_words if w.lower() not in stopwords_set]

    seen: set[str] = set()
    terms: list[str] = []
    for item in proper + content:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            terms.append(item)
        if len(terms) == n:
            break

    if not terms:
        fallback = sorted(
            [w.lower() for w in re.findall(r"\b[a-zA-Z]{4,}\b", sentence)
             if w.lower() not in stopwords_set],
            key=len, reverse=True,
        )
        terms = fallback[:n]

    return terms


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Split a script into a pipeline job JSON template",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--script", required=True, help="Path to plain-text script file")
    p.add_argument("--out", required=True, help="Output path for job JSON")
    p.add_argument("--title", default=None, help="Video title — used as the output folder name")
    p.add_argument(
        "--voice", default="en-US-AriaNeural",
        help="edge_tts voice name (default: en-US-AriaNeural)",
    )
    p.add_argument(
        "--rate", type=float, default=1.0,
        help="Voice rate, 0.5–2.0 (default: 1.0)",
    )
    p.add_argument(
        "--aspect", default="16:9", choices=["16:9", "9:16", "1:1"],
        help="Video aspect ratio (default: 16:9)",
    )
    p.add_argument(
        "--source", default="pexels", choices=["pexels", "pixabay"],
        help="Primary stock video provider (default: pexels)",
    )
    p.add_argument(
        "--terms", type=int, default=2, choices=[1, 2, 3],
        help="Search terms per sentence (default: 2)",
    )
    p.add_argument(
        "--task-id", default=None,
        help="Explicit task ID (default: auto-generated UUID)",
    )
    return p


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("cp"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = _build_parser()
    args = parser.parse_args()

    if not os.path.exists(args.script):
        print(f"Error: script file not found: {args.script}", file=sys.stderr)
        sys.exit(1)

    with open(args.script, "r", encoding="utf-8-sig") as fh:
        raw_text = fh.read()

    if not raw_text.strip():
        print("Error: script file is empty.", file=sys.stderr)
        sys.exit(1)

    _ensure_nltk_data()

    from nltk.corpus import stopwords as nltk_stopwords
    stopwords_set = set(nltk_stopwords.words("english"))

    sentences = split_sentences(raw_text)
    print(f"Sentences detected: {len(sentences)}")

    sentence_entries = []
    for sent in sentences:
        terms = extract_search_terms(sent, stopwords_set, n=args.terms)
        sentence_entries.append({
            "text": sent,
            "search_terms": terms,
            "media_type": "video",
        })

    task_id = args.task_id or str(uuid.uuid4())

    job = {
        "task_id": task_id,
        "video_title": args.title or "",
        "video_script": raw_text.strip(),
        "sentences": sentence_entries,
        "voice_name": args.voice,
        "voice_rate": args.rate,
        "video_aspect": args.aspect,
        "video_clip_duration": 5,
        "subtitle_enabled": True,
        "subtitle_highlight": False,
        "font_name": "Inter_18pt-SemiBold.ttf",
        "text_fore_color": "#FFFFFF",
        "font_size": 30,
        "stroke_color": "#000000",
        "stroke_width": 1.5,
        "subtitle_position": "bottom",
        "bgm_search_term": "",
        "bgm_file": "random",
        "bgm_volume": 0.15,
        "video_source": args.source,
    }

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(job, fh, indent=2, ensure_ascii=False)

    print(f"Job JSON written to: {args.out}")
    print(f"Task ID: {task_id}")

    preview_count = min(3, len(sentence_entries))
    print(f"\nSentence preview ({preview_count} of {len(sentence_entries)}):")
    for i, entry in enumerate(sentence_entries[:preview_count], 1):
        text_preview = entry["text"][:70] + ("..." if len(entry["text"]) > 70 else "")
        print(f"  [{i}] {text_preview}")
        print(f"       terms: {entry['search_terms']}")
    if len(sentence_entries) > preview_count:
        print(f"  ... ({len(sentence_entries) - preview_count} more sentences)")


if __name__ == "__main__":
    main()
