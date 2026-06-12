# Agent Guide — MoneyPrinterTurbo Documentary Pipeline

You are an AI agent operating this pipeline to produce long-form documentary videos.
Read this file before every run.

---

## How the Pipeline Works

```
script.txt  ──►  sentence_prep.py  ──►  job.json  ──►  [YOU ENRICH]  ──►  (worker runs cli.py)  ──►  final.mp4
```

1. **`sentence_prep.py`** splits the script into sentences and writes a job JSON with stub visual concepts.
2. **You read every sentence** and rewrite `visual_concepts`, `content_track`, `visual_caption`, and `media_type` for each one.
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
| `--terms` | `3` | `1`, `2`, `3` |

---

## Step 2 — Enrich Every Sentence (your main job)

`sentence_prep.py` produces stub visual concepts using keyword extraction. **Do not trust them.**
Read every sentence in `job.json` and set these fields yourself:

### `visual_concepts`

`visual_concepts` is a list of **1-3 subject-free local visual ideas** for
this sentence, ordered from most-specific to broadest. "Subject-free" means:
**do not repeat the words from `video_topic`** (see "Context anchoring"
below) — the pipeline combines each concept with `video_topic` automatically
to build the actual search query, so repeating the subject here just
produces a redundant/garbled query like `"ultra-processed cereal box
ultra-processed food industry"`.

The pipeline builds a **query ladder** from `visual_concepts` + `video_topic`:

```
["{concept[0]} {video_topic}", "{concept[1]} {video_topic}", ..., video_topic]
```

(duplicates removed, in order). The final bare-`video_topic` rung is added
automatically as the safety net — **you never need to write a "generic
fallback" concept yourself**; the pipeline guarantees the subject is never
dropped from the query even when every concept you wrote fails.

- **`visual_concepts[0]`** — the most specific local visual idea this
  sentence calls for: a particular object, action, or scene detail.
- **`visual_concepts[1]`** (optional) — a related but broader local idea —
  easier to find footage of than `[0]`.
- **`visual_concepts[2]`** (optional) — a broad scene/category idea.

You decide, per sentence, how specific `visual_concepts[0]` should be — this
is the same kind of judgment call as `media_type` below. Many sentences
don't demand literal footage of the exact action described; ask: *would
generic b-roll of the surrounding scene communicate this beat just as well?*
If yes, write `visual_concepts` toward the broader end instead of forcing a
specific idea that's unlikely to return anything.

**Example — niche idea not warranted:**

> "I walked through the freezer aisle."

With `video_topic = "grocery price inflation"`, this doesn't require footage
of someone specifically walking through a *freezer* aisle — generic
supermarket b-roll communicates the idea just as well. Write:

```jsonc
"visual_concepts": ["frozen food aisle", "shopper with cart"]
```

which builds the query ladder:

```
["frozen food aisle grocery price inflation",
 "shopper with cart grocery price inflation",
 "grocery price inflation"]
```

Note that neither concept repeats "grocery"/"price"/"inflation" — `video_topic`
supplies that context to every rung automatically.

**Example — niche idea warranted:**

With `video_topic = "ultra-processed food industry"`:

| Sentence mentions | `visual_concepts` | Resulting query ladder |
|---|---|---|
| Kellogg's Chocos | `["Chocos cereal box"]` | `["Chocos cereal box ultra-processed food industry", "ultra-processed food industry"]` |
| Elon Musk | `["Elon Musk portrait", "tech CEO interview"]` | `["Elon Musk portrait ultra-processed food industry", "tech CEO interview ultra-processed food industry", "ultra-processed food industry"]` |
| Amazon rainforest | `["rainforest aerial", "tropical canopy"]` | `["rainforest aerial ultra-processed food industry", "tropical canopy ultra-processed food industry", "ultra-processed food industry"]` |

Note `video_topic`'s own words ("ultra-processed", "food", "industry") never
appear inside `visual_concepts` — only the *local* idea does.

Additional rules:
- **Action / process** → describe what the camera sees: `["surgeon operating room", "medical procedure"]`
- **Abstract concept** → find a concrete visual metaphor: don't use `"hope"`, use `"sunrise over city"`
- Max **3 words per concept**
- **Never write a vague `visual_concepts[0]`** — a good `[1]`/`[2]` does not excuse a weak `[0]`. And don't pad `[1]`/`[2]` with near-duplicates of `[0]` — each entry should be a genuinely broader idea than the one before it, or it doesn't actually buy you anything.

### `visual_caption`

In addition to `visual_concepts` (which stay short, API-query-shaped, ≤3
words, and subject-free), write a `visual_caption`: **one sentence
describing what the camera should show**, used to automatically rank
candidate footage by relevance (see "Automated Relevance Filter" below).
`visual_concepts` and `visual_caption` serve different purposes — don't just
copy one into the other:

- `visual_concepts` are local-idea keywords combined with `video_topic` to
  build search queries.
- `visual_caption` is a natural-language description of the *shot itself*,
  scored against each downloaded candidate to pick the best match and reject
  off-topic results. Like `visual_concepts`, `visual_caption` doesn't need to
  repeat `video_topic`'s words — the pipeline appends `video_topic` to the
  caption automatically when scoring.

| Sentence | `visual_concepts` | `visual_caption` |
|---|---|---|
| "I walked into a Kroger" | `["Kroger storefront", "supermarket interior"]` | `"wide shot of a supermarket interior with aisles and shoppers"` |
| "I bought a box of Kellogg's Chocos" | `["Chocos cereal box"]` | `"a box of Kellogg's Chocos chocolate cereal on a shelf"` |
| "Their headquarters sits in downtown Seattle" | `["downtown office buildings", "city skyline aerial"]` | `"aerial view of downtown Seattle office buildings"` |

Write a `visual_caption` for **every** sentence, including ones with
`media_type: "image"` — it's used for image candidates too.

### Context anchoring — `video_topic` and the query ladder

Before enriching sentences, set a `video_topic` field (see Job JSON
reference below) — a short phrase describing what this video is actually
about (e.g. `"ultra-processed food industry"`, `"weight loss diet trends"`,
`"smartphone manufacturing"`).

`video_topic` now does **two jobs**:

1. **Query anchoring** (new) — the pipeline appends `video_topic` to every
   `visual_concepts` entry to build the actual search query (see
   `visual_concepts` above). Because it's appended to *every* query,
   **`video_topic` must stay short and query-shaped — 2-6 words**. A long or
   sentence-like `video_topic` (e.g. "a documentary exploring how processed
   foods took over the American diet") will pollute every search query in
   the job and return nothing useful.
2. **Relevance scoring** (existing) — `video_topic` is appended to
   `visual_caption` when scoring candidates (see "Automated Relevance
   Filter" below).

**How to choose `video_topic`**: capture the video's primary subject/domain
(e.g. `"ultra-processed food industry"`), or — if the whole video centers on
one recurring named entity (a company, product line, person) — that entity
plus its domain (e.g. `"Tesla electric vehicles"`).

**Disambiguation is now a second line of defense.** Because `video_topic` is
appended to every query automatically, most ambiguous single words get
resolved for free — `"scale"` + `"ultra-processed food industry"` already
steers away from musical scales or fish scales. But pick `visual_concepts`
that don't *rely entirely* on this: if a concept is so generic that even
combined with `video_topic` it could mean several things, add a
disambiguating word to the concept itself:

| Ambiguous concept | Could wrongly return even with topic appended | Better concept |
|---|---|---|
| "scale" | musical scale, fish scale, scale model | `"kitchen scale"` / `"bathroom scale"` |
| "score" | sports scoreboard, sheet music | `"credit score chart"` |
| "field" | football field, academic field | `"wheat field aerial"` |
| "court" | basketball court, royal court | `"courtroom interior"` |
| "key" | piano key, house key | `"car key fob"` |
| "drive" | golf drive, hard drive | `"USB flash drive"` |
| "bar" | gym bar, music bar, law bar exam | `"chocolate bar"` / `"candy bar"` |

This isn't optional polish — it's the single biggest cause of a football
player or a page of sheet music showing up in an unrelated documentary. A
generic single-word concept that's still ambiguous after the topic is
appended is a red flag during the Step 2.5 review pass below.

### Worked example — full ladder construction

With `video_topic = "ultra-processed food industry"`:

| # | Sentence | `content_track` | `visual_concepts` | Query ladder tried in order |
|---|---|---|---|---|
| 1 | "I bought a box of Kellogg's Chocos" | `"named"` | `["Chocos cereal box"]` | `["Chocos cereal box ultra-processed food industry", "ultra-processed food industry"]` (Serper first) |
| 2 | "I walked through the freezer aisle" | `"broll"` | `["frozen food aisle", "shopper with cart"]` | `["frozen food aisle ultra-processed food industry", "shopper with cart ultra-processed food industry", "ultra-processed food industry"]` (stock video/image sources) |
| 3 | "Companies reformulate recipes to cut costs" | `"broll"` | `["food factory production line"]` | `["food factory production line ultra-processed food industry", "ultra-processed food industry"]` |

Notice the subject ("ultra-processed food industry") is present in **every**
query, and the final rung of every ladder is the bare topic — it can never be
dropped, no matter how broad the fallback gets.

`content_track` is explained in the next section.

### `content_track`

Every sentence has a `content_track`: `"named"` or `"broll"` (default
`"broll"` if omitted).

- **`"named"`** — this sentence's `visual_concepts[0]` names a **specific,
  identifiable entity**: a named product/brand/SKU, a named person, a named
  place/landmark, or a specific historical event. The pipeline routes
  `"named"` sentences to **Google Images (via Serper)** first — stock
  libraries (Pexels/Pixabay/Unsplash/Wikimedia) rarely carry the exact
  product box, the exact storefront, or the exact person, but Google Images
  often does. `media_type` is ignored for `"named"` sentences — the primary
  fetch is always an image search.
- **`"broll"`** (default) — generic conceptual/scene footage: actions,
  environments, abstract concepts, categories. Routed to the existing
  stock-video/image sources exactly as before, driven by `media_type`.

**Decision rule**: if you wrote `visual_concepts[0]` as a *specific named
thing* (a product box, a person's name, a named building/landmark), mark the
sentence `"named"`. If `visual_concepts[0]` is a *category or scene*
(supermarket aisle, factory floor, city street), leave it `"broll"`.

| Sentence | `visual_concepts[0]` | `content_track` |
|---|---|---|
| "I bought a box of Kellogg's Chocos" | `"Chocos cereal box"` | `"named"` |
| "I walked through the freezer aisle" | `"frozen food aisle"` | `"broll"` |
| "Elon Musk took the stage" | `"Elon Musk portrait"` | `"named"` |
| "The factory floor buzzed with activity" | `"factory floor"` | `"broll"` |
| "Their headquarters sits in downtown Seattle" (specific building) | `"Amazon HQ Seattle"` | `"named"` |

**Don't overuse `"named"`.** Each `"named"` sentence spends a Serper API call
(a finite, configured quota — see `serper_api_keys` in `config.toml`) and
always counts as an image for the `max_image_ratio` cap (the cap-avoidance
logic just doesn't apply to it — see "Pipeline Internals" below). Reserve
`"named"` for sentences where a generic stock photo/video genuinely wouldn't
represent the subject — most sentences should remain `"broll"`.

### `media_type`

**Default to `"video"`.** Footage carries a documentary; still images are
the exception, not the rule. As a soft target, **no more than roughly 1 in
4 sentences (≈20-25%) should end up as `media_type: "image"`**. After
enriching all sentences, count them. If the image fraction is higher, go
back through the borderline `"image"` sentences and ask whether a
generic-category *video* would actually work for that beat instead (e.g. a
"supermarket interior" video instead of a still photo of a product on a
shelf). The pipeline also enforces its own soft image-ratio cap as a
backstop, but don't rely on that — write the enrichment as if it were the
only safeguard.

`media_type` is a **preference**, not a hard requirement: if the chosen type
finds nothing for `visual_concepts`, the pipeline automatically retries with
the other type using the same query ladder. So picking `"video"` for a scene
is low-risk — worst case it falls back to an image.

**`media_type` is ignored for `content_track: "named"` sentences** — those
always fetch via Google Images (Serper) first, regardless of `media_type`.
For `"named"` sentences, `media_type` only determines which media type is
tried next if Serper and the rest of `named_track_image_source_order` all
fail.

Reserve `"image"` for **genuinely static subjects** — things that are
inherently a single flat object/photo, where stock video of them barely
exists or just shows someone holding/using them:

| Use `"image"` for | Use `"video"` for |
|---|---|
| Named products and brands (any specific SKU/model) | Scenes, locations, environments (stores, restaurants, offices) |
| Named people (portraits, headshots) | Landmarks/buildings used as a backdrop |
| Logos, screenshots, documents | Generic action (people walking, traffic) |
| Maps, artworks, charts, diagrams | Processes (manufacturing, surgery) |
| Historical events / archive photos | Anything with continuous motion |
| | Anything you're tempted to call "image" but isn't a single static object — when in doubt, use `"video"` |

**Why scenes should be video, even with a specific name**: a sentence like
"I walked into a Kroger" is describing a *place the narrator is in*, not
displaying a product. A real Kroger storefront photo from the open web is
very likely a heavily watermarked stock image — but a generic "supermarket
interior" stock *video* reads naturally as b-roll for the scene. For
locations/scenes, prefer `content_track: "broll"`, `media_type: "video"`,
with `visual_concepts[0]` = the specific named place (e.g. `"Kroger
storefront"`) and `visual_concepts[1]` = a broader category (e.g.
`"supermarket interior"`).

Examples:

| Sentence | `content_track` | `media_type` | `visual_concepts` |
|---|---|---|---|
| "I walked into a Kroger" | `"broll"` | `"video"` | `["Kroger storefront", "supermarket interior"]` |
| "We grabbed lunch at a Chipotle" | `"broll"` | `"video"` | `["Chipotle restaurant", "fast casual restaurant interior"]` |
| "Their headquarters sits in downtown Seattle" | `"broll"` | `"video"` | `["downtown office buildings", "city skyline aerial"]` |
| "The Eiffel Tower rose in 1889" (historical subject) | `"named"` | `"image"` | `["Eiffel Tower 1889 construction"]` |
| "I bought a box of Kellogg's Chocos" | `"named"` | `"image"` | `["Chocos cereal box"]` |
| "Tim Cook took the stage" | `"named"` | `"image"` | `["Tim Cook portrait"]` |

**Default to `"image"` (and `content_track: "named"`) only for genuinely
static, identifiable subjects** — products, portraits, logos, documents,
archive photos. For places the narration is *set in* or *passing through*,
default to `"broll"` + `"video"`.

Images are never cropped: each image renders as a centered inset (slowly
zooming from ~75% to ~82.5% of its "fit" size) over a blurred, darkened
copy of itself filling the rest of the frame. This means square and portrait
product/portrait photos always show their full content and compose well in a
16:9 frame — pick the most accurate image without worrying about its aspect
ratio or framing.

Image search order (for `content_track: "broll"`, `media_type: "image"`):
**DuckDuckGo → Wikimedia Commons → Pexels Photos → Pixabay Images →
Unsplash**. For `content_track: "named"` sentences, the order is **Google
Images (Serper) → DuckDuckGo → Wikimedia Commons → Pexels Photos → Pixabay
Images → Unsplash** (`named_track_image_source_order` in `config.toml`,
configurable). Serper is tried first for `"named"` sentences because it's the
most likely to return a real photo of the exact named product/person/place/
event. Known watermarked stock-photo domains (Shutterstock, iStock, Getty,
Alamy, etc.) are filtered out of both DuckDuckGo and Serper results
automatically.

---

## Step 2 — Full Job JSON structure (reference)

```jsonc
{
  "task_id": "auto-generated-uuid",        // leave as-is
  "video_script": "Full script text...",   // do not edit
  "video_topic": "ultra-processed food industry", // YOU write this — short (2-6 word) phrase describing the video's overall subject; appended to every search query AND to visual_caption when scoring candidates

  "sentences": [
    {
      "text": "The sentence as it appears in the script.",
      "visual_concepts": ["local idea", "broader local idea"],  // YOU write these — 1-3 subject-free local visual ideas, specific → broad, see above
      "content_track": "broll",           // YOU decide: "named" (specific product/person/place/event → Google Images) or "broll" (default, generic scene → stock sources)
      "visual_caption": "what the camera should show, one sentence", // YOU write this
      "media_type": "video"               // YOU decide: "video" or "image" (ignored when content_track = "named")
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
- Does `visual_concepts[0]` describe something a camera would physically show **for that specific sentence**?
- Did I write a generic fallback (`"business"`, `"technology"`, `"people"`) instead of a concrete visual?
- If a named person, product, or place appears again later in the script, does it have the same specific concept it got the first time (consistency)?
- **Cold-reader check**: if I combined `visual_concepts[0]` with `video_topic` and typed the result into an image/video search with zero other context, would the results plausibly match this sentence? If there's a real chance of an unrelated-domain result (sports, music, a different industry, etc.), make the concept more specific.
- **Concept-ladder check**: does any `visual_concepts` entry repeat `video_topic`'s own words? It shouldn't — the pipeline appends `video_topic` automatically, so repeating it here produces a redundant/garbled query. Do `visual_concepts[1]`/`[2]` (if present) actually get progressively broader than `[0]`, or are they near-duplicates?
- **`content_track` check**: is every specific named product/person/place/event marked `"named"`? Is every generic scene/category left as `"broll"` (not overused)?
- **Image-ratio check**: count how many sentences ended up with `media_type: "image"` (including `content_track: "named"` ones, which are always images). If it's noticeably above ~20-25% of the total, revisit the weakest "image"/"named" calls and consider whether a generic-category "broll" video would already cover it.

Fix anything that looks weak. The review pass exists because the writing mode and the footage-quality audit mode catch different problems — the same sentence often looks fine when you write it but obviously vague when you read it cold.

The pipeline automatically dedupes clips and images against everything already
used earlier in the same run (`used_urls`), so reusing the same
`visual_concepts[0]` for a recurring entity is safe and still encouraged for
consistency — the pipeline will pick a different result for the repeat
occurrence rather than showing the identical clip/image twice.

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
                    ├─ build query ladder: ["{concept} {video_topic}" for concept in visual_concepts] + [video_topic]
                    ├─ content_track = "named"
                    │     → Google Images (Serper) → DuckDuckGo → Wikimedia → Pexels → Pixabay → Unsplash, per query ladder
                    │     → each candidate downloaded, NSFW-gated, relevance-margin checked
                    │     └─ nothing passes → fall back to video using the same query ladder (broll sources)
                    └─ content_track = "broll" (default)
                          ├─ media_type = "video" → Pexels/Pixabay video search per query ladder
                          │     → each candidate downloaded, NSFW-gated, relevance-margin checked
                          │     └─ nothing passes → fall back to image (same query ladder, broll image sources)
                          └─ media_type = "image" → DuckDuckGo/Wikimedia/Pexels/Pixabay/Unsplash per query ladder
                                → same NSFW + relevance-margin checks → Ken Burns render
                                └─ nothing passes → fall back to video (same query ladder)
                    (all directions dedupe against used_urls from earlier sentences;
                     if everything still fails, the topic-wide pool of every
                     sentence's visual_concepts is tried as a last resort)
                          └─► combine_videos() sequential + xfade crossfade
                                └─► subtitle.srt (edge_tts timing or whisper fallback)
                                      └─► generate_video() → final.mp4
                                            (subtitles burned in, BGM ducked and mixed)
```

---

## Automated Relevance Filter (defense-in-depth)

The query ladder built from `visual_concepts` + `video_topic` determines
**what gets searched** — it does not determine what gets *kept*. Every
candidate returned by any provider, for any query in the ladder, still has
to pass the same NSFW and relevance checks below. A non-empty search result
is never auto-accepted.

The pipeline scores candidate images and footage against each sentence's
`visual_caption` (or `visual_concepts[0]` if missing) **combined with
`video_topic`** — `video_topic` is always appended, even when
`visual_caption` is present. For video, 3-5 frames are sampled from each
candidate and pooled. A candidate is accepted only if its score beats a set
of "junk" anchors (watermark/text-overlay, unrelated stock photo,
blurry/low-quality, explicit content) by a configurable margin — there is no
fixed global threshold, so the filter adapts to how strong each sentence's
candidate pool is. This is a safety net for noisy search results — it is
**not** a substitute for writing a good, specific `visual_caption` and
topic-anchored `visual_concepts`. Garbage in, garbage out still applies: the
filter can only choose among the candidates the search actually returns.

**Generic emotion/reaction captions are still a trap, even with the
`video_topic` anchor.** A `visual_caption` like `"a person looking
surprised"` for a video about disappearing grocery items will get combined
into `"a person looking surprised, disappearing grocery items from
supermarkets"` — but a clip of a woman reacting to a positive pregnancy test
can still score well against that combined prompt, because "a person looking
surprised" dominates the match and the topic words add only a small nudge.
**Write the reaction *into* the grocery scene** instead of describing the
emotion in isolation: `"a shopper looking surprised while reading a
discontinued label in a supermarket aisle"`, not `"a person looking
surprised"`. The same applies to `visual_concepts` — `"surprised"` alone is
almost always the wrong concept; describe the on-topic action/scene the
surprise happens *during* (e.g. `"shopper reading product label"`).

**The NSFW pixel gate is a separate, mandatory safety layer** — every
downloaded image and every sampled video frame is scanned at the pixel level
(independent of CLIP/relevance) and hard-rejected if it contains exposed
nudity, regardless of how relevant it scored. This applies to every source
(Google Images/Serper, DuckDuckGo, Wikimedia, Pexels, Pixabay, Unsplash, and
video providers) and every `media_type`/`content_track` — relevance scoring
never overrides it.

---

## Common Mistakes to Avoid

- **Trusting the stub visual concepts** — rewrite all of them, they are a scaffold not an answer
- **Using abstract terms** — always picture what a camera lens would physically show
- **Leaving `content_track: "broll"` (or unset) for a load-bearing named entity** (product, portrait, logo, document) — use `"named"` so the exact subject is searched for via Google Images
- **Defaulting to `content_track: "named"` for a generic scene** (store, restaurant, office) — a real photo of a specific storefront is usually a watermarked stock image; use `"broll"` + `"video"` with a specific concept + broader category fallback (e.g. `["Kroger storefront", "supermarket interior"]`) instead
- **Repeating `video_topic`'s words inside `visual_concepts`** (e.g. `video_topic = "ultra-processed food industry"`, `visual_concepts = ["ultra-processed cereal box"]`) — the pipeline appends `video_topic` to every query automatically; repeating it produces a redundant/garbled query. Write only the *local* idea.
- **Forcing a niche `visual_concepts[0]` the sentence doesn't actually need** (e.g. "person walking through freezer aisle" for "I walked through the freezer aisle") — if generic b-roll of the surrounding scene communicates the beat, write `visual_concepts` at the broader end instead of an idea that's unlikely to return anything
- **Writing all `visual_concepts` entries as near-duplicates** — each entry should be a genuinely broader idea than the one before it, or the fallback chain never actually helps when `[0]` fails
- **Marking too many sentences as `"image"`/`"named"`** — footage should dominate; keep images to roughly ≤20-25% of sentences
- **Writing an ambiguous `video_topic`** (too long, sentence-like, or so generic it doesn't anchor anything) — keep it 2-6 words and concrete; it's appended to every search query in the job
- **Using bare emotion/reaction words** (e.g. "surprised", "shocked", "excited") as `visual_concepts` or in `visual_caption` — stock libraries are dominated by a handful of generic reaction tropes (pregnancy tests, lottery wins, opening gifts) that have nothing to do with the video's subject. `video_topic` is appended automatically but isn't always enough to outweigh a strong emotion match — describe the on-topic scene the reaction happens *during* instead
- **Writing a vague or generic `visual_caption`** (e.g. `"a video clip"`, `"relevant footage"`) — it must describe a specific shot, or it can't distinguish good candidates from bad ones
- **Treating `visual_caption` as a copy of `visual_concepts[0]`** — the caption describes the *shot* (composition, subject, setting), not a search query
- **Leaving `bgm_search_term` blank on emotional content** — music significantly improves impact
