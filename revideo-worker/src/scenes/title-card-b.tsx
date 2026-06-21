import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant B — Kinetic Reveal
// Title glides upward while fading in; a thin rule wipes out from centre beneath it;
// subtitle materialises below. Dark charcoal base (#060606).

export default makeScene2D('title-card-b', function* (view) {
  const vars = useScene().variables;
  const title    = String(vars.get('title',    'Untitled')());
  const subtitle = String(vars.get('subtitle', '')());
  const duration = Number(vars.get('duration', 5)());

  const hasSub = subtitle.length > 0;

  // Final resting positions
  const TITLE_Y    = hasSub ? -92 : -32;
  const TITLE_Y_0  = TITLE_Y + 48;   // starts below, slides up
  const RULE_Y     = hasSub ? -12 : 36;
  const RULE_MAX_W = 500;
  const SUB_Y      = hasSub ? 62 : 0;

  const containerRef = createRef<Rect>();
  const titleRef     = createRef<Txt>();
  const ruleRef      = createRef<Rect>();
  const subRef       = createRef<Txt>();

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#060606'} opacity={1}>
      <Txt
        ref={titleRef}
        text={title}
        y={TITLE_Y_0}
        fontSize={86}
        fontWeight={700}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1600}
      />
      <Rect
        ref={ruleRef}
        width={0}
        height={2}
        fill={'#ffffff'}
        opacity={0.4}
        y={RULE_Y}
      />
      <Txt
        ref={subRef}
        text={subtitle}
        y={SUB_Y}
        fontSize={36}
        fontWeight={300}
        fill={'#aaaaaa'}
        opacity={0}
        letterSpacing={3}
        textAlign={'center'}
        maxWidth={1400}
      />
    </Rect>,
  );

  // ── Animation in ──────────────────────────────────────────────────────
  const ANIM_IN  = 1.35;
  const ANIM_OUT = 0.40;

  yield* all(
    // Title glides up + fades in
    tween(0.70, v => {
      const t = easeOutCubic(v);
      titleRef().opacity(t);
      titleRef().y(TITLE_Y_0 + (TITLE_Y - TITLE_Y_0) * t);
    }),
    // Rule wipes from centre outward, slight delay
    chain(
      waitFor(0.30),
      tween(0.52, v => ruleRef().width(easeInOutCubic(v) * RULE_MAX_W)),
    ),
    // Subtitle fades in after rule appears
    ...(hasSub
      ? [chain(waitFor(0.82), tween(0.53, v => subRef().opacity(easeOutCubic(v))))]
      : []),
  );

  yield* waitFor(Math.max(0, duration - ANIM_IN - ANIM_OUT));
  yield* tween(ANIM_OUT, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
