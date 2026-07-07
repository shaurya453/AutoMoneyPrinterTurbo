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

1–3 **search queries** sent verbatim to Pexels, Pixabay, DDG, and Wikimedia. Think: what would you type into pexels.com to find this exact shot? Do not include bare `video_topic` words in your concepts — for `named_entity` videos the pipeline also tries `"{concept} {video_topic}"` as a rung automatically; for `thematic` videos concepts are used as-is (the topic appears only as a last-resort catch-all rung, not paired with each concept).

**Query structure: subject + action or subject + setting (2–5 words)**
- `[0]` = specific but realistically findable — mentally test it on pexels.com. If it returns fewer than 20 results, it will fail. Aim for terms that return 100+.
- `[1]` = broader fallback used when `[0]` finds nothing usable. Widen the subject, not just reword it.
- `[2]` = widest fallback — almost any related scene or category that could work.

**Rules:**
- `[0]` must describe what a camera physically shows — no abstract nouns (`"hope"` → `"sunrise over city"`, `"effectiveness"` → `"product on clean surface"`)
- `[1]`/`[2]` must be genuinely broader than `[0]` — not synonyms or near-duplicates
- Disambiguate collisions: `"court"` → `"courtroom interior"`, `"bar"` → `"crowded bar interior"`, `"firefly"` → `"firefly insect glowing"`, `"jaguar"` → `"jaguar big cat"`, `"python"` → `"python snake coiled"`
- Same `[0]` on ≤2 consecutive sentences and ≤4 times total. Numbered variants (`"shelf angle 5"`, `"shelf angle 6"`) count as ONE — forbidden.
- **Pre-submission audit:** unique `[0]` count ≥ `max(15, ceil(N/4))`. For 100 sentences → ≥25 unique.

**What makes a good broll search term:**

| Works | Fails | Why it fails |
|---|---|---|
| `"woman applying face cream"` | `"moisturizer effectiveness concept"` | abstract noun + "concept" returns nothing |
| `"dermatologist examining patient skin"` | `"mature skin texture close-up"` | too niche, near-zero Pexels results |
| `"skincare products bathroom shelf"` | `"shine control on mature skin"` | adjective phrase, not a searchable subject |
| `"sunscreen bottle outdoor background"` | `"the feel of a product by noon"` | sentence fragment, no searchable subject |
| `"woman morning skincare routine"` | `"trust and confidence"` | emotion word, returns stock smiles not skincare |
| `"businesswoman reading financial report"` | `"corporate accountability"` | abstract — returns random office stock |
| `"assembly line workers manufacturing"` | `"industrial efficiency concept"` | "concept" suffix always returns garbage |

**Avoid these patterns in `[0]`:**
- Trailing nouns: `"concept"`, `"philosophy"`, `"approach"`, `"effectiveness"`, `"idea"`
- Pure emotion/mood words as the primary subject: `"trust"`, `"hope"`, `"confidence"`, `"disappointment"`
- Overly specific niche shots that don't exist in stock libraries: `"sixty-year-old woman applying SPF 50 by a window"` → `"woman applying sunscreen face"`
- Sentence fragments or narrative phrases

Match visuals to what the **specific sentence** is about — not the overall theme:
- Named product/person → use `content_track: "named"` (see below)
- Historical event → archival footage, vintage product, old manufacturing
- Corporate strategy → boardroom, business documents, executive meeting

---

### `visual_caption`

One sentence describing the shot — used by the CLIP relevance filter and VLM. `video_topic` appended automatically.

- Describes a **concrete, photographable scene**: specific subject, action, setting, and composition
- Written as **stock-photo language** — imagine the caption that would appear under a Getty image. A human photographer should be able to recreate the shot from your description alone
- **Never abstract concepts or emotions**: not `"moisturizer effectiveness"`, `"trust and confidence"`, or `"skincare philosophy"` — these produce unrelated garbage from search engines
- **Never brand names** on broll sentences (they won't be found by Pexels/DDG/Wikimedia)
- Specific enough to reject off-topic stock: `"a woman pressing her fingertips to her cheek while looking in a bathroom mirror"` not `"a person touching their face"`
- Required on every sentence. **No two sentences may share the same caption.**

**Good examples:**
- `"close-up of sunscreen bottle held in a woman's hand against a sunny outdoor background"`
- `"dermatologist in white coat examining a patient's skin under a bright clinic light"`
- `"row of tinted moisturizer bottles lined up on a white bathroom shelf"`
- `"woman pressing fingertip to cheek checking skin texture in bathroom mirror"`

**Bad examples (too abstract — will return garbage):**
- `"moisturizer application concept"` → returns dental braces, boats, cartoon coloring pages
- `"shine control on mature skin"` → returns go-karts, playing cards
- `"the feel of a product by noon"` → returns nothing relevant

---

### `content_track`

**Decision:** if you can Google the entity by exact name and expect the right image, use `"named"`. Otherwise use `"broll"`. **70–80% of sentences should be `"broll"`** — Serper quota is finite.

| Value | When | Source |
|---|---|---|
| `"named"` | Specific, uniquely identifiable entity | Google Images (Serper) first |
| `"broll"` | Generic scene, action, category, location | Stock video/image |

**Entity continuity:** once you assign `"named"` to an entity, keep `content_track: "named"` for every subsequent sentence still in that entity's section — even without repeating the name. Revert to `"broll"` only when the narration genuinely moves on to a new topic.

**`visual_concepts[0]` = the most relevant Serper-searchable entity for that specific sentence.** This does NOT have to be the same product name every sentence. Within a named entity section, each sentence may point at whichever related entity best illustrates what that sentence is actually saying — the main product, the inventor, a specific component or accessory, a comparison product, the brand itself. The only constraint: `visual_concepts[0]` must be a recognisable proper noun or brand name that Serper can find. Never use sub-detail descriptions as `[0]` — they fail Serper search.

| Sentence | `visual_concepts[0]` | `visual_concepts[1]` | why `[0]` changes |
|---|---|---|---|
| "The Bose 901 costs seven hundred dollars restored." | `"Bose 901 speaker pair"` | `"vintage floor speakers"` | main product |
| "Let me be fair to Amar Bose for a second." | `"Amar Bose"` | `"Bose 901 speaker"` | sentence is about the person |
| "It requires its powered equalizer in the signal path." | `"Bose 901 equalizer"` | `"stereo rack components"` | sentence is about the accessory |
| "Those rear-firing drivers bounce off your wall." | `"Bose 901 speaker"` | `"rear speaker placement"` | back to the product |
| "Next up is the KEF LS50 Meta." | `"KEF LS50 Meta speaker pair"` | `"bookshelf speakers"` | new entity |

**Use `"named"` for:** named people, branded products/SKUs, company logos/HQ, specific vehicles, named landmarks, historical events, named documents/laws, named artworks, distinctive species.

**Stay on `"broll"` for:** generic location types, real places as atmosphere (storefront photos are watermarked), generic actions, a company's generic product category.

For named products, be specific in `[0]`: `"Kellogg's Corn Pops original yellow box"` not `"Kellogg's Corn Pops"`.

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

- **`must_show` required when:** narration names a specific physical object that must be visible — set it to whatever `visual_concepts[0]` is targeting (the product, the person, the component). It changes sentence by sentence.
- **`must_show` empty (`[]`) when:** general scene with no mandatory element, OR the entity in `[0]` is a **person** (the narration + `visual_caption` give the VLM sufficient context; requiring a product in `must_show` hard-rejects valid portraits that don't show it)
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

Use active, kinetic search terms — `"engineer soldering circuit board"` beats `"technology office interior"`. Vary depth and scale across the 5 cuts. All 5 must be immediately findable on Pexels (100+ results each).

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
- **Abstract `visual_concepts`** — `"hope"` → `"sunrise over city horizon"`; `"effectiveness"` → `"product on clean surface"`
- **"Concept" suffix on `[0]`** — `"moisturizer concept"`, `"industrial efficiency concept"`, `"trust concept"` always return garbage; strip the suffix and name a physical subject
- **Mood/emotion words as primary subject** — `"trust"`, `"hope"`, `"confidence"` as `[0]` pull random stock smiles; describe the scene that *produces* that mood instead
- **Overly narrow niche queries** — if fewer than 20 Pexels results would exist for your term, broaden it: `"sixty-year-old applying SPF 50 outdoors"` → `"woman applying sunscreen face"`
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
- **Named entity continuity** — keep `content_track: "named"` until the narration genuinely moves on; `visual_concepts[0]` may change sentence-by-sentence to the most relevant related entity (person, component, accessory) — it does not have to repeat the same product name
- **Sub-detail description as `visual_concepts[0]`** — `"KEF LS50 Meta driver detail"` fails Serper; use `"KEF LS50 Meta speaker"` and put the detail in `[1]` and `visual_caption`
- **`must_show` locked to product on a person sentence** — when `[0]` targets a person, leave `must_show` empty; listing the product hard-rejects valid portraits
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
