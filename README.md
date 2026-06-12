# MoneyPrinterTurbo — Documentary Video Pipeline

A headless, CLI-driven pipeline that turns a plain-text script into a finished documentary-style video with narration, stock footage, subtitles, and background music. Designed to be driven by an AI agent (Claude Code, Codex, etc.) with no browser or GUI required.

---

## How it works

```
script.txt  →  sentence_prep.py  →  job.json  →  [agent enriches]  →  cli.py  →  final.mp4
```

1. **`sentence_prep.py`** splits your script into sentences and writes a job JSON with stub `visual_concepts`.
2. **You (or an AI agent) enrich** the job JSON — rewriting `visual_concepts`, setting `content_track` (`"named"` or `"broll"`) and `media_type` (`"video"` or `"image"`), and writing a short `video_topic` that anchors every search query.
3. **`cli.py`** runs the full pipeline: TTS → timestamp alignment → clip/image fetch → assembly → subtitles → BGM → `final.mp4`.

---

## Requirements

- Python 3.11+
- FFmpeg on your system PATH (or set `ffmpeg_path` in `config.toml`)
- API keys for at least one stock media provider (see Configuration)

Install Python dependencies:

```bash
pip install -r requirements.txt
```

---

## Configuration

Copy the example config and fill in your API keys:

```bash
cp config.example.toml config.toml
```

| Key | Where to get it | Required? |
|---|---|---|
| `pexels_api_keys` | [pexels.com/api](https://www.pexels.com/api/) | Yes (or Pixabay) |
| `pixabay_api_keys` | [pixabay.com/api/docs](https://pixabay.com/api/docs/) | Yes (or Pexels) |
| `unsplash_api_keys` | [unsplash.com/developers](https://unsplash.com/developers) | Optional — image fallback |
| `serper_api_keys` | [serper.dev](https://serper.dev/) | Optional — Google Images for `content_track: "named"` sentences |

For `content_track: "broll"` sentences (the default), image searches try DuckDuckGo and Wikimedia Commons first (no key needed), then fall back to Pexels Photos, Pixabay Images, and Unsplash. For `content_track: "named"` sentences (specific products, people, places, events), Serper/Google Images is tried first, then the same fallback chain — see `named_track_image_source_order` in `config.toml`.

---

## Usage

### Step 1 — Write your script

Create a plain-text file with your script. Put all per-job files inside `storage/tasks/<title>/`:

```bash
mkdir -p "storage/tasks/My Video"
# write your script to storage/tasks/My Video/script.txt
```

### Step 2 — Generate the job JSON

```bash
python sentence_prep.py \
  --script "storage/tasks/My Video/script.txt" \
  --out    "storage/tasks/My Video/job.json" \
  --title  "My Video"
```

**Optional flags:**

| Flag | Default | Description |
|---|---|---|
| `--voice` | `en-US-AriaNeural` | Any [edge_tts voice](https://github.com/rany2/edge-tts) |
| `--rate` | `1.0` | Speech rate, `0.5`–`2.0` |
| `--aspect` | `16:9` | `16:9`, `9:16`, `1:1` |
| `--source` | `pexels` | `pexels` or `pixabay` |

### Step 3 — Enrich the job JSON

Open `storage/tasks/My Video/job.json` and set:

- **`video_topic`** (top-level) — a short (2-6 word) description of the video's subject. It's appended to every search query and used for relevance scoring, so it must read like a search term.
- **`visual_concepts`** (per sentence) — 1-3 subject-free local visual ideas, specific → broad (e.g. `["frozen food aisle", "shopper with cart"]`). The pipeline combines each with `video_topic` to build the actual search queries, with a bare-`video_topic` rung as a final fallback.
- **`content_track`** (per sentence) — `"named"` for a specific product/person/place/event (routed to Google Images first via Serper), or `"broll"` (default) for a generic conceptual scene (routed to stock video/image libraries).
- **`media_type`** (per sentence) — `"video"` for motion b-roll, `"image"` for stills. Ignored for `content_track: "named"`, which always searches images.

See [`AGENT_GUIDE.md`](AGENT_GUIDE.md) for detailed guidance, including worked examples of the query-ladder construction and the `content_track` decision rule.

### Step 4 — Run the pipeline

```bash
python cli.py --job "storage/tasks/My Video/job.json"
```

Add `--log-level DEBUG` for verbose output. On success, the path to `final.mp4` is printed as JSON.

---

## Output

Output is written to `storage/tasks/<Video Title>/`:

| File | Description |
|---|---|
| `final.mp4` | Finished video with subtitles, narration, and BGM |
| `audio.mp3` | TTS narration |
| `subtitle.srt` | Generated subtitle file |
| `combined.mp4` | Assembled clips before subtitle/BGM burn-in |
| `temp/clip-NNNN.mp4` | Per-sentence trimmed clips |

Re-running with the same title creates `My Video (2)`, `My Video (3)`, etc.

---

## Background music

Online BGM is fetched from Pixabay (no extra key — uses your existing `pixabay_api_keys`) when `bgm_search_term` is set in the job JSON. You can also drop your own MP3s into `resource/songs/` and use `"bgm_file": "random"` to pick one at runtime:

```json
"bgm_search_term": "cinematic documentary score",
"bgm_file": "random"
```

BGM is automatically ducked during narration and rises back between sentences.

To render without any background music, set `"bgm_file": "none"` (and leave `bgm_search_term` empty) in the job JSON.

---

## Subtitles

Subtitles are generated from the **faster-whisper** sentence-level timestamp alignment (`base` model by default — see `[whisper]` in `config.toml`), which is also what keeps each sentence's footage in sync with the narration.

---

## Agentic use

This pipeline is built to be driven by an AI agent. Read [`AGENT_GUIDE.md`](AGENT_GUIDE.md) for the full workflow, including how to write effective search terms, when to use images vs. video, and how to choose BGM.

---

## Stack

| Component | Library |
|---|---|
| TTS | [edge-tts](https://github.com/rany2/edge-tts) |
| Timestamp alignment | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) |
| Video assembly | [MoviePy 2](https://github.com/Zulko/moviepy) + FFmpeg |
| Stock video | Pexels, Pixabay |
| Stock images | DuckDuckGo, Wikimedia Commons, Pexels Photos, Pixabay Images, Unsplash, Serper (Google Images) |
| BGM | Pixabay Music (online) or user-supplied MP3s in `resource/songs/` |
| Subtitles | Pillow (burned-in), Inter SemiBold |
| Footage safety/relevance | NudeNet ONNX (NSFW gate), CLIP ViT-B/32 (relevance ranking) |
