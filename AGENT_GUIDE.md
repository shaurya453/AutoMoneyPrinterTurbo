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
| `--terms` | `3` | `1`, `2`, `3` |

---

## Step 2 — Enrich Every Sentence (your main job)

`sentence_prep.py` produces stub search terms using keyword extraction. **Do not trust them.**
Read every sentence in `job.json` and set three fields yourself:

### `search_terms`

`search_terms` is a **3-tier hierarchy**, ordered from most-specific/hardest-to-find to most-generic/easiest-to-find:

- **`search_terms[0]` — niche**: the most specific imagery this sentence calls for — a particular task, action, product, person, or place named or strongly implied by the sentence.
- **`search_terms[1]` — medium**: a less specific but still closely-related fallback for the same idea — easier to find footage of than tier 0.
- **`search_terms[2]` — generic**: a broad, almost-always-available term that still fits the sentence's idea. This is the safety net.

The pipeline tries tier 0 first, falling back to tier 1 then tier 2 if a tier's candidates are missing, already used, or get rejected by the NSFW/relevance gates. **If video search exhausts all three tiers with nothing usable, the pipeline falls back to image search — which tries the same three terms in the same order.** So the tiers you write here govern *both* media types; you don't write separate terms for the image fallback.

**You decide, per sentence, whether tier 0 should actually be niche** — this is the same kind of judgment call as `media_type` below. Many sentences don't demand literal footage of the exact action described; ask: *would generic b-roll of the surrounding scene communicate this beat just as well?* If yes, write all three terms toward the medium/generic end instead of forcing a niche search that's unlikely to return anything.

**Example — niche term not warranted:**

> "I walked through the freezer aisle."

This doesn't require footage of someone specifically walking through a *freezer* aisle — generic supermarket b-roll of a person with a shopping cart communicates the idea just as well, and is far more likely to exist. Don't write `["person walking through freezer aisle", "frozen food aisle", "supermarket"]`; instead write something like:

```jsonc
"search_terms": ["shopper browsing frozen food aisle", "person pushing shopping cart supermarket", "grocery store interior"]
```

Here even tier 0 is already at the "medium" specificity — there's no sharper niche shot this sentence needs.

**Example — niche term warranted:**

| Sentence mentions | `search_terms[0]` (niche) | `search_terms[1]` (medium) | `search_terms[2]` (generic) |
|---|---|---|---|
| Kellogg's Chocos | `"Kellogg's Chocos box"` | `"chocolate cereal box"` | `"cereal box on shelf"` |
| Elon Musk | `"Elon Musk portrait"` | `"tech CEO interview"` | `"businessman portrait"` |
| iPhone 15 Pro | `"iPhone 15 Pro"` | `"Apple smartphone"` | `"smartphone close-up"` |
| Amazon rainforest | `"Amazon rainforest aerial"` | `"tropical rainforest canopy"` | `"forest aerial drone"` |
| Goldman Sachs (as a backdrop/scene) | `"Goldman Sachs building"` | `"Wall Street bank office"` | `"city office building exterior"` |

**Never write a vague tier 0** if you do choose to make it niche — a good tier 1/2 fallback does not excuse a weak tier 0. And don't pad tiers 1/2 with near-duplicates of tier 0 — each step down should be a genuinely easier/broader search than the one before it, or the fallback chain doesn't actually buy you anything.

Additional rules:
- **Action / process** → describe what the camera sees: `["surgeon operating room", "medical procedure", "hospital hallway"]`
- **Abstract concept** → find a concrete visual metaphor: don't use `"hope"`, use `"sunrise over city"`
- Max **3 words per term**
- **Static subjects** (products/SKUs, portraits, logos, screenshots, documents, charts, historical archive photos): always set `media_type` to `"image"` — stock photo libraries (especially Wikimedia Commons and DuckDuckGo) have real product shots and portraits that video libraries lack
- **Scenes, locations, and environments** (stores, restaurants, offices, landmarks used as a backdrop): set `media_type` to `"video"` — see below

### `visual_caption`

In addition to `search_terms` (which stay short, API-query-shaped, ≤3 words),
write a `visual_caption`: **one sentence describing what the camera should
show**, used to automatically rank candidate footage by relevance (see
"Automated Relevance Filter" below). `search_terms` and `visual_caption`
serve different purposes — don't just copy one into the other:

- `search_terms` are keywords fed to image/video search APIs.
- `visual_caption` is a natural-language description of the *shot itself*,
  scored against each downloaded candidate to pick the best match and reject
  off-topic results.

| Sentence | `search_terms` | `visual_caption` |
|---|---|---|
| "I walked into a Kroger" | `["Kroger storefront", "supermarket entrance", "supermarket interior"]` | `"wide shot of a supermarket interior with aisles and shoppers"` |
| "I bought a box of Kellogg's Chocos" | `["Kellogg's Chocos box", "chocolate cereal box", "cereal box on shelf"]` | `"a box of Kellogg's Chocos chocolate cereal on a shelf"` |
| "Their headquarters sits in downtown Seattle" | `["Amazon HQ Seattle", "downtown Seattle office buildings", "city office buildings aerial"]` | `"aerial view of downtown Seattle office buildings"` |

Write a `visual_caption` for **every** sentence, including ones with
`media_type: "image"` — it's used for image candidates too.

### Context anchoring — check every term against the video's subject

Before finalizing `search_terms`, set a `video_topic` field (see Job JSON
reference below) — a short phrase describing what this video is actually
about (e.g. `"ultra-processed food industry"`, `"weight loss diet trends"`,
`"smartphone manufacturing"`). Then, for **every** `search_terms` entry, ask:
**if I typed this term into an image/video search with no other context,
could the results plausibly come from a completely different domain than
this video's topic?**

Many common words are polysemous and will return wildly off-topic results on
their own. If a term is ambiguous, add a word grounded in the video's topic
to disambiguate it:

| Ambiguous term | Could wrongly return | Disambiguated with topic context |
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
generic single word with no topic anchor is a red flag during the Step 2.5
review pass below.

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
finds nothing for `search_terms`, the pipeline automatically retries with the
other type using the same terms. So picking `"video"` for a scene is
low-risk — worst case it falls back to an image.

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
locations/scenes, prefer `"video"` with `search_terms[0]` = the specific
named place + context (e.g. `"Kroger storefront"`), `search_terms[1]` = a
closer category fallback (e.g. `"supermarket entrance"`), and
`search_terms[2]` = a broad category video (e.g. `"supermarket interior"`).

Examples:

| Sentence | `media_type` | `search_terms` |
|---|---|---|
| "I walked into a Kroger" | `"video"` | `["Kroger storefront", "supermarket entrance", "supermarket interior"]` |
| "We grabbed lunch at a Chipotle" | `"video"` | `["Chipotle restaurant", "fast casual restaurant interior", "people eating restaurant"]` |
| "Their headquarters sits in downtown Seattle" | `"video"` | `["Amazon HQ Seattle", "downtown Seattle office buildings", "city office buildings aerial"]` |
| "The Eiffel Tower rose in 1889" (historical subject) | `"image"` | `["Eiffel Tower 1889 construction", "Eiffel Tower archive photo", "Paris historical photo"]` |
| "I bought a box of Kellogg's Chocos" | `"image"` | `["Kellogg's Chocos box", "chocolate cereal box", "cereal box on shelf"]` |
| "Tim Cook took the stage" | `"image"` | `["Tim Cook portrait", "tech CEO keynote", "businessman on stage"]` |

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
  "video_topic": "ultra-processed food industry", // YOU write this — short phrase describing the video's overall subject

  "sentences": [
    {
      "text": "The sentence as it appears in the script.",
      "search_terms": ["niche term", "medium term", "generic term"],  // YOU write these — 3-tier hierarchy, see above
      "visual_caption": "what the camera should show, one sentence", // YOU write this
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
- **Cold-reader check**: if I typed `search_terms[0]` into an image/video search with zero knowledge of this video's subject, would the results plausibly match this sentence? If there's a real chance of an unrelated-domain result (sports, music, a different industry, etc.), add a word from `video_topic` to anchor it.
- **Tier-hierarchy check**: do `search_terms[1]` and `[2]` actually get progressively easier/broader than `[0]`, or are all three near-duplicates? And did I genuinely consider whether tier 0 needs to be niche at all for this sentence — or would a medium/generic shot already cover it (the "freezer aisle" case)?
- **Image-ratio check**: count how many sentences ended up with `media_type: "image"`. If it's noticeably above ~20-25% of the total, revisit the weakest "image" calls and switch them to "video" with a generic-category term.

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
                    ├─ preferred=video → Pexels/Pixabay search per search_terms[0..2]
                    │     → each candidate downloaded, NSFW-gated, relevance-margin checked
                    │     → tries terms[0] then [1] then [2] until one passes
                    │     │     └─ nothing passes any tier → fall back to image (same search_terms, same tier order)
                    │     └─ pass → trim to duration
                    └─ preferred=image → DuckDuckGo/Wikimedia/Pexels/Pixabay/Unsplash per search_terms[0..2]
                          → same NSFW + relevance-margin checks, same tier order → Ken Burns render
                          └─ nothing passes any tier → fall back to video (same search_terms)
                    (both directions dedupe against used_urls from earlier sentences)
                          └─► combine_videos() sequential + xfade crossfade
                                └─► subtitle.srt (edge_tts timing or whisper fallback)
                                      └─► generate_video() → final.mp4
                                            (subtitles burned in, BGM ducked and mixed)
```

---

## Automated Relevance Filter (defense-in-depth)

The pipeline scores candidate images and footage against each sentence's
`visual_caption` (or `search_terms[0]` if missing) **combined with
`video_topic`** — `video_topic` is always appended, even when
`visual_caption` is present. For video, 3-5 frames are sampled from each
candidate and pooled. A candidate is accepted only if its score beats a set
of "junk" anchors (watermark/text-overlay, unrelated stock photo,
blurry/low-quality, explicit content) by a configurable margin — there is no
fixed global threshold, so the filter adapts to how strong each sentence's
candidate pool is. This is a safety net for noisy search results — it is
**not** a substitute for writing a good, specific `visual_caption` and
topic-anchored `search_terms`. Garbage in, garbage out still applies: the
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
surprised"`. The same applies to `search_terms` — `"surprised"` alone is
almost always the wrong term; describe the on-topic action/scene the
surprise happens *during* (e.g. `"shopper reading product label"`).

**The NSFW pixel gate is a separate, mandatory safety layer** — every
downloaded image and every sampled video frame is scanned at the pixel level
(independent of CLIP/relevance) and hard-rejected if it contains exposed
nudity, regardless of how relevant it scored. This applies to every source
(DuckDuckGo, Wikimedia, Pexels, Pixabay, Unsplash, and video providers) and
every `media_type` — relevance scoring never overrides it.

---

## Common Mistakes to Avoid

- **Trusting the stub search terms** — rewrite all of them, they are a scaffold not a answer
- **Using abstract terms** — always picture what a camera lens would physically show
- **Leaving `media_type: "video"` for a static subject** (product, portrait, logo, document) — use `"image"` so the exact subject is searched for
- **Defaulting to `media_type: "image"` for a named scene/location** (store, restaurant, office) — a real photo of a specific storefront is usually a watermarked stock image; use `"video"` with a specific term + generic category fallback (e.g. `["Kroger storefront", "supermarket entrance", "supermarket interior"]`) instead
- **Forcing a niche `search_terms[0]` the sentence doesn't actually need** (e.g. "person walking through freezer aisle" for "I walked through the freezer aisle") — if generic b-roll of the surrounding scene communicates the beat, write all three tiers at the medium/generic end instead of a tier 0 that's unlikely to return anything
- **Writing all three `search_terms` as near-duplicates** — each tier should be a genuinely broader/easier search than the one before it, or the fallback chain never actually helps when tier 0 fails
- **Marking too many sentences as `"image"`** — footage should dominate; keep images to roughly ≤20-25% of sentences
- **Writing ambiguous single-word search terms with no topic anchor** (e.g. "scale", "score", "field") — these are the most common cause of off-topic results; ground them with a word from `video_topic`
- **Using bare emotion/reaction words** (e.g. "surprised", "shocked", "excited") as `search_terms` or in `visual_caption` — stock libraries are dominated by a handful of generic reaction tropes (pregnancy tests, lottery wins, opening gifts) that have nothing to do with the video's subject. `video_topic` is appended automatically but isn't always enough to outweigh a strong emotion match — describe the on-topic scene the reaction happens *during* instead
- **Writing a vague or generic `visual_caption`** (e.g. `"a video clip"`, `"relevant footage"`) — it must describe a specific shot, or it can't distinguish good candidates from bad ones
- **Treating `visual_caption` as a copy of `search_terms[0]`** — the caption describes the *shot* (composition, subject, setting), not a search query
- **Leaving `bgm_search_term` blank on emotional content** — music significantly improves impact
