import '../global.css';
import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant C — Lollipop Chart
// Thin stems grow upward from a baseline; a circular dot pops in at each tip with
// a slight overshoot spring; value labels materialise above the dots.

const COLORS = [
  '#4f8ef7', '#f7964f', '#4fd1a0', '#f74f7e',
  '#b44ff7', '#f7e14f', '#4fcef7', '#f74fb3',
];

export default makeScene2D('infographic-c', function* (view) {
  const vars = useScene().variables;
  const title     = String(vars.get('title',  'Statistics')());
  const labels    = Array.from(vars.get('labels', ['A', 'B', 'C'])() as string[]);
  const rawValues = Array.from(vars.get('values', [1, 2, 3])()   as number[]);
  const unit      = String(vars.get('unit',   '')());
  const duration  = Number(vars.get('duration', 8)());

  const values = rawValues.map(Number);
  const n      = Math.min(labels.length, values.length);
  const maxVal = Math.max(...values.slice(0, n), 0.001);

  const CHART_W     = 1580;
  const MAX_STEM_H  = 460;
  const AXIS_Y      = 250;
  const BAR_SLOT_W  = CHART_W / n;
  const STEM_W      = 5;
  const DOT_D       = 36;

  const stemXs   = Array.from({length: n}, (_, i) => -CHART_W / 2 + BAR_SLOT_W * (i + 0.5));
  const targetHs = values.slice(0, n).map(v => (v / maxVal) * MAX_STEM_H);
  const dotYs    = targetHs.map(h => AXIS_Y - h - DOT_D / 2);
  const valYs    = targetHs.map(h => AXIS_Y - h - DOT_D - 38);

  const formatVal = (v: number) => {
    const s = Number.isInteger(v) ? String(v) : v.toFixed(1);
    return unit ? `${s} ${unit}` : s;
  };

  const STAGGER  = 0.14;
  const STEM_DUR = 1.0;
  const DOT_DUR  = 0.28;

  const titleRef = createRef<Txt>();
  const axisRef  = createRef<Rect>();
  const stemRefs = Array.from({length: n}, () => createRef<Rect>());
  const dotRefs  = Array.from({length: n}, () => createRef<Rect>());
  const valRefs  = Array.from({length: n}, () => createRef<Txt>());

  view.add(
    <Rect width={1920} height={1080} fill={'#0a0a0a'} layout={false}>
      <Txt
        ref={titleRef}
        text={title}
        y={-410}
        fontSize={56}
        fontWeight={700}
        fontFamily={'Inter, sans-serif'}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        width={1680}
        textWrap={true}
      />
      <Rect
        ref={axisRef}
        width={CHART_W + 60}
        height={2}
        fill={'#3a3a3a'}
        y={AXIS_Y}
        opacity={0}
      />

      {Array.from({length: n}, (_, i) => (
        <Txt
          text={labels[i]}
          x={stemXs[i]}
          y={AXIS_Y + 44}
          fontSize={20}
          fontWeight={400}
          fontFamily={'Inter, sans-serif'}
          fill={'#999999'}
          textAlign={'center'}
          width={BAR_SLOT_W - 12}
          textWrap={true}
        />
      ))}

      {Array.from({length: n}, (_, i) => (
        <Rect
          ref={stemRefs[i]}
          width={STEM_W}
          height={0}
          fill={COLORS[i % COLORS.length]}
          x={stemXs[i]}
          y={AXIS_Y}
          radius={3}
        />
      ))}

      {Array.from({length: n}, (_, i) => (
        <Rect
          ref={dotRefs[i]}
          width={0}
          height={0}
          fill={COLORS[i % COLORS.length]}
          x={stemXs[i]}
          y={dotYs[i]}
          radius={0}
        />
      ))}

      {Array.from({length: n}, (_, i) => (
        <Txt
          ref={valRefs[i]}
          text={formatVal(values[i])}
          x={stemXs[i]}
          y={valYs[i]}
          fontSize={22}
          fontWeight={700}
          fontFamily={'Inter, sans-serif'}
          fill={'#ffffff'}
          opacity={0}
          textAlign={'center'}
          width={BAR_SLOT_W - 16}
          textWrap={true}
        />
      ))}
    </Rect>,
  );

  yield* tween(0.55, v => titleRef().opacity(easeInOutCubic(v)));
  yield* tween(0.22, v => axisRef().opacity(easeInOutCubic(v)));

  yield* all(
    ...stemRefs.map((stemRef, i) =>
      chain(
        waitFor(i * STAGGER),
        tween(STEM_DUR, v => {
          const h = easeInOutCubic(v) * targetHs[i];
          stemRef().height(h);
          stemRef().y(AXIS_Y - h / 2);
        }),
        tween(DOT_DUR, v => {
          const spring = v < 0.65
            ? (v / 0.65) * 1.14
            : 1.14 - ((v - 0.65) / 0.35) * 0.14;
          const s = spring * DOT_D;
          dotRefs[i]().width(s);
          dotRefs[i]().height(s);
          dotRefs[i]().radius(s / 2);
        }),
      ),
    ),
  );

  yield* all(...valRefs.map(ref => tween(0.35, v => ref().opacity(easeOutCubic(v)))));

  const animUsed = 0.55 + 0.22 + (n - 1) * STAGGER + STEM_DUR + DOT_DUR + 0.35;
  yield* waitFor(Math.max(0, duration - animUsed));
});
