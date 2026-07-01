import '../global.css';
import {blur, makeScene2D, Rect, Txt, Video} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant A — Bullet List (slides from left)

const COLORS = [
  '#4f8ef7', '#f7964f', '#4fd1a0', '#f74f7e',
  '#b44ff7', '#f7e14f', '#4fcef7', '#f74fb3',
];

const FLOAT_AMP      = 5;
const FLOAT_PERIOD   = 3.5;
const easeInOutSine  = (v: number) => -(Math.cos(Math.PI * v) - 1) / 2;

export default makeScene2D('list', function* (view) {
  const vars = useScene().variables;

  const title    = String(vars.get('title', '')());
  const rawItems = vars.get('items', ['Item 1', 'Item 2', 'Item 3'])();
  const items    = Array.from(rawItems as string[]);
  const duration = Number(vars.get('duration', 8)());
  const bgVideo  = String(vars.get('bgVideo', '')());

  const n        = Math.min(items.length, 6);
  const hasTitle = title.length > 0;

  const ROW_GAP     = n <= 4 ? 88 : 72;
  const LIST_OFFSET = hasTitle ? 40 : 0;
  const totalH      = Math.max(0, n - 1) * ROW_GAP;
  const rowYs       = Array.from({length: n}, (_, i) => LIST_OFFSET - totalH / 2 + i * ROW_GAP);
  const TITLE_Y     = hasTitle ? rowYs[0] - ROW_GAP * 1.8 : 0;

  const LEFT_ANCHOR = -640;
  const BULLET_X    = LEFT_ANCHOR + 6;
  const TEXT_X      = LEFT_ANCHOR + 12 + 22 + 550;
  const SLIDE_DX    = 40;
  const STAGGER     = 0.35;
  const ANIM_OUT    = 0.35;
  const titleTime_c = hasTitle ? 0.55 : 0;
  const ROW_DUR     = Math.max(0.4, duration * 0.30 - titleTime_c - (n - 1) * STAGGER - ANIM_OUT);

  const floatRef     = createRef<Rect>();
  const titleRef     = createRef<Txt>();
  const bulletRefs   = Array.from({length: n}, () => createRef<Rect>());
  const textRefs     = Array.from({length: n}, () => createRef<Txt>());

  view.add(
    <Rect width={1920} height={1080} layout={false}>
      {bgVideo
        ? <Video src={bgVideo} width={1920} height={1080} opacity={0.55} filters={[blur(16)]} loop play />
        : <Rect width={1920} height={1080} fill={'#0a0a0a'} />
      }
      <Rect width={1920} height={1080} fill={'rgba(0,0,0,0.52)'} />
      <Rect ref={floatRef} width={1920} height={1080} layout={false}>
        <Txt text={' '} x={-9999} y={0} fontSize={54} fontWeight={700}
          fontFamily={'Stack Sans Text, sans-serif'} fill={'#000001'} />
        <Txt ref={titleRef} text={title} x={0} y={TITLE_Y} fontSize={54} fontWeight={700}
          fontFamily={'Stack Sans Text, sans-serif'} fill={'#ffffff'} opacity={0} textAlign={'center'}
          justifyContent={'center'} width={1920} textWrap={true} />
        {Array.from({length: n}, (_, i) => (
          <Rect ref={bulletRefs[i]} width={12} height={12} fill={COLORS[i % COLORS.length]}
            radius={2} x={BULLET_X - SLIDE_DX} y={rowYs[i]} opacity={0} />
        ))}
        {Array.from({length: n}, (_, i) => (
          <Txt ref={textRefs[i]} text={items[i]} fontSize={40} fontWeight={400}
            fontFamily={'Stack Sans Text, sans-serif'} fill={'#e8e8e8'} x={TEXT_X - SLIDE_DX} y={rowYs[i]}
            width={1100} textAlign={'left'} justifyContent={'flex-start'} textWrap={true} opacity={0} />
        ))}
      </Rect>
    </Rect>,
  );

  const titleTime = hasTitle ? 0.40 + 0.15 : 0;
  if (hasTitle) {
    yield* tween(0.40, v => titleRef().opacity(easeInOutCubic(v)));
    yield* waitFor(0.15);
  }

  yield* all(
    ...Array.from({length: n}, (_, i) =>
      chain(
        waitFor(i * STAGGER),
        tween(ROW_DUR, v => {
          const t = easeOutCubic(v);
          bulletRefs[i]().opacity(t);
          textRefs[i]().opacity(t);
          bulletRefs[i]().x(BULLET_X - SLIDE_DX + SLIDE_DX * t);
          textRefs[i]().x(TEXT_X - SLIDE_DX + SLIDE_DX * t);
        }),
      ),
    ),
  );

  const animIn  = titleTime + (n - 1) * STAGGER + ROW_DUR;
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
