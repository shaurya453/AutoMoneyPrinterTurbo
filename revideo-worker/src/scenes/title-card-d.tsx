import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant D — Cold Slide (editorial)
// Title glides in from the right while fading; a warm gold accent rule wipes out from
// centre beneath it; subtitle follows with a matching rightward slide. Near-black (#030303)
// base gives a clean, newspaper-editorial feel distinct from the other variants.

export default makeScene2D('title-card-d', function* (view) {
  const vars = useScene().variables;
  const title    = String(vars.get('title',    'Untitled')());
  const subtitle = String(vars.get('subtitle', '')());
  const duration = Number(vars.get('duration', 5)());

  const hasSub = subtitle.length > 0;

  const TITLE_Y  = hasSub ? -80 : -20;
  const RULE_Y   = hasSub ? -8  : 36;
  const RULE_W   = 320;
  const SUB_Y    = hasSub ? 68  : 0;
  const SLIDE_T  = 44;   // initial rightward offset for title
  const SLIDE_S  = 30;   // initial rightward offset for subtitle

  const containerRef = createRef<Rect>();
  const titleRef     = createRef<Txt>();
  const ruleRef      = createRef<Rect>();
  const subRef       = createRef<Txt>();

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#030303'} opacity={1}>
      <Txt
        ref={titleRef}
        text={title}
        x={SLIDE_T}
        y={TITLE_Y}
        fontSize={88}
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
        fill={'#c8a96e'}
        y={RULE_Y}
      />
      <Txt
        ref={subRef}
        text={subtitle}
        x={SLIDE_S}
        y={SUB_Y}
        fontSize={38}
        fontWeight={300}
        fill={'#a8a8a8'}
        opacity={0}
        letterSpacing={2}
        textAlign={'center'}
        maxWidth={1400}
      />
    </Rect>,
  );

  const ANIM_IN  = 1.28;
  const ANIM_OUT = 0.35;

  yield* all(
    // Title slides from right → centre + fades
    tween(0.55, v => {
      const t = easeOutCubic(v);
      titleRef().opacity(t);
      titleRef().x(SLIDE_T * (1 - t));
    }),
    // Gold accent rule wipes from centre outward, delayed
    chain(
      waitFor(0.44),
      tween(0.38, v => ruleRef().width(easeInOutCubic(v) * RULE_W)),
    ),
    // Subtitle slides from right → centre + fades, further delayed
    ...(hasSub
      ? [chain(
          waitFor(0.65),
          tween(0.48, v => {
            const t = easeOutCubic(v);
            subRef().opacity(t);
            subRef().x(SLIDE_S * (1 - t));
          }),
        )]
      : []),
  );

  yield* waitFor(Math.max(0, duration - ANIM_IN - ANIM_OUT));
  yield* tween(ANIM_OUT, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
