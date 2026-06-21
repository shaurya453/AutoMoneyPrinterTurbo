import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant C — Left-Bar Frame
// A thin vertical accent bar draws down the left side of the frame; the title
// slides in from the left and settles slightly right-of-centre; a small horizontal
// rule materialises between title and subtitle. Neutral dark base (#080808).

export default makeScene2D('title-card-c', function* (view) {
  const vars = useScene().variables;
  const title    = String(vars.get('title',    'Untitled')());
  const subtitle = String(vars.get('subtitle', '')());
  const duration = Number(vars.get('duration', 5)());

  const hasSub = subtitle.length > 0;

  // Vertical accent bar — pinned at top, grows downward
  const BAR_X     = -740;
  const BAR_TOP_Y = -160;  // top edge stays here
  const BAR_MAX_H = 320;

  // Text block positioned slightly right-of-centre to balance the bar
  const TEXT_X_FINAL = 60;
  const TEXT_X_START = TEXT_X_FINAL - 64;  // slides in from the left
  const TITLE_Y      = hasSub ? -68 : -22;
  const TICK_Y       = hasSub ? 12 : 34;   // dividing rule
  const SUB_Y        = hasSub ? 62 : 0;

  const containerRef = createRef<Rect>();
  const barRef       = createRef<Rect>();
  const titleRef     = createRef<Txt>();
  const tickRef      = createRef<Rect>();
  const subRef       = createRef<Txt>();

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#080808'} opacity={1}>
      {/* Vertical accent bar (starts collapsed at top) */}
      <Rect
        ref={barRef}
        width={2}
        height={0}
        fill={'#ffffff'}
        x={BAR_X}
        y={BAR_TOP_Y}
        opacity={0.75}
      />

      {/* Title — starts offset left, slides to TEXT_X_FINAL */}
      <Txt
        ref={titleRef}
        text={title}
        x={TEXT_X_START}
        y={TITLE_Y}
        fontSize={80}
        fontWeight={700}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1440}
      />

      {/* Thin horizontal rule between title and subtitle */}
      <Rect
        ref={tickRef}
        width={0}
        height={2}
        fill={'#444444'}
        x={TEXT_X_FINAL}
        y={TICK_Y}
      />

      {/* Subtitle */}
      <Txt
        ref={subRef}
        text={subtitle}
        x={TEXT_X_FINAL}
        y={SUB_Y}
        fontSize={34}
        fontWeight={300}
        fill={'#888888'}
        opacity={0}
        letterSpacing={4}
        textAlign={'center'}
        maxWidth={1380}
      />
    </Rect>,
  );

  // ── Animation in ──────────────────────────────────────────────────────
  const ANIM_IN  = 1.40;
  const ANIM_OUT = 0.35;

  yield* all(
    // Vertical bar draws downward (top pinned at BAR_TOP_Y)
    tween(0.55, v => {
      const h = easeInOutCubic(v) * BAR_MAX_H;
      barRef().height(h);
      barRef().y(BAR_TOP_Y + h / 2);
    }),
    // Title slides rightward and fades in
    chain(
      waitFor(0.18),
      tween(0.62, v => {
        const t = easeOutCubic(v);
        titleRef().opacity(t);
        titleRef().x(TEXT_X_START + (TEXT_X_FINAL - TEXT_X_START) * t);
      }),
    ),
    // Horizontal rule wipes out
    chain(
      waitFor(0.52),
      tween(0.42, v => tickRef().width(easeInOutCubic(v) * 260)),
    ),
    // Subtitle fades in
    ...(hasSub
      ? [chain(waitFor(0.90), tween(0.50, v => subRef().opacity(easeOutCubic(v))))]
      : []),
  );

  yield* waitFor(Math.max(0, duration - ANIM_IN - ANIM_OUT));
  yield* tween(ANIM_OUT, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
