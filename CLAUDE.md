# CLAUDE.md — Developer Guide

This file is read by Claude Code at the start of every session. Keep it up to date when architecture or workflows change.

---

## What This Project Does

Headless CLI pipeline: turns a plain-text script into a finished documentary-style MP4 with narration (edge-tts), stock footage, Ken Burns still images, lower-third labels (Revideo), subtitles, and background music. Driven by an AI agent (see `AGENT_GUIDE.md`).

```
script.txt  →  sentence_prep.py  →  job.json  →  [agent enriches]  →  cli.py  →  final.mp4
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
| `sentence_prep.py` | Splits a script into a stub job JSON |
| `app/services/pipeline.py` | Main orchestrator — TTS → Whisper → clip fetch → combine → subtitles → final |
| `app/services/video.py` | FFmpeg/MoviePy helpers: Ken Burns, `combine_videos()`, `generate_video()` |
| `app/services/material.py` | Stock footage/image search and download |
| `app/services/graphics.py` | Python wrapper for Revideo graphic clip rendering (subprocess) |
| `app/services/voice.py` | TTS: edge-tts or Kokoro ONNX |
| `app/services/nsfw.py` | NudeNet ONNX pixel-level NSFW gate — hard-rejects before relevance |
| `app/services/relevance.py` | CLIP ViT-B/32 ONNX relevance scoring vs. junk anchors |
| `app/services/subtitle.py` | Subtitle burn-in (Pillow, Inter SemiBold) |
| `app/models/schema.py` | `VideoAspect`, `VideoParams`, enums — `VideoAspect.to_resolution()` → `(w, h)` |
| `app/config.py` | Loads `config.toml` |
| `AGENT_GUIDE.md` | Full enrichment spec for the AI agent |
| `resource/songs/` | Local BGM MP3s (fallback when online search is empty) |
| `resource/backgrounds/` | Gradient background MP4s served by Revideo/Vite via symlinks in `revideo-worker/public/` |
| `resource/graphics/` | Static compositing assets (e.g. `lower_third_shadow.png`) |
| `resource/overlays/` | Motion overlay MP4s blended during rendering (screen / multiply modes) |
| `storage/tasks/<title>/` | Per-job working directory |

### Revideo motion-graphics worker

Lives at `revideo-worker/` (inside the repository root).

| File | Role |
|---|---|
| `render.js` | CLI: reads JSON from stdin, routes to project file by `type`+`variant`, calls `renderVideo()`, prints MP4 path to stdout |
| `src/projects/lower-third.ts` | Revideo project for `lower_third` (single variant) |
| `src/projects/infographic{,-b,-c,-d}.ts` | Revideo projects for infographic variants A–D |
| `src/projects/list{,-b,-c,-d}.ts` | Revideo projects for list variants A–D |
| `src/scenes/lower-third.tsx` | Lower third — left-anchored label with semi-transparent backdrop, fade in/out |
| `src/scenes/infographic{,-b,-c,-d}.tsx` | Scene files: A vert bars · B horiz bars · C lollipop · D callouts |
| `src/scenes/list{,-b,-c,-d}.tsx` | Scene files: A bullets · B numbered · C cascade · D card grid |
| `package.json` | `@revideo/{core,2d,renderer,vite-plugin,ui}` v0.10.4 |

---

## Pipeline Architecture

### Stage flow (inside `pipeline.start()`)

1. **TTS** — `voice.tts()` → `audio.mp3`; also used to produce edge-tts word-level timings for subtitles
2. **Whisper alignment** — `_get_sentence_timestamps()` (faster-whisper) aligns each narration sentence to an `(start, end)` span. **Graphic sentences (`content_track: "graphic"`) are filtered out before Whisper** and re-inserted with `(0.0, 0.0)` placeholder timestamps
3. **Pass 1 — clip planning** — compute footage duration for each sentence from Whisper timestamps. Graphic sentences override to their `duration` field
4. **Pass 2 — clip fetch / render** — for each sentence:
   - `"graphic"` → `graphics.render_graphic_clip()` (subprocess to `revideo-worker/render.js`)
   - `"named"` or `"broll"` → `_fetch_clip()` (search → NSFW gate → relevance → Ken Burns for images)
5. **Gap-fill** — if total footage < audio, extra generic clips are fetched
6. **Outro extension** — last clip looped to reach `audio_duration + 2 s`
7. **`combine_videos()`** — sequential concat with xfade crossfade → `temp/combined.mp4`
8. **`generate_video()`** — subtitles (Pillow), fade-out, BGM duck → `final.mp4`

### Ken Burns (still images)

**Portrait images** (h ≥ w): FIT-scaled to 95% of frame, centered on a blurred+darkened background (same image, downscale-upscale blur at 6%, 50% brightness). No animation — static display.

**Landscape images** (w > h): COVER-CROP fills the full frame — no blurred background visible. Animation is chosen randomly from a pool derived from the image's aspect-ratio overflow:
- `zoom_in` — always in pool (4% extra zoom via 8× zoompan on the cover-cropped base)
- `pan_lr` / `pan_rl` — added when the image is wider than the frame proportionally (`h_excess > 2% of frame_w`)
- `pan_ud` — added when the image is taller than the frame proportionally after cover-scale (`v_excess > 2% of frame_h`)

`_pick_animation(allowed)` enforces no-consecutive-repeat across clips. `_PAN_Z = 1.04`. Ease-out quadratic timing on all pan/zoom travel.

Primary path: `video._render_ken_burns_ffmpeg()`. Cover mode uses `-vf` (single input, no overlay). Pan animations at 2× PIL scale + 2:1 lanczos FFmpeg downscale for sub-pixel smooth motion. MoviePy fallback: `video.apply_ken_burns()`.

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

`render.js` maps `type`+`variant` → Revideo project file via `VARIANT_POOL`. `graphics.py` picks the variant, enforcing no-consecutive-repeat rotation unless a named `style` overrides it. Adding a new type = new `src/scenes/foo.tsx` + new `src/projects/foo.ts` + new entry in `VARIANT_POOL` + matching entry in `graphics.py`'s `STYLE_MAP` and `_POOL_SIZES`.

The rendered H.264 MP4 slots into `temp/clips/` identically to any stock clip — `combine_videos()` sees no difference.

**Supported `type` values:**

| `type` | Variants | Key `variables` |
|---|---|---|
| `lower_third` | single | `label` (2–6 words) |
| `infographic` | A: vertical bars · B: horizontal bars · C: lollipop · D: number callouts | `title`, `labels[]`, `values[]`, `unit` |
| `list` | A: bullets · B: numbered · C: cascade reveal · D: card grid | `items[]`, `title` |

Named style hints (e.g. `"style": "callouts"`) map to specific variants — see `AGENT_GUIDE.md`. The pipeline rotates variants automatically when no style is specified to avoid consecutive repeats.

---

## Job JSON Structure

Created by `sentence_prep.py`, enriched by the AI agent, consumed by `cli.py`.

Top-level fields an agent must set: `video_topic`, `video_type`, `motif_palette` (thematic only), and per-sentence `visual_concepts`, `content_track`, `visual_caption`, `media_type`.

For graphic sentences, the agent sets `content_track: "graphic"`, `graphic_type`, `duration`, `variables`, and leaves `text: ""`. Graphic sentences must **not** appear in `video_script`.

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
| `[whisper]` | `model_size`, `device`, `compute_type` |
| `[app].max_image_ratio` | Soft cap on image clip fraction (default 0.25) |

---

## Testing

No automated test suite. Manual test jobs live in `storage/tasks/`:

| Job | Tests |
|---|---|
| `test-zoom-fadeout/` | Ken Burns on image sentences (sentences 1, 2, 4 have `media_type: "image"`) |
| `test-graphic/` | Revideo lower_third integration — first sentence has `graphic_type: "lower_third"` |

Quick test workflow:

```bash
# Delete previous output so cli.py produces fresh files
rm -rf storage/tasks/test-graphic/temp storage/tasks/test-graphic/final.mp4 storage/tasks/test-graphic/audio.mp3
venv/bin/python cli.py --job storage/tasks/test-graphic/job.json
```

Test Revideo standalone (no pipeline):

```bash
cd /home/deploy/AutoMoneyPrinterTurbo/revideo-worker
echo '{"type":"lower_third","outPath":"/tmp/test.mp4","duration":5,"width":1920,"height":1080,"fps":30,"variables":{"label":"Sony WH-1000XM5"}}' | node render.js
ffprobe -v error -show_entries format=duration -of compact /tmp/test.mp4
```

---

## Portal Integration

This pipeline is the backend for `/home/deploy/portal/` (Next.js + SQLite). The `portal-worker` PM2 process drives two phases:

1. **Phase 1** — AI agent (Claude via CLI) reads `AGENT_GUIDE.md`, writes the script, runs `sentence_prep.py`, enriches `job.json`, prints `JOB_JSON_PATH: /absolute/path`
2. **Gate** — worker runs `venv/bin/python validate_job.py <path>`: `VALIDATION_ERROR` blocks Phase 2; `VALIDATION_WARNINGS` is logged but proceeds
3. **Phase 2** — worker runs `venv/bin/python cli.py --job <path>`

Check status: `pm2 list` — services are `portal-web` (Next.js, port 3000) and `portal-worker`.

---

## Dependency Notes

- **Python 3.11+**, venv at `venv/`
- **FFmpeg** on system PATH (or set `ffmpeg_path` in `config.toml`)
- **Node.js 22+** — already present (portal-worker uses it); no new install needed
- **Revideo** — installed in `/home/deploy/revideo-worker/node_modules/`; Puppeteer's Chromium cached at `~/.cache/puppeteer/`
- **Kokoro ONNX TTS** — optional, lives at `/home/deploy/kokoro-onnx/`, separate venv at `kokoro-venv/`
- **CLIP model** — lazy-loaded on first relevance check; cached in `~/.cache/`
- **NudeNet ONNX** — downloaded on first NSFW check; cached in `~/.cache/`

---

## Common Gotchas

- `VideoAspect.to_resolution()` returns `(width, height)` — not `.width`/`.height` attributes
- Graphic sentences must be absent from `video_script` (TTS only narrates that field)
- `zoompan` z values must be ≥ 1.0 — values < 1 produce negative x-offset → garbage output
- `combine_videos()` expects all clips to be H.264 MP4 at target resolution and 30 fps
- Revideo `renderVideo()` Puppeteer args go inside `settings.puppeteer.args`, not at top level
- The pipeline is sequential — no parallel clip fetching; Revideo renders are also synchronous
