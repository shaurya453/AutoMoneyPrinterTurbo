# Agent Guide — MoneyPrinterTurbo Documentary Pipeline

You are an AI agent operating this pipeline to produce long-form documentary videos.
Read this file before every run.

---

## How the Pipeline Works

```
script.txt  ──►  sentence_prep.py  ──►  job.json  ──►  [YOU ENRICH]  ──►  (worker runs cli.py)  ──►  final.mp4
```

1. **`sentence_prep.py`** splits the script into sentences and writes a job JSON with stub search terms.
2. **You read every sentence** and rewrite `search_terms` and `media_type` for each one.
3. **The worker** automatically runs `cli.py` after you print the `JOB_JSON_PATH:` marker. **Do not run cli.py yourself.**

---

## Step 1 — Generate the Job JSON

Place all per-job files inside `storage/tasks/<title>/` — **never in the project root**.

```bash
mkdir -p "storage/tasks/My Video"
# write the script to storage/tasks/My Video/script.txt first, then:
venv/bin/python sentence_prep.py --script "storage/tasks/My Video/script.txt" --out "storage/tasks/My Video/job.json" --title "My Video"
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

**The golden rule**: `search_terms[0]` must show the exact thing named in the sentence. `search_terms[1]` is the same-category fallback — what you'd show if the exact thing couldn't be found.

| Sentence mentions | `search_terms[0]` (exact) | `search_terms[1]` (category fallback) |
|---|---|---|
| Kellogg's Chocos | `"Kellogg's Chocos"` | `"chocolate cereal box"` |
| Elon Musk | `"Elon Musk portrait"` | `"tech CEO interview"` |
| iPhone 15 Pro | `"iPhone 15 Pro"` | `"Apple smartphone"` |
| Amazon rainforest | `"Amazon rainforest aerial"` | `"tropical rainforest canopy"` |
| Paris | `"Paris Eiffel Tower"` | `"European city landmark"` |
| Goldman Sachs | `"Goldman Sachs building"` | `"Wall Street bank office"` |

The pipeline tries term 0 first. Term 1 only runs if term 0 finds no unused footage. **Never write a vague term 0** — a good fallback in slot 1 does not excuse a weak slot 0.

Additional rules:
- **Action / process** → describe what the camera sees: `["surgeon operating room", "medical procedure"]`
- **Abstract concept** → find a concrete visual metaphor: don't use `"hope"`, use `"sunrise over city"`
- Max **3 words per term**
- For named products, brands, people, and places: always set `media_type` to `"image"` — stock photo libraries (especially Wikimedia Commons) have real product shots and portraits that video libraries lack

### `media_type`

Set `"image"` when a **still photo** captures it better than stock video:

| Use `"image"` for | Use `"video"` for |
|---|---|
| Named products and brands (any specific SKU/model) | Generic action (people walking, traffic) |
| Named people (portraits, headshots) | Processes (manufacturing, surgery) |
| Named companies (HQ building, logo) | Scenery and environments |
| Named cities / landmarks | Anything with continuous motion |
| Historical events / archive photos | Generic category b-roll |
| Maps, logos, documents, screenshots | |
| Artworks, charts, diagrams | |

**Default to `"image"` whenever a specific named entity is mentioned** — it is better to show a real photo of the exact thing than a generic video that happens to be in the same category.

Images are never cropped: each image renders as a centered inset (slowly
zooming from ~75% to ~82.5% of its "fit" size) over a blurred, darkened
copy of itself filling the rest of the frame. This means square and portrait
product/portrait photos always show their full content and compose well in a
16:9 frame — pick the most accurate image without worrying about its aspect
ratio or framing.

Image search order: **DuckDuckGo → Wikimedia Commons → Pexels Photos → Pixabay Images → Unsplash**
DuckDuckGo is checked first — it's a free, keyless broad open-web image search (similar to the old Google/Bing image search), giving the best chance of finding a real photo of the exact named product, brand, person, or place that `search_terms[0]` should describe. Wikimedia is the curated fallback for encyclopaedic subjects. The stock-photo providers (Pexels/Pixabay/Unsplash) are the category fallback when neither finds the exact term.

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
      "media_type": "video"               // YOU decide: "video" or "image"
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
  "font_name": "Inter_18pt-SemiBold.ttf",
  "text_fore_color": "#FFFFFF",
  "font_size": 30,
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

You can also place your own MP3s in `resource/songs/` and use `"bgm_file": "random"` to pick one at runtime.

BGM is automatically **ducked to 15%** during narration and rises back between sentences.

---

## Step 2.5 — Review pass (mandatory)

After enriching all sentences, re-read the **entire** sentences list as a quality audit — this is a different mental mode from writing. Ask for every sentence:
- Does `search_terms[0]` describe something a camera would physically show **for that specific sentence**?
- Did I write a generic fallback (`"business"`, `"technology"`, `"people"`) instead of a concrete visual?
- If a named person, product, or place appears again later in the script, does it have the same specific term it got the first time (consistency)?

Fix anything that looks weak. The review pass exists because the writing mode and the footage-quality audit mode catch different problems — the same sentence often looks fine when you write it but obviously vague when you read it cold.

---

## Step 3 — Done: Print the Path and Stop

Once job.json is fully enriched, print the path and stop. The worker takes over from here.

```
JOB_JSON_PATH: /home/deploy/AutoMoneyPrinterTurbo/storage/tasks/My Video/job.json
```

Do NOT run `cli.py`. The worker runs it automatically after detecting the marker above.

**The pipeline (`cli.py`) will write output to `storage/tasks/<task_id>/`:**

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
- **Leaving `bgm_search_term` blank on emotional content** — music significantly improves impact
