"""
sentence_prep.py — local sentence splitter and search-term extractor

Splits a plain-text script into sentences, extracts stock-footage search
terms for each sentence using stopword filtering and proper-noun detection,
and writes a complete job JSON template ready for agent review.

The agent should review the output and:
  - Override search_terms for abstract or emotional sentences
  - Set media_type to "image" where still images fit better than footage
  - Optionally set pan_direction ("left" | "right" | "up" | "down") on image sentences

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

    # Normalise line breaks so paragraph breaks don't confuse the tokeniser
    text = re.sub(r"\r\n|\r", "\n", text)
    # Collapse multiple blank lines to a single paragraph break marker
    paragraphs = re.split(r"\n{2,}", text.strip())

    sentences = []
    for para in paragraphs:
        para = re.sub(r"\s+", " ", para).strip()
        if not para:
            continue
        sentences.extend(sent_tokenize(para))

    return [s.strip() for s in sentences if s.strip()]


# ---------------------------------------------------------------------------
# Search-term extraction
# ---------------------------------------------------------------------------

def _proper_noun_phrases(words_raw: list[str], stopwords_set: set) -> list[str]:
    """
    Detect consecutive capitalised words mid-sentence as proper-noun phrases.
    The first word of the sentence is always capitalised and is intentionally
    skipped to avoid treating every sentence opener as a proper noun.
    Returns a list of single words or multi-word phrases.
    """
    phrases = []
    i = 1  # skip sentence-initial word
    while i < len(words_raw):
        word = re.sub(r"[^\w]", "", words_raw[i])
        if (
            word
            and word[0].isupper()
            and word.lower() not in stopwords_set
            and len(word) > 2
        ):
            # Greedily consume consecutive caps to form a phrase
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

    # 1. Proper-noun phrases (highest priority — most specific for search)
    proper = _proper_noun_phrases(words_raw, stopwords_set)

    # 2. Common content words: 4+ chars, not a stopword
    all_words = re.findall(r"\b[a-zA-Z]{4,}\b", sentence)
    content = [w.lower() for w in all_words if w.lower() not in stopwords_set]

    # 3. Merge, deduplicate, cap at n
    seen: set[str] = set()
    terms: list[str] = []
    for item in proper + content:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            terms.append(item)
        if len(terms) == n:
            break

    # 4. Fallback: longest non-stopword words in the sentence
    if not terms:
        fallback = sorted(
            [w.lower() for w in re.findall(r"\b[a-zA-Z]{4,}\b", sentence)
             if w.lower() not in stopwords_set],
            key=len,
            reverse=True,
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
    # Windows cmd/PowerShell default to cp1252; reconfigure so Unicode previews print cleanly
    if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("cp"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = _build_parser()
    args = parser.parse_args()

    if not os.path.exists(args.script):
        print(f"Error: script file not found: {args.script}", file=sys.stderr)
        sys.exit(1)

    # utf-8-sig strips the BOM that Windows tools (PowerShell, Notepad) often prepend
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
            # pan_direction: omit here; agent sets it on image sentences if desired
        })

    task_id = args.task_id or str(uuid.uuid4())

    job = {
        "task_id": task_id,
        "video_script": raw_text.strip(),
        "sentences": sentence_entries,
        "voice_name": args.voice,
        "voice_rate": args.rate,
        "video_aspect": args.aspect,
        "video_clip_duration": 5,
        "subtitle_enabled": True,
        "font_name": "Charm-Bold.ttf",
        "text_fore_color": "#FFFFFF",
        "font_size": 55,
        "stroke_color": "#000000",
        "stroke_width": 1.5,
        "subtitle_position": "bottom",
        # bgm_search_term: set a search query to fetch BGM from Jamendo online.
        # Leave "" to fall back to bgm_file behaviour (random local or none).
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

    print("\nReview the output, then:")
    print("  - Override search_terms for abstract or emotional sentences")
    print("  - Set media_type to \"image\" where still images suit better")
    print("  - Optionally set pan_direction on image sentences")


if __name__ == "__main__":
    main()
