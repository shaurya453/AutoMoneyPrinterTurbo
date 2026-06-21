import '../global.css';
import {makeScene2D, Layout, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant B — Numbered List (Drop-in)
// Items drop in from above with a zero-padded number prefix ("01.", "02."…).
// Motion direction (top-to-bottom drop) and dark blue-purple palette make this
// visually distinct from Variant A (left slide, black background).

export default makeScene2D('list-b', function* (view) {
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

  const LAYOUT_X = -50;
  const DROP_DY  = 48;   // items start 48px ABOVE final position, drop down
  const STAGGER  = 0.22;
  const ROW_DUR  = 0.50;
  const ANIM_OUT = 0.35;

  const containerRef = createRef<Rect>();
  const titleRef     = createRef<Txt>();
  const rowRefs      = Array.from({length: n}, () => createRef<Layout>());

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#060616'} opacity={1}>
      <Txt
        ref={titleRef}
        text={title}
        y={TITLE_Y}
        fontSize={52}
        fontWeight={700}
        fontFamily={'Inter, sans-serif'}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1600}
      />

      {Array.from({length: n}, (_, i) => (
        <Layout
          ref={rowRefs[i]}
          direction={'row'}
          alignItems={'center'}
          justifyContent={'start'}
          gap={32}
          x={LAYOUT_X}
          y={rowYs[i] - DROP_DY}
          opacity={0}
        >
          <Txt
            text={String(i + 1).padStart(2, '0') + '.'}
            fontSize={52}
            fontWeight={800}
            fontFamily={'Inter, sans-serif'}
            fill={'#4fcef7'}
            width={88}
          />
          <Txt
            text={items[i]}
            fontSize={38}
            fontWeight={300}
            fontFamily={'Inter, sans-serif'}
            fill={'#d8d8d8'}
            width={1020}
          />
        </Layout>
      ))}
    </Rect>,
  );

  const titleTime = hasTitle ? 0.40 + 0.15 : 0;
  if (hasTitle) {
    yield* tween(0.40, v => titleRef().opacity(easeInOutCubic(v)));
    yield* waitFor(0.15);
  }

  // Items drop down from above into position, staggered
  yield* all(
    ...rowRefs.slice(0, n).map((rowRef, i) =>
      chain(
        waitFor(i * STAGGER),
        tween(ROW_DUR, v => {
          const t = easeOutCubic(v);
          rowRef().opacity(t);
          rowRef().y(rowYs[i] - DROP_DY + DROP_DY * t);
        }),
      ),
    ),
  );

  const animIn = titleTime + (n - 1) * STAGGER + ROW_DUR;
  yield* waitFor(Math.max(0, duration - animIn - ANIM_OUT));
  yield* tween(ANIM_OUT, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
