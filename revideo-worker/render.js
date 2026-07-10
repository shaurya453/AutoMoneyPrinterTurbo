/**
 * CLI wrapper: reads a JSON payload from stdin, renders a Revideo graphic
 * segment, and writes the output file path to stdout.
 *
 * stdin JSON fields:
 *   type       – graphic type: "lower_third" | "infographic" | "list"
 *   outPath    – absolute path for the rendered MP4
 *   duration   – clip length in seconds
 *   width      – output width in pixels  (default 1920)
 *   height     – output height in pixels (default 1080)
 *   fps        – frames per second       (default 30)
 *   variables  – key/value pairs passed to the Revideo scene
 */
import {renderVideo} from '@revideo/renderer';
import fs from 'fs';
import path from 'path';
import {fileURLToPath} from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Single source of truth for variant metadata, shared with graphics.py.
// Gradient backgrounds are served from revideo-worker/public/ by Vite
// (symlinks in public/ point to resource/*.mp4 — no file:// needed).
const MANIFEST = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'variants.json'), 'utf-8'),
);
const BG_VIDEOS = MANIFEST.bg_videos;

// Maps graphic type → ordered pool of Revideo project files (one per variant).
// Variant 0 is the default (original). The caller passes a `variant` index so
// graphics.py can enforce no-consecutive-repeat selection without needing state here.
const VARIANT_POOL = Object.fromEntries(
  Object.entries(MANIFEST.types).map(([type, cfg]) => [
    type,
    cfg.variants.map(v => path.join(__dirname, v.project)),
  ]),
);

async function main() {
  // Read all stdin
  let raw = '';
  for await (const chunk of process.stdin) {
    raw += chunk;
  }

  let input;
  try {
    input = JSON.parse(raw);
  } catch (err) {
    process.stderr.write(`render.js: invalid JSON on stdin: ${err}\n`);
    process.exit(1);
  }

  const {
    type = 'lower_third',
    variant = 0,   // pool index; graphics.py picks to avoid consecutive repeats
    outPath,
    duration = 5,
    width = 1920,
    height = 1080,
    fps = 30,
    variables = {},
  } = input;

  if (!outPath) {
    process.stderr.write('render.js: outPath is required\n');
    process.exit(1);
  }

  const pool = VARIANT_POOL[type];
  if (!pool) {
    process.stderr.write(`render.js: unknown graphic type "${type}"\n`);
    process.exit(1);
  }
  const projectFile = pool[variant % pool.length];

  // Inject a gradient background for infographic and list types.
  // Python (graphics.py) picks the background for no-consecutive-repeat enforcement;
  // fall back to random selection only when called standalone (e.g. manual testing).
  if (MANIFEST.types[type].uses_bg_video && !variables.bgVideo) {
    variables.bgVideo = BG_VIDEOS[Math.floor(Math.random() * BG_VIDEOS.length)];
  }

  try {
    await renderVideo({
      projectFile,
      variables: {...variables, duration},
      settings: {
        outFile: path.basename(outPath),
        outDir: path.dirname(outPath),
        workers: 1,
        dimensions: [width, height],
        fps,
        logProgress: false,
        puppeteer: {
          args: ['--no-sandbox', '--disable-setuid-sandbox'],
        },
      },
    });

    process.stdout.write(outPath + '\n');
    process.exit(0);
  } catch (err) {
    process.stderr.write(`render.js: renderVideo failed: ${err}\n`);
    process.exit(1);
  }
}

main();
