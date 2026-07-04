# Agent Guide — MoneyPrinterTurbo Documentary Pipeline

```
script.txt → sentence_prep.py → job.json → [YOU ENRICH] → cli.py → final.mp4
```

Read this file once. Enrich every sentence in `job.json`. Print `JOB_JSON_PATH:` when done. Do not run `cli.py`.

---

## Step 1 — Generate Job JSON

```bash
mkdir -p "storage/tasks/My Video"
venv/bin/python scripts/sentence_prep.py --script "storage/tasks/My Video/script.txt" \
  --out "storage/tasks/My Video/job.json" --title "My Video" --voice "<voice>" --aspect "16:9"
```

---

## Step 2 — Enrich Every Sentence

Set job-root fields and patch each sentence. **Do not rebuild the array from scratch.**

> **CRITICAL — never clear `text`.** Whisper uses it for timestamp alignment. Blanking any `text` breaks the pipeline: 2-second placeholder clips, empty subtitles, 50+ gap-fill clips. Patch only the fields below.

---

### Job-level fields

**`video_topic`** — 2–6 word subject phrase (e.g. `"ultra-processed food industry"`). Appended to every search query automatically.

| `video_type` | When to use |
|---|---|
| `"thematic"` | Abstract idea, trend, or category |
| `"named_entity"` | One specific recurring subject — brand, product, person |

**`max_image_ratio`** (required):

| Content type | Value |
|---|---|
| Historical/archival, named-entity, scientific | `1.0` |
| General thematic / explainer | `0.6` |
| Lifestyle / travel / action | `0.3` |

**`motif_palette`** (thematic only) — 4–8 visually distinct, concrete, searchable anchors covering the theme.

---

### `visual_concepts`

1–3 concrete, **subject-free** local visual ideas, specific → broad (≤3 words each for broll; named entity identifiers may be longer). Do not include `video_topic` words — appended automatically.

**Rules:**
- `[0]` must describe what a camera physically shows — no abstract nouns (`"hope"` → `"sunrise over city"`)
- `[1]`/`[2]` must be genuinely broader than `[0]` — not synonyms or near-duplicates
- Disambiguate collisions: `"court"` → `"courtroom interior"`, `"bar"` → `"crowded bar interior"`, `"firefly"` → `"firefly insect glowing"`, `"jaguar"` → `"jaguar big cat"`, `"python"` → `"python snake coiled"`
- Same `[0]` on ≤2 consecutive sentences and ≤4 times total. Numbered variants (`"shelf angle 5"`, `"shelf angle 6"`) count as ONE — forbidden.
- **Pre-submission audit:** unique `[0]` count ≥ `max(15, ceil(N/4))`. For 100 sentences → ≥25 unique.

Match visuals to what the **specific sentence** is about — not the overall theme:
- Named product/person → use `content_track: "named"` (see below)
- Historical event → archival footage, vintage product, old manufacturing
- Corporate strategy → boardroom, business documents, executive meeting

---

### `visual_caption`

One sentence describing the shot — used by the CLIP relevance filter. `video_topic` appended automatically.

- Describes the **shot** (subject, setting, composition) — not a search query
- Specific enough to reject off-topic stock: `"a worried shopper reading a discontinued label in a supermarket aisle"` not `"a person looking surprised"`
- Required on every sentence. **No two sentences may share the same caption.**

---

### `content_track`

**Decision:** if you can Google the entity by exact name and expect the right image, use `"named"`. Otherwise use `"broll"`. **70–80% of sentences should be `"broll"`** — Serper quota is finite.

| Value | When | Source |
|---|---|---|
| `"named"` | Specific, uniquely identifiable entity | Google Images (Serper) first |
| `"broll"` | Generic scene, action, category, location | Stock video/image |

**Entity continuity:** once you assign `"named"` to an entity, keep it for every subsequent sentence still discussing that entity — even without repeating the name. Revert to `"broll"` only when the narration genuinely moves on.

| Sentence | `visual_concepts[0]` | `content_track` |
|---|---|---|
| "The KEF LS50 Meta costs twelve hundred dollars." | `"KEF LS50 Meta speaker pair"` | `"named"` |
| "It uses a Uni-Q driver array at its heart." | `"KEF LS50 Meta driver detail"` | `"named"` ← same entity |
| "Next up is the Focal Aria 906." | `"Focal Aria 906 speaker"` | `"named"` ← new entity |

**Use `"named"` for:** named people, branded products/SKUs, company logos/HQ, specific vehicles, named landmarks, historical events, named documents/laws, named artworks, distinctive species.

**Stay on `"broll"` for:** generic location types, real places as atmosphere (storefront photos are watermarked), generic actions, a company's generic product category.

For named products, anchor `[0]` to the specific version: `"Kellogg's Corn Pops original yellow box"` not `"Kellogg's Corn Pops"`. Add the key visual to `must_show`.

---

### `media_type`

| Use `"image"` for | Use `"video"` for |
|---|---|
| Named products, brands, portraits | Scenes, environments, locations |
| Logos, documents, historical photos | Actions, processes, crowds, nature in motion |

Ignored for `content_track: "named"` (always image). For real places, prefer `"broll"` + `"video"`.

---

### `visual_effect`

Motion overlay composited during rendering. **Target ≥40% of all broll/named sentences, distributed evenly across the entire video.** Assign where the effect genuinely accentuates what's on screen — do not front-load. Leave blank for neutral or transitional clips.

| Value | Overlay | When to use |
|---|---|---|
| `"threat"` | Blood splatter | Danger, conflict, violence, harm |
| `"cold"` | Snow particles | Tension, isolation, despair |
| `"warmth"` | Sun rays | Hope, triumph, joy, prosperity |
| `"mystery"` | Fog wisps | Eerie, unknown, conspiracy, dread |
| `"sepia"` | Film grain + dust scratches | Historic, archival, nostalgia |
| `"tech"` | Scan-line flicker | Digital, surveillance, data, systems |
| `"hacker_tech"` | Heavy glitch / scan-lines | Hacking, malicious tech |
| `"dream"` | Bokeh orbs | Memory, fantasy, aspiration |
| `"noir"` | Rain streaks | Crime, cynicism, moral decay |
| `"nature"` | Dust motes + light shafts | Ecology, growth, outdoors |
| `"revelation"` | Lens flare burst | Discovery, truth, turning point |

**Rules:**
- Never set on graphic sentences.
- **Never on two consecutive sentences** — always separate with ≥1 plain sentence.

---

### `must_show` / `avoid`

Keywords passed to the VLM reviewer. The VLM hard-rejects clips missing a `must_show` item.

- **`must_show` required when:** narration names a specific physical object, or sentence is `"named"` for a branded product (add product name + key visual)
- **`must_show` optional (`[]`):** general scene with no single mandatory element
- **`avoid`:** when the topic makes wrong image types likely (meme charts in a finance doc; cartoon wildlife in a nature doc)

---

### `assigned_motif` (thematic only)

Which `motif_palette` entry drives `visual_concepts`. Never the same on consecutive sentences. Distribute evenly. Translate into a specific scene for `visual_concepts`.

---

### Graphic Cues

Graphics are **narrated** — the graphic plays while the narrator speaks. Keep `text` intact and in `video_script`. Set `content_track` as normal (`"broll"` for generic context, `"named"` for a named entity). Do NOT set `duration`. Add `visual_concepts` and `visual_caption` as footage fallback.

**ALL graphic fields must be inside `"variables": {}`** — top-level `title`, `label`, etc. are silently ignored and the graphic renders blank.

| `graphic_type` | Required inside `variables` | Optional |
|---|---|---|
| `"lower_third"` | `label` (2–6 words, title case) | — |
| `"infographic"` | `title`, `labels[]`, `values[]` | `unit`, `style` |
| `"list"` | `items[]` (2–6 strings) | `title`, `style` |

| Type | Styles (omit to auto-rotate) |
|---|---|
| `lower_third` | single preset — no styles |
| `infographic` | `"bars"` · `"horizontal"` (labels >3 words) · `"lollipop"` · `"callouts"` (2–3 standalone stats) |
| `list` | `"bullets"` · `"numbered"` · `"cascade"` · `"grid"` (4–6 items only) |

**When to insert:**

| Type | Insert when | Hook zone |
|---|---|---|
| `lower_third` | Every `content_track: "named"` sentence after sentence 4 — **required** | BANNED |
| `infographic` | Narrator presents 2+ quantities the viewer must compare — numbers meaningless without visual | BANNED |
| `list` | Narrator enumerates parallel items where every item is one short phrase | BANNED |

**Placement rules:**
- `lower_third` is required on every `"named"` sentence after sentence 4 — do not skip.
- Infographics and lists are **supporting only** — insert when the narration cannot be understood without a visual. Never insert just because a number or list appears.
- **Combined infographic + list ≤ 8.**
- **No two infographic/list graphics within 3 sentences of each other.**

**Type-specific rules:**
- `lower_third`: `label` — entity name/identifier, no punctuation. Only on `"named"` sentences. Banned in sentences 0–4.
- `infographic`: `labels` and `values` same length; 2–8 data points; all `values` positive. `"callouts"` for 2–3 standalone stats. `"horizontal"` when any label >3 words.
- `list`: 2–6 items; single-sentence items only. For 2 numerically differing items, use `"infographic"` `"callouts"`. `"grid"` only for 4–6 items.

**Merging list stubs:** `sentence_prep.py` creates one stub per enumerated item. Merge them:
1. Extract each item to its core phrase → `variables.items`
2. **Delete every item stub sentence** — leave zero stubs
3. Set `graphic_type: "list"` on the intro sentence

---

## Hook Zone — Sentences 0–4

| Rule | Requirement |
|---|---|
| Graphics | All types banned |
| `media_type` on `broll` | `"video"` only — no images |
| `visual_concepts` | All 3 slots filled on every sentence |
| `visual_concepts[0]` | Distinct on every sentence — no repeats |
| `visual_effect` | ≥2 of the 5 sentences must carry an effect; no two consecutive |

**Effect placement example:**
```
sent 0: warmth
sent 1: —
sent 2: —
sent 3: revelation
sent 4: —
```

Use active, kinetic visual concepts — `"engineer soldering circuit board close-up"` beats `"technology office interior"`. Vary depth and scale across the 5 cuts.

---

## Job JSON Reference

```jsonc
{
  "task_id": "auto-generated-uuid",
  "video_script": "Narration only — no graphic text",
  "video_topic": "ultra-processed food industry",
  "video_type": "thematic",
  "max_image_ratio": 0.6,
  "motif_palette": ["ATM machine cash withdrawal", ...],  // thematic only

  "sentences": [
    // narration sentence
    {
      "text": "The sentence as written — NEVER modify.",
      "visual_concepts": ["concrete scene description", "broader fallback"],
      "content_track": "broll",         // "broll" | "named"
      "visual_caption": "one sentence describing the shot",
      "visual_effect": "",              // target ≥40% of broll/named sentences
      "media_type": "video",            // "video" | "image"
      "assigned_motif": "...",          // thematic only
      "must_show": [],
      "avoid": []
    },
    // lower_third example
    {
      "text": "The Sony WH-1000XM5 consistently tops every noise-cancelling chart.",
      "content_track": "named",
      "graphic_type": "lower_third",
      "variables": { "label": "Sony WH-1000XM5" },
      "visual_concepts": ["Sony WH-1000XM5 headphones", "premium over-ear headphones"],
      "visual_caption": "Sony WH-1000XM5 headphones on a clean white surface",
      "must_show": ["Sony WH-1000XM5"]
    },
    // infographic example
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
      "visual_concepts": ["electric vehicle factory", "EV assembly plant"],
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

Match `bgm_search_term` to tone: `"tense thriller score"`, `"uplifting corporate background"`, `"melancholic piano"`. Don't leave it generic on emotional content.

---

## Step 2.5 — Review Pass (mandatory)

**Hook Zone (sentences 0–4):**
- [ ] No graphics?
- [ ] All `broll` sentences have `media_type: "video"`?
- [ ] ≥2 effects, no two consecutive?
- [ ] All 3 `visual_concepts` slots filled?
- [ ] Distinct `visual_concepts[0]` on every sentence?

**Full video:**
- [ ] `[0]` physically showable for *that specific sentence* (not just the video theme)?
- [ ] No `video_topic` words in any `visual_concepts` entry?
- [ ] `[1]`/`[2]` genuinely broader than `[0]` — not synonyms?
- [ ] Every named entity marked `"named"`? Every generic scene `"broll"`?
- [ ] No two sentences share the same `visual_caption`?
- [ ] `max_image_ratio` set at job root?
- [ ] Unique `[0]` count ≥ `max(15, ceil(N/4))`?
- [ ] Effects ≥40% of broll/named sentences, distributed evenly — no two adjacent, no front-loading?
- [ ] All graphics narrated: real `text`, no `duration`, `visual_concepts`/`visual_caption` set?
- [ ] Every `content_track: "named"` sentence after sentence 4 has `graphic_type: "lower_third"`?
- [ ] Each infographic/list present because narration cannot be understood without it?
- [ ] Combined infographic + list ≤ 8? No two within 3 sentences of each other?
- [ ] `labels` and `values` same length on all infographics?
- [ ] All list item stubs deleted?
- [ ] No two consecutive sentences share `assigned_motif`? (thematic)

---

## Step 3 — Print Path and Stop

```
JOB_JSON_PATH: /home/deploy/AutoMoneyPrinterTurbo/storage/tasks/My Video/job.json
```

Do NOT run `cli.py`. The worker runs it automatically.

---

## Common Mistakes

- **Clearing `text`** — catastrophic; never touch it
- **Abstract `visual_concepts`** — `"hope"` → `"sunrise over city horizon"`
- **`video_topic` words in concepts** — pipeline appends them; repeating garbles queries
- **Ambiguous single words** — `"court"` → `"courtroom interior"`, `"bar"` → `"crowded bar interior"`
- **Brand/animal/software collisions** — `"firefly"` → `"firefly insect glowing"`, `"jaguar"` → `"jaguar big cat"`, `"python"` → `"python snake coiled"`
- **Numbered concept variants** — `"shelf angle 5"` ≡ `"shelf angle 6"` — forbidden
- **Near-duplicate `[1]`/`[2]`** — must widen the search, not restate `[0]`
- **Overusing `"named"`** — 70–80% should be `"broll"`; Serper quota is finite
- **`"named"` for real places** — storefront/landmark photos are watermarked; use `"broll"` + `"video"`
- **Duplicate `visual_caption`** — every sentence must have a unique caption
- **Generic `bgm_search_term`** — match to tone on emotional content
- **Graphic fields at the top level** — `title`, `style`, `label`, `items` MUST be inside `"variables": {}`; top-level fields are silently ignored and the graphic renders blank
- **`duration` on a graphic sentence** — Whisper derives it; setting it desyncs audio
- **`lower_third` on broll** — only valid on `content_track: "named"` sentences after sentence 4
- **Missing `lower_third`** — required on every `"named"` sentence after sentence 4; no exceptions
- **Named entity continuity** — keep `"named"` until the narration genuinely moves on
- **Effects on consecutive sentences** — always separate with ≥1 plain sentence
- **Under-density or front-loaded effects** — target ≥40% across the whole video, evenly distributed
- **List item stubs left in `sentences`** — ALL stubs must be deleted
- **`list` for multi-sentence items** — if any item needs 2+ sentences, use regular broll for all
- **`list` for two numerically differing items** — use `"infographic"` `"callouts"` instead
- **`infographic` for a single statistic** — needs ≥2 labeled values
- **`infographic` with long labels and `"bars"`** — use `"horizontal"` when any label >3 words
- **`"grid"` for fewer than 4 items** — use `"bullets"` or `"cascade"` for 2–3 items
- **Infographic/list because data appears** — only when viewer cannot follow the narration without it
- **Two infographic/list graphics within 3 sentences** — separate with ≥3 footage sentences
- **More than 8 combined infographic + list** — hard limit ≤8
