import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant D — Number Callouts
// Each data point is presented as one large, boldly-coloured number that counts up
// from zero, with the category label sitting beneath it. No axes, no bars — the
// emphasis is on the magnitude of each individual figure rather than comparison.
// Works best for 2–4 high-impact statistics read aloud by the narrator.

const COLORS = [
  '#4f8ef7', '#f7964f', '#4fd1a0', '#f74f7e',
  '#b44ff7', '#f7e14f', '#4fcef7', '#f74fb3',
];

export default makeScene2D('infographic-d', function* (view) {
  const vars = useScene().variables;

  const title     = String(vars.get('title', 'Statistics')());
  const labels    = Array.from(vars.get('labels', ['A', 'B', 'C'])() as string[]);
  const rawValues = Array.from(vars.get('values', [1, 2, 3])() as number[]);
  const unit      = String(vars.get('unit', '')());
  const duration  = Number(vars.get('duration', 8)());

  const values = rawValues.map(Number);
  const n      = Math.min(labels.length, values.length);

  const SLOT_W  = 1600 / n;
  const SLOT_XS = Array.from({length: n}, (_, i) => -800 + SLOT_W * (i + 0.5));

  // Vertical anchors
  const NUM_Y   = -20;
  const UNIT_Y  =  80;
  const LABEL_Y = 116;

  const COUNT_DUR = 1.0;
  const STAGGER   = 0.18;

  // Format the counted/final number for display
  const formatCount = (v: number, target: number): string => {
    if (Number.isInteger(target)) return String(Math.round(v));
    return v.toFixed(1);
  };

  // Refs
  const titleRef = createRef<Txt>();
  const divRef   = createRef<Rect>();
  const numRefs  = Array.from({length: n}, () => createRef<Txt>());
  const unitRefs = Array.from({length: n}, () => createRef<Txt>());
  const lblRefs  = Array.from({length: n}, () => createRef<Txt>());

  view.add(
    <Rect width={1920} height={1080} fill={'#0a0a0a'}>
      {/* Title */}
      <Txt
        ref={titleRef}
        text={title}
        y={-400}
        fontSize={52}
        fontWeight={700}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1680}
      />

      {/* Thin divider below title */}
      <Rect
        ref={divRef}
        width={0}
        height={1}
        fill={'#2a2a2a'}
        y={-320}
      />

      {/* Large counted numbers */}
      {Array.from({length: n}, (_, i) => (
        <Txt
          ref={numRefs[i]}
          text={'0'}
          x={SLOT_XS[i]}
          y={NUM_Y}
          fontSize={118}
          fontWeight={800}
          fill={COLORS[i % COLORS.length]}
          opacity={0}
          textAlign={'center'}
        />
      ))}

      {/* Unit labels (shown only when unit is non-empty) */}
      {Array.from({length: n}, (_, i) => (
        <Txt
          ref={unitRefs[i]}
          text={unit}
          x={SLOT_XS[i]}
          y={UNIT_Y}
          fontSize={26}
          fontWeight={300}
          fill={'#666666'}
          opacity={0}
          textAlign={'center'}
        />
      ))}

      {/* Category labels */}
      {Array.from({length: n}, (_, i) => (
        <Txt
          ref={lblRefs[i]}
          text={labels[i]}
          x={SLOT_XS[i]}
          y={LABEL_Y}
          fontSize={28}
          fontWeight={400}
          fill={'#888888'}
          opacity={0}
          textAlign={'center'}
          maxWidth={SLOT_W - 20}
        />
      ))}
    </Rect>,
  );

  // ── Animation ─────────────────────────────────────────────────────────

  // 1. Title + divider
  yield* tween(0.4, v => titleRef().opacity(easeInOutCubic(v)));
  yield* tween(0.25, v => divRef().width(easeInOutCubic(v) * 1600));

  // 2. Numbers count up, staggered — fade in fast at the start of each tween
  yield* all(
    ...numRefs.map((numRef, i) =>
      chain(
        waitFor(i * STAGGER),
        tween(COUNT_DUR, v => {
          numRef().opacity(Math.min(1, v * 5));
          numRef().text(formatCount(easeInOutCubic(v) * values[i], values[i]));
        }),
      ),
    ),
  );

  // 3. Labels and units fade in together
  yield* all(
    ...lblRefs.map(ref => tween(0.3, v => ref().opacity(easeOutCubic(v)))),
    ...(unit ? unitRefs.map(ref => tween(0.3, v => ref().opacity(easeOutCubic(v)))) : []),
  );

  const animUsed = 0.4 + 0.25 + (n - 1) * STAGGER + COUNT_DUR + 0.3;
  yield* waitFor(Math.max(0, duration - animUsed));
});
