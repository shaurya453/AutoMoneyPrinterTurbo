"""
sentence_prep.py — sentence splitter and search-term scaffolder

Splits a plain-text script into sentences and writes a job JSON template.
Visual concepts are auto-extracted as a rough scaffold only — the agent
running this pipeline must rewrite them (and set media_type, content_track)
in Step 2 before calling cli.py.

A <<PLUG>>...<</PLUG>> marker pair in the script wraps the product-plug
paragraph; its sentences get "is_plug": true in the output and the marker
tokens themselves are stripped before sentence splitting.

Usage:
    python sentence_prep.py --script script.txt --out job.json
    python sentence_prep.py --script script.txt --out job.json \\
        --voice en-US-AriaNeural --aspect 16:9 --source pexels --terms 3
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
# Markdown cleanup
# ---------------------------------------------------------------------------

def strip_markdown(text: str) -> str:
    # Heading markers (##, ###, etc.) at the start of a line
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    # Bold/italic markers (**text**, *text*, __text__, _text_)
    text = re.sub(r"\*{1,3}|_{1,3}", "", text)
    # Bullet/list markers (-, *, + at the start of a line)
    text = re.sub(r"^[\-\*\+]\s+", "", text, flags=re.MULTILINE)
    # Inline code and code fences
    text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text)
    # Horizontal rules (--- or *** on their own line)
    text = re.sub(r"^\s*[-\*]{3,}\s*$", "", text, flags=re.MULTILINE)
    return text


# ---------------------------------------------------------------------------
# Product-plug marker
# ---------------------------------------------------------------------------

_PLUG_RE = re.compile(r"<<PLUG>>(.*?)<</PLUG>>", re.DOTALL | re.IGNORECASE)


def extract_plug_span(text: str) -> tuple[str, tuple[int, int] | None]:
    """Strip <<PLUG>>...<</PLUG>> marker tokens from text, returning the
    cleaned text and the (start, end) char span of the FIRST marked span's
    inner content within that cleaned text. Returns (text, None) if no marker
    is present. If more than one marker pair is found, only the first is used
    as the plug span, but every pair's tokens are stripped so no literal
    marker text survives into the script either way.
    """
    matches = list(_PLUG_RE.finditer(text))
    if not matches:
        return text, None

    if len(matches) > 1:
        print(
            f"Warning: {len(matches)} <<PLUG>> marker pairs found; only the "
            "first will be used as the plug block.",
            file=sys.stderr,
        )

    cleaned_parts = []
    plug_span = None
    cursor = 0
    for i, m in enumerate(matches):
        cleaned_parts.append(text[cursor:m.start()])
        inner_start = sum(len(p) for p in cleaned_parts)
        cleaned_parts.append(m.group(1))
        inner_end = inner_start + len(m.group(1))
        if i == 0:
            plug_span = (inner_start, inner_end)
        cursor = m.end()
    cleaned_parts.append(text[cursor:])
    return "".join(cleaned_parts), plug_span


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
        "--terms", type=int, default=3, choices=[1, 2, 3],
        help="Search terms per sentence (default: 3)",
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

    raw_text = strip_markdown(raw_text)
    raw_text, plug_span = extract_plug_span(raw_text)

    if not raw_text.strip():
        print("Error: script file is empty.", file=sys.stderr)
        sys.exit(1)

    _ensure_nltk_data()

    from nltk.corpus import stopwords as nltk_stopwords
    stopwords_set = set(nltk_stopwords.words("english"))

    if plug_span:
        start, end = plug_span
        before_sents = split_sentences(raw_text[:start])
        plug_sents = split_sentences(raw_text[start:end])
        after_sents = split_sentences(raw_text[end:])
        sentences = before_sents + plug_sents + after_sents
        plug_indices = set(range(len(before_sents), len(before_sents) + len(plug_sents)))
        print(f"Sentences detected: {len(sentences)} ({len(plug_sents)} marked as plug block)")
    else:
        sentences = split_sentences(raw_text)
        plug_indices = set()
        print(f"Sentences detected: {len(sentences)}")

    sentence_entries = []
    for idx, sent in enumerate(sentences):
        terms = extract_search_terms(sent, stopwords_set, n=args.terms)
        entry = {
            "text": sent,
            "visual_concepts": terms,
            "content_track": "broll",
            "media_type": "video",
        }
        if idx in plug_indices:
            entry["is_plug"] = True
        sentence_entries.append(entry)

    task_id = args.task_id or str(uuid.uuid4())

    job = {
        "task_id": task_id,
        "video_title": args.title or "",
        "video_script": raw_text.strip(),
        "sentences": sentence_entries,
        "voice_name": args.voice,
        "voice_rate": args.rate,
        "video_aspect": args.aspect,
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
        print(f"       terms: {entry['visual_concepts']}")
    if len(sentence_entries) > preview_count:
        print(f"  ... ({len(sentence_entries) - preview_count} more sentences)")


if __name__ == "__main__":
    main()
