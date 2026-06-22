import '../global.css';
import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant A — Bullet List
// Each item slides in from the left with a small coloured square bullet.
// Items appear staggered so viewers can read them as they land.

const COLORS = [
  '#4f8ef7', '#f7964f', '#4fd1a0', '#f74f7e',
  '#b44ff7', '#f7e14f', '#4fcef7', '#f74fb3',
];

export default makeScene2D('list', function* (view) {
  const vars = useScene().variables;

  const title    = String(vars.get('title', '')());
  const rawItems = vars.get('items', ['Item 1', 'Item 2', 'Item 3'])();
  const items    = Array.from(rawItems as string[]);
  const duration = Number(vars.get('duration', 8)());

  const n        = Math.min(items.length, 6);
  const hasTitle = title.length > 0;

  const ROW_GAP     = n <= 4 ? 88 : 72;
  const LIST_OFFSET = hasTitle ? 40 : 0;
  const totalH      = Math.max(0, n - 1) * ROW_GAP;
  const rowYs       = Array.from({length: n}, (_, i) => LIST_OFFSET - totalH / 2 + i * ROW_GAP);
  const TITLE_Y     = hasTitle ? rowYs[0] - ROW_GAP * 1.8 : 0;

  // Row layout: [bullet 12px] [gap 22px] [text 1100px]  — total 1134px
  // Row left edge at LEFT_ANCHOR; elements positioned as direct children of container
  const LEFT_ANCHOR = -640;
  const BULLET_X    = LEFT_ANCHOR + 6;          // bullet centre
  const TEXT_X      = LEFT_ANCHOR + 12 + 22 + 550; // text centre (= LEFT_ANCHOR + 584)
  const SLIDE_DX    = 40;
  const STAGGER     = 0.20;
  const ROW_DUR     = 0.45;
  const ANIM_OUT    = 0.35;

  const containerRef  = createRef<Rect>();
  const titleRef      = createRef<Txt>();
  const bulletRefs    = Array.from({length: n}, () => createRef<Rect>());
  const textRefs      = Array.from({length: n}, () => createRef<Txt>());

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#0a0a0a'} opacity={1} layout={false}>
      <Txt
        ref={titleRef}
        text={title}
        y={TITLE_Y}
        fontSize={54}
        fontWeight={700}
        fontFamily={'Inter, sans-serif'}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        width={1600}
        textWrap={true}
      />

      {Array.from({length: n}, (_, i) => (
        <Rect
          ref={bulletRefs[i]}
          width={12}
          height={12}
          fill={COLORS[i % COLORS.length]}
          radius={2}
          x={BULLET_X - SLIDE_DX}
          y={rowYs[i]}
          opacity={0}
        />
      ))}

      {Array.from({length: n}, (_, i) => (
        <Txt
          ref={textRefs[i]}
          text={items[i]}
          fontSize={40}
          fontWeight={400}
          fontFamily={'Inter, sans-serif'}
          fill={'#e8e8e8'}
          x={TEXT_X - SLIDE_DX}
          y={rowYs[i]}
          width={1100}
          textAlign={'left'}
          textWrap={true}
          opacity={0}
        />
      ))}
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

  const animIn = titleTime + (n - 1) * STAGGER + ROW_DUR;
  yield* waitFor(Math.max(0, duration - animIn - ANIM_OUT));
  yield* tween(ANIM_OUT, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
