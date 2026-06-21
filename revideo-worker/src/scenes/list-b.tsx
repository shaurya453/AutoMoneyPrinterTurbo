import {makeScene2D, Layout, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant B — Numbered List
// Items slide in from the right with a zero-padded cyan number prefix ("01.", "02."…).
// The opposite slide direction and cooler palette make this visually distinct from
// Variant A. Use when order matters — top-ranked items, chronological steps, etc.

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

  const SLIDE_DX = 40;    // slides in from the right (positive initial x)
  const STAGGER  = 0.22;
  const ROW_DUR  = 0.45;
  const ANIM_OUT = 0.35;

  const containerRef = createRef<Rect>();
  const titleRef     = createRef<Txt>();
  const rowRefs      = Array.from({length: n}, () => createRef<Layout>());

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#060610'} opacity={1}>
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
        maxWidth={1600}
      />

      {/* Numbered rows */}
      {Array.from({length: n}, (_, i) => (
        <Layout
          ref={rowRefs[i]}
          direction={'row'}
          alignItems={'center'}
          gap={24}
          width={1200}
          x={SLIDE_DX}
          y={rowYs[i]}
          opacity={0}
        >
          <Txt
            text={String(i + 1).padStart(2, '0') + '.'}
            fontSize={46}
            fontWeight={800}
            fill={'#4fcef7'}
            width={80}
          />
          <Txt
            text={items[i]}
            fontSize={38}
            fontWeight={300}
            fill={'#d8d8d8'}
            maxWidth={1060}
          />
        </Layout>
      ))}
    </Rect>,
  );

  // ── Animation ─────────────────────────────────────────────────────────

  const titleTime = hasTitle ? 0.40 + 0.15 : 0;
  if (hasTitle) {
    yield* tween(0.40, v => titleRef().opacity(easeInOutCubic(v)));
    yield* waitFor(0.15);
  }

  // Rows slide in from the right, staggered
  yield* all(
    ...rowRefs.slice(0, n).map((rowRef, i) =>
      chain(
        waitFor(i * STAGGER),
        tween(ROW_DUR, v => {
          const t = easeOutCubic(v);
          rowRef().opacity(t);
          rowRef().x(SLIDE_DX * (1 - t));
        }),
      ),
    ),
  );

  const animIn = titleTime + (n - 1) * STAGGER + ROW_DUR;
  yield* waitFor(Math.max(0, duration - animIn - ANIM_OUT));
  yield* tween(ANIM_OUT, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
