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

### `content_track`

- **`"named"`** — `visual_concepts[0]` names a specific identifiable entity (product/brand, person, landmark, historical event). Routes to Google Images (Serper) first. `media_type` is ignored for the primary fetch.
- **`"broll"`** — generic scene, action, or category. Routes to stock video/image sources.
- **`"graphic"`** — animated motion-graphic segment (see Graphic Cues below).

**Decision rule:** if `visual_concepts[0]` is a *specific named thing*, use `"named"`. If it's a *category or scene*, use `"broll"`.

| Sentence | `visual_concepts[0]` | `content_track` |
|---|---|---|
| "I bought a box of Kellogg's Chocos" | `"Chocos cereal box"` | `"named"` |
| "I walked through the freezer aisle" | `"frozen food aisle"` | `"broll"` |
| "Elon Musk took the stage" | `"Elon Musk portrait"` | `"named"` |

Don't overuse `"named"` — it spends a Serper API call. Most sentences should be `"broll"`. For `named_entity` videos, use `"named"` only when the actual entity must be literally visible; use `"broll"` for atmosphere and background shots.

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
| `"list"` | Bullet or numbered list | `items` (string[], 2–6) | `title`, `style` |

#### Duration (Pattern 1 only)

| `graphic_type` | Duration |
|---|---|
| `"title_card"` | 5.0 s |
| `"infographic"` | 10.0 s (4+ bars); 8.0 s (2–3 bars) |
| `"transition"` | 3.0 s |
| `"list"` | 6 s (2–3 items); 8 s (4–5); 10 s (6) |

#### Style hint (optional)

Add `"style": "<name>"` to request a specific variant. Unknown names fall back to auto-rotation.

| `graphic_type` | `style` | What you get |
|---|---|---|
| `title_card` | `"minimal"` | Simple fade-in, centred |
| `title_card` | `"kinetic"` | Title glides up, white rule wipe |
| `title_card` | `"framed"` | Left vertical bar frames title |
| `title_card` | `"editorial"` | Gold rule, slides from right — journalistic |
| `infographic` | `"bars"` | Vertical bar chart |
| `infographic` | `"horizontal"` | Horizontal bars — better for long labels |
| `infographic` | `"lollipop"` | Stem + dot chart |
| `infographic` | `"callouts"` | Large counting numbers — best for 2–3 big stats |
| `transition` | `"line"` | Accent line grows from centre |
| `transition` | `"sweep"` | Dark panel sweeps across screen |
| `transition` | `"brackets"` | Corner brackets frame label |
| `transition` | `"crosshair"` | Thin crosshair from centre |
| `list` | `"bullets"` | Coloured square bullets, slides from left |
| `list` | `"numbered"` | Cyan number prefix, slides from right |

#### When to insert a graphic cue

| Trigger | `graphic_type` | Rule |
|---|---|---|
| Explicit chapter/section heading in the script | `"title_card"` | **Required — always Pattern 2.** TTS speaks the heading; the title card graphic plays simultaneously. Keep in `video_script`. 1 per chapter heading. |
| Other major structural break — time jump, location, narrative phase | `"transition"` | Always Pattern 1 (silent). Never at first or last sentence. 0–2 per video. |
| Sentence states a single powerful, quotable fact | `"title_card"` | Pattern 1 or 2. Never at start or end of video. 0–1 per video. |
| Sentence compares ≥2 entities with specific numbers | `"infographic"` | Prefer Pattern 2. 0–1 per video (0–2 if genuinely distinct comparisons). |
| Sentence lists 2–6 distinct items, features, or steps | `"list"` | Prefer Pattern 2 when narrator reads items. 0–1 per video. |

**Total graphic entries: ≤5 per video. Minimum 1 if the script has any chapter/section headings.**

#### Graphic cue rules

- `title_card`: `title` ≤10 words. `subtitle` 3–6 words or `""`. For chapter headings, use the chapter name as `title` and `"chapter N"` as `subtitle` (e.g. `title: "A Glowing Obsession"`, `subtitle: "chapter two"`). Non-chapter title cards must be quotable and specific — not just interesting.
- `infographic`: `labels` and `values` must be same length. `values` must be positive. 2–8 bars. `title` = metric + scope (e.g. `"Global EV Sales (M units, 2023)"`). Use `"callouts"` for 2–3 standalone stats; bar styles for comparisons.
- `transition`: `label` ≤4 words, title case. `sublabel` ≤6 words, lower case, or omit. Only for real narrative pivots — not every paragraph break, and not for chapter headings (use `title_card` Pattern 2 for those).
- `list`: 2–6 items. Don't use for two items that differ numerically — use `"infographic"` instead. Add `"style": "numbered"` when order matters.

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

- **`video_type: "thematic"`** — default `"video"`. Keep images ≤25% of sentences.
- **`video_type: "named_entity"`** — default `"image"`. Stock video rarely carries the specific entity.

`media_type` is a preference — if the chosen type finds nothing, the pipeline retries with the other.
`media_type` is ignored for `content_track: "named"` sentences (always image via Serper first).

| Use `"image"` for | Use `"video"` for |
|---|---|
| Named products, brands, SKUs | Scenes, locations, environments |
| Named people (portraits) | Landmarks used as backdrop |
| Logos, screenshots, documents | Generic action (walking, traffic) |
| Maps, artworks, historical photos | Processes (manufacturing, surgery) |

For places the narration is *set in or passing through*, use `"broll"` + `"video"` — a real storefront photo is usually watermarked; generic store interior video reads naturally as b-roll.

---

## Step 2 — Full Job JSON (reference)

```jsonc
{
  "task_id": "auto-generated-uuid",
  "video_script": "Full narration only — no graphic text",
  "video_topic": "ultra-processed food industry",
  "video_type": "thematic",
  "motif_palette": ["ATM machine cash withdrawal", ...],  // thematic only

  "sentences": [
    // Narration sentence
    {
      "text": "The sentence as it appears in the script.",
      "visual_concepts": ["concrete scene description", "broader local idea"],
      "content_track": "broll",       // "named" | "broll" | "graphic"
      "visual_caption": "what the camera should show, one sentence",
      "media_type": "video",          // "video" | "image"
      "assigned_motif": "..."         // thematic only
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
        "unit": "M units"
      },
      "visual_concepts": ["electric vehicle factory production line", "EV assembly plant"],
      "visual_caption": "rows of electric cars on a modern assembly line"
    }
  ],

  "voice_name": "en-US-AriaNeural",
  "voice_rate": 1.0,
  "video_aspect": "16:9",
  "video_source": "pexels",
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

---

## Step 2.5 — Review Pass (mandatory)

Re-read the entire sentences list as a quality audit:

- Does `visual_concepts[0]` describe something a camera would physically show for *that specific sentence*?
- **Thematic cold-reader check**: if `visual_concepts[0]` is searched alone (no topic), are results useful?
- **Named-entity cold-reader check**: if `visual_concepts[0]` + `video_topic` were typed into search, would results match?
- Does any `visual_concepts` entry repeat `video_topic`'s own words? It shouldn't.
- Do `[1]`/`[2]` get progressively broader than `[0]`, or are they near-duplicates?
- Is every specific named product/person/place marked `"named"`? Is every generic scene `"broll"`?
- Are images ≤25% of total sentences? If over, revisit borderline calls.
- **Motif-rotation check**: are any two consecutive sentences assigned the same motif? Swap if so.
- **Graphic review**: Pattern 1 has `text: ""` and `duration` set; Pattern 2 has real `text`, no `duration`, and `visual_concepts`/`visual_caption` set. Total ≤5 graphic entries.
- **Chapter heading check**: scan `video_script` for lines that look like headings (all-caps, "CHAPTER", "PART", "SECTION", numbered acts). Each one must have `graphic_type: "title_card"` set (Pattern 2) on its sentence entry — it must remain in `video_script` and keep its `text` so TTS speaks it.

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
- **Leaving `bgm_search_term` blank on emotional content** — music significantly improves impact
- **Graphic `text` not empty (Pattern 1)** — `text` must be `""` for silent graphic slots
- **Graphic content in `video_script`** — `video_script` is narration only; graphic slots have no spoken words
- **Setting `duration` on a Pattern 2 graphic** — duration is derived from Whisper; don't set it
- **Missing `visual_concepts`/`visual_caption` on Pattern 2 graphic** — these are the footage fallback if render fails
- **Title card at start or end of video** — mid-video only; pipeline handles fade-in/out at edges
- **Title card for a merely interesting sentence** — must be quotable, specific, and impactful; when in doubt, skip
- **Infographic for a single statistic** — use `title_card` or `"callouts"` style instead; infographics need ≥2 labeled values
- **`list` for two numerically differing items** — use `"infographic"` with `"callouts"` style instead
- **`labels` and `values` arrays of different lengths** — must match exactly
- **More than 8 infographic bars** — too small to read; split if needed
- **Transition `label` longer than 4 words** — it's a section marker, not a sentence
- **Transition for every topic shift** — only for major structural breaks (time jump, location, narrative phase)
- **More than 3 graphic entries total** — cut to the most impactful ones
