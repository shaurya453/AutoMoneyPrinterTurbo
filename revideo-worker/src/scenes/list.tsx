import {makeScene2D, Layout, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant A — Bullet List
// Each item slides in from the left with a small coloured square bullet. Items
// appear staggered so viewers can read them as they land. Works for both Pattern 1
// (structural/silent) and Pattern 2 (narrated — items animate while VO reads them).
// Supports 2–6 items; cap at 6 to keep text legible at 1920 × 1080.

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

  const n       = Math.min(items.length, 6);
  const hasTitle = title.length > 0;

  // Vertical layout — rows spread symmetrically around a vertical centre offset
  const ROW_GAP     = n <= 4 ? 88 : 72;
  const LIST_OFFSET = hasTitle ? 40 : 0;
  const totalH      = Math.max(0, n - 1) * ROW_GAP;
  const rowYs       = Array.from({length: n}, (_, i) => LIST_OFFSET - totalH / 2 + i * ROW_GAP);
  const TITLE_Y     = hasTitle ? rowYs[0] - ROW_GAP * 1.8 : 0;

  const SLIDE_DX = 40;    // slides in from the left (negative initial x)
  const STAGGER  = 0.20;
  const ROW_DUR  = 0.45;
  const ANIM_OUT = 0.35;

  const containerRef = createRef<Rect>();
  const titleRef     = createRef<Txt>();
  const rowRefs      = Array.from({length: n}, () => createRef<Layout>());

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#0a0a0a'} opacity={1}>
      {/* Title — always in DOM; only animated when non-empty */}
      <Txt
        ref={titleRef}
        text={title}
        y={TITLE_Y}
        fontSize={54}
        fontWeight={700}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1600}
      />

      {/* Bullet rows */}
      {Array.from({length: n}, (_, i) => (
        <Layout
          ref={rowRefs[i]}
          direction={'row'}
          alignItems={'center'}
          gap={22}
          width={1200}
          x={-SLIDE_DX}
          y={rowYs[i]}
          opacity={0}
        >
          <Rect
            width={12}
            height={12}
            fill={COLORS[i % COLORS.length]}
            radius={2}
          />
          <Txt
            text={items[i]}
            fontSize={40}
            fontWeight={400}
            fill={'#e8e8e8'}
            maxWidth={1130}
          />
        </Layout>
      ))}
    </Rect>,
  );

  // ── Animation ─────────────────────────────────────────────────────────

  // 1. Title fades in (only if present)
  const titleTime = hasTitle ? 0.40 + 0.15 : 0;
  if (hasTitle) {
    yield* tween(0.40, v => titleRef().opacity(easeInOutCubic(v)));
    yield* waitFor(0.15);
  }

  // 2. Rows slide in from the left, staggered
  yield* all(
    ...rowRefs.slice(0, n).map((rowRef, i) =>
      chain(
        waitFor(i * STAGGER),
        tween(ROW_DUR, v => {
          const t = easeOutCubic(v);
          rowRef().opacity(t);
          rowRef().x(-SLIDE_DX + SLIDE_DX * t);
        }),
      ),
    ),
  );

  const animIn = titleTime + (n - 1) * STAGGER + ROW_DUR;
  yield* waitFor(Math.max(0, duration - animIn - ANIM_OUT));
  yield* tween(ANIM_OUT, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
