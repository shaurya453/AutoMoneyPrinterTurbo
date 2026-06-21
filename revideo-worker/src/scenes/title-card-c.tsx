import '../global.css';
import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant C — Left-Bar Frame
// A thin vertical accent bar draws down the left side of the frame; the title and
// subtitle are LEFT-ALIGNED starting from just right of the bar. Neutral dark base (#080808).

export default makeScene2D('title-card-c', function* (view) {
  const vars = useScene().variables;
  const title    = String(vars.get('title',    'Untitled')());
  const subtitle = String(vars.get('subtitle', '')());
  const duration = Number(vars.get('duration', 5)());

  const hasSub = subtitle.length > 0;

  // Vertical accent bar — pinned at top, grows downward
  const BAR_X     = -740;
  const BAR_TOP_Y = -160;
  const BAR_MAX_H = 320;

  // Text block: left-aligned, starting ~100px to the right of the bar.
  // TEXT_CENTER_X is the center of the maxWidth bounding box; with textAlign='left',
  // the text left edge sits at TEXT_CENTER_X - TEXT_W/2 ≈ -640 (100px gap from bar).
  const TEXT_W         = 1400;
  const TEXT_CENTER_X  = 60;   // TEXT_CENTER_X - TEXT_W/2 = 60 - 700 = -640
  const TEXT_X_START   = TEXT_CENTER_X - 64;  // slides rightward into final position

  const TITLE_Y = hasSub ? -80 : -24;
  const TICK_Y  = hasSub ? 8 : 32;
  // Tick rule: left-aligned from same left edge (-640), extends 260px right.
  // Centre of the tick rect: -640 + 130 = -510
  const TICK_X  = -510;
  const SUB_Y   = hasSub ? 68 : 0;

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

      {/* Title — left-aligned, slides right into position */}
      <Txt
        ref={titleRef}
        text={title}
        x={TEXT_X_START}
        y={TITLE_Y}
        fontSize={80}
        fontWeight={700}
        fontFamily={'Inter, sans-serif'}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'left'}
        maxWidth={TEXT_W}
      />

      {/* Thin horizontal rule — left-aligned with text */}
      <Rect
        ref={tickRef}
        width={0}
        height={2}
        fill={'#444444'}
        x={TICK_X}
        y={TICK_Y}
      />

      {/* Subtitle — left-aligned */}
      <Txt
        ref={subRef}
        text={subtitle}
        x={TEXT_X_START}
        y={SUB_Y}
        fontSize={34}
        fontWeight={300}
        fontFamily={'Inter, sans-serif'}
        fill={'#888888'}
        opacity={0}
        letterSpacing={4}
        textAlign={'left'}
        maxWidth={TEXT_W}
      />
    </Rect>,
  );

  const ANIM_IN  = 1.40;
  const ANIM_OUT = 0.35;

  yield* all(
    // Vertical bar draws downward (top pinned at BAR_TOP_Y)
    tween(0.55, v => {
      const h = easeInOutCubic(v) * BAR_MAX_H;
      barRef().height(h);
      barRef().y(BAR_TOP_Y + h / 2);
    }),
    // Title and subtitle slide rightward and fade in together
    chain(
      waitFor(0.18),
      tween(0.62, v => {
        const t = easeOutCubic(v);
        titleRef().opacity(t);
        titleRef().x(TEXT_X_START + (TEXT_CENTER_X - TEXT_X_START) * t);
      }),
    ),
    // Horizontal rule wipes out from tick centre
    chain(
      waitFor(0.52),
      tween(0.42, v => tickRef().width(easeInOutCubic(v) * 260)),
    ),
    ...(hasSub
      ? [chain(
          waitFor(0.90),
          tween(0.50, v => {
            const t = easeOutCubic(v);
            subRef().opacity(t);
            subRef().x(TEXT_X_START + (TEXT_CENTER_X - TEXT_X_START) * t);
          }),
        )]
      : []),
  );

  yield* waitFor(Math.max(0, duration - ANIM_IN - ANIM_OUT));
  yield* tween(ANIM_OUT, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
