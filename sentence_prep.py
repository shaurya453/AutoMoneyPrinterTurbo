"""
sentence_prep.py — sentence splitter + search-term extractor

Splits a plain-text script into sentences, assigns stock-footage search terms,
media type (video/image), and optional pan direction for each sentence, then
writes a complete job JSON template ready for cli.py.

Two modes:

  NLTK mode (default)
    Fast local extraction using stopword filtering and proper-noun detection.
    Good enough for a quick draft; terms should be reviewed before running.

    python sentence_prep.py --script script.txt --out job.json

  LLM mode  (--llm claude | openai)
    Sends all sentences in a single API call.  The model understands context
    and will set media_type="image" for specific products/people/places and
    produce visually concrete search terms.

    python sentence_prep.py --script script.txt --out job.json --llm claude
    python sentence_prep.py --script script.txt --out job.json --llm openai

    API keys are read from environment variables:
      ANTHROPIC_API_KEY  — required for --llm claude
      OPENAI_API_KEY     — required for --llm openai

    Override the model with --llm-model:
      --llm claude --llm-model claude-haiku-4-5-20251001   (default)
      --llm openai  --llm-model gpt-4o-mini                (default)
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
# NLTK search-term extraction (local, no API key needed)
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


def _nltk_enrich(sentences: list[str], n: int) -> list[dict]:
    _ensure_nltk_data()
    from nltk.corpus import stopwords as nltk_stopwords
    sw = set(nltk_stopwords.words("english"))
    entries = []
    for sent in sentences:
        entries.append({
            "text": sent,
            "search_terms": extract_search_terms(sent, sw, n=n),
            "media_type": "video",
        })
    return entries


# ---------------------------------------------------------------------------
# LLM enrichment — one API call for all sentences
# ---------------------------------------------------------------------------

_LLM_SYSTEM_PROMPT = """\
You are a visual research assistant for a documentary video pipeline.

For each sentence in the script, output a JSON object with:
  "search_terms": array of 1-2 short stock-footage/image search queries
  "media_type": "video" or "image"
  "pan_direction": "left"|"right"|"up"|"down" — ONLY include this key when media_type is "image"

Rules for search_terms:
- Use CONCRETE VISUAL NOUNS a camera could capture: "crowded Tokyo street", "surgeon operating room"
- For a specific named product, brand, person, or place: use its exact name as the first term
  (e.g. "iPhone 15 Pro", "Elon Musk", "Amazon headquarters Seattle")
- Second term should be a broader fallback: ["Cybertruck reveal event", "electric truck launch"]
- Never use abstract words like "innovation", "hope", "impact"
- Max 3 words per term

Rules for media_type:
- "image" when the sentence refers to a SPECIFIC thing a still photo captures better than stock footage:
    specific products, named people, historical events, maps, logos, screenshots, documents
- "video" for everything else: action, scenery, crowds, processes, generic categories

Rules for pan_direction (image only):
- "right": subject is on the left side of typical photos, pan toward it
- "left": subject is on the right side
- "up": tall subject — building, full-body portrait, banner
- "down": reveal from top — overhead shot, menu, document
- Omit the key entirely for centred/symmetrical subjects

Output ONLY a JSON array with one object per sentence, in the same order as the input.
No explanation, no markdown fences, just the raw JSON array.
"""

_LLM_USER_TEMPLATE = """\
Here are the sentences. Return one JSON object per sentence:

{sentences_json}
"""


def _llm_enrich_claude(sentences: list[str], model: str) -> list[dict]:
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")

    client = anthropic.Anthropic(api_key=api_key)
    user_msg = _LLM_USER_TEMPLATE.format(
        sentences_json=json.dumps(sentences, ensure_ascii=False, indent=2)
    )

    print(f"Calling Claude ({model}) for {len(sentences)} sentences…")
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_LLM_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()
    enriched = json.loads(raw)
    return _merge_llm_output(sentences, enriched)


def _llm_enrich_openai(sentences: list[str], model: str) -> list[dict]:
    import openai

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable is not set")

    client = openai.OpenAI(api_key=api_key)
    user_msg = _LLM_USER_TEMPLATE.format(
        sentences_json=json.dumps(sentences, ensure_ascii=False, indent=2)
    )

    print(f"Calling OpenAI ({model}) for {len(sentences)} sentences…")
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=4096,
    )
    raw = response.choices[0].message.content.strip()
    # OpenAI json_object mode wraps arrays — unwrap if needed
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        parsed = next(iter(parsed.values()))
    return _merge_llm_output(sentences, parsed)


def _merge_llm_output(sentences: list[str], llm_items: list) -> list[dict]:
    """Zip sentence text back with LLM-generated fields; validate each entry."""
    entries = []
    for i, sent in enumerate(sentences):
        item = llm_items[i] if i < len(llm_items) else {}
        entry = {"text": sent}

        terms = item.get("search_terms", [])
        entry["search_terms"] = [str(t) for t in terms] if terms else [sent[:40]]

        mt = item.get("media_type", "video")
        entry["media_type"] = "image" if str(mt).strip().lower() == "image" else "video"

        if entry["media_type"] == "image":
            pd = item.get("pan_direction")
            if pd and str(pd).strip().lower() in ("left", "right", "up", "down"):
                entry["pan_direction"] = str(pd).strip().lower()

        entries.append(entry)
    return entries


def _llm_enrich(provider: str, model: str | None, sentences: list[str]) -> list[dict]:
    defaults = {
        "claude": "claude-haiku-4-5-20251001",
        "openai": "gpt-4o-mini",
    }
    resolved_model = model or defaults.get(provider, "")

    if provider == "claude":
        return _llm_enrich_claude(sentences, resolved_model)
    if provider == "openai":
        return _llm_enrich_openai(sentences, resolved_model)
    raise ValueError(f"Unknown LLM provider: {provider!r}. Use 'claude' or 'openai'.")


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
        help="Search terms per sentence for NLTK mode (default: 2)",
    )
    p.add_argument(
        "--llm", default=None, choices=["claude", "openai"],
        help="Use an LLM to generate search terms and media types (overrides NLTK)",
    )
    p.add_argument(
        "--llm-model", default=None,
        help="Override the LLM model (default: claude-haiku-4-5-20251001 / gpt-4o-mini)",
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
    sentences = split_sentences(raw_text)
    print(f"Sentences detected: {len(sentences)}")

    # --- enrich sentences with search terms + media type ---
    if args.llm:
        try:
            sentence_entries = _llm_enrich(args.llm, args.llm_model, sentences)
            print(f"LLM enrichment complete ({args.llm})")
        except Exception as exc:
            print(f"Warning: LLM enrichment failed ({exc}), falling back to NLTK", file=sys.stderr)
            sentence_entries = _nltk_enrich(sentences, args.terms)
    else:
        sentence_entries = _nltk_enrich(sentences, args.terms)

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
        print(f"       terms: {entry['search_terms']}  type: {entry['media_type']}")
    if len(sentence_entries) > preview_count:
        print(f"  ... ({len(sentence_entries) - preview_count} more sentences)")


if __name__ == "__main__":
    main()
