import '../global.css';
import {blur, makeScene2D, Rect, Txt, Video} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant D — Number Callouts

const COLORS = [
  '#4f8ef7', '#f7964f', '#4fd1a0', '#f74f7e',
  '#b44ff7', '#f7e14f', '#4fcef7', '#f74fb3',
];

const FLOAT_AMP      = 5;
const FLOAT_PERIOD   = 3.5;
const easeInOutSine  = (v: number) => -(Math.cos(Math.PI * v) - 1) / 2;

export default makeScene2D('infographic-d', function* (view) {
  const vars = useScene().variables;

  const title     = String(vars.get('title', 'Statistics')());
  const labels    = Array.from(vars.get('labels', ['A', 'B', 'C'])() as string[]);
  const rawValues = Array.from(vars.get('values', [1, 2, 3])() as number[]);
  const unit      = String(vars.get('unit', '')());
  const duration  = Number(vars.get('duration', 8)());
  const bgVideo   = String(vars.get('bgVideo', '')());

  const values = rawValues.map(Number);
  const n      = Math.min(labels.length, values.length);

  const SLOT_W  = 1600 / n;
  const SLOT_XS = Array.from({length: n}, (_, i) => -800 + SLOT_W * (i + 0.5));

  const NUM_Y   = -20;
  const UNIT_Y  =  80;
  const LABEL_Y = 120;

  const STAGGER   = 0.30;
  const INTRO_DUR = 0.4 + 0.25;
  const END_DUR   = 0.3;
  const COUNT_DUR = Math.max(0.5, duration * 0.30 - INTRO_DUR - (n - 1) * STAGGER - END_DUR);

  const formatCount = (v: number, target: number): string => {
    if (Number.isInteger(target)) return String(Math.round(v));
    return v.toFixed(1);
  };

  const floatRef = createRef<Rect>();
  const titleRef = createRef<Txt>();
  const divRef   = createRef<Rect>();
  const numRefs  = Array.from({length: n}, () => createRef<Txt>());
  const unitRefs = Array.from({length: n}, () => createRef<Txt>());
  const lblRefs  = Array.from({length: n}, () => createRef<Txt>());

  view.add(
    <Rect width={1920} height={1080} layout={false}>
      {bgVideo
        ? <Video src={bgVideo} width={1920} height={1080} opacity={0.55} filters={[blur(16)]} loop play />
        : <Rect width={1920} height={1080} fill={'#0a0a0a'} />
      }
      <Rect width={1920} height={1080} fill={'rgba(0,0,0,0.52)'} />
      <Rect ref={floatRef} width={1920} height={1080} layout={false}>
        <Txt text={' '} x={-9999} y={0} fontSize={52} fontWeight={700}
          fontFamily={'Stack Sans Text, sans-serif'} fill={'#000001'} />
        <Txt
          ref={titleRef}
          text={title}
          x={0} y={-400}
          fontSize={52} fontWeight={700} fontFamily={'Stack Sans Text, sans-serif'}
          fill={'#ffffff'} opacity={0}
          textAlign={'center'} justifyContent={'center'}
          width={1920} textWrap={true}
        />
        <Rect ref={divRef} width={0} height={1} fill={'#2a2a2a'} y={-320} />
        {Array.from({length: n}, (_, i) => (
          <Txt
            ref={numRefs[i]}
            text={'0'}
            x={SLOT_XS[i]} y={NUM_Y}
            fontSize={118} fontWeight={700} fontFamily={'Stack Sans Text, sans-serif'}
            fill={COLORS[i % COLORS.length]} opacity={0}
            textAlign={'center'} justifyContent={'center'}
          />
        ))}
        {Array.from({length: n}, (_, i) => (
          <Txt
            ref={unitRefs[i]}
            text={unit}
            x={SLOT_XS[i]} y={UNIT_Y}
            fontSize={26} fontWeight={300} fontFamily={'Stack Sans Text, sans-serif'}
            fill={'#666666'} opacity={0}
            textAlign={'center'} justifyContent={'center'}
          />
        ))}
        {Array.from({length: n}, (_, i) => (
          <Txt
            ref={lblRefs[i]}
            text={labels[i]}
            x={SLOT_XS[i]} y={LABEL_Y}
            fontSize={26} fontWeight={400} fontFamily={'Stack Sans Text, sans-serif'}
            fill={'#888888'} opacity={0}
            textAlign={'center'} justifyContent={'center'}
            width={SLOT_W - 20} textWrap={true}
          />
        ))}
      </Rect>
    </Rect>,
  );

  yield* tween(0.4, v => titleRef().opacity(easeInOutCubic(v)));
  yield* tween(0.25, v => divRef().width(easeInOutCubic(v) * 1600));

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

  yield* all(
    ...lblRefs.map(ref => tween(0.3, v => ref().opacity(easeOutCubic(v)))),
    ...(unit ? unitRefs.map(ref => tween(0.3, v => ref().opacity(easeOutCubic(v)))) : []),
  );

  const animUsed = 0.4 + 0.25 + (n - 1) * STAGGER + COUNT_DUR + 0.3;
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
