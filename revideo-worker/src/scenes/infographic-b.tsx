import '../global.css';
import {blur, makeScene2D, Rect, Txt, Video} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant B — Horizontal Bars

const COLORS = [
  '#4f8ef7', '#f7964f', '#4fd1a0', '#f74f7e',
  '#b44ff7', '#f7e14f', '#4fcef7', '#f74fb3',
];

const FLOAT_AMP      = 5;
const FLOAT_PERIOD   = 3.5;
const easeInOutSine  = (v: number) => -(Math.cos(Math.PI * v) - 1) / 2;

export default makeScene2D('infographic-b', function* (view) {
  const vars = useScene().variables;
  const title     = String(vars.get('title',  'Statistics')());
  const labels    = Array.from(vars.get('labels', ['A', 'B', 'C'])() as string[]);
  const rawValues = Array.from(vars.get('values', [1, 2, 3])()   as number[]);
  const unit      = String(vars.get('unit',   '')());
  const duration  = Number(vars.get('duration', 8)());
  const bgVideo   = String(vars.get('bgVideo', '')());

  const values = rawValues.map(Number);
  const n      = Math.min(labels.length, values.length);
  const maxVal = Math.max(...values.slice(0, n), 0.001);

  const TITLE_Y    = -370;
  const BAR_H      = 52;
  const ROW_GAP    = 96;
  const AXIS_X     = -220;
  const LABEL_W    = 280;
  const LABEL_GAP  = 24;
  const LABEL_X    = AXIS_X - LABEL_W / 2 - LABEL_GAP;
  const MAX_BAR_W  = 680;
  const STAGGER    = 0.22;
  const INTRO_DUR  = 0.50 + 0.22;
  const END_DUR    = 0.35;
  const BAR_DUR    = Math.max(0.5, duration * 0.30 - INTRO_DUR - (n - 1) * STAGGER - END_DUR);

  const totalH   = BAR_H + (n - 1) * ROW_GAP;
  const chartTop = -totalH / 2;
  const barYs    = Array.from({length: n}, (_, i) => chartTop + i * ROW_GAP + BAR_H / 2);
  const targetWs = values.slice(0, n).map(v => (v / maxVal) * MAX_BAR_W);

  const formatVal = (v: number) => {
    const s = Number.isInteger(v) ? String(v) : v.toFixed(1);
    return unit ? `${s} ${unit}` : s;
  };

  const floatRef = createRef<Rect>();
  const titleRef = createRef<Txt>();
  const axisRef  = createRef<Rect>();
  const barRefs  = Array.from({length: n}, () => createRef<Rect>());
  const valRefs  = Array.from({length: n}, () => createRef<Txt>());

  view.add(
    <Rect width={1920} height={1080} layout={false}>
      {bgVideo
        ? <Video src={bgVideo} width={1920} height={1080} opacity={0.55} filters={[blur(16)]} loop play />
        : <Rect width={1920} height={1080} fill={'#0a0a0a'} />
      }
      <Rect width={1920} height={1080} fill={'rgba(0,0,0,0.52)'} />
      <Rect ref={floatRef} width={1920} height={1080} layout={false}>
        <Txt
          ref={titleRef}
          text={title}
          x={0} y={TITLE_Y}
          fontSize={52} fontWeight={700} fontFamily={'Stack Sans Text, sans-serif'}
          fill={'#ffffff'} opacity={0}
          textAlign={'center'} justifyContent={'center'}
          width={1920} textWrap={true}
        />
        <Rect ref={axisRef} width={2} height={totalH + 48} fill={'#3a3a3a'} x={AXIS_X} y={0} opacity={0} />
        {Array.from({length: n}, (_, i) => (
          <Txt
            text={labels[i]}
            x={LABEL_X} y={barYs[i]}
            fontSize={24} fontWeight={400} fontFamily={'Stack Sans Text, sans-serif'}
            fill={'#999999'}
            textAlign={'right'} justifyContent={'flex-end'}
            width={LABEL_W} textWrap={true}
          />
        ))}
        {Array.from({length: n}, (_, i) => (
          <Rect
            ref={barRefs[i]}
            width={0} height={BAR_H}
            fill={COLORS[i % COLORS.length]}
            x={AXIS_X} y={barYs[i]} radius={4}
          />
        ))}
        {Array.from({length: n}, (_, i) => (
          <Txt
            ref={valRefs[i]}
            text={formatVal(values[i])}
            x={AXIS_X + targetWs[i] + 102} y={barYs[i]}
            fontSize={24} fontWeight={700} fontFamily={'Stack Sans Text, sans-serif'}
            fill={'#ffffff'} opacity={0}
            textAlign={'left'} width={180}
          />
        ))}
      </Rect>
    </Rect>,
  );

  yield* tween(0.50, v => titleRef().opacity(easeInOutCubic(v)));
  yield* tween(0.22, v => axisRef().opacity(easeInOutCubic(v)));

  yield* all(
    ...barRefs.map((barRef, i) =>
      chain(
        waitFor(i * STAGGER),
        tween(BAR_DUR, v => {
          const w = easeInOutCubic(v) * targetWs[i];
          barRef().width(w);
          barRef().x(AXIS_X + w / 2);
        }),
      ),
    ),
  );

  yield* all(...valRefs.map(ref => tween(0.35, v => ref().opacity(easeOutCubic(v)))));

  const animUsed = 0.50 + 0.22 + (n - 1) * STAGGER + BAR_DUR + 0.35;
  const holdDur  = Math.max(0, duration - animUsed);
  if (holdDur > 0.1) {
    const qd = FLOAT_PERIOD / 4;
    const pts = [FLOAT_AMP, 0, -FLOAT_AMP, 0];
    let fe = 0; let qi = 0;
    while (fe + qd <= holdDur - 0.05) {
      const fr = pts[(qi + 3) % 4], to = pts[qi % 4];
      yield* tween(qd, v => floatRef().y(fr + (to - fr) * easeInOutSine(v)));
      fe += qd; qi++;
    }
    const rem = holdDur - fe;
    if (rem > 0.05) {
      const fr = pts[(qi + 3) % 4], to = pts[qi % 4];
      yield* tween(rem, v => floatRef().y(fr + (to - fr) * easeInOutSine(v)));
    }
  }
});
