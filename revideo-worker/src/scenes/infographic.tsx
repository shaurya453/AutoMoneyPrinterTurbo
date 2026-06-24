import '../global.css';
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
  '#4f8ef7', '#f7964f', '#4fd1a0', '#f74f7e',
  '#b44ff7', '#f7e14f', '#4fcef7', '#f74fb3',
];

export default makeScene2D('infographic', function* (view) {
  const vars = useScene().variables;

  const title = String(vars.get('title', 'Statistics')());
  const labels = Array.from(vars.get('labels', ['A', 'B', 'C'])() as string[]);
  const rawValues = Array.from(vars.get('values', [1, 2, 3])() as number[]);
  const unit = String(vars.get('unit', '')());
  const duration = Number(vars.get('duration', 8)());

  const values = rawValues.map(Number);
  const n = Math.min(labels.length, values.length);
  const maxVal = Math.max(...values.slice(0, n), 0.001);

  const CHART_W = 1580;
  const MAX_BAR_H = 480;
  const AXIS_Y = 250;
  const BAR_SLOT_W = CHART_W / n;
  const BAR_W = Math.min(BAR_SLOT_W * 0.55, 180);
  const STAGGER = 0.12;
  const BAR_DUR = 1.3;

  const targetHeights = values.slice(0, n).map(v => (v / maxVal) * MAX_BAR_H);
  const barXs = Array.from({length: n}, (_, i) => -CHART_W / 2 + BAR_SLOT_W * (i + 0.5));

  const formatVal = (v: number): string => {
    const s = Number.isInteger(v) ? String(v) : v.toFixed(1);
    return unit ? `${s} ${unit}` : s;
  };

  // Base value label y positions (above each bar tip)
  const baseValYs = targetHeights.map(h => AXIS_Y - h - 48);

  // Anti-collision: if two adjacent labels (sorted by height desc) are within 38px
  // vertically, alternate them up/down by 18px.
  const valYs = [...baseValYs];
  const sortedIdx = Array.from({length: n}, (_, i) => i)
    .sort((a, b) => targetHeights[b] - targetHeights[a]);
  for (let k = 0; k < sortedIdx.length - 1; k++) {
    const a = sortedIdx[k];
    const b = sortedIdx[k + 1];
    if (Math.abs(valYs[a] - valYs[b]) < 38) {
      valYs[a] -= 18;
      valYs[b] += 18;
    }
  }

  const titleRef = createRef<Txt>();
  const axisRef = createRef<Rect>();
  const barRefs = Array.from({length: n}, () => createRef<Rect>());
  const valRefs = Array.from({length: n}, () => createRef<Txt>());

  view.add(
    <Rect width={1920} height={1080} fill={'#0a0a0a'} layout={false}>
      <Txt
        ref={titleRef}
        text={title}
        x={0}
        y={-420}
        fontSize={56}
        fontWeight={700}
        fontFamily={'Inter, sans-serif'}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        justifyContent={'center'}
        textWrap={true}
        width={1680}
      />
      <Rect
        ref={axisRef}
        width={CHART_W + 60}
        height={2}
        fill={'#444444'}
        y={AXIS_Y}
        opacity={0}
      />

      {/* Category labels */}
      {Array.from({length: n}, (_, i) => (
        <Txt
          text={labels[i]}
          x={barXs[i]}
          y={AXIS_Y + 44}
          fontSize={20}
          fontWeight={400}
          fontFamily={'Inter, sans-serif'}
          fill={'#999999'}
          textAlign={'center'}
          justifyContent={'center'}
          width={BAR_SLOT_W - 12}
          textWrap={true}
        />
      ))}

      {/* Bars */}
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

      {/* Value labels — constrained to bar slot width to prevent overflow */}
      {Array.from({length: n}, (_, i) => (
        <Txt
          ref={valRefs[i]}
          text={formatVal(values[i])}
          x={barXs[i]}
          y={valYs[i]}
          fontSize={22}
          fontWeight={700}
          fontFamily={'Inter, sans-serif'}
          fill={'#ffffff'}
          opacity={0}
          textAlign={'center'}
          justifyContent={'center'}
          width={BAR_SLOT_W - 20}
          textWrap={true}
        />
      ))}
    </Rect>,
  );

  yield* tween(0.6, v => titleRef().opacity(easeInOutCubic(v)));
  yield* tween(0.25, v => axisRef().opacity(easeInOutCubic(v)));

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

  yield* all(...valRefs.map(ref => tween(0.4, v => ref().opacity(easeInOutCubic(v)))));

  const animUsed = 0.6 + 0.25 + (n - 1) * STAGGER + BAR_DUR + 0.4;
  yield* waitFor(Math.max(0, duration - animUsed));
});
