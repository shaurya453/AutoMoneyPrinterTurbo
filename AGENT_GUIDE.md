# Agent Guide — MoneyPrinterTurbo Documentary Pipeline

You are an AI agent operating this pipeline to produce long-form documentary videos.
Read this file before every run.

---

## How the Pipeline Works

```
script.txt  ──►  sentence_prep.py  ──►  job.json  ──►  [YOU ENRICH]  ──►  cli.py  ──►  final.mp4
```

1. **`sentence_prep.py`** splits the script into sentences and writes a job JSON with stub search terms.
2. **You read every sentence** and rewrite `search_terms`, `media_type`, and `pan_direction` for each one.
3. **`cli.py`** runs the full pipeline: TTS → timestamp alignment → clip fetch → assembly → subtitles → final video.

---

## Step 1 — Generate the Job JSON

Place all per-job files inside `jobs/<title>/` — **never in the project root**.
The `jobs/` directory is gitignored so these files won't pollute the repo.

```bash
mkdir -p "jobs/My Video"
# write the script to jobs/My Video/script.txt first, then:
python sentence_prep.py --script "jobs/My Video/script.txt" --out "jobs/My Video/job.json" --title "My Video"
```

**Optional flags:**

| Flag | Default | Description |
|---|---|---|
| `--title` | *(none)* | Video title — used as the output folder name under `storage/tasks/`. Re-running with the same title creates `Title (2)`, `Title (3)`, etc. |
| `--voice` | `en-US-AriaNeural` | Any edge_tts voice name |
| `--rate` | `1.0` | `0.5` – `2.0` |
| `--aspect` | `16:9` | `16:9`, `9:16`, `1:1` |
| `--source` | `pexels` | `pexels`, `pixabay` |
| `--terms` | `2` | `1`, `2`, `3` |

---

## Step 2 — Enrich Every Sentence (your main job)

`sentence_prep.py` produces stub search terms using keyword extraction. **Do not trust them.**
Read every sentence in `job.json` and set three fields yourself:

### `search_terms`

Think: *what would a stock footage camera actually show for this sentence?*

- **Named product / brand / model** → use the exact name: `["iPhone 15 Pro", "Apple product launch"]`
- **Named person** → use their name: `["Elon Musk interview", "tech CEO portrait"]`
- **Named place** → use the place: `["Amazon rainforest aerial", "tropical deforestation"]`
- **Action / process** → describe what the camera sees: `["surgeon operating room", "medical procedure"]`
- **Abstract concept** → find a concrete visual metaphor: don't use `"hope"`, use `"sunrise over city"`
- Always give **2 terms**: a specific first term, a broader fallback second term
- Max **3 words per term**

### `media_type`

Set `"image"` when a **still photo** captures it better than stock video:

| Use `"image"` for | Use `"video"` for |
|---|---|
| Specific products (iPhone, car model) | Generic action (people walking, traffic) |
| Named people (portraits) | Processes (manufacturing, surgery) |
| Historical events / archive photos | Scenery and environments |
| Maps, logos, documents, screenshots | Anything with continuous motion |
| Artworks, charts, diagrams | Generic category b-roll |

### `pan_direction` (images only)

Controls the Ken Burns camera move. Set it whenever `media_type` is `"image"`:

| Value | When to use |
|---|---|
| `"right"` | Subject is on the left side of the frame — pan toward it |
| `"left"` | Subject is on the right side |
| `"up"` | Tall subject: building, full-body portrait, banner |
| `"down"` | Reveal from top: overhead shot, document, menu |
| *(omit)* | Centred or symmetrical subject — centre zoom only |

Image search order: **Pexels Photos → Pixabay Images → Unsplash → Wikimedia Commons**
Wikimedia needs no key and is best for historical/encyclopaedic subjects.

---

## Step 2 — Full Job JSON structure (reference)

```jsonc
{
  "task_id": "auto-generated-uuid",        // leave as-is
  "video_script": "Full script text...",   // do not edit

  "sentences": [
    {
      "text": "The sentence as it appears in the script.",
      "search_terms": ["term1", "term2"],  // YOU write these
      "media_type": "video",              // YOU decide: "video" or "image"
      "pan_direction": "right"            // YOU set on image sentences
    }
  ],

  // --- Voice ---
  "voice_name": "en-US-AriaNeural",
  "voice_rate": 1.0,

  // --- Video layout ---
  "video_aspect": "16:9",          // "16:9" | "9:16" | "1:1"
  "video_clip_duration": 5,        // not used by pipeline (kept for schema compat)
  "video_source": "pexels",        // "pexels" | "pixabay"

  // --- Subtitles ---
  "subtitle_enabled": true,
  "subtitle_position": "bottom",   // "bottom" | "top" | "center"
  "font_name": "Charm-Bold.ttf",
  "text_fore_color": "#FFFFFF",
  "font_size": 55,
  "stroke_color": "#000000",
  "stroke_width": 1.5,

  // --- Background music ---
  "bgm_search_term": "",           // search query for Jamendo online fetch
  "bgm_file": "random",            // fallback: "random" | "none" | filename
  "bgm_volume": 0.15               // 0.0 – 1.0
}
```

---

## Step 2 — Background Music

**To fetch music online (recommended):**
```jsonc
"bgm_search_term": "cinematic documentary score",
"bgm_file": "random"
```
Searches Pixabay Music using your existing `pixabay_api_keys`. Falls back to a random local file.

**Random local file:**
```jsonc
"bgm_search_term": "",
"bgm_file": "random"
```

**No BGM:**
```jsonc
"bgm_search_term": "",
"bgm_file": "none"
```

BGM is automatically **ducked to 15%** during narration and rises back between sentences.

---

## Step 3 — Run the Pipeline

```bash
python cli.py --job "jobs/My Video/job.json"
```

```bash
python cli.py --job "jobs/My Video/job.json" --log-level DEBUG   # verbose output
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

- **Trusting the stub search terms** — rewrite all of them, they are a scaffold not a answer
- **Using abstract terms** — always picture what a camera lens would physically show
- **Leaving `media_type: "video"` for a specific product or person** — use `"image"` so the exact subject is searched for
- **Forgetting `pan_direction` on portrait images** — default centre zoom looks static on tall subjects
- **Leaving `bgm_search_term` blank on emotional content** — music significantly improves impact
