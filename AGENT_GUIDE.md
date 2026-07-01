# Agent Guide — MoneyPrinterTurbo Documentary Pipeline

```
script.txt → sentence_prep.py → job.json → [YOU ENRICH] → cli.py → final.mp4
```

Read this file once. Enrich every sentence in `job.json`. Print `JOB_JSON_PATH:` when done. Do not run `cli.py`.

---

## Step 1 — Generate Job JSON

```bash
mkdir -p "storage/tasks/My Video"
venv/bin/python sentence_prep.py --script "storage/tasks/My Video/script.txt" \
  --out "storage/tasks/My Video/job.json" --title "My Video" --voice "<voice>" --aspect "16:9"
```

---

## Step 2 — Enrich Every Sentence

Set `video_topic`, `video_type`, and (thematic only) `motif_palette` at the job root. Patch only the fields listed here on each sentence — **do not rebuild the array from scratch**.

> **CRITICAL — never clear `text`.**
> `sentence_prep.py` writes each narration sentence into `text`. Whisper uses it for timestamp alignment. If you blank `text` on any sentence the entire pipeline breaks: every clip gets a 2-second placeholder, subtitles are empty, and 50+ irrelevant gap-fill clips are generated. **Leave `text` exactly as written. Patch only the fields below.**

---

### `video_topic` / `video_type`

**`video_topic`** — 2–6 word subject phrase (e.g. `"ultra-processed food industry"`). Appended to search queries automatically.

| `video_type` | When to use | Query behaviour |
|---|---|---|
| `"thematic"` | Abstract idea, trend, or category | Bare concept tried first; topic folded in as fallback |
| `"named_entity"` | One specific recurring subject — brand, product, person | Topic appended to every concept query |

---

### `visual_concepts`

1–3 concrete, **subject-free** local visual ideas, specific → broad (≤3 words each). Subject-free = do not repeat `video_topic` words — the pipeline appends them automatically.

**Rules:**
- `[0]` must describe what a camera physically shows — no abstract nouns (`"hope"` → `"sunrise over city"`)
- `[1]`/`[2]` must be genuinely broader than `[0]`, not synonyms or near-duplicates
- Disambiguate words that collide with unrelated domains: `"court"` → `"courtroom interior"`, `"bar"` → `"crowded bar interior"`, `"firefly"` → `"firefly insect glowing"`, `"jaguar"` → `"jaguar big cat"`, `"python"` → `"python snake coiled"` (animal/brand/software collisions are common)
- **Variety limits (hard):** same `[0]` on ≤2 consecutive sentences and ≤4 times total. Numbered variants (`"shelf angle 5"`, `"shelf angle 6"`) count as ONE — forbidden.
- **Pre-submission audit (required):** unique `[0]` count must be ≥ `max(15, ceil(N/4))`. For 100 sentences → ≥25 unique. Shortfall → replace repeated entries.

Match visuals to what the narration is **actually about** — not to the overall video theme:
- Corporate strategy → boardroom, business documents, executive meeting (not store aisles)
- Historical event → archival footage, vintage product, old manufacturing (not current footage)
- Named product/person → `content_track: "named"` (see below)
- Generic scene → describe the specific action or setting the sentence requires

---

### `visual_caption`

One sentence describing the shot — used by the CLIP relevance filter to rank and reject candidates. `video_topic` is appended automatically.

- Describes the **shot** (subject, setting, composition) — not a search query
- Specific enough to reject off-topic stock: `"a worried shopper reading a discontinued label in a supermarket aisle"` — not `"a person looking surprised"`
- Required on every sentence, including `media_type: "image"` ones
- **No two sentences may share the same caption.** Duplicate captions break the relevance filter.

---

### `visual_effect`

Motion overlay composited during rendering. **Target ≤20% of `broll`/`named` sentences** — assign only at distinct narrative beats where the effect genuinely accentuates what's on screen. Leave blank for neutral or transitional clips.

> **Hook Zone exception:** sentences 0–4 must carry at least 2 visual effects regardless of the global 20% cap — see Hook Zone below.

| Value | Overlay | When to use |
|---|---|---|
| `"threat"` | Blood splatter clusters | Danger, conflict, violence, harm |
| `"cold"` | Falling snow particles | Tension, isolation, despair, winter |
| `"warmth"` | Golden sun rays | Hope, triumph, joy, prosperity |
| `"mystery"` | Drifting fog wisps | Eerie, unknown, conspiracy, dread |
| `"sepia"` | Film grain + dust scratches | Historic, archival, nostalgia |
| `"tech"` | Scan-line flicker | Digital, surveillance, data, systems |
| `"hacker_tech"` | Heavy scan-line / glitch | Hacking, advanced architecture, tech used maliciously |
| `"dream"` | Soft bokeh light orbs | Memory, fantasy, aspiration |
| `"noir"` | Rain streaks | Crime, cynicism, moral decay, shadows |
| `"nature"` | Dust motes + light shafts | Ecology, life, growth, outdoors |
| `"revelation"` | Lens flare burst | Discovery, truth, turning point, exposure |

**Rules:**
- Never set on `graphic` sentences.
- **Never on two consecutive sentences** — always separate effect-bearing sentences with ≥2 plain sentences.
- `sepia`+`noir` combined ≤1 per video. `tech`+`hacker_tech` combined ≤1 per video.

---

### `content_track`

- **`"named"`** — `[0]` is a specific, uniquely identifiable entity. Routes to Google Images (Serper) first.
- **`"broll"`** — generic scene, action, category, or location. Routes to stock video/image sources.

**Decision rule:** if you can Google the entity by exact name and expect the right image, use `"named"`. If it's a category or scene type, use `"broll"`. **70–80% of sentences should be `"broll"`** — Serper quota is finite.

**Entity continuity (critical):** once the narration names a specific entity and you assign `"named"`, keep `"named"` with that entity's visuals for every subsequent sentence that still discusses the same entity — even if those sentences don't repeat the name. Only revert to `"broll"` when the narration has genuinely moved on.

| Sentence | `visual_concepts[0]` | `content_track` |
|---|---|---|
| "The KEF LS50 Meta costs twelve hundred dollars." | `"KEF LS50 Meta speaker pair"` | `"named"` |
| "It uses a Uni-Q driver array at its heart." | `"KEF LS50 Meta driver detail"` | `"named"` ← still on same product |
| "Next up is the Focal Aria 906." | `"Focal Aria 906 speaker"` | `"named"` ← new entity |

**Use `"named"` for:** named people (portraits), branded products/SKUs, company logos/HQ, specific vehicles/aircraft/ships, named buildings/landmarks, historical events, named documents/laws/reports, named artworks, species with a distinctive look.

**Stay on `"broll"` for:** generic location types, named places used as atmosphere (real photos are usually watermarked), generic actions where identity doesn't matter, a company's generic product category.

**Named product detail:** include the identifying visual in `[0]`:
- BAD: `"Kellogg's Corn Pops"` — returns any version
- GOOD: `"Kellogg's Corn Pops original yellow box"` — anchors to specific packaging

For `"named"` branded products, also add the key visual identifier to `must_show`.

---

### Graphic Cues

All graphics are **narrated** — the graphic plays on screen while the narrator speaks. Keep the narration in `text` and in `video_script`. Set `graphic_type` and `variables` on the sentence. Set `content_track` the same as any other sentence (`"broll"` for generic context, `"named"` when showing a named entity). Do NOT set `duration` — Whisper derives it. Add `visual_concepts` and `visual_caption` as footage fallback if render fails.

**ALL fields below must be placed inside a `"variables": {}` object on the sentence — not at the top level.** The pipeline reads `sent["variables"]`; top-level `title`, `style`, etc. are silently ignored and the graphic renders blank.

| `graphic_type` | Fields inside `variables` |
|---|---|
| `"lower_third"` | `label` (2–6 words, title case — entity name or identifier) |
| `"infographic"` | `title`, `labels[]`, `values[]`, optional `unit`, `style` |
| `"list"` | `items[]` (2–6 strings), optional `title`, `style` |

| Type | Styles (pick one, or omit to auto-rotate) |
|---|---|
| `lower_third` | (no styles — single preset) |
| `infographic` | `"bars"` · `"horizontal"` (long labels) · `"lollipop"` · `"callouts"` (2–3 standalone stats) |
| `list` | `"bullets"` · `"numbered"` (ordered) · `"cascade"` · `"grid"` (4–6 equal-weight items) |

**When to insert:**

| Trigger | Type | Budget | Hook Zone (sent 0–4) |
|---|---|---|---|
| Named entity on screen where a text identifier adds genuine viewer value | `lower_third` | 0–3 | **BANNED** |
| 2+ quantities the viewer must compare side by side | `infographic` | 0–2 | **BANNED** |
| Parallel enumerable items where every item is a single short sentence | `list` | 0–1 | **BANNED** |

**Budget rule:** lower_third + infographic + list combined ≤ 3. Total graphics ≤ 4.

**Handling list items:** `sentence_prep.py` creates one stub per enumerated item. Merge them:
1. Extract every item to its core phrase and collect in `variables.items`.
2. **Delete every item stub sentence** — leave zero stubs.
3. Set `graphic_type: "list"` on the intro sentence.
Only use `list` when every item fits in one short sentence — if any item needs 2+ sentences, use regular broll for all.

**Type-specific rules:**
- `lower_third`: `label` is 2–6 words, title case, no punctuation. Only when the entity name adds genuine viewer value — a brand being reviewed, a person being named, a product in close-up. Do NOT use for generic broll or entities obvious from context. Strictly forbidden in sentences 0–4.
- `infographic`: `labels` and `values` same length; 2–8 data points; all `values` positive. `"callouts"` for 2–3 standalone stats. `"horizontal"` when any label is >3 words.
- `list`: 2–6 items. Single-sentence items only. For 2 numerically differing items, use `"infographic"` `"callouts"` instead. `"grid"` only for 4–6 items.

---

### `must_show` / `avoid`

Keywords passed to the VLM reviewer. The VLM hard-rejects clips missing a `must_show` item.

- **`must_show` required when:** narration names a specific physical object the viewer must see, or sentence is `"named"` for a branded product (add product name + key visual).
- **`must_show` optional (`[]`) when:** general scene with no single mandatory element.
- **`avoid`:** set when the topic makes wrong image types likely (meme charts in a finance doc; cartoon wildlife in a nature doc).

---

### `media_type`

| Use `"image"` for | Use `"video"` for |
|---|---|
| Named products, brands, portraits | Scenes, environments, locations |
| Logos, documents, historical photos | Actions, processes, crowds, nature in motion |

Ignored for `content_track: "named"` (always image). For real places the narration passes through, prefer `"broll"` + `"video"` — storefront photos are usually watermarked.

---

### `max_image_ratio` (job root)

| Content type | Value |
|---|---|
| Historical/archival, named-entity, scientific | `1.0` |
| General thematic / explainer | `0.6` |
| Lifestyle / travel / action | `0.3` |

---

### `motif_palette` / `assigned_motif` (thematic only)

**`motif_palette`** (job root) — 4–8 visually distinct, concrete, searchable anchors covering the theme.

**`assigned_motif`** (per sentence) — which palette entry drives `visual_concepts`. Never the same on consecutive sentences. Distribute evenly. Translate the motif into a specific scene description for `visual_concepts`.

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
      "text": "The sentence as it appears in the script.",  // NEVER modify this field
      "visual_concepts": ["concrete scene description", "broader fallback"],
      "content_track": "broll",        // "broll" | "named"
      "visual_caption": "one sentence describing the shot",
      "visual_effect": "",             // see table above — ≤20% of sentences
      "media_type": "video",           // "video" | "image"
      "assigned_motif": "...",         // thematic only
      "must_show": [],
      "avoid": []
    },
    // Narrated graphic — text stays in video_script; no duration field
    // lower_third example — label over stock footage of a named entity
    {
      "text": "The Sony WH-1000XM5 consistently tops every noise-cancelling chart.",
      "content_track": "named",
      "graphic_type": "lower_third",
      "variables": {
        "label": "Sony WH-1000XM5"
      },
      "visual_concepts": ["Sony WH-1000XM5 headphones", "premium over-ear headphones"],
      "visual_caption": "Sony WH-1000XM5 headphones on a clean white surface"
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

## Hook Zone — Sentences 0–4

The first 30 seconds determine whether a viewer stays. Apply stricter rules here.

### Hard bans (sentences 0–4)

- **All graphic types (`lower_third`, `infographic`, `list`) are forbidden.** Save data graphics and name labels for after the viewer is hooked.
- **`media_type: "image"` on any `broll` sentence is forbidden.** Static photos kill opening pace.

### Required (sentences 0–4)

| Rule | Why |
|---|---|
| `media_type: "video"` on every `broll` sentence | Motion holds attention; stills release it |
| All 3 `visual_concepts` slots filled | Maximum variety across the 5 opening cuts |
| Distinct `visual_concepts[0]` on every sentence — no repeats | Consecutive similar shots feel like a slideshow |
| **At least 2 of the 5 sentences carry a `visual_effect`** | Cinematic overlays signal production quality immediately |
| No two consecutive hook sentences share an effect | Effect saturation desensitizes; spacing builds contrast |

**Effect placement example:**
```
sent 0: warmth   (effect)
sent 1: —
sent 2: —
sent 3: revelation  (effect)
sent 4: —
```

### Strongly recommended (sentences 0–4)

- **Write hook sentences at 8–14 words.** Shorter sentences → shorter clips → faster cut rate.
- **Use active, kinetic visual concepts.** `"engineer soldering circuit board close-up"` beats `"technology office interior"`. `"water rushing over rocks"` beats `"river landscape"`.
- **Vary depth and scale** — mix extreme close-ups, medium shots, and wider contexts.

---

## Step 2.5 — Review Pass (mandatory)

**Hook Zone (sentences 0–4):**
- [ ] No graphics in sentences 0–4?
- [ ] All `broll` sentences have `media_type: "video"`?
- [ ] ≥2 of the 5 sentences carry a `visual_effect`?
- [ ] All 3 `visual_concepts` slots filled on every sentence?
- [ ] Every sentence has a distinct `visual_concepts[0]`?

**Full video:**
- [ ] `[0]` is physically showable for *that specific sentence* (not just the video theme)?
- [ ] No `video_topic` words in any `visual_concepts` entry?
- [ ] `[1]`/`[2]` genuinely broader than `[0]` — not synonyms?
- [ ] Every named entity marked `"named"`? Every generic scene `"broll"`?
- [ ] No two sentences share the same `visual_caption`?
- [ ] `max_image_ratio` set at job root?
- [ ] Unique `[0]` count ≥ `max(15, ceil(N/4))`?
- [ ] Effects ≤20% of broll/named; no two adjacent with effects; sepia+noir ≤1; tech+hacker_tech ≤1?
- [ ] All graphics narrated: real `text`, no `duration`, `visual_concepts`/`visual_caption` set?
- [ ] Total graphics ≤4? `labels` and `values` same length on infographics?
- [ ] All list item stubs deleted from `sentences`?
- [ ] No two consecutive sentences share `assigned_motif`? (thematic)

---

## Step 3 — Print Path and Stop

```
JOB_JSON_PATH: /home/deploy/AutoMoneyPrinterTurbo/storage/tasks/My Video/job.json
```

Do NOT run `cli.py`. The worker runs it automatically.

---

## Common Mistakes

- **Clearing `text`** — catastrophic pipeline failure; never touch it
- **Abstract `visual_concepts`** — `"hope"` → `"sunrise over city horizon"`
- **Repeating `video_topic` words in concepts** — pipeline appends them; repeating garbles queries
- **Ambiguous single words** — `"court"` → `"courtroom interior"`, `"bar"` → `"crowded bar interior"`
- **Brand/animal/software name collisions** — `"firefly"` → `"firefly insect glowing"`, `"jaguar"` → `"jaguar big cat"`, `"python"` → `"python snake coiled"`
- **Numbered concept variants** — `"shelf angle 5"` ≡ `"shelf angle 6"` — explicitly forbidden
- **Near-duplicate `[1]`/`[2]`** — must widen the search, not restate `[0]`
- **Overusing `"named"`** — spends Serper quota; 70–80% should be `"broll"`
- **`"named"` for real places** — storefront/landmark photos are usually watermarked; use `"broll"` + `"video"`
- **Generic or duplicate `visual_caption`** — must describe the specific shot; no two sentences may share one
- **Missing `bgm_search_term`** on emotional content — match it to tone
- **Graphic fields at the top level** — `title`, `style`, `label`, `items`, etc. MUST be inside `"variables": {}`. Top-level placement is silently ignored; the graphic renders blank
- **`duration` on a graphic sentence** — Whisper derives it; setting it desyncs audio
- **`lower_third` on generic broll** — only use when a named entity is specifically on screen and a label adds genuine value
- **Named entity continuity** — after naming an entity, keep `content_track: "named"` until the narration genuinely moves on
- **Effects on consecutive sentences** — always separate effect sentences with ≥2 plain sentences
- **Leaving list item stubs in `sentences`** — ALL stubs must be deleted
- **`list` for multi-sentence items** — if any item needs 2+ sentences, use regular broll for all
- **`list` for two numerically differing items** — use `"infographic"` `"callouts"` instead
- **`infographic` for a single statistic** — needs ≥2 labeled values
- **`infographic` with long labels and `"bars"` style** — use `"horizontal"` when any label exceeds ~3 words
- **`"grid"` list style for fewer than 4 items** — use `"bullets"` or `"cascade"` for 2–3 items
- **More than 4 total graphic entries** — hard limit is ≤4
