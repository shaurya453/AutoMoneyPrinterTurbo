# MoneyPrinterTurbo — Documentary Video Pipeline

A headless, CLI-driven pipeline that turns a plain-text script into a finished documentary-style video with narration, stock footage, subtitles, and background music. Designed to be driven by an AI agent (Claude Code, Codex, etc.) with no browser or GUI required.

---

## How it works

```
script.txt  →  sentence_prep.py  →  job.json  →  [agent enriches]  →  cli.py  →  final.mp4
```

1. **`sentence_prep.py`** splits your script into sentences and writes a job JSON with stub search terms.
2. **You (or an AI agent) enrich** the job JSON — rewriting `search_terms`, setting `media_type` (`"video"` or `"image"`), and adding `pan_direction` for image shots.
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
| `jamendo_client_id` | [devportal.jamendo.com](https://devportal.jamendo.com/) | Optional — online BGM fetch |

Wikimedia Commons is also used as a final image fallback and needs no key.

> `config.toml` is gitignored and must never be committed — it contains your live API keys.

---

## Usage

### Step 1 — Write your script

Create a plain-text file with your script. Put all per-job files inside `jobs/<title>/`:

```bash
mkdir -p "jobs/My Video"
# write your script to jobs/My Video/script.txt
```

### Step 2 — Generate the job JSON

```bash
python sentence_prep.py \
  --script "jobs/My Video/script.txt" \
  --out    "jobs/My Video/job.json" \
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

Open `jobs/My Video/job.json` and for each sentence set:

- **`search_terms`** — what a stock camera would physically show (2 terms, max 3 words each)
- **`media_type`** — `"video"` for motion b-roll, `"image"` for specific products/people/places
- **`pan_direction`** — Ken Burns direction on image shots (`"left"`, `"right"`, `"up"`, `"down"`)

See [`AGENT_GUIDE.md`](AGENT_GUIDE.md) for detailed guidance, especially on when to use images vs. video.

### Step 4 — Run the pipeline

```bash
python cli.py --job "jobs/My Video/job.json"
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

The pipeline ships with ~30 royalty-free BGM tracks in `resource/songs/`. You can also fetch music from Jamendo by setting `bgm_search_term` in the job JSON:

```json
"bgm_search_term": "cinematic documentary score",
"bgm_file": "random"
```

BGM is automatically ducked during narration and rises back between sentences.

---

## Subtitles

Subtitles default to the **edge_tts** word-timing provider (fast, no extra model). Switch to **Whisper** for higher accuracy on dense or fast speech:

```toml
# config.toml
subtitle_provider = "whisper"
```

The Whisper model (`base` by default) is also used internally for sentence timestamp alignment regardless of the subtitle provider setting.

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
| Stock images | Pexels Photos, Pixabay Images, Unsplash, Wikimedia Commons |
| BGM | Jamendo (online) + local `resource/songs/` |
| Subtitles | Pillow (burned-in), Inter SemiBold |
