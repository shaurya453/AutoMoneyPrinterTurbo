# Agent Guide — MoneyPrinterTurbo Documentary Pipeline

You are an AI agent operating this pipeline to produce long-form documentary videos.
Read this file before every run.

---

## How the Pipeline Works

```
script.txt  ──►  sentence_prep.py  ──►  job.json  ──►  [YOU REVIEW/EDIT]  ──►  cli.py  ──►  final.mp4
```

1. **`sentence_prep.py`** splits the script into sentences, extracts search terms, and writes a job JSON template.
2. **You review and edit** the job JSON — override search terms, assign media types, set BGM.
3. **`cli.py`** runs the full pipeline: TTS → timestamp alignment → clip fetch → assembly → subtitles → final video.

---

## Step 1 — Generate the Job JSON

```bash
python sentence_prep.py --script script.txt --out job.json
```

**Optional flags:**

| Flag | Default | Options |
|---|---|---|
| `--voice` | `en-US-AriaNeural` | Any edge_tts voice name |
| `--rate` | `1.0` | `0.5` – `2.0` |
| `--aspect` | `16:9` | `16:9`, `9:16`, `1:1` |
| `--source` | `pexels` | `pexels`, `pixabay` |
| `--terms` | `2` | `1`, `2`, `3` |

---

## Step 2 — Review and Edit the Job JSON

This is your primary responsibility. The auto-extracted search terms are a starting point — you must improve them.

### Full Job JSON structure

```jsonc
{
  "task_id": "auto-generated-uuid",        // leave as-is or set your own
  "video_script": "Full script text...",   // do not edit

  // --- Per-sentence entries (one per sentence) ---
  "sentences": [
    {
      "text": "The sentence as it appears in the script.",
      "search_terms": ["term1", "term2"],  // EDIT THESE — see rules below
      "media_type": "video",              // "video" or "image"
      "pan_direction": "right"            // image-only: "left"|"right"|"up"|"down" (omit for centre zoom)
    }
  ],

  // --- Voice ---
  "voice_name": "en-US-AndrewNeural",
  "voice_rate": 1.0,

  // --- Video layout ---
  "video_aspect": "16:9",          // "16:9" | "9:16" | "1:1"
  "video_clip_duration": 5,        // not used by pipeline (kept for schema compat)
  "video_source": "pexels",        // stock video provider: "pexels" | "pixabay"

  // --- Subtitles ---
  "subtitle_enabled": true,
  "subtitle_position": "bottom",   // "bottom" | "top" | "center"
  "font_name": "Charm-Bold.ttf",
  "text_fore_color": "#FFFFFF",
  "font_size": 55,
  "stroke_color": "#000000",
  "stroke_width": 1.5,

  // --- Background music ---
  "bgm_search_term": "",           // set a search query to fetch BGM from Jamendo online
  "bgm_file": "random",            // fallback if bgm_search_term is empty: "random" | "none" | filename
  "bgm_volume": 0.15               // 0.0 – 1.0
}
```

---

## Step 2a — Search Term Rules

Search terms are sent directly to Pexels or Pixabay. Quality here directly determines visual quality.

**Do:**
- Use **concrete, visual nouns**: `"crowded city street"`, `"surgical procedure"`, `"1970s protest march"`
- Use **proper nouns** for named subjects: `"Amazon rainforest"`, `"Wall Street trading floor"`
- Use **2 terms per sentence** — the pipeline tries them in order and stops at the first that returns results
- Make the second term a **broader fallback**: `["Pinochet stadium", "political prisoners Chile"]`

**Don't:**
- Leave abstract emotional terms: `"fear"`, `"hope"`, `"the truth"` — replace them with concrete visuals
- Repeat the sentence text verbatim
- Use more than 3 words per term

---

## Step 2b — Media Type Rules

Set `"media_type": "image"` when the sentence requires a **specific still** that stock footage cannot provide:

- Historical photos, maps, portraits of real people, documents, artworks
- Diagrams, charts, logos, screenshots
- Any subject where motion would be distracting or inaccurate

Images are automatically animated with a **Ken Burns effect** (slow zoom + pan). Set `"pan_direction"` to guide the eye:

| Value | Use when |
|---|---|
| `"right"` | Subject is on the left, pan toward it |
| `"left"` | Subject is on the right |
| `"up"` | Revealing top of frame (e.g. tall building, full portrait) |
| `"down"` | Revealing bottom of frame |
| *(omit)* | Centre zoom only — good for symmetrical subjects |

Image search order: **Pexels Photos → Pixabay Images → Unsplash → Wikimedia Commons**
Wikimedia needs no key and is best for historical/encyclopaedic subjects.

---

## Step 2c — Background Music

**To fetch music online (recommended):**
```jsonc
"bgm_search_term": "cinematic documentary score",
"bgm_file": "random"   // used as fallback only
```
The pipeline searches Jamendo (free music, requires `jamendo_client_id` in `config.toml`) and downloads a matching track. Falls back to a random local file if the search fails.

**To use a random local file:**
```jsonc
"bgm_search_term": "",
"bgm_file": "random"
```

**To disable BGM:**
```jsonc
"bgm_search_term": "",
"bgm_file": "none"
```

BGM is automatically **ducked to 15% volume** during narration and rises back between sentences.

---

## Step 3 — Run the Pipeline

```bash
python cli.py --job job.json
```

```bash
python cli.py --job job.json --log-level DEBUG   # verbose output
```

**Output** is written to `storage/tasks/<task_id>/`:

| File | Description |
|---|---|
| `final.mp4` | Finished video with subtitles, BGM, narration |
| `audio.mp3` | TTS narration |
| `subtitle.srt` | Generated subtitle file |
| `combined.mp4` | Assembled clips before subtitle/BGM burn-in |
| `clips/clip-NNNN.mp4` | Per-sentence trimmed clips |

The path to `final.mp4` is printed as JSON to stdout on success.

---

## Pipeline Internals (for debugging)

```
TTS (edge_tts)
  └─► audio.mp3
        └─► faster-whisper (base, cpu) → per-sentence timestamps
              └─► for each sentence:
                    ├─ media_type=video → Pexels/Pixabay search → download → trim to duration
                    └─ media_type=image → Pexels/Pixabay/Unsplash/Wikimedia → Ken Burns render
                          └─► combine_videos() sequential + xfade crossfade
                                └─► subtitle.srt (edge_tts timing or whisper fallback)
                                      └─► generate_video() → final.mp4
                                            (subtitles burned in, BGM ducked and mixed)
```

---

## Common Mistakes to Avoid

- **Abstract search terms** — always visualise what the camera would show
- **Setting `media_type: "image"` for action scenes** — use video for anything with movement
- **Leaving `bgm_search_term` blank on emotional content** — music significantly improves impact
- **Not setting `pan_direction` on portrait images** — default centre zoom looks static on tall subjects
- **Skipping the job review step** — auto-extracted terms are adequate but not optimal; your edits are the difference between a generic and a polished video
