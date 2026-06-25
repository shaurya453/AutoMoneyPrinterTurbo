# Agent Guide — MoneyPrinterTurbo Documentary Pipeline

```
script.txt → sentence_prep.py → job.json → [YOU ENRICH] → (worker runs cli.py) → final.mp4
```

Read this file once, then enrich every sentence in `job.json`. Print `JOB_JSON_PATH:` when done. Do not run `cli.py`.

---

## Step 1 — Generate Job JSON

```bash
mkdir -p "storage/tasks/My Video"
venv/bin/python sentence_prep.py --script "storage/tasks/My Video/script.txt" \
  --out "storage/tasks/My Video/job.json" --title "My Video" --voice "<voice>" --aspect "16:9"
```

---

## Step 2 — Enrich Every Sentence

Set `video_topic`, `video_type`, and (thematic only) `motif_palette` at the job root. Then rewrite every sentence's fields — do not trust the stubs.

### `video_topic` and `video_type`

**`video_topic`** — 2–6 word phrase for the video's subject (e.g. `"ultra-processed food industry"`). Appended to search queries and to `visual_caption` when scoring candidates.

**`video_type`** — controls how `video_topic` is used in queries:

| `video_type` | When to use | Query behavior |
|---|---|---|
| `"thematic"` | Abstract idea, trend, or category — no single object represents it | Bare concept tried first; topic folded in only as fallback |
| `"named_entity"` | One specific recurring subject — brand, product, person | Topic appended to every concept query |

### `visual_concepts`

1–3 **subject-free local visual ideas**, specific → broad. "Subject-free" means do not repeat `video_topic`'s words — the pipeline appends it automatically. Each concept should be concrete and self-sufficient (3 words max).

The pipeline builds a query ladder: `[concept[0], concept[0] + topic, concept[1], concept[1] + topic, ..., topic]` for thematic; `[concept[0] + topic, ...]` for named_entity.

**Rules:**
- `[0]` must describe something a camera would physically show — no abstract nouns
- `[1]`/`[2]` must be genuinely broader than `[0]`, not near-duplicates
- For thematic videos, `[0]` must be self-sufficient without the topic appended
- Never force a niche `[0]` the sentence doesn't need — if generic b-roll communicates the beat, write toward the broader end
- Disambiguate single words that could match unrelated domains (e.g. `"court"` → `"courtroom interior"`, `"scale"` → `"kitchen scale"`)
- Disambiguate animal/insect names that are also brand names (e.g. `"firefly"` matches Firefly-branded LED bulbs; use `"firefly insect glowing"` or `"glowing beetle dark field"` instead). Same applies to `"jaguar"` (car), `"swift"` (programming language), `"python"` (software), etc.
- **Variety rule (critical for long videos):** the same visual_concepts pair must not appear on more than 2–3 sentences across the whole video. Actively track what you have already written — pick new scenes, angles, or settings as the script progresses. Never fall back to recycling a small set of generic concepts (`"shopper inspecting label"`, `"financial report pages"`, etc.) for sentences where the narration clearly calls for something more specific.

**Example (thematic, `video_topic = "grocery price inflation"`):**
- Sentence: "I walked through the freezer aisle."
- `visual_concepts`: `["frozen food aisle", "shopper with cart"]`
- Query ladder: `["frozen food aisle", "frozen food aisle grocery price inflation", "shopper with cart", ..., "grocery price inflation"]`

### `visual_caption`

One sentence describing the shot — used by the CLIP relevance filter to rank and reject candidates. `video_topic` is appended automatically when scoring.

- Describes the **shot** (composition, subject, setting) — not a search query
- Must be specific enough to distinguish good candidates from bad
- Write the reaction *into* the scene: `"a shopper looking surprised reading a discontinued label in a supermarket aisle"` — not `"a person looking surprised"`
- When the subject is an animal, insect, or organism that shares its name with a brand or product, name the biological category explicitly: `"a glowing firefly insect hovering above grass at night"` — not `"firefly light"` (matches Firefly-branded bulbs)
- Required on every sentence including `media_type: "image"` ones

| Sentence | `visual_concepts` | `visual_caption` |
|---|---|---|
| "I walked into a Kroger" | `["Kroger storefront", "supermarket interior"]` | `"wide shot of a supermarket interior with aisles and shoppers"` |
| "Tim Cook took the stage" | `["Tim Cook portrait"]` | `"Tim Cook speaking on a stage at a product event"` |

### `must_show` and `avoid` (optional)

Optional lists of plain-English keywords used to guide the VLM footage reviewer (when `vlm_verify_enabled = true` in config) and to pre-sort candidates by metadata match.

```jsonc
"must_show": ["shopping cart", "grocery aisle"],   // footage MUST contain these
"avoid": ["people's faces", "text overlays", "logos"]  // footage should avoid these
```

**When to set these:**
- Set `must_show` when a specific visual element is essential to the narration and generic b-roll would mislead (e.g. the sentence explicitly describes a shopping cart being pushed).
- Set `avoid` when the topic makes certain image types likely to appear but wrong (e.g. a finance documentary should avoid meme-style charts; a nature doc should avoid cartoon wildlife).
- Both fields are optional lists. Omit them (or set to `[]`) for most sentences — the VLM uses sensible defaults (avoid watermarks, text overlays, cartoons) when not set.
- Do NOT use these as a substitute for `visual_concepts` — they are a refinement on top of the search query, not the query itself.

### `content_track`

- **`"named"`** — `visual_concepts[0]` is a specific, uniquely identifiable entity that has a real-world name. Routes to Google Images (Serper) first. `media_type` is ignored for the primary fetch.
- **`"broll"`** — generic scene, action, category, or location type. Routes to stock video/image sources.
- **`"graphic"`** — animated motion-graphic segment (see Graphic Cues below).

**Decision rule:** if `visual_concepts[0]` is a *specific named thing that could be searched by name and return the right result*, use `"named"`. If it's a *category, scene, or general location type*, use `"broll"`.

#### Named entity types — use `"named"` for these

| Category | Examples | `visual_concepts[0]` pattern |
|---|---|---|
| **Person** | Elon Musk, Marie Curie, Steve Jobs, Barack Obama | `"<Full Name> portrait"` or `"<Full Name> speaking"` |
| **Branded product / SKU** | iPhone 15 Pro, Kellogg's Corn Flakes, Nike Air Max | `"<product name>"` |
| **Company / brand** | Tesla, Apple, NASA, OpenAI | `"<company> logo"` or `"<company> headquarters"` |
| **Specific vehicle model** | SR-71 Blackbird, Ford Mustang GT500, Space Shuttle Challenger | `"<vehicle name>"` |
| **Named aircraft / ship / spacecraft** | USS Enterprise, Titanic, Apollo 11 lunar module | `"<craft name>"` |
| **Named building / structure** | Eiffel Tower, Burj Khalifa, Empire State Building | `"<building name>"` |
| **Named location / landmark** | Times Square, Grand Canyon, Chernobyl exclusion zone | `"<landmark name>"` |
| **Named country / city (when identity matters)** | Tokyo skyline, Vatican City, Silicon Valley campus | `"<place name> skyline"` or `"<place name> aerial"` |
| **Historical event** | Apollo 11 moon landing, D-Day Normandy, Berlin Wall fall | `"<event name> photograph"` or `"<event name> footage"` |
| **Named document / law / report** | Magna Carta, Declaration of Independence, GDPR regulation | `"<document name>"` |
| **Named film / book / album / game** | Titanic 1997 film poster, Dark Side of the Moon album cover | `"<title> <medium>"` |
| **Named scientific concept with a known diagram** | DNA double helix diagram, Periodic Table of Elements | `"<concept> diagram"` |
| **Named organism (species with a common image)** | Giant Panda, Blue Whale, Great White Shark | `"<species name>"` — NOT combined with a commercial object |
| **Named artwork / photograph** | Mona Lisa painting, Earthrise NASA photograph | `"<artwork name> <artist/source>"` |
| **Logo / flag / emblem** | NASA logo, US flag, United Nations emblem | `"<entity> logo"` or `"<entity> flag"` |

#### When to stay on `"broll"` instead

| Scenario | Why `"broll"` | Example |
|---|---|---|
| Generic location type | Not a named place — no unique image exists | `"busy city street"`, `"hospital corridor"` |
| Named place as backdrop / atmosphere | Serper returns watermarked editorial photos; b-roll reads more naturally | `"modern office interior"` instead of `"Google office"` |
| Generic action involving a named thing | The action matters, not the specific entity | `"scientist examining sample"` not `"Marie Curie in lab"` (unless the script explicitly invokes her) |
| Any company's generic product category | Use category b-roll unless the specific brand is being named | `"electric car charging"` instead of `"Tesla charging"` when script says "an EV" |
| Named person doing a generic action | Only use `"named"` when their face/identity is the point | `"person giving keynote speech"` unless the speaker is the story |

#### Named entity examples

| Sentence | `visual_concepts[0]` | `content_track` |
|---|---|---|
| "Elon Musk unveiled the Cybertruck" | `"Elon Musk portrait"` | `"named"` |
| "Tesla dominates EV sales" | `"Tesla logo"` | `"named"` |
| "The SR-71 flew at Mach 3.2" | `"SR-71 Blackbird aircraft"` | `"named"` |
| "Apollo 11 touched down on the moon" | `"Apollo 11 moon landing photograph"` | `"named"` |
| "The Eiffel Tower was built in 1889" | `"Eiffel Tower Paris"` | `"named"` |
| "Raphaël Dubois discovered luciferase" | `"Raphaël Dubois scientist portrait"` | `"named"` |
| "I walked through the freezer aisle" | `"frozen food aisle"` | `"broll"` |
| "Cars lined up for miles" | `"cars in traffic jam"` | `"broll"` |
| "A scientist worked late into the night" | `"scientist in lab at night"` | `"broll"` |
| "The ship disappeared beneath the waves" | `"ship sinking ocean"` | `"broll"` |

Don't overuse `"named"` — it spends a Serper API call. Most sentences (70–80%) should be `"broll"`. For `named_entity` videos, use `"named"` only when the actual entity must be literally visible; use `"broll"` for atmosphere and background shots.

### Graphic Cues

Two patterns — choose the right one for each use case.

#### Pattern 1 — Structural graphic (silent, standalone)

A sentence slot with no narration. Plays in its own time window.

| Field | Value |
|---|---|
| `text` | `""` |
| `content_track` | `"graphic"` |
| `graphic_type` | see table below |
| `duration` | clip length in seconds |
| `variables` | type-specific key/value object |

Do NOT include this slot in `video_script`.

#### Pattern 2 — Narrated graphic (plays during VO)

The graphic is the visual for a normal narration sentence. Duration derived from Whisper — do NOT set `duration`. Requires `visual_concepts` and `visual_caption` as footage fallback if render fails.

| Field | Value |
|---|---|
| `text` | the narration sentence (non-empty) |
| `content_track` | `"broll"` or `"named"` |
| `graphic_type` | see table below |
| `variables` | type-specific key/value object |
| `visual_concepts` | required — footage fallback |
| `visual_caption` | required — footage fallback |

#### Supported `graphic_type` values

| `graphic_type` | What it renders | Required `variables` | Optional |
|---|---|---|---|
| `"title_card"` | Animated title on dark background | `title` (≤10 words) | `subtitle`, `style` |
| `"infographic"` | Animated chart with value labels | `title`, `labels` (string[]), `values` (number[]) | `unit`, `style` |
| `"transition"` | Section divider, fades in/out | `label` (≤4 words) | `sublabel`, `style` |
| `"list"` | Animated list — bullets, numbers, bars, or cards | `items` (string[], 2–6) | `title`, `style` |

#### Duration (Pattern 1 only)

| `graphic_type` | Duration |
|---|---|
| `"title_card"` | 5.0 s |
| `"infographic"` | 10.0 s (4+ data points); 8.0 s (2–3 data points) |
| `"transition"` | 3.0 s |
| `"list"` | 6 s (2–3 items); 8 s (4–5 items); 10 s (6 items) |

#### Style hint — when to use each variant

Add `"style": "<name>"` to request a specific variant. If omitted, the pipeline rotates through variants automatically to avoid consecutive repeats. Unknown style names fall back to rotation.

**`title_card` styles:**

| `style` | What you get | Best for |
|---|---|---|
| `"minimal"` | Title fades in centred on dark bg, subtitle fades below | Clean intros, general use, emotional quotes |
| `"kinetic"` | Title glides upward while fading in; white rule wipes beneath | High-energy openers, modern tech/finance tone |
| `"framed"` | Thin vertical bar draws down left edge; title and subtitle slide in | Structured documentary feel, multi-chapter videos |
| `"editorial"` | Title slides in from right; warm gold accent rule | Journalism, long-form investigative, historical |

**`infographic` styles:**

| `style` | What you get | Best for |
|---|---|---|
| `"bars"` | Vertical bar chart, staggered growth | Comparing values of similar magnitude; 3–8 categories |
| `"horizontal"` | Horizontal bars from a vertical axis | Long category labels that won't fit below a vertical bar |
| `"lollipop"` | Thin stems with a dot at the tip, spring pop | Sparse data where spacing matters; 3–8 categories |
| `"callouts"` | Large bold numbers count up from zero; category label beneath | 2–3 big standalone stats where the number IS the story |

**`transition` styles:**

| `style` | What you get | Best for |
|---|---|---|
| `"line"` | Short accent line grows from centre; label fades in | Minimal, general-purpose section break |
| `"sweep"` | Deep-navy panel sweeps in from left, sweeps out to right | Cinematic chapter turns, dramatic time jumps |
| `"brackets"` | L-shaped brackets draw in from opposing corners | Precision/technical tone, structured editorial |
| `"crosshair"` | Crosshair lines grow from centre, dim to near-invisible; text appears on grid | Data journalism, surveillance, investigative tone |

**`list` styles:**

| `style` | What you get | Best for |
|---|---|---|
| `"bullets"` | Coloured square bullets; rows slide in from left, staggered | General-purpose, unordered, up to 6 items |
| `"numbered"` | Cyan zero-padded numbers ("01.", "02."…); rows drop from above | Ordered steps, ranked lists, how-to sequences |
| `"cascade"` | Coloured bar sweeps full width behind each row; text fades on top | Dramatic reveals, highlight-reel style, up to 6 items |
| `"grid"` | Bordered card grid with accent number badge top-left | 4–6 items where you want equal visual weight per item |

#### When to insert a graphic cue

| Trigger | `graphic_type` | Rule |
|---|---|---|
| Explicit chapter/section heading in the script | `"title_card"` | **Required — always Pattern 2.** TTS speaks the heading; the title card graphic plays simultaneously. Keep in `video_script`. 1 per chapter heading. |
| Other major structural break — time jump, location, narrative phase | `"transition"` | Always Pattern 1 (silent). Never at first or last sentence. 0–2 per video. |
| Sentence states a single powerful, quotable fact | `"title_card"` | Pattern 1 or 2. Never at start or end of video. 0–1 per video. |
| Sentence compares ≥2 entities with specific numbers | `"infographic"` | Prefer Pattern 2. 0–1 per video (0–2 if genuinely distinct comparisons). |
| Sentence lists 2–6 distinct items, features, or steps | `"list"` | Prefer Pattern 2 when narrator reads items. 0–1 per video. |

**Total graphic entries: ≤5 per video. Minimum 1 if the script has any chapter/section headings.**

#### Handling list content split across multiple sentences

`sentence_prep.py` creates one stub sentence per item when the script enumerates points. The agent must **merge these back into a single list graphic** — they must NOT remain as separate broll entries.

**Recognition signs:** consecutive sentences that form a set ("First…", "Second…", "Another…", numbered/lettered items, or parallel structure where each sentence names one distinct thing).

**How to merge:**

1. Identify the **intro sentence** — the one that sets up the list (e.g. "Here are five ways to save money.").
2. Strip each item sentence to its core phrase (no "First, they…" preamble) and put all of them in `variables.items`.
3. **Delete the individual item sentences** from `sentences` entirely — do not leave them as broll entries.
4. Create **one** `list` graphic entry using the intro sentence:
   - **Pattern 2 (preferred):** set `graphic_type: "list"` on the intro sentence. The graphic plays while narrator reads the intro. Set `visual_concepts` and `visual_caption` as footage fallback.
   - **Pattern 1 (alternative):** keep the intro sentence as a plain broll entry and add a separate silent `"text": ""` list slot after it with `duration` set.

**Example — before (stubs from sentence_prep):**
```jsonc
{ "text": "Here are five ways ultra-processed foods hook consumers." },
{ "text": "First, they add excessive sugar." },
{ "text": "Second, they use artificial flavors." },
{ "text": "Third, they engineer the perfect crunch." },
{ "text": "Fourth, they hit the bliss point with fat and salt." },
{ "text": "Fifth, they make the packaging irresistible." }
```

**After (Pattern 2 — one entry, five item stubs removed):**
```jsonc
{
  "text": "Here are five ways ultra-processed foods hook consumers.",
  "content_track": "broll",
  "graphic_type": "list",
  "variables": {
    "title": "How Ultra-Processed Foods Hook You",
    "items": [
      "Excessive added sugar",
      "Artificial flavors",
      "Engineered crunch texture",
      "Fat-salt-sugar bliss point",
      "Irresistible packaging"
    ],
    "style": "bullets"
  },
  "visual_concepts": ["processed snack products shelf", "junk food close-up"],
  "visual_caption": "close-up of brightly coloured processed snack packaging on a supermarket shelf"
}
```

#### Graphic cue rules

- `title_card`: `title` ≤10 words. `subtitle` 3–6 words or `""`. For chapter headings, use the chapter name as `title` and `"chapter N"` as `subtitle` (e.g. `title: "A Glowing Obsession"`, `subtitle: "chapter two"`). Non-chapter title cards must be quotable and specific — not just interesting.
- `infographic`: `labels` and `values` must be same length. `values` must be positive. 2–8 data points. `title` = metric + scope (e.g. `"Global EV Sales (M units, 2023)"`). Use `"callouts"` for 2–3 standalone stats; use `"bars"`, `"horizontal"`, or `"lollipop"` for side-by-side comparisons. Use `"horizontal"` when any label is longer than ~3 words.
- `transition`: `label` ≤4 words, title case. `sublabel` ≤6 words, lower case, or omit. Only for real narrative pivots — not every paragraph break, and not for chapter headings (use `title_card` Pattern 2 for those).
- `list`: 2–6 items. Don't use for two items that differ numerically — use `"infographic"` with `"callouts"` style instead. Use `"numbered"` when order matters; `"grid"` for 4–6 equal-weight items; `"cascade"` for a dramatic reveal effect; `"bullets"` for general unordered lists. **Never leave each item as a separate narration sentence** — all items belong in `variables.items`; remove the individual item stubs from `sentences` after extracting them (see "Handling list content split across multiple sentences" above).

---

### `motif_palette` and `assigned_motif` (thematic videos only)

**`motif_palette`** (job root) — 4–8 distinct visual anchors covering the theme. Each motif must be visually distinct, concrete, and searchable on its own.

```jsonc
// credit card scams:
"motif_palette": [
  "ATM machine cash withdrawal",
  "phishing text message on phone",
  "hooded figure at laptop",
  "worried person reading bank statement",
  "padlock on credit card",
  "bank fraud alert notification"
]
```

**`assigned_motif`** (per sentence) — which motif drives this sentence's `visual_concepts`.
- Never assign the same motif to two consecutive sentences.
- Distribute palette as evenly as possible.
- Translate the motif into a specific scene description for `visual_concepts`.

| `assigned_motif` | `visual_concepts` | `visual_caption` |
|---|---|---|
| `"worried person reading bank statement"` | `["person inspecting bank statement at kitchen table", "reviewing financial documents"]` | `"close-up of a worried person scanning a bank statement at a kitchen table"` |
| `"hooded figure at laptop"` | `["hooded person typing on laptop in dark room", "cybercriminal at computer"]` | `"silhouette of a hooded figure hunched over a laptop in a dimly lit room"` |

---

### `media_type`

Per-sentence preference (`"video"` or `"image"`). If the preferred type finds nothing, the pipeline retries with the other. Ignored for `content_track: "named"` (always image via Serper first).

| Use `"image"` for | Use `"video"` for |
|---|---|
| Named products, brands, SKUs | Scenes, locations, environments |
| Named people (portraits) | Landmarks used as backdrop |
| Logos, screenshots, documents | Generic action (walking, traffic) |
| Maps, artworks, historical photos | Processes (manufacturing, surgery) |
| Microscopy, diagrams, archival stills | Nature in motion, machinery, crowds |

For places the narration is *set in or passing through*, use `"broll"` + `"video"` — a real storefront photo is usually watermarked; generic store interior video reads naturally as b-roll.

### `max_image_ratio` (job root)

Controls how much of the video can be still images. Set this at the job root based on content type — there is no pipeline-level cap; the agent's value is the only constraint.

| Content type | `max_image_ratio` | Rationale |
|---|---|---|
| Historical / archival (events, people, eras) | `1.0` | Best material is photographs; video b-roll would be generic filler |
| Scientific / nature documentary | `1.0` | Microscopy, diagrams, wildlife stills often beat generic b-roll |
| General thematic / explainer | `0.6` | Mix of b-roll and stills; video keeps it dynamic |
| Lifestyle / travel / action | `0.3` | Motion is the point; images feel static |
| Named entity (product, brand, person) | `1.0` | Default is already image; Serper delivers them |

These are starting points — adjust within a job if a particular section is unusually image-heavy or video-heavy. The ratio is enforced as a soft cap: when exceeded, the pipeline retries that clip as video before falling back to image.

---

## Step 2 — Full Job JSON (reference)

```jsonc
{
  "task_id": "auto-generated-uuid",
  "video_script": "Full narration only — no graphic text",
  "video_topic": "ultra-processed food industry",
  "video_type": "thematic",
  "max_image_ratio": 0.6,                                 // set per content type — see media_type section
  "motif_palette": ["ATM machine cash withdrawal", ...],  // thematic only

  "sentences": [
    // Narration sentence
    {
      "text": "The sentence as it appears in the script.",
      "visual_concepts": ["concrete scene description", "broader local idea"],
      "content_track": "broll",       // "named" | "broll" | "graphic"
      "visual_caption": "what the camera should show, one sentence",
      "media_type": "video",          // "video" | "image"
      "assigned_motif": "...",        // thematic only
      "must_show": [],                // optional — keywords footage MUST contain
      "avoid": []                     // optional — keywords footage should avoid
    },
    // Pattern 1 graphic (silent)
    {
      "text": "",
      "content_track": "graphic",
      "graphic_type": "transition",
      "duration": 3.0,
      "variables": { "label": "The Collapse", "sublabel": "2008" }
    },
    // Pattern 2 graphic (narrated — no duration)
    {
      "text": "China led with 8.1 million EVs, Europe 3.2 million, the US 1.4 million.",
      "content_track": "broll",
      "graphic_type": "infographic",
      "variables": {
        "title": "Global EV Sales (M units, 2023)",
        "labels": ["China", "Europe", "USA"],
        "values": [8.1, 3.2, 1.4],
        "unit": "M units",
        "style": "bars"
      },
      "visual_concepts": ["electric vehicle factory production line", "EV assembly plant"],
      "visual_caption": "rows of electric cars on a modern assembly line"
    }
  ],

  "voice_name": "en-US-AriaNeural",
  "voice_rate": 1.0,
  "video_aspect": "16:9",
  "video_source": "pexels",           // "pexels" | "pixabay"
  "subtitle_enabled": true,
  "subtitle_position": "bottom",
  "font_name": "Inter_18pt-SemiBold.ttf",
  "text_fore_color": "#FFFFFF",
  "font_size": 30,
  "stroke_color": "#000000",
  "stroke_width": 1.5,
  "bgm_search_term": "cinematic documentary score",
  "bgm_file": "random",
  "bgm_volume": 0.15
}
```

**BGM options:**
- Online fetch: `"bgm_search_term": "cinematic documentary score"`, `"bgm_file": "random"`
- Random local: `"bgm_search_term": ""`, `"bgm_file": "random"`
- No BGM: `"bgm_search_term": ""`, `"bgm_file": "none"`

Match `bgm_search_term` to the video's tone: `"tense thriller score"`, `"uplifting corporate background"`, `"melancholic piano"`, etc. Don't leave it generic for videos with a strong emotional arc.

---

## Step 2.5 — Review Pass (mandatory)

Re-read the entire sentences list as a quality audit:

- Does `visual_concepts[0]` describe something a camera would physically show for *that specific sentence*?
- **Thematic cold-reader check**: if `visual_concepts[0]` is searched alone (no topic), are results useful?
- **Named-entity cold-reader check**: if `visual_concepts[0]` + `video_topic` were typed into search, would results match?
- Does any `visual_concepts` entry repeat `video_topic`'s own words? It shouldn't.
- Do `[1]`/`[2]` get progressively broader than `[0]`, or are they near-duplicates?
- Is every specific named product/person/place marked `"named"`? Is every generic scene `"broll"`?
- Is `max_image_ratio` set at the job root and appropriate for the content type?
- **Motif-rotation check**: are any two consecutive sentences assigned the same motif? Swap if so.
- **Graphic review**: Pattern 1 has `text: ""` and `duration` set; Pattern 2 has real `text`, no `duration`, and `visual_concepts`/`visual_caption` set. Total ≤5 graphic entries.
- **Chapter heading check**: scan `video_script` for lines that look like headings (all-caps, "CHAPTER", "PART", "SECTION", numbered acts). Each one must have `graphic_type: "title_card"` set (Pattern 2) on its sentence entry — it must remain in `video_script` and keep its `text` so TTS speaks it.
- **Style check**: does each graphic's `style` match the content? Long labels → `"horizontal"` infographic; 4–6 list items with equal weight → `"grid"`; ordered steps → `"numbered"`; 2–3 standalone stats → `"callouts"`.
- **Concept-repetition check**: scan the entire sentences list for any visual_concepts pair that appears more than 3 times. Replace every overused entry with a distinct alternative that fits that sentence's specific narration beat.

---

## Step 3 — Print Path and Stop

```
JOB_JSON_PATH: /home/deploy/AutoMoneyPrinterTurbo/storage/tasks/My Video/job.json
```

Do NOT run `cli.py`. The worker runs it automatically.

---

## Common Mistakes

- **Trusting stub visual concepts** — rewrite all of them
- **Abstract `visual_concepts`** — describe what a camera lens physically shows; `"hope"` → `"sunrise over city"`
- **Repeating `video_topic` words in `visual_concepts`** — the pipeline appends topic automatically; repeating produces garbled queries
- **Ambiguous single-word concepts** — `"court"`, `"bar"`, `"scale"` match unrelated domains; always disambiguate
- **Brand-name collision on animal names** — `"firefly bulb"` returns Firefly-branded LED products; `"jaguar"` returns the car; use the biological category: `"firefly insect glowing"`, `"jaguar big cat"`, etc.
- **Chapter headings left as plain b-roll** — sentences like "CHAPTER TWO — A Glowing Obsession" must use Pattern 2 `title_card` (TTS speaks the heading, graphic plays simultaneously); they stay in `video_script` and keep their `text`; do NOT use Pattern 1 (which silences the VO)
- **Near-duplicate fallbacks** — `[1]`/`[2]` must be genuinely broader than `[0]`, not synonyms
- **Wrong `video_type`** — `"thematic"` for abstract/category videos; `"named_entity"` for one specific recurring subject
- **Overusing `"named"`** — each call spends a Serper quota; use for genuinely specific entities only
- **Using `"named"` for scenes/locations** — a real Kroger photo is usually watermarked; use `"broll"` + `"video"` instead
- **Generic emotion captions** — `"a person looking surprised"` matches off-topic stock; describe the on-topic scene the reaction happens *during*
- **Vague `visual_caption`** — `"a video clip"` or `"relevant footage"` can't distinguish good candidates from bad
- **Copying `visual_caption` from `visual_concepts`** — the caption describes the shot; `visual_concepts` are API queries
- **Leaving `bgm_search_term` blank on emotional content** — music significantly improves impact; match it to the tone
- **Graphic `text` not empty (Pattern 1)** — `text` must be `""` for silent graphic slots
- **Graphic content in `video_script`** — `video_script` is narration only; graphic slots have no spoken words
- **Setting `duration` on a Pattern 2 graphic** — duration is derived from Whisper; don't set it
- **Missing `visual_concepts`/`visual_caption` on Pattern 2 graphic** — these are the footage fallback if render fails
- **Title card at start or end of video** — mid-video only; pipeline handles fade-in/out at edges
- **Title card for a merely interesting sentence** — must be quotable, specific, and impactful; when in doubt, skip
- **Infographic for a single statistic** — use `title_card` or `"callouts"` style instead; infographics need ≥2 labeled values
- **Infographic with long labels but `"bars"` style** — use `"horizontal"` when labels exceed ~3 words; vertical bars clip label text
- **`list` for two numerically differing items** — use `"infographic"` with `"callouts"` style instead
- **`labels` and `values` arrays of different lengths** — must match exactly
- **More than 8 infographic data points** — too small to read; split if needed
- **Transition `label` longer than 4 words** — it's a section marker, not a sentence
- **Transition for every topic shift** — only for major structural breaks (time jump, location, narrative phase)
- **More than 5 graphic entries total** — cut to the most impactful ones; the hard limit is ≤5
- **Using `"grid"` style for fewer than 4 list items** — grid cards look sparse; use `"bullets"` or `"cascade"` for 2–3 items
- **Omitting `style` when content has a clear fit** — don't leave the pipeline to guess; if labels are long, write `"horizontal"`; if it's a step sequence, write `"numbered"`
