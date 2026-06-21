import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {
  all,
  chain,
  createRef,
  easeInOutCubic,
  tween,
  useScene,
  waitFor,
} from '@revideo/core';

const COLORS = [
  '#4f8ef7', // blue
  '#f7964f', // orange
  '#4fd1a0', // green
  '#f74f7e', // pink
  '#b44ff7', // purple
  '#f7e14f', // yellow
  '#4fcef7', // cyan
  '#f74fb3', // magenta
];

export default makeScene2D('infographic', function* (view) {
  const vars = useScene().variables;

  const title = String(vars.get('title', 'Statistics')());
  const labels = Array.from(vars.get('labels', ['A', 'B', 'C'])() as string[]);
  const rawValues = Array.from(vars.get('values', [1, 2, 3])() as number[]);
  const unit = String(vars.get('unit', '')());
  const duration = Number(vars.get('duration', 8)());

  // Coerce to numbers in case JSON passes strings
  const values = rawValues.map(Number);
  const n = Math.min(labels.length, values.length);
  const maxVal = Math.max(...values.slice(0, n), 0.001);

  // Layout constants (canvas is 1920 × 1080, origin at center)
  const CHART_W = 1600;
  const MAX_BAR_H = 500;
  const AXIS_Y = 260; // y-coordinate of the axis line (bottom of bars)
  const BAR_SLOT_W = CHART_W / n;
  const BAR_W = Math.min(BAR_SLOT_W * 0.55, 190);
  const STAGGER = 0.12;
  const BAR_DUR = 1.3;

  const targetHeights = values.slice(0, n).map(v => (v / maxVal) * MAX_BAR_H);
  const barXs = Array.from({length: n}, (_, i) => -CHART_W / 2 + BAR_SLOT_W * (i + 0.5));

  const formatVal = (v: number): string => {
    const s = Number.isInteger(v) ? String(v) : v.toFixed(1);
    return unit ? `${s} ${unit}` : s;
  };

  // Refs for animated elements
  const titleRef = createRef<Txt>();
  const axisRef = createRef<Rect>();
  const barRefs = Array.from({length: n}, () => createRef<Rect>());
  const valRefs = Array.from({length: n}, () => createRef<Txt>());

  view.add(
    <Rect width={1920} height={1080} fill={'#0a0a0a'}>
      {/* Title */}
      <Txt
        ref={titleRef}
        text={title}
        y={-420}
        fontSize={58}
        fontWeight={700}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1680}
      />

      {/* Horizontal axis */}
      <Rect
        ref={axisRef}
        width={CHART_W + 60}
        height={2}
        fill={'#444444'}
        y={AXIS_Y}
        opacity={0}
      />

      {/* Category labels — static, visible from start */}
      {Array.from({length: n}, (_, i) => (
        <Txt
          text={labels[i]}
          x={barXs[i]}
          y={AXIS_Y + 46}
          fontSize={24}
          fontWeight={400}
          fill={'#999999'}
          textAlign={'center'}
          maxWidth={BAR_SLOT_W - 12}
        />
      ))}

      {/* Bars — start at height 0, animated to target */}
      {Array.from({length: n}, (_, i) => (
        <Rect
          ref={barRefs[i]}
          width={BAR_W}
          height={0}
          fill={COLORS[i % COLORS.length]}
          x={barXs[i]}
          y={AXIS_Y}
          radius={6}
        />
      ))}

      {/* Value labels — positioned at final heights, revealed after bars grow */}
      {Array.from({length: n}, (_, i) => (
        <Txt
          ref={valRefs[i]}
          text={formatVal(values[i])}
          x={barXs[i]}
          y={AXIS_Y - targetHeights[i] - 38}
          fontSize={28}
          fontWeight={700}
          fill={'#ffffff'}
          opacity={0}
          textAlign={'center'}
        />
      ))}
    </Rect>,
  );

  // ── Animation sequence ──────────────────────────────────────────────

  // 1. Title fades in
  yield* tween(0.6, v => titleRef().opacity(easeInOutCubic(v)));

  // 2. Axis draws in
  yield* tween(0.25, v => axisRef().opacity(easeInOutCubic(v)));

  // 3. Bars grow from the axis upward, staggered
  yield* all(
    ...barRefs.map((barRef, i) =>
      chain(
        waitFor(i * STAGGER),
        tween(BAR_DUR, v => {
          const h = easeInOutCubic(v) * targetHeights[i];
          barRef().height(h);
          barRef().y(AXIS_Y - h / 2);
        }),
      ),
    ),
  );

  // 4. Value labels all fade in together
  yield* all(...valRefs.map(ref => tween(0.4, v => ref().opacity(easeInOutCubic(v)))));

  // 5. Hold for the remainder of the requested duration
  const animUsed = 0.6 + 0.25 + (n - 1) * STAGGER + BAR_DUR + 0.4;
  yield* waitFor(Math.max(0, duration - animUsed));
});
