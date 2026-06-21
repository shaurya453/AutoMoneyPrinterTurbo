import '../global.css';
import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant C — Cascade Reveal
// A coloured accent bar sweeps across the full width behind each row, then the
// item text fades in on top. Items cascade top-to-bottom, creating a "reveal"
// effect. Dark slate background (#0a0e18) distinct from A and B.

const COLORS = [
  '#4f8ef7', '#f7964f', '#4fd1a0', '#f74f7e',
  '#b44ff7', '#f7e14f', '#4fcef7', '#f74fb3',
];

export default makeScene2D('list-c', function* (view) {
  const vars = useScene().variables;

  const title    = String(vars.get('title', '')());
  const rawItems = vars.get('items', ['Item 1', 'Item 2', 'Item 3'])();
  const items    = Array.from(rawItems as string[]);
  const duration = Number(vars.get('duration', 8)());

  const n        = Math.min(items.length, 6);
  const hasTitle = title.length > 0;

  const ROW_H       = n <= 4 ? 72 : 58;
  const ROW_GAP     = n <= 4 ? 96 : 78;
  const LIST_OFFSET = hasTitle ? 48 : 0;
  const totalH      = Math.max(0, n - 1) * ROW_GAP + ROW_H;
  const rowYs       = Array.from({length: n}, (_, i) => LIST_OFFSET - totalH / 2 + i * ROW_GAP + ROW_H / 2);
  const TITLE_Y     = hasTitle ? rowYs[0] - ROW_GAP * 1.6 : 0;

  const BAR_W    = 1760;  // full-width accent bar
  const BAR_X    = 0;     // centred
  const TEXT_X   = -820;  // left edge for text (with maxWidth keeping it right)

  const SWEEP_DUR = 0.28;
  const FADE_DUR  = 0.30;
  const STAGGER   = 0.30;
  const ANIM_OUT  = 0.35;

  const containerRef = createRef<Rect>();
  const titleRef     = createRef<Txt>();
  const barRefs      = Array.from({length: n}, () => createRef<Rect>());
  const textRefs     = Array.from({length: n}, () => createRef<Txt>());

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#0a0e18'} opacity={1}>
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
        maxWidth={1600}
      />

      {/* Accent bars — one per row, starts collapsed */}
      {Array.from({length: n}, (_, i) => (
        <Rect
          ref={barRefs[i]}
          width={0}
          height={ROW_H}
          fill={COLORS[i % COLORS.length]}
          opacity={0.12}
          x={BAR_X - BAR_W / 2}  // starts anchored at left edge
          y={rowYs[i]}
          radius={4}
        />
      ))}

      {/* Item text */}
      {Array.from({length: n}, (_, i) => (
        <Txt
          ref={textRefs[i]}
          text={items[i]}
          x={TEXT_X}
          y={rowYs[i]}
          fontSize={n <= 4 ? 40 : 34}
          fontWeight={400}
          fontFamily={'Inter, sans-serif'}
          fill={'#e8e8e8'}
          opacity={0}
          textAlign={'left'}
          maxWidth={1600}
        />
      ))}
    </Rect>,
  );

  const titleTime = hasTitle ? 0.40 + 0.12 : 0;
  if (hasTitle) {
    yield* tween(0.40, v => titleRef().opacity(easeInOutCubic(v)));
    yield* waitFor(0.12);
  }

  // Cascade: for each row, sweep bar then fade text
  yield* all(
    ...Array.from({length: n}, (_, i) =>
      chain(
        waitFor(i * STAGGER),
        // Bar sweeps from left to full width (left edge stays pinned at BAR_X - BAR_W/2)
        tween(SWEEP_DUR, v => {
          const w = easeInOutCubic(v) * BAR_W;
          barRefs[i]().width(w);
          barRefs[i]().x(BAR_X - BAR_W / 2 + w / 2);
        }),
        // Text fades in immediately after bar sweeps
        tween(FADE_DUR, v => textRefs[i]().opacity(easeOutCubic(v))),
      ),
    ),
  );

  const animIn = titleTime + (n - 1) * STAGGER + SWEEP_DUR + FADE_DUR;
  yield* waitFor(Math.max(0, duration - animIn - ANIM_OUT));
  yield* tween(ANIM_OUT, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
