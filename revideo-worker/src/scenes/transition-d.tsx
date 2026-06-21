import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant D — Crosshair
// A thin horizontal and vertical line simultaneously grow outward from the screen
// centre, forming a crosshair grid. The section label materialises at the centre
// once the lines settle. Clinical, precise — suits structural or analytical pivots.

export default makeScene2D('transition-d', function* (view) {
  const vars = useScene().variables;

  const label    = String(vars.get('label',    '')());
  const sublabel = String(vars.get('sublabel', '')());
  const duration = Number(vars.get('duration', 3.0)());

  const LABEL_Y    = sublabel ? -48 : -24;
  const SUBLABEL_Y = 30;

  const containerRef = createRef<Rect>();
  const hRef         = createRef<Rect>();
  const vRef         = createRef<Rect>();
  const labelRef     = createRef<Txt>();
  const subRef       = createRef<Txt>();

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#0c0c0c'} opacity={1}>
      {/* Crosshair lines — rendered behind the label */}
      <Rect ref={hRef} width={0}   height={2} fill={'#ffffff'} opacity={0.32} />
      <Rect ref={vRef} width={2}   height={0} fill={'#ffffff'} opacity={0.32} />

      {/* Section label and optional context */}
      <Txt
        ref={labelRef}
        text={label}
        y={LABEL_Y}
        fontSize={78}
        fontWeight={700}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1500}
        letterSpacing={4}
      />
      <Txt
        ref={subRef}
        text={sublabel}
        y={SUBLABEL_Y}
        fontSize={32}
        fontWeight={300}
        fill={'#777777'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1400}
        letterSpacing={2}
      />
    </Rect>,
  );

  // ── Animation ─────────────────────────────────────────────────────────

  // 1. Both lines expand from centre simultaneously; vertical has a slight delay
  yield* all(
    tween(0.34, v => hRef().width(easeInOutCubic(v) * 1720)),
    chain(
      waitFor(0.06),
      tween(0.30, v => vRef().height(easeInOutCubic(v) * 620)),
    ),
  );

  // 2. Label materialises; sublabel follows
  if (sublabel) {
    yield* all(
      tween(0.32, v => labelRef().opacity(easeOutCubic(v))),
      chain(waitFor(0.18), tween(0.28, v => subRef().opacity(easeOutCubic(v)))),
    );
  } else {
    yield* tween(0.32, v => labelRef().opacity(easeOutCubic(v)));
  }

  const animIn = 0.40 + 0.32 + (sublabel ? 0.28 : 0);
  yield* waitFor(Math.max(0, duration - animIn - 0.35));
  yield* tween(0.35, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
