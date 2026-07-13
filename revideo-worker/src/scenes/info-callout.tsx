import '../global.css';
import {Circle, Line, makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Info Callout — three fixed-position text boxes (product feature callouts),
// each connected by an animated PCB-trace-style leader line (45° diagonal
// then a horizontal/vertical run) to a dot. Dots stay close to their own box
// so the frame's centre third — where the named-entity product sits — is
// left clear. Same colorkey-compositing convention as lower-third.tsx: solid
// black background keyed to transparent by the pipeline, box/text/line/dot
// stay opaque. Fade in/out of the whole composited layer is applied by
// FFmpeg, not here — this scene only animates its own box/line/dot reveal.

const ACCENT = '#ffffff';
// Thin border so the white lines/dots stay visible over light/white regions
// of the product photo. NOT pure black (#000000) — that's the exact colorkey
// key colour, so a black border would just get keyed out along with the
// background. #1a1a1a is the same "reads as black, survives colorkey" shade
// already used for the box backdrops.
const BORDER_COLOR = '#1a1a1a';
const LINE_WIDTH = 5;
const LINE_BORDER_WIDTH = LINE_WIDTH + 4; // +2px border ring on each side
const DOT_SIZE = 16;
const DOT_BORDER_SIZE = DOT_SIZE + 4; // +2px border ring on each side

// Fixed corner positions — there's no per-image object detection telling us
// where the product actually sits, so a tasteful fixed layout (matching
// lower-third's hardcoded single position) is the right call.
const BOX_POS: [number, number][] = [
  [-700, -380], // top-left
  [700, -380],  // top-right
  [0, 400],     // bottom-centre
];
// Leader-line endpoints stay close to their own box, well clear of the
// frame's centre third where the product itself sits.
const DOT_POS: [number, number][] = [
  [-420, -180],
  [420, -180],
  [0, 220],
];
// PCB-trace look: each leader is a 45°-diagonal segment from the box, then a
// horizontal/vertical run into the dot — never a bare diagonal straight shot.
// Box 3 -> dot 3 is already perfectly vertical (same x), so it needs no elbow.
const LINE_PATHS: [number, number][][] = [
  [BOX_POS[0], [-500, -180], DOT_POS[0]],
  [BOX_POS[1], [500, -180], DOT_POS[1]],
  [BOX_POS[2], DOT_POS[2]],
];

export default makeScene2D('info-callout', function* (view) {
  const vars = useScene().variables;

  const rawLabels = Array.from(vars.get('labels', [] as string[])() as string[]);
  const duration  = Number(vars.get('duration', 5)());

  // Defensive: skip empty/missing entries and cap at 3 — a validator-caught
  // bad shape upstream shouldn't be able to crash the render.
  const labels = rawLabels.map(l => String(l ?? '').trim()).filter(Boolean).slice(0, 3);
  const n = labels.length;

  // Base ("ideal, unhurried") per-box reveal timings, then scaled down to fit
  // whatever `duration` this clip actually gets — it's whisper-driven and can
  // be short, so this guarantees all n callouts fully complete rather than
  // getting cut off mid-animation, just faster on a short clip (floored at
  // 25% speed so it never becomes an unreadable instant snap).
  const BASE_STAGGER  = 0.28;
  const BASE_BOX_DUR  = 0.32;
  const BASE_LINE_DUR = 0.28;
  const BASE_DOT_DUR  = 0.16;

  // 30% of clip duration for the reveal animation, matching the existing
  // infographic scenes' convention (their count-up/bar-fill animations are
  // also budgeted at duration * 0.30, with the remainder held static).
  const baseTotal = n > 0 ? (n - 1) * BASE_STAGGER + BASE_BOX_DUR + BASE_LINE_DUR + BASE_DOT_DUR : 0;
  const introBudget = duration * 0.30;
  const scale = baseTotal > 0 ? Math.max(0.25, Math.min(1, introBudget / baseTotal)) : 1;

  const STAGGER  = BASE_STAGGER * scale;
  const BOX_DUR  = BASE_BOX_DUR * scale;
  const LINE_DUR = BASE_LINE_DUR * scale;
  const DOT_DUR  = BASE_DOT_DUR * scale;

  const boxRefs        = Array.from({length: n}, () => createRef<Rect>());
  const lineRefs       = Array.from({length: n}, () => createRef<Line>());
  const lineBorderRefs = Array.from({length: n}, () => createRef<Line>());
  const dotRefs        = Array.from({length: n}, () => createRef<Circle>());
  const dotBorderRefs  = Array.from({length: n}, () => createRef<Circle>());

  view.add(
    <Rect width={1920} height={1080} fill={'#000000'} layout={false}>
      {/* Border lines first (wider, dark), main white lines on top (narrower)
          — leaves a thin border ring visible along both edges. Boxes paint
          over the near end, dots over the tip.
          antialiased=false: a soft-AA edge on a thin stroke sits right in the
          colorkey's black-threshold/blend zone and gets partially (and
          unevenly) keyed out, producing a speckled/glitchy fringe. A hard,
          non-antialiased edge is either fully the stroke colour or fully
          pure black, so colorkey classifies every pixel cleanly. */}
      {labels.map((_, i) => (
        <Line
          ref={lineBorderRefs[i]}
          points={LINE_PATHS[i]}
          stroke={BORDER_COLOR}
          lineWidth={LINE_BORDER_WIDTH}
          lineCap={'square'}
          lineJoin={'miter'}
          antialiased={false}
          end={0}
        />
      ))}
      {labels.map((_, i) => (
        <Line
          ref={lineRefs[i]}
          points={LINE_PATHS[i]}
          stroke={ACCENT}
          lineWidth={LINE_WIDTH}
          lineCap={'square'}
          lineJoin={'miter'}
          antialiased={false}
          end={0}
        />
      ))}
      {labels.map((label, i) => (
        <Rect
          ref={boxRefs[i]}
          x={BOX_POS[i][0]}
          y={BOX_POS[i][1]}
          fill={'#1a1a1a'}
          padding={[12, 20]}
          radius={8}
          maxWidth={420}
          layout={true}
          opacity={0}
        >
          <Txt
            text={label}
            fontSize={34}
            fontWeight={600}
            fontFamily={'Playfair Display, serif'}
            fill={'#ffffff'}
            textAlign={'center'}
            textWrap={true}
          />
        </Rect>
      ))}
      {labels.map((_, i) => (
        <Circle
          ref={dotBorderRefs[i]}
          x={DOT_POS[i][0]}
          y={DOT_POS[i][1]}
          width={DOT_BORDER_SIZE}
          height={DOT_BORDER_SIZE}
          fill={BORDER_COLOR}
          antialiased={false}
          opacity={0}
        />
      ))}
      {labels.map((_, i) => (
        <Circle
          ref={dotRefs[i]}
          x={DOT_POS[i][0]}
          y={DOT_POS[i][1]}
          width={DOT_SIZE}
          height={DOT_SIZE}
          fill={ACCENT}
          antialiased={false}
          opacity={0}
        />
      ))}
    </Rect>
  );

  if (n > 0) {
    yield* all(
      ...labels.map((_, i) => chain(
        waitFor(i * STAGGER),
        tween(BOX_DUR, v => boxRefs[i]().opacity(easeOutCubic(v))),
        tween(LINE_DUR, v => {
          const e = easeInOutCubic(v);
          lineRefs[i]().end(e);
          lineBorderRefs[i]().end(e);
        }),
        tween(DOT_DUR, v => {
          const o = easeOutCubic(v);
          dotRefs[i]().opacity(o);
          dotBorderRefs[i]().opacity(o);
        }),
      )),
    );
  }

  const animUsed = n > 0 ? (n - 1) * STAGGER + BOX_DUR + LINE_DUR + DOT_DUR : 0;
  const holdDur = Math.max(0, duration - animUsed);
  if (holdDur > 0) {
    yield* waitFor(holdDur);
  }
});
