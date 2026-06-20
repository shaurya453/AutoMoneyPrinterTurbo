# MoneyPrinterTurbo — Documentary Video Pipeline

A headless, CLI-driven pipeline that turns a plain-text script into a finished documentary-style video with narration, stock footage, subtitles, and background music. Designed to be driven by an AI agent (Claude Code, Codex, etc.) with no browser or GUI required.

---

## How it works

```
script.txt  →  sentence_prep.py  →  job.json  →  [agent enriches]  →  cli.py  →  final.mp4
```

1. **`sentence_prep.py`** splits your script into sentences and writes a job JSON with stub fields.
2. **An AI agent enriches** the job JSON — writing `visual_concepts`, `visual_caption`, `assigned_motif`, `content_track`, `media_type`, and top-level `video_topic` / `video_type` / `motif_palette`. See [`AGENT_GUIDE.md`](AGENT_GUIDE.md) for the full enrichment spec.
3. **`cli.py`** runs the full pipeline: TTS → timestamp alignment → clip/image fetch → assembly → subtitles → BGM → `final.mp4`.

---

## Portal integration

This pipeline is the backend render engine for the [portal](../portal/) web app. The portal's `portal-worker` process drives it via a two-phase flow:

```
Portal (user submits job)
  │
  ├─ Phase 1 — AI agent (Claude / Codex CLI)
  │     Reads AGENT_GUIDE.md, writes script.txt, runs sentence_prep.py,
  │     enriches job.json, prints:  JOB_JSON_PATH: /absolute/path/job.json
  │
  └─ Phase 2 — worker spawns cli.py
        venv/bin/python cli.py --job <path>
        On success, renames final.mp4 → <sanitized videoTitle>.mp4
```

**Worker checks:**
- `portal-web` serves the Next.js frontend on port 3000
- `portal-worker` polls `data/jobs.sqlite` for queued jobs every 5 s
- Both run under PM2 (`pm2 list` to check status)

**Payload decoding** (portal → job.json):

| Portal field | job.json field | Values |
|---|---|---|
| `orientation: "Landscape"` | `video_aspect` | `"16:9"` |
| `orientation: "Portrait"` | `video_aspect` | `"9:16"` |
| `subtitles: "Off"` | `subtitle_enabled` | `false` |
| `subtitles: "On"` | `subtitle_enabled` | `true` |
| `backgroundMusic: "Off"` | `bgm_file` | `"none"` |
| `backgroundMusic: "On"` | `bgm_file` | `"random"` |

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
venv/bin/python sentence_prep.py \
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

Open `storage/tasks/My Video/job.json` and set these fields. See [`AGENT_GUIDE.md`](AGENT_GUIDE.md) for the complete spec and worked examples.

**Top-level fields:**

| Field | Type | Description |
|---|---|---|
| `video_topic` | string | 2–6 word search anchor appended to every query (e.g. `"caffeine sleep disruption"`) |
| `video_type` | `"thematic"` \| `"named_entity"` | `"thematic"` tries bare concept first; `"named_entity"` always appends topic |
| `motif_palette` | array of strings | 1 entry per sentence — short scene descriptions that rotate across the video for visual variety |

**Per-sentence fields:**

| Field | Type | Description |
|---|---|---|
| `visual_concepts` | array of strings | 1–3 subject-free search terms, specific → broad (e.g. `["frozen food aisle", "shopper with cart"]`) |
| `visual_caption` | string | One-sentence shot description fed to CLIP relevance scoring |
| `assigned_motif` | string | Entry from `motif_palette`; no two consecutive sentences share the same motif |
| `content_track` | `"broll"` \| `"named"` | `"named"` routes to Google Images first (via Serper); `"broll"` uses stock video/image libraries |
| `media_type` | `"video"` \| `"image"` | Ignored for `"named"` track (always images); for `"broll"`, sets clip vs. still |

### Step 4 — Run the pipeline

```bash
venv/bin/python cli.py --job "storage/tasks/My Video/job.json"
```

Add `--log-level DEBUG` for verbose output. On success, the path to `final.mp4` is printed as JSON.

---

## Output

Output is written to `storage/tasks/<Video Title>/`:

| File | Description |
|---|---|
| `<Video Title>.mp4` | Finished video (renamed from `final.mp4` by the worker) |
| `final.mp4` | Same file when run standalone (not via worker) |
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

To render without subtitles, set `"subtitle_enabled": false` in the job JSON.

---

## Agentic use

This pipeline is built to be driven by an AI agent. Read [`AGENT_GUIDE.md`](AGENT_GUIDE.md) for the full workflow, including:

- How to construct the `visual_concepts` query ladder
- The `motif_palette` / `assigned_motif` visual variety system
- When to use `content_track: "named"` vs `"broll"` and images vs. video
- The `video_type` decision rule (`"thematic"` vs `"named_entity"`)
- How to write `visual_caption` for CLIP relevance scoring

The Phase 1 system prompt used by the portal worker lives at `../portal/data/global-system-prompt.txt`.

---

## Stack

| Component | Library |
|---|---|
| TTS | [edge-tts](https://github.com/rany2/edge-tts) |
| Timestamp alignment | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) |
| Video assembly | [MoviePy 2](https://github.com/Zulko/moviepy) + FFmpeg |
| Stock video | Pexels, Pixabay, Coverr |
| Stock images | DuckDuckGo, Wikimedia Commons, Pexels Photos, Pixabay Images, Unsplash, Serper (Google Images) |
| BGM | Pixabay Music (online) or user-supplied MP3s in `resource/songs/` |
| Subtitles | Pillow (burned-in), Inter SemiBold |
| Footage safety | NudeNet ONNX (NSFW hard-reject gate) |
| Footage relevance | CLIP ViT-B/32 ONNX (cosine similarity vs. junk anchors, 1× embed per image) |
