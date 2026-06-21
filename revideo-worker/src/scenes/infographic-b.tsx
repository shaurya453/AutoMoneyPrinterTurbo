import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant B — Horizontal Bars
// Bars extend leftward-to-rightward from a vertical axis; category labels sit in a
// right-aligned column to the left; value labels materialise at each bar tip.

const COLORS = [
  '#4f8ef7', '#f7964f', '#4fd1a0', '#f74f7e',
  '#b44ff7', '#f7e14f', '#4fcef7', '#f74fb3',
];

export default makeScene2D('infographic-b', function* (view) {
  const vars = useScene().variables;
  const title     = String(vars.get('title',  'Statistics')());
  const labels    = Array.from(vars.get('labels', ['A', 'B', 'C'])() as string[]);
  const rawValues = Array.from(vars.get('values', [1, 2, 3])()   as number[]);
  const unit      = String(vars.get('unit',   '')());
  const duration  = Number(vars.get('duration', 8)());

  const values = rawValues.map(Number);
  const n      = Math.min(labels.length, values.length);
  const maxVal = Math.max(...values.slice(0, n), 0.001);

  // Layout — all coordinates relative to canvas centre (1920 × 1080)
  const TITLE_Y    = -370;
  const BAR_H      = 52;
  const ROW_GAP    = 96;   // distance between bar-centre rows
  const BAR_LEFT   = -400; // x of the left edge of all bars (also the axis x)
  const MAX_BAR_W  = 860;  // bar width for the maximum value

  const totalH  = BAR_H + (n - 1) * ROW_GAP;
  const chartTop = -totalH / 2;
  const barYs    = Array.from({length: n}, (_, i) => chartTop + i * ROW_GAP + BAR_H / 2);
  const targetWs = values.slice(0, n).map(v => (v / maxVal) * MAX_BAR_W);

  const formatVal = (v: number) => {
    const s = Number.isInteger(v) ? String(v) : v.toFixed(1);
    return unit ? `${s} ${unit}` : s;
  };

  const STAGGER = 0.13;
  const BAR_DUR = 1.05;

  // Refs
  const titleRef = createRef<Txt>();
  const axisRef  = createRef<Rect>();
  const barRefs  = Array.from({length: n}, () => createRef<Rect>());
  const valRefs  = Array.from({length: n}, () => createRef<Txt>());

  view.add(
    <Rect width={1920} height={1080} fill={'#0a0a0a'}>
      {/* Title */}
      <Txt
        ref={titleRef}
        text={title}
        y={TITLE_Y}
        fontSize={52}
        fontWeight={700}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1680}
      />

      {/* Vertical axis line — sits just left of bars */}
      <Rect
        ref={axisRef}
        width={2}
        height={totalH + 48}
        fill={'#3a3a3a'}
        x={BAR_LEFT - 1}
        y={0}
        opacity={0}
      />

      {/* Category labels — right-aligned column left of axis */}
      {Array.from({length: n}, (_, i) => (
        <Txt
          text={labels[i]}
          x={BAR_LEFT - 26}
          y={barYs[i]}
          fontSize={26}
          fontWeight={400}
          fill={'#999999'}
          textAlign={'right'}
          maxWidth={300}
        />
      ))}

      {/* Bars — start collapsed, grow rightward */}
      {Array.from({length: n}, (_, i) => (
        <Rect
          ref={barRefs[i]}
          width={0}
          height={BAR_H}
          fill={COLORS[i % COLORS.length]}
          x={BAR_LEFT}
          y={barYs[i]}
          radius={4}
        />
      ))}

      {/* Value labels — appear at bar tip after bars finish */}
      {Array.from({length: n}, (_, i) => (
        <Txt
          ref={valRefs[i]}
          text={formatVal(values[i])}
          x={BAR_LEFT + targetWs[i] + 60}
          y={barYs[i]}
          fontSize={26}
          fontWeight={700}
          fill={'#ffffff'}
          opacity={0}
          textAlign={'center'}
          maxWidth={260}
        />
      ))}
    </Rect>,
  );

  // ── Animation ─────────────────────────────────────────────────────────
  yield* tween(0.50, v => titleRef().opacity(easeInOutCubic(v)));
  yield* tween(0.22, v => axisRef().opacity(easeInOutCubic(v)));

  // Bars grow left → right, staggered
  yield* all(
    ...barRefs.map((barRef, i) =>
      chain(
        waitFor(i * STAGGER),
        tween(BAR_DUR, v => {
          const w = easeInOutCubic(v) * targetWs[i];
          barRef().width(w);
          barRef().x(BAR_LEFT + w / 2);
        }),
      ),
    ),
  );

  // Value labels fade in together
  yield* all(...valRefs.map(ref => tween(0.35, v => ref().opacity(easeOutCubic(v)))));

  const animUsed = 0.50 + 0.22 + (n - 1) * STAGGER + BAR_DUR + 0.35;
  yield* waitFor(Math.max(0, duration - animUsed));
});
