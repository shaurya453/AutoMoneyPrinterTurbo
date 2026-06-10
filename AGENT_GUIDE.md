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
| Paris (as a backdrop/scene) | `"Paris Eiffel Tower"` | `"European city aerial"` |
| Goldman Sachs (as a backdrop/scene) | `"Goldman Sachs building"` | `"Wall Street bank office"` |

The pipeline tries term 0 first. Term 1 only runs if term 0 finds no unused footage. **Never write a vague term 0** — a good fallback in slot 1 does not excuse a weak slot 0.

Additional rules:
- **Action / process** → describe what the camera sees: `["surgeon operating room", "medical procedure"]`
- **Abstract concept** → find a concrete visual metaphor: don't use `"hope"`, use `"sunrise over city"`
- Max **3 words per term**
- **Static subjects** (products/SKUs, portraits, logos, screenshots, documents, charts, historical archive photos): always set `media_type` to `"image"` — stock photo libraries (especially Wikimedia Commons and DuckDuckGo) have real product shots and portraits that video libraries lack
- **Scenes, locations, and environments** (stores, restaurants, offices, landmarks used as a backdrop): set `media_type` to `"video"` — see below

### `media_type`

`media_type` is a **preference**, not a hard requirement: if the chosen type
finds nothing for `search_terms`, the pipeline automatically retries with the
other type using the same terms. So picking `"video"` for a scene is
low-risk — worst case it falls back to an image.

Set `"image"` for **static subjects** — things that are inherently a single
flat object/photo, where stock video of them barely exists or just shows
someone holding/using them:

| Use `"image"` for | Use `"video"` for |
|---|---|
| Named products and brands (any specific SKU/model) | Scenes, locations, environments (stores, restaurants, offices) |
| Named people (portraits, headshots) | Landmarks/buildings used as a backdrop |
| Logos, screenshots, documents | Generic action (people walking, traffic) |
| Maps, artworks, charts, diagrams | Processes (manufacturing, surgery) |
| Historical events / archive photos | Anything with continuous motion |

**Why scenes should be video, even with a specific name**: a sentence like
"I walked into a Kroger" is describing a *place the narrator is in*, not
displaying a product. A real Kroger storefront photo from the open web is
very likely a heavily watermarked stock image — but a generic "supermarket
interior" stock *video* reads naturally as b-roll for the scene. For
locations/scenes, prefer `"video"` with `search_terms[0]` = the specific
named place + context (e.g. `"Kroger storefront"`) and `search_terms[1]` =
a generic category video (e.g. `"supermarket interior"`).

Examples:

| Sentence | `media_type` | `search_terms` |
|---|---|---|
| "I walked into a Kroger" | `"video"` | `["Kroger storefront", "supermarket interior"]` |
| "We grabbed lunch at a Chipotle" | `"video"` | `["Chipotle restaurant", "fast casual restaurant"]` |
| "Their headquarters sits in downtown Seattle" | `"video"` | `["Amazon HQ Seattle", "downtown office buildings"]` |
| "The Eiffel Tower rose in 1889" (historical subject) | `"image"` | `["Eiffel Tower 1889 construction", "Eiffel Tower archive photo"]` |
| "I bought a box of Kellogg's Chocos" | `"image"` | `["Kellogg's Chocos", "chocolate cereal box"]` |
| "Tim Cook took the stage" | `"image"` | `["Tim Cook portrait", "tech CEO keynote"]` |

**Default to `"image"` only for genuinely static subjects** — products,
portraits, logos, documents, archive photos. For places the narration is
*set in* or *passing through*, default to `"video"`.

Images are never cropped: each image renders as a centered inset (slowly
zooming from ~75% to ~82.5% of its "fit" size) over a blurred, darkened
copy of itself filling the rest of the frame. This means square and portrait
product/portrait photos always show their full content and compose well in a
16:9 frame — pick the most accurate image without worrying about its aspect
ratio or framing.

Image search order: **DuckDuckGo → Wikimedia Commons → Pexels Photos → Pixabay Images → Unsplash**
DuckDuckGo is checked first — it's a free, keyless broad open-web image search (similar to the old Google/Bing image search), giving the best chance of finding a real photo of the exact named product, brand, person, or place that `search_terms[0]` should describe. Known watermarked stock-photo domains (Shutterstock, iStock, Getty, Alamy, etc.) are filtered out of DuckDuckGo results automatically. Wikimedia is the curated fallback for encyclopaedic subjects. The stock-photo providers (Pexels/Pixabay/Unsplash) are the category fallback when neither finds the exact term.

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

The pipeline automatically dedupes clips and images against everything already
used earlier in the same run (`used_urls`), so reusing the same `search_terms[0]`
for a recurring entity is safe and still encouraged for consistency — the
pipeline will pick a different result for the repeat occurrence rather than
showing the identical clip/image twice.

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
              └─► for each sentence (media_type = preferred type):
                    ├─ preferred=video → Pexels/Pixabay search → download → trim to duration
                    │     └─ nothing found → fall back to image (same search_terms)
                    └─ preferred=image → DuckDuckGo/Wikimedia/Pexels/Pixabay/Unsplash → Ken Burns render
                          └─ nothing found → fall back to video (same search_terms)
                    (both directions dedupe against used_urls from earlier sentences)
                          └─► combine_videos() sequential + xfade crossfade
                                └─► subtitle.srt (edge_tts timing or whisper fallback)
                                      └─► generate_video() → final.mp4
                                            (subtitles burned in, BGM ducked and mixed)
```

---

## Common Mistakes to Avoid

- **Trusting the stub search terms** — rewrite all of them, they are a scaffold not a answer
- **Using abstract terms** — always picture what a camera lens would physically show
- **Leaving `media_type: "video"` for a static subject** (product, portrait, logo, document) — use `"image"` so the exact subject is searched for
- **Defaulting to `media_type: "image"` for a named scene/location** (store, restaurant, office) — a real photo of a specific storefront is usually a watermarked stock image; use `"video"` with a specific term + generic category fallback (e.g. `["Kroger storefront", "supermarket interior"]`) instead
- **Leaving `bgm_search_term` blank on emotional content** — music significantly improves impact
