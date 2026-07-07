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

> **Do NOT set `graphic_type`, `variables`, or `visual_effect` on any sentence.** A dedicated graphics and overlay pass runs after you finish and handles all of those fields.

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

> **HARD LIMITS — verified automatically after you finish. Violations trigger a correction pass.**
> - Same `[0]` on **≤2 consecutive sentences** — three in a row is always wrong
> - Same `[0]` **≤4 times across the entire video** — this is a *global* cap, not per-product
> - Unique `[0]` count **≥ max(15, ceil(N/4))** — for 100 sentences you need ≥25 distinct values
>
> Numbered variants count as ONE: `"shelf angle 5"` ≡ `"shelf angle 6"` — forbidden.
> Self-audit your `[0]` column before writing job.json.

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

**Named-section variety is mandatory.** A product section with 6+ sentences cannot use the product name as `[0]` for all of them — it hits the ≤4 global cap and the ≤2 consecutive cap simultaneously. Rotate through: the product, a key feature or component, a related accessory, the brand, a comparison product, or the founder/designer. Every named section longer than 2 sentences must show `[0]` variety.

| Sentence | `visual_concepts[0]` | `visual_concepts[1]` | why `[0]` changes |
|---|---|---|---|
| "The Bose 901 costs seven hundred dollars restored." | `"Bose 901 speaker pair"` | `"vintage floor speakers"` | main product — use 1 of ≤4 allowed |
| "Let me be fair to Amar Bose for a second." | `"Amar Bose"` | `"Bose 901 speaker"` | sentence is about the person |
| "It requires its powered equalizer in the signal path." | `"Bose 901 equalizer"` | `"stereo rack components"` | rotate to accessory — no consecutive repeat |
| "Those rear-firing drivers bounce off your wall." | `"Bose 901 speaker"` | `"rear speaker placement"` | back to product — 2 of ≤4 allowed |
| "The bass is weirdly satisfying." | `"Bose 901 speaker"` | `"home audio setup"` | 3 of ≤4 allowed; one more use left globally |
| "Some audiophiles find the EQ coloration divisive." | `"stereo equalizer rack unit"` | `"audiophile listening room"` | must rotate — 4th use of "Bose 901 speaker" would be the last |
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

### `must_show` / `avoid`

Keywords passed to the VLM reviewer. The VLM hard-rejects clips missing a `must_show` item.

- **`must_show` required when:** narration names a specific physical object that must be visible — set it to whatever `visual_concepts[0]` is targeting (the product, the person, the component). It changes sentence by sentence.
- **`must_show` empty (`[]`) when:** general scene with no mandatory element, OR the entity in `[0]` is a **person** (the narration + `visual_caption` give the VLM sufficient context; requiring a product in `must_show` hard-rejects valid portraits that don't show it)
- **`avoid`:** when the topic makes wrong image types likely (meme charts in a finance doc; cartoon wildlife in a nature doc)

---

### `assigned_motif` (thematic only)

Which `motif_palette` entry drives `visual_concepts`. Never the same on consecutive sentences. Distribute evenly. Translate into a specific scene for `visual_concepts`.

---

## Hook Zone — Sentences 0–4

| Rule | Requirement |
|---|---|
| `media_type` on `broll` | `"video"` only — no images |
| `visual_concepts` | All 3 slots filled on every sentence |
| `visual_concepts[0]` | Distinct on every sentence — no repeats |

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
    {
      "text": "The sentence as written — NEVER modify.",
      "visual_concepts": ["concrete scene description", "broader fallback"],
      "content_track": "broll",         // "broll" | "named"
      "visual_caption": "one sentence describing the shot",
      "media_type": "video",            // "video" | "image"
      "assigned_motif": "...",          // thematic only
      "must_show": [],
      "avoid": []
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
- [ ] All `broll` sentences have `media_type: "video"`?
- [ ] All 3 `visual_concepts` slots filled?
- [ ] Distinct `visual_concepts[0]` on every sentence?

**Full video:**
- [ ] `[0]` physically showable for *that specific sentence* (not just the video theme)?
- [ ] No `video_topic` words in any `visual_concepts` entry?
- [ ] `[1]`/`[2]` genuinely broader than `[0]` — not synonyms?
- [ ] Every named entity marked `"named"`? Every generic scene `"broll"`?
- [ ] No two sentences share the same `visual_caption`?
- [ ] `max_image_ratio` set at job root?
- [ ] Tally every `[0]` value: any used >4 times total? Any used >2 times consecutively?
- [ ] Count distinct `[0]` values: is the total ≥ `max(15, ceil(N/4))`? (N = enrichable sentences)
- [ ] Named sections with 3+ sentences: does `[0]` rotate — not the same product name throughout?
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
- **Named entity continuity** — keep `content_track: "named"` until the narration genuinely moves on; `visual_concepts[0]` may change sentence-by-sentence to the most relevant related entity (person, component, accessory) — it does not have to repeat the same product name
- **Sub-detail description as `visual_concepts[0]`** — `"KEF LS50 Meta driver detail"` fails Serper; use `"KEF LS50 Meta speaker"` and put the detail in `[1]` and `visual_caption`
- **`must_show` locked to product on a person sentence** — when `[0]` targets a person, leave `must_show` empty; listing the product hard-rejects valid portraits that don't show it
