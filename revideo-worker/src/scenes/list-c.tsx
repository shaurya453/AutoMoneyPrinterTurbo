import '../global.css';
import {blur, makeScene2D, Rect, Txt, Video} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant C — Cascade Reveal (bar sweep)

const COLORS = [
  '#4f8ef7', '#f7964f', '#4fd1a0', '#f74f7e',
  '#b44ff7', '#f7e14f', '#4fcef7', '#f74fb3',
];

const FLOAT_AMP      = 5;
const FLOAT_PERIOD   = 3.5;
const easeInOutSine  = (v: number) => -(Math.cos(Math.PI * v) - 1) / 2;

export default makeScene2D('list-c', function* (view) {
  const vars = useScene().variables;

  const title    = String(vars.get('title', '')());
  const rawItems = vars.get('items', ['Item 1', 'Item 2', 'Item 3'])();
  const items    = Array.from(rawItems as string[]);
  const duration = Number(vars.get('duration', 8)());
  const bgVideo  = String(vars.get('bgVideo', '')());

  const n        = Math.min(items.length, 6);
  const hasTitle = title.length > 0;

  const ROW_H       = n <= 4 ? 72 : 58;
  const ROW_GAP     = n <= 4 ? 96 : 78;
  const LIST_OFFSET = hasTitle ? 48 : 0;
  const totalH      = Math.max(0, n - 1) * ROW_GAP + ROW_H;
  const rowYs       = Array.from({length: n}, (_, i) => LIST_OFFSET - totalH / 2 + i * ROW_GAP + ROW_H / 2);
  const TITLE_Y     = hasTitle ? rowYs[0] - ROW_GAP * 1.6 : 0;

  const BAR_W    = 1760;
  const BAR_X    = 0;
  const TEXT_W   = 1280;
  const TEXT_X   = 0;

  const STAGGER     = 0.45;
  const FADE_DUR    = 0.40;
  const ANIM_OUT    = 0.35;
  const titleTime_c = hasTitle ? 0.52 : 0;
  const SWEEP_DUR   = Math.max(0.3, duration * 0.30 - titleTime_c - (n - 1) * STAGGER - FADE_DUR - ANIM_OUT);

  const floatRef = createRef<Rect>();
  const titleRef = createRef<Txt>();
  const barRefs  = Array.from({length: n}, () => createRef<Rect>());
  const textRefs = Array.from({length: n}, () => createRef<Txt>());

  view.add(
    <Rect width={1920} height={1080} layout={false}>
      {bgVideo
        ? <Video src={bgVideo} width={1920} height={1080} opacity={0.55} filters={[blur(16)]} loop play />
        : <Rect width={1920} height={1080} fill={'#0a0e18'} />
      }
      <Rect width={1920} height={1080} fill={'rgba(0,0,0,0.52)'} />
      <Rect ref={floatRef} width={1920} height={1080} layout={false}>
        <Txt text={' '} x={-9999} y={0} fontSize={54} fontWeight={700}
          fontFamily={'Stack Sans Text, sans-serif'} fill={'#000001'} />
        <Txt ref={titleRef} text={title} x={0} y={TITLE_Y} fontSize={54} fontWeight={700}
          fontFamily={'Stack Sans Text, sans-serif'} fill={'#ffffff'} opacity={0} textAlign={'center'}
          justifyContent={'center'} width={1920} textWrap={true} />
        {Array.from({length: n}, (_, i) => (
          <Rect ref={barRefs[i]} width={0} height={ROW_H} fill={COLORS[i % COLORS.length]}
            opacity={0.12} x={BAR_X - BAR_W / 2} y={rowYs[i]} radius={4} />
        ))}
        {Array.from({length: n}, (_, i) => (
          <Txt ref={textRefs[i]} text={items[i]} x={TEXT_X} y={rowYs[i]}
            fontSize={n <= 4 ? 40 : 34} fontWeight={400} fontFamily={'Stack Sans Text, sans-serif'}
            fill={'#e8e8e8'} opacity={0} textAlign={'left'} justifyContent={'flex-start'}
            width={TEXT_W} textWrap={true} />
        ))}
      </Rect>
    </Rect>,
  );

  const titleTime = hasTitle ? 0.40 + 0.12 : 0;
  if (hasTitle) {
    yield* tween(0.40, v => titleRef().opacity(easeInOutCubic(v)));
    yield* waitFor(0.12);
  }

  yield* all(
    ...Array.from({length: n}, (_, i) =>
      chain(
        waitFor(i * STAGGER),
        tween(SWEEP_DUR, v => {
          const w = easeInOutCubic(v) * BAR_W;
          barRefs[i]().width(w);
          barRefs[i]().x(BAR_X - BAR_W / 2 + w / 2);
        }),
        tween(FADE_DUR, v => textRefs[i]().opacity(easeOutCubic(v))),
      ),
    ),
  );

  const animIn  = titleTime + (n - 1) * STAGGER + SWEEP_DUR + FADE_DUR;
  const holdDur = Math.max(0, duration - animIn - ANIM_OUT);
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
    floatRef().y(0);
  }
  yield* tween(ANIM_OUT, v => floatRef().opacity(1 - easeInOutCubic(v)));
});
