# CLAUDE.md — Developer Guide

This file is read by Claude Code at the start of every session. Keep it up to date when architecture or workflows change.

---

## What This Project Does

Headless CLI pipeline: turns a plain-text script into a finished documentary-style MP4 with narration (edge-tts), stock footage, Ken Burns still images, lower-third labels (Revideo), subtitles, and background music. Driven by an AI agent (see `AGENT_GUIDE.md`).

```
script.txt  →  scripts/sentence_prep.py  →  job.json  →  [agent enriches]  →  cli.py  →  final.mp4
```

---

## How to Run

```bash
# One-shot
venv/bin/python cli.py --job storage/tasks/<title>/job.json

# Debug output
venv/bin/python cli.py --job storage/tasks/<title>/job.json --log-level DEBUG

# Skip whisper re-alignment (useful for re-testing footage fetch without waiting for TTS + whisper)
SKIP_WHISPER=1 venv/bin/python cli.py --job storage/tasks/<title>/job.json

# Disable all Revideo graphic rendering (pure graphics skipped; narrated graphics fall back to footage)
REVIDEO_ENABLED=0 venv/bin/python cli.py --job storage/tasks/<title>/job.json
```

Output lands in `storage/tasks/<title>/`: `final.mp4`, `audio.mp3`, `subtitle.srt`, `temp/clips/clip-NNNN.mp4`.

Re-running with the same title creates `<title> (2)`, `<title> (3)`, etc. automatically — but only when the prior run reached Phase 2 (i.e. `audio.mp3` or `final.mp4` already exists). A directory with only a stale `job.json`/`script.txt` is reused (Phase-1 retry path).

---

## Key Files

| File | Role |
|---|---|
| `cli.py` | Entry point — parses args, calls `pipeline.start()` |
| `scripts/sentence_prep.py` | Splits a script into a stub job JSON |
| `scripts/validate_job.py` | Validates a job.json before Phase 2 (called by portal-worker) |
| `app/services/pipeline/` | Main orchestrator package — TTS → Whisper → clip fetch → combine → subtitles → final |
| `app/services/pipeline/_whisper.py` | Whisper sentence-timestamp alignment helpers |
| `app/services/pipeline/_planning.py` | Clip planning: Pass-1 slot math (`plan_clip_slots`), query building, ffmpeg trim, shared planning constants |
| `app/services/pipeline/_fetch.py` | Clip fetching: search → NSFW gate → relevance → Ken Burns |
| `app/services/pipeline/_orchestrate.py` | `start()` function: orchestrates all pipeline stages |
| `app/services/pipeline/_quality.py` | `write_quality_report()` — aggregates per-clip fetch/rescue/placeholder/gap outcomes into `<title>.quality.json` |
| `app/services/render/` | FFmpeg/MoviePy rendering package |
| `app/services/render/ken_burns.py` | Ken Burns animations, `render_ken_burns_clip()` |
| `app/services/render/combine.py` | `combine_videos()`, xfade concat, `XFADE_CLIP_LIMIT` |
| `app/services/render/generate.py` | `generate_video()`, subtitle burn-in, BGM ducking, `get_bgm_file()`, SFX bed mixing (`_render_sfx_bed_wav()`) |
| `app/services/render/effects.py` | `apply_visual_effect()`, `composite_lower_third()`, color grade/grain/vignette (`_grade_chain_str()`, per-sentence `visual_grade`/`grain`/`vignette` fields, fused into the same ffmpeg pass as the mood-texture blend when both fire) |
| `app/services/render/sfx.py` | Mood-triggered one-shot SFX library, `pick_sfx_for_mood()` — mirrors `_EFFECT_OVERLAYS`' mood vocabulary |
| `app/services/render/_common.py` | Shared codec helpers, `SubClippedVideoClip`, `close_clip()` |
| `app/services/media/` | Stock footage/image search and download package |
| `app/services/media/_search_cache.py` | SQLite-backed cache for provider search *responses* (distinct from the downloaded-asset caches below), `cached_get_json()`/`cached_post_json()` |
| `app/services/media/images.py` | Image search (Pexels, Pixabay, Unsplash, Wikimedia, DDG, Serper, NASA, Internet Archive, Smithsonian), `download_image()` |
| `app/services/media/videos.py` | Video search (Pexels, Pixabay, Coverr), `download_video()`, BGM download |
| `app/services/tts/` | TTS package: edge-tts, Kokoro ONNX, Supertonic, Minimax (routed through Algrow's cheaper proxy by default), ElevenLabs (Algrow's premade catalog only, see below) |
| `app/services/tts/__init__.py` | `tts()` dispatcher, `NO_VOICE_NAME` |
| `app/services/scoring/nsfw.py` | NudeNet ONNX pixel-level NSFW gate — hard-rejects before relevance |
| `app/services/scoring/relevance.py` | CLIP ViT-B/32 ONNX relevance scoring vs. junk anchors |
| `app/services/scoring/vlm.py` | VLM visual captioning (on-disk cache) |
| `app/utils/graphics.py` | Python wrapper for Revideo graphic clip rendering (subprocess) |
| `app/utils/subtitle.py` | SRT parsing helpers (`file_to_subtitles`) |
| `app/models/schema.py` | `VideoAspect`, `VideoParams`, enums — `VideoAspect.to_resolution()` → `(w, h)` |
| `app/config.py` | Loads `config.toml` |
| `AGENT_GUIDE.md` | Full enrichment spec for the AI agent |
| `resource/songs/` | Local BGM MP3s (fallback when online search is empty) |
| `resource/backgrounds/` | Gradient background MP4s served by Revideo/Vite via symlinks in `revideo-worker/public/` |
| `resource/graphics/` | Static compositing assets (e.g. `lower_third_shadow.png`) |
| `resource/overlays/` | Motion overlay MP4s blended during rendering (screen / multiply modes) |
| `resource/sfx/` | One-shot SFX mp3s (ported from vidspeed's `public/effects/sfx/`), keyed by `app/services/render/sfx.py::_SFX_LIBRARY` |
| `storage/tasks/<title>/` | Per-job working directory |

### Revideo motion-graphics worker

Lives at `revideo-worker/` (inside the repository root).

| File | Role |
|---|---|
| `render.js` | CLI: reads JSON from stdin, routes to project file by `type`+`variant`, calls `renderVideo()`, prints MP4 path to stdout |
| `variants.json` | **Single source of truth** for variant metadata — `render.js` derives `VARIANT_POOL`/`BG_VIDEOS` from it, `graphics.py` derives `_POOL_SIZES`/`STYLE_MAP`/`_BG_VIDEOS`. Adding a variant = scene+project file + one entry here (no Python change) |
| `src/projects/lower-third.ts` | Revideo project for `lower_third` (single variant) |
| `src/projects/info-callout.ts` | Revideo project for `info_callout` (single variant) |
| `src/projects/infographic{,-b,-c,-d}.ts` | Revideo projects for infographic variants A–D |
| `src/projects/list{,-b,-c,-d}.ts` | Revideo projects for list variants A–D |
| `src/projects/quote-card.ts` | Revideo project for `quote_card` (single variant) |
| `src/projects/timeline-card.ts` | Revideo project for `timeline_card` (single variant) |
| `src/scenes/lower-third.tsx` | Lower third — left-anchored label with semi-transparent backdrop, fade in/out |
| `src/scenes/info-callout.tsx` | Info callout — 3 fixed-position text boxes with animated leader lines to dots near centre, staggered reveal scaled to clip duration |
| `src/scenes/quote-card.tsx` | Quote card — full-replace attributed quote (large serif quote-mark + quote + muted attribution line) |
| `src/scenes/timeline-card.tsx` | Timeline card — full-replace date/era marker, minimal one/two-line chapter break |
| `src/scenes/infographic{,-b,-c,-d}.tsx` | Scene files: A vert bars · B horiz bars · C lollipop · D callouts |
| `src/scenes/list{,-b,-c,-d}.tsx` | Scene files: A bullets · B numbered · C cascade · D card grid |
| `package.json` | `@revideo/{core,2d,renderer,vite-plugin,ui}` v0.10.4 |

---

## Pipeline Architecture

### Stage flow (inside `pipeline.start()`)

1. **TTS** — `tts.tts()` → `audio.mp3`; also used to produce edge-tts word-level timings for subtitles
2. **Loudness normalization** — `tts.normalize_narration_loudness()` (two-pass EBU R128 `loudnorm`, target -16 LUFS / -1.5 dBTP) runs in place on `audio.mp3` before alignment. TTS providers vary wildly in output level (some near-silent, some clipping), and both extremes hurt Whisper's word-level alignment accuracy. Non-fatal — a failed pass just leaves the original audio
3. **Whisper alignment** — `_get_sentence_timestamps()` (faster-whisper) aligns each narration sentence to an `(start, end)` span via SequenceMatcher token alignment. A sentence whose tokens can't be matched (e.g. empty `text`) falls back to `(last_end, last_end + 2.0)` — which wrecks the Pass-1 plan for everything after it, which is why `validate_job.py` hard-errors on empty `text`
4. **Pass 1 — clip planning** — compute each sentence's footage slot from absolute Whisper timestamps (slot runs until the next sentence's narration begins). A narrated-graphic sentence (`graphic_type` set + non-empty `text`) keeps its Whisper-derived slot; list/infographic slots are floored at 5 s for readability
5. **Pass 2 — clip fetch / render** — for each sentence:
   - `graphic_type` set (always narrated; `content_track` stays `"broll"`/`"named"`) → `graphics.render_graphic_clip()` (subprocess to `revideo-worker/render.js`). `lower_third`/`info_callout` renders are composited over fetched footage (see `_COMPOSITE_GRAPHIC_TYPES` in `_planning.py`); `list`/`infographic` renders replace the footage slot entirely
   - otherwise → `_fetch_clip()` (search → NSFW gate → relevance → Ken Burns for images)
6. **Clip-fetch resilience** — rescue fetch → placeholder → position-aware gap-fill → tail gap-fill (see below)
7. **Outro extension** — last clip looped to reach `audio_duration + 2 s`
8. **`combine_videos()`** — sequential concat with xfade crossfade → `temp/combined.mp4`
9. **`generate_video()`** — subtitles (Pillow), fade-out, BGM duck, SFX mix → `final.mp4`

### SFX (one-shot sound effects)

Phase B's `apply_visual_effect()` call site in `_orchestrate.py` also calls `render.pick_sfx_for_mood(visual_effect, rng=job_rng)` whenever a mood texture actually fires, appending `(cue_start, path, volume)` to a `sfx_cues` list (cue start = the running sum of `planned_clip_durations` *before* this clip is appended — an approximation of the clip's position in the final timeline, not frame-exact against the post-crossfade output, which is fine for a short mood accent). `sfx_cues` threads through to `generate_video()`, which pre-renders every cue into one full-duration WAV bed (`_render_sfx_bed_wav()`, mirroring `_render_bgm_wav()`'s "decode to PCM, mix offline" approach) and mixes it in as an extra `amix` input alongside narration and BGM. SFX are **not** ducked against narration — they're meant to cut through at their moment (same design choice vidspeed made). Toggle via `config.toml`'s `sfx_enabled`/`sfx_volume_scale` (default `0.5`, halved from the original `1.0` after a real render's accents came through too loud/too long). Every cue is also hard-capped to `_SFX_MAX_CUE_SECONDS` (3.5s) with a `_SFX_FADE_OUT_SECONDS` (0.25s) linear tail fade in `_render_sfx_bed_wav()` — several library assets are ambience-length (`noir-rain.mp3` is 90s, `geiger-click.mp3` 72s, `unsettling-drone.mp3` 35.7s) rather than true one-shots, and without the cap+fade they either play through as background ambience or click when the WAV bed's per-cue array is naively truncated. Library/mood grouping: `app/services/render/sfx.py`. Tests: `tests/test_sfx.py` (cap + fade + volume-scale regression coverage).

### Clip-fetch resilience (rescue → placeholder → gap-fill)

Four layered fallbacks in `_orchestrate.py`'s Phase B keep the timeline in
sync when a fetch fails, narrowest/most-relevant first:

1. **Rescue fetch** — a sentence whose primary search failed entirely
   retries against `_job_gapfill_terms` (per-job, topically-loose terms from
   `job.gapfill_terms`, falling back to a hardcoded safety net for old
   job.json files). Sentences carrying a `lower_third` graphic try their
   named section's other `visual_concepts[0]` values first
   (`_entity_concepts_map`, grouped by `entity_name`) before falling back to
   fully generic terms — a generic image under a specific product-name label
   is the most visually jarring case, so it gets the most relevant rescue.
2. **Placeholder** — if rescue also fails, the slot is filled with footage
   of the exact planned duration so audio/video stay in sync
   (`_recent_real_clips`, cycling through the last 3 real clips so
   consecutive failures don't repeat the exact same footage). If the anchor
   is a Ken Burns still image, its source image (`_recent_real_image_sources`)
   is re-rendered continuously via `render_ken_burns_clip()` for the full
   needed duration instead of `stream_loop`-ing the finished clip — looping
   would replay the baked-in animation from frame 0 partway through. Stock-
   video anchors (no source image) fall back to `stream_loop` + mirror-flip.
3. **Position-aware gap-fill** — the rare case where both the fetch and
   every placeholder attempt fail (`catastrophic_gap` in `quality.json`) is
   patched *at its exact timeline position*, not the tail — a tail-only fix
   would make the total video duration match the audio again while
   everything downstream of the hole stays permanently offset from the VO.
4. **Tail gap-fill** — after all of the above, if total footage duration
   still falls short of the audio, extra generic clips are appended to the
   end until they match (or the concept/gapfill-term pool is exhausted).

### Ken Burns (still images)

**Portrait images** (h ≥ w): FIT-scaled to 95% of frame, centered on a blurred+darkened background (same image, downscale-upscale blur at 6%, 50% brightness; plain white background + drop shadow for transparent PNGs). Animation is picked from `["fade", "zoom_in", "zoom_out"]`.

**Landscape images** (w > h): COVER-CROP fills the full frame — no blurred background visible. Animation is chosen from a pool derived from the image's aspect-ratio overflow:
- `zoom_in`, `fade`, `screen_3d_lr`, `screen_3d_ud` — always in pool. The `screen_3d_*` presets are 3D screen-mockup animations (tilted perspective easing to flat, rendered per-frame via PIL/MoviePy in `_render_3d_effect()`, blurred-image backdrop + soft shadow)
- `pan_lr` / `pan_rl` — added when the image is wider than the frame proportionally (`h_excess > 2% of frame_w`)
- `pan_ud` — added when the image is taller than the frame proportionally after cover-scale (`v_excess > 2% of frame_h`)

`_pick_animation(allowed, effect)` enforces no-consecutive-repeat across clips and soft-weights the pick by the sentence's `visual_effect` mood (`_EFFECT_ANIM_WEIGHTS`). `_PAN_Z = 1.04`. Ease-out quadratic timing on all pan/zoom travel.

Primary path: `ken_burns._render_ken_burns_ffmpeg()`. Cover mode uses `-vf` (single input, no overlay). Pan animations at 2× PIL scale + 2:1 lanczos FFmpeg downscale for sub-pixel smooth motion. MoviePy fallback: `apply_ken_burns()`.

### Revideo integration

Python calls `node revideo-worker/render.js` via `subprocess.run()` with a JSON payload on stdin:

```json
{
  "type": "lower_third",
  "outPath": "/absolute/path/clip-0000.mp4",
  "duration": 5.0,
  "width": 1920,
  "height": 1080,
  "fps": 30,
  "variables": { "label": "Sony WH-1000XM5" }
}
```

`render.js` maps `type`+`variant` → Revideo project file via `VARIANT_POOL`, which (like `graphics.py`'s `_POOL_SIZES`/`STYLE_MAP`/`_BG_VIDEOS`) is derived from `revideo-worker/variants.json` at load time. `graphics.py` picks the variant, enforcing no-consecutive-repeat rotation unless a named `style` overrides it. Adding a new *variant* of an existing type = new `src/scenes/foo.tsx` + new `src/projects/foo.ts` + one entry in `variants.json` — no Python or render.js change. Adding a wholly new *type* also needs one Python-side edit: `scripts/validate_job.py`'s `_VALID_GTYPES` allowlist (and its cost-class grouping — composite-over-footage like `lower_third`/`info_callout`, or full-replace like `infographic`/`list`/`quote_card`/`timeline_card`).

The rendered H.264 MP4 slots into `temp/clips/` identically to any stock clip — `combine_videos()` sees no difference.

**Supported `type` values:**

| `type` | Variants | Key `variables` |
|---|---|---|
| `lower_third` | single | `label` (2–6 words) |
| `info_callout` | single | `labels[]` (exactly 3 short strings) — named-entity images only, composites over footage like `lower_third` |
| `infographic` | A: vertical bars · B: horizontal bars · C: lollipop · D: number callouts | `title`, `labels[]`, `values[]`, `unit` |
| `list` | A: bullets · B: numbered · C: cascade reveal · D: card grid | `items[]`, `title` |
| `quote_card` | single | `quote` (required), `attribution` (optional) — attributed quote, full-replace |
| `timeline_card` | single | `label` (required, e.g. "1969"), `sublabel` (optional) — date/era marker, full-replace |

Named style hints (e.g. `"style": "callouts"`) map to specific variants — see `AGENT_GUIDE.md`. The pipeline rotates variants automatically when no style is specified to avoid consecutive repeats.

**`quote_card`/`timeline_card` are AMPT-side plumbing only as of this writing** — like `infographic`/`list`, they're assigned by portal's graphics-audit pass (`worker.mjs`), never the enrichment agent. Portal's prompt/schema needs its own update to actually start choosing them; until then they render correctly if a job.json sets them by hand (e.g. for testing) but nothing in the current pipeline will assign one automatically.

---

## Job JSON Structure

Created by `sentence_prep.py`, enriched by the AI agent, consumed by `cli.py`.

Top-level fields an agent must set: `video_topic`, `video_type`, `motif_palette` (thematic only), `gapfill_terms` (10 generic-but-on-topic filler terms, used by rescue/gap-fill fallbacks above), and per-sentence `visual_concepts`, `content_track`, `visual_caption`, `media_type`.

Named-entity sentences (`content_track: "named"`) also carry `entity_name` — the full, non-rotating canonical name of the product/person (brand + product + SPF/size/shade/model), constant across every sentence in that named section even as `visual_concepts[0]` rotates. Portal's graphics-audit pass (`worker.mjs`) uses it to group a section's sentences and generate its `lower_third` label deterministically — see portal's `CLAUDE.md`.

**Graphics are always narrated ("Pattern 2").** `content_track: "graphic"` is **no longer supported** — `validate_job.py` hard-errors on it, as it does on any broll/named sentence with empty `text`. A graphic is a normal narrated `"broll"`/`"named"` sentence with `graphic_type` (`lower_third` | `infographic` | `list` | `info_callout`) and `variables` added — and those fields are set by **portal's graphics-audit pass**, never by the enrichment agent (AGENT_GUIDE.md explicitly forbids the agent from setting `graphic_type`, `variables`, or `visual_effect`).

Full spec: `AGENT_GUIDE.md`.

---

## Configuration

`config.toml` (copy from `config.example.toml`). Key sections:

| Key | Purpose |
|---|---|
| `pexels_api_keys` | Stock video + photos |
| `pixabay_api_keys` | Stock video + photos + BGM |
| `unsplash_api_keys` | Image fallback |
| `serper_api_keys` | Google Images for `content_track: "named"` |
| `openverse_client_id` / `_secret` | Openverse (CC-licensed diagrams/evidence imagery) — **currently dormant**: Cloudflare blocks this server's IP on every `api.openverse.org` endpoint (register, token, search), confirmed even with valid registered credentials (2026-07-13). Removed from `named_track_image_source_order`/`_DEFAULT_IMAGE_SOURCE_ORDER`; credentials kept in case a `[proxy]` is added later. `scripts/register_openverse.py` hits their `/v1/auth_tokens/register/` endpoint but must be run from a non-datacenter IP |
| `smithsonian_api_keys` | Smithsonian Open Access (CC0 museum/archival imagery) — free instant self-serve key at api.data.gov/signup. Skipped silently if unset, same convention as `serper_api_keys` |
| `[whisper]` | `model_size`, `device`, `compute_type` |
| `[app].max_image_ratio` | Soft cap on image clip fraction (code fallback 1.0 = uncapped; portal always writes the user's choice into `job.max_image_ratio`, which wins) |
| `[app].crossfade_transition` | ffmpeg xfade transition name for `combine_videos()`'s concat (default `"fade"`; `"hblur"` reads as a fast horizontal-blur "whip" cut — see `_XFADE_TRANSITIONS` in `combine.py`) |

### Archival image providers (NASA / Internet Archive / Smithsonian)

Free, public-domain/CC0 sources added to `named_track_image_source_order` for documentary/historical named-entity subjects (space, historical events, museum artifacts) — deliberately *not* added to `_DEFAULT_IMAGE_SOURCE_ORDER` (generic broll queries rarely match their collections and would just burn API calls). NASA's search response derives its own large-size image URL from `nasa_id` (no second request per candidate); Internet Archive's search only returns item identifiers, so each candidate needs one `archive.org/metadata/<id>` lookup to find its actual image file; Smithsonian requires an API key and most records carry no `online_media` at all, so results are filtered post-hoc. DVIDS (military/government imagery) was considered and dropped — its API requires a manually-requested key, not the instant self-serve registration the other three offer.

`app/services/media/_common.py::is_quota_exhaustion()` distinguishes hard quota exhaustion (HTTP 402, or a phrase like "usage limit"/"quota" in the body) from an ordinary transient 429 — on a match, `set_provider_cooldown()` is given a 6-hour cooldown instead of the short rate-limit backoffs used elsewhere, since quota exhaustion won't self-heal within a single job run. Wired into the three new providers plus Pexels/Pixabay (previously had no cooldown handling at all).

### Provider search-response cache

`app/services/media/_search_cache.py` caches the raw JSON *search responses* from every provider (SQLite, WAL mode, `storage/search_cache.sqlite3`) — distinct from `cache_images/`/`cache_videos/`/`cache_bgm/`, which cache the *downloaded asset files*. `_common.py::_cached_api_get_json()`/`_cached_api_post_json()` wrap the shared `_api_get_json()`/`_api_post_json()` HTTP helpers and are what every provider in `images.py`/`videos.py` actually calls now (except the dormant Openverse path, which never succeeds anyway). Cache key = `sha256(provider, url-with-secret-params-redacted, header-names, POST-body)` — redacting API-key query params means a rotated key doesn't fragment the cache, and the POST body is folded in for Serper (whose fixed-URL endpoint carries the actual query in the body). Controlled by `search_cache_enabled`/`search_cache_ttl_seconds` in `config.toml` (default on, 24h TTL) — matters most for portal's automatic same-title retries (`<title> (2)`, `(3)`...), which otherwise re-issue the exact same `visual_concepts` queries against every provider from scratch.

### AI avatar provider switch

`app/services/avatar.py` generates talking-head clips for avatar-marked sentences via `avatar_provider` config (`"segmind"` — default, active — or `"runpod"` — dormant fallback), resolved by `_provider()`. This is a **temporary, reversible** swap: RunPod's client code (`_submit_runpod`/`_extract_runpod_video_url`/`_runpod_endpoint`/`_runpod_headers`) is kept intact, just unused while `avatar_provider = "segmind"` — flip the config key to revert, same "keep dormant, don't delete" pattern as the Openverse image provider above.

Segmind's client (`_submit_segmind`/`_extract_segmind_video_url`) posts to its async `POST /v2/infinite-talk` endpoint (unwrapped body, `resolution` not `size`, auth via `x-api-key` not `Authorization: Bearer`), then polls `GET /requests/{id}/status` until `COMPLETED`/`FAILED`, then fetches `GET /requests/{id}` for the final payload. Confirmed via a live probe (2026-07-14): the completed payload's `output` field is a bare URL string, not nested — `_extract_segmind_video_url` handles that as its first case, with defensive fallbacks for other shapes kept as a safety net.

Both `_submit_runpod`/`_submit_segmind` return `(video_url, cost_usd)` — the real dollar cost as reported by the provider itself (RunPod: `output.cost`; Segmind: `metrics.cost`, also confirmed via the live probe), not an estimate. `generate_avatar_clip()` threads it into the per-clip `report` dict as `avatar_cost_usd`, and `_quality.py`'s `write_quality_report()` sums it (excluding dry-run entries) into `<title>.quality.json`'s `cost.avatar_cost_usd`, alongside the existing `cost.avatar_request_count`.

`scripts/segmind_avatar_probe.py` is the standalone verification tool for this integration — it hits the real Segmind API once (publishing the test image/audio via `avatar.publish_dir()`/`publish_asset()`) and prints raw JSON at every stage, without touching the pipeline. Use it again if Segmind's API shape ever changes unexpectedly.

### Minimax TTS: Algrow proxy (cheaper billing)

`app/services/tts/minimax.py`'s `minimax_tts()` (the `"minimax:"` voice_name engine) is routed through Algrow (https://algrow.online), a multi-provider TTS proxy, by default — toggle via config `minimax_provider` = `"algrow"` (default) or `"direct"` (dormant fallback, calls Minimax's own `t2a_v2` API directly with `minimax_api_key`/`minimax_group_id`, same "keep dormant, don't delete" pattern as the avatar provider switch above).

Algrow's `provider=minimax` path only accepts voice_ids **cloned inside Algrow's own account** — it has no knowledge of voices cloned directly with Minimax's own API/GroupId (confirmed live: `GET /api/voices/minimax` returned an empty list for voices that already existed directly in Minimax). `config.toml`'s `[app.minimax_algrow_voice_map]` table maps each stable `"minimax:"` voice_name (Minimax's own `moss_audio_*` id, what the portal UI and job.json actually store) to the voice_id Algrow issued for that same voice after cloning. **This table must stay the last entry in `[app]`** — a bracketed TOML subtable captures every key that follows it until the next `[section]` header.

`scripts/algrow_clone_from_minimax.py` is the one-off bootstrap that populated this map: for each existing voice it generates a ~45s reference sample via Minimax's *direct* API (bypassing the algrow-provider dispatch, since the clone source must be real Minimax audio), uploads it to Algrow's `POST /api/voices/minimax/clone` (requires ≥30s of audio, consumes one of the account's limited clone slots), and prints the resulting `voice_id` to add to the map. Run it again for any new Minimax voice added in the future.

Minimax's ~9.7min Algrow latency (below) broke the portal's voice-preview feature — its route only waits 120s before giving up, and a never-before-previewed voice has no cached file yet to fall back on, so the click failed with a generic "no supported source" audio error. Fix: `minimax.py` exposes a public `direct_minimax_tts()` (Minimax's own fast, synchronous API, bypassing the `minimax_provider` switch entirely) used by `scripts/minimax_preview.py` and by `algrow_clone_from_minimax.py`'s reference-sample generation — previews are a few fixed, cached-forever words, so there's no real cost benefit to routing them through Algrow, only a latency cost. Production narration is unaffected; it still goes through `minimax_tts()`'s config-driven dispatch.

`app/services/tts/algrow.py` implements the async job API: `POST /api/generate-simple` (multipart form: `script`/`voice_id`/`provider="minimax"`/`speed`/`pitch`/`volume`; **Algrow rejects any single call under 200 characters** — `_split_for_algrow()` merges a too-short trailing chunk into its predecessor rather than sending it standalone) returns `{job_id, status: "pending"}`, then `GET /api/job-status/{job_id}` is polled until `"completed"` (→ `audio_url`, a permanent CDN link, downloaded directly) or `"failed"`. Confirmed live (2026-07-14) via a real end-to-end `minimax_tts()` call — that test took **~9.7 minutes**, hence `algrow_timeout_seconds = 1200`. `_run_chunked()` gives every chunk its own full deadline rather than sharing one, since a multi-chunk script would otherwise starve chunk 2+ after chunk 1 alone ate most of a shared budget.

### ElevenLabs TTS: Algrow's premade catalog (`"elevenlabs:"` voices)

`"elevenlabs:"` voice_names route straight to `algrow_elevenlabs_tts()` in `app/services/tts/algrow.py` (dispatched from `resolve_tts_engine()`/`tts()` in `app/services/tts/__init__.py`) — there is no direct-ElevenLabs fallback to keep dormant, this is a brand new engine. Unlike Minimax, Algrow's `provider=elevenlabs` path draws from Algrow's own large **premade voice catalog** (`GET /api/voices`, confirmed live 2026-07-14 — supports `search`/`gender`/`age`/`language`/`accent`/`sort`/`page`/`page_size` query params) — the catalog's `voice_id` is used **directly**, no cloning or `[app.minimax_algrow_voice_map]`-style mapping needed. Each catalog entry also ships its own official `preview_url` (a public, unauthenticated ElevenLabs CDN link) — confirmed live to be directly downloadable.

`_algrow_submit_and_download()` is the shared generate/poll/download core used by both Minimax and ElevenLabs (`_algrow_minimax_tts_single`/`_algrow_elevenlabs_tts_single` just supply different `extra_fields` — `provider="elevenlabs"` additionally sends `model_id` (default `"eleven_multilingual_v2"`, override via `algrow_elevenlabs_model` config), `stability`/`similarity_boost` (both 0.5). A live end-to-end test (2026-07-14, voice_id `Ix8C14HEHgIQkJswik2o` / "Peter Baker") completed in **~11-16 seconds** — dramatically faster than Minimax's ~9.7 minutes on the same proxy, for reasons not fully understood (possibly per-provider backend differences on Algrow's side) — don't assume this holds for every ElevenLabs voice without spot-checking.

Because ElevenLabs generation is fast enough to fit inside a normal HTTP request/response cycle, `scripts/elevenlabs_preview.py` generates portal previews through the *real* Algrow path (unlike Minimax previews, which were moved off Algrow entirely — see the preview-latency note above) — its `PREVIEW_TEXT` is padded to 210 characters specifically to clear Algrow's 200-char minimum.

---

## Testing

```bash
make test               # unit tests (pytest, ~1s, no network): slot planning,
                        # query ladder, validate_job.py gate, scorer lazy-load races,
                        # media fetch circuit breakers (host blocks / provider cooldowns)
make validate-fixtures  # run validate_job.py over the committed fixture jobs
make smoke              # end-to-end pipeline on tests/fixtures/smoke-basic.job.json
                        # (needs network: TTS + stock APIs; SKIP_WHISPER=1 REVIDEO_ENABLED=0)
make smoke-graphics     # same on the named-track + narrated-graphics fixture (Revideo on)
```

Unit tests live in `tests/`; committed fixture jobs in `tests/fixtures/` (safe from portal's clean-slate, which only wipes `storage/tasks/`). The smoke targets copy a fixture into `storage/tasks/<name>/` and run `cli.py` on it. Fixture jobs have scaffold-quality `visual_concepts` (fine for pipeline plumbing tests, not for footage-quality checks) and are short enough to trip a few benign validator warnings (hook-zone rules assume 40+ sentence jobs).

For an ad-hoc job from arbitrary text, `scripts/sentence_prep.py --script <txt> --out storage/tasks/<t>/job.json --title <t>` still works as before.

Test Revideo standalone (no pipeline):

```bash
cd /home/deploy/AutoMoneyPrinterTurbo/revideo-worker
echo '{"type":"lower_third","outPath":"/tmp/test.mp4","duration":5,"width":1920,"height":1080,"fps":30,"variables":{"label":"Sony WH-1000XM5"}}' | node render.js
ffprobe -v error -show_entries format=duration -of compact /tmp/test.mp4

# info_callout — pass fewer than 3 labels to check the defensive skip path
echo '{"type":"info_callout","outPath":"/tmp/test2.mp4","duration":5,"width":1920,"height":1080,"fps":30,"variables":{"labels":["30-Hour Battery","Adaptive ANC","USB-C Fast Charge"]}}' | node render.js
```

---

## Portal Integration

This pipeline is the backend for `/home/deploy/portal/` (Next.js + SQLite). The `portal-worker` PM2 process drives the job pipeline:

1. **Phase 1** — AI agent (Claude or Codex via CLI) reads `AGENT_GUIDE.md`, writes the script, runs `sentence_prep.py`, enriches `job.json`, prints `JOB_JSON_PATH: /absolute/path`
2. **Graphics audit** — `runGraphicsAudit()` in portal's `scripts/worker.mjs` deterministically assigns one `lower_third` per named-entity section (grouped by `entity_name`, no LLM involved), then an LLM pass handles optional list/infographic/info_callout graphics and visual-effect distribution (`info_callout` only on named-entity sentences — a judgment call, unlike the deterministic lower_third). See portal's `CLAUDE.md` for details.
3. **Gate** — worker runs `venv/bin/python scripts/validate_job.py <path>`: `VALIDATION_ERROR` blocks Phase 2; `VALIDATION_WARNINGS` is logged but proceeds
4. **Phase 2** — worker runs `venv/bin/python cli.py --job <path>`

Check status: `pm2 list` — services are `portal-web` (Next.js, port 3000) and `portal-worker`.
`pm2` isn't on PATH by default in a fresh shell — it's installed at
`~/.npm-global/bin/pm2`. Either run `export PATH="$HOME/.npm-global/bin:$PATH"`
first, or call it directly as `~/.npm-global/bin/pm2 <command>`.

---

## Dependency Notes

- **Python 3.11+**, venv at `venv/`
- **FFmpeg** on system PATH (or set `ffmpeg_path` in `config.toml`)
- **Node.js 22+** — already present (portal-worker uses it); no new install needed
- **Revideo** — installed in `revideo-worker/node_modules/` (inside this repo); Puppeteer's Chromium cached at `~/.cache/puppeteer/`
- **Kokoro ONNX TTS** — optional, lives at `/home/deploy/kokoro-onnx/`, separate venv at `kokoro-venv/`
- **CLIP model** — lazy-loaded on first relevance check; cached in `~/.cache/`
- **NudeNet ONNX** — downloaded on first NSFW check; cached in `~/.cache/`

---

## Common Gotchas

- `VideoAspect.to_resolution()` returns `(width, height)` — not `.width`/`.height` attributes
- Every sentence must have non-empty `text` — an unmatched sentence gets a degenerate Whisper span at the end of the audio, which collapses every later sentence's planned slot (`validate_job.py` hard-errors on this, and on the legacy `content_track: "graphic"`)
- `zoompan` z values must be ≥ 1.0 — values < 1 produce negative x-offset → garbage output
- `combine_videos()` expects all clips to be H.264 MP4 at target resolution and 30 fps
- Revideo `renderVideo()` Puppeteer args go inside `settings.puppeteer.args`, not at top level
- Clip fetching is parallelised via `ThreadPoolExecutor` (`clip_fetch_workers` in config.toml, default 4). Revideo renders remain synchronous — Phase A renders graphics sequentially before footage-fetch futures are submitted. All scorer lazy-loads (CLIP model, NudeNet) must be thread-safe; use `_model_load_lock` (double-checked locking) as in `relevance.py`
- `entity_name` must be set (and identical) on every sentence in a named section — if the enrichment agent leaves it blank, portal's graphics-audit pass falls back to a fragile brand-token heuristic (leading word of `visual_concepts[0]`, lowercased) that can mislabel or duplicate `lower_third`s when `visual_concepts[0]` rotates within the section
- A job's `quality.json` can report `catastrophic_gap_count > 0` — a slot where the real fetch *and* every placeholder attempt failed. Check `catastrophic_gap_unrecovered_count`: 0 means the position-aware gap-fill pass patched it in place (no drift); anything above 0 means real, uncorrected timeline drift starting at that clip
