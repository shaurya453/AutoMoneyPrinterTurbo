/**
 * CLI wrapper: reads a JSON payload from stdin, renders a Revideo graphic
 * segment, and writes the output file path to stdout.
 *
 * stdin JSON fields:
 *   type       – graphic type: "title_card" | "infographic" | "transition"
 *   outPath    – absolute path for the rendered MP4
 *   duration   – clip length in seconds
 *   width      – output width in pixels  (default 1920)
 *   height     – output height in pixels (default 1080)
 *   fps        – frames per second       (default 30)
 *   variables  – key/value pairs passed to the Revideo scene
 */
import {renderVideo} from '@revideo/renderer';
import path from 'path';
import {fileURLToPath} from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Maps graphic type → ordered pool of Revideo project files (one per variant).
// Variant 0 is the default (original). The caller passes a `variant` index so
// graphics.py can enforce no-consecutive-repeat selection without needing state here.
const VARIANT_POOL = {
  title_card: [
    path.join(__dirname, 'src', 'project.ts'),                    // A — fade-in centred
    path.join(__dirname, 'src', 'projects', 'title-card-b.ts'),   // B — kinetic reveal
    path.join(__dirname, 'src', 'projects', 'title-card-c.ts'),   // C — left-bar frame
    path.join(__dirname, 'src', 'projects', 'title-card-d.ts'),   // D — cold slide (editorial)
  ],
  infographic: [
    path.join(__dirname, 'src', 'projects', 'infographic.ts'),    // A — vertical bars
    path.join(__dirname, 'src', 'projects', 'infographic-b.ts'),  // B — horizontal bars
    path.join(__dirname, 'src', 'projects', 'infographic-c.ts'),  // C — lollipop chart
    path.join(__dirname, 'src', 'projects', 'infographic-d.ts'),  // D — number callouts
  ],
  transition: [
    path.join(__dirname, 'src', 'projects', 'transition.ts'),     // A — line + label
    path.join(__dirname, 'src', 'projects', 'transition-b.ts'),   // B — panel sweep
    path.join(__dirname, 'src', 'projects', 'transition-c.ts'),   // C — corner brackets
    path.join(__dirname, 'src', 'projects', 'transition-d.ts'),   // D — crosshair
  ],
  list: [
    path.join(__dirname, 'src', 'projects', 'list.ts'),           // A — bullet list
    path.join(__dirname, 'src', 'projects', 'list-b.ts'),         // B — numbered list
  ],
};

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
    type = 'title_card',
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
    process.stderr.write(`render.js: unknown type "${type}", falling back to title_card\n`);
  }
  const activePool = pool ?? VARIANT_POOL.title_card;
  const projectFile = activePool[variant % activePool.length];

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
