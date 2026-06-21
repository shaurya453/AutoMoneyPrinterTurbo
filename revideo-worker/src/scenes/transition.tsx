import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {
  all,
  chain,
  createRef,
  easeInOutCubic,
  easeOutCubic,
  tween,
  useScene,
  waitFor,
} from '@revideo/core';

export default makeScene2D('transition', function* (view) {
  const vars = useScene().variables;

  const label = String(vars.get('label', '')());
  const sublabel = String(vars.get('sublabel', '')());
  const duration = Number(vars.get('duration', 3.0)());

  // Layout (canvas 1920 × 1080, origin at center)
  const LINE_W = 120;
  const LINE_Y = sublabel ? -108 : -88;
  const LABEL_Y = sublabel ? -48 : -28;
  const SUBLABEL_Y = 28;

  const containerRef = createRef<Rect>();
  const lineRef = createRef<Rect>();
  const labelRef = createRef<Txt>();
  const sublabelRef = createRef<Txt>();

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#080808'} opacity={1}>
      {/* Accent line — grows symmetrically from center */}
      <Rect
        ref={lineRef}
        width={0}
        height={2}
        fill={'#ffffff'}
        y={LINE_Y}
      />

      {/* Section label */}
      <Txt
        ref={labelRef}
        text={label}
        y={LABEL_Y}
        fontSize={80}
        fontWeight={700}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1600}
        letterSpacing={4}
      />

      {/* Optional sublabel */}
      <Txt
        ref={sublabelRef}
        text={sublabel}
        y={SUBLABEL_Y}
        fontSize={34}
        fontWeight={300}
        fill={'#888888'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1400}
        letterSpacing={2}
      />
    </Rect>,
  );

  // ── Animation sequence ──────────────────────────────────────────────

  // 1. Accent line grows outward from its center (symmetric left + right)
  yield* tween(0.35, v => lineRef().width(easeInOutCubic(v) * LINE_W));

  // 2. Label fades in; sublabel follows with a short delay
  if (sublabel) {
    yield* all(
      tween(0.4, v => labelRef().opacity(easeOutCubic(v))),
      chain(waitFor(0.15), tween(0.35, v => sublabelRef().opacity(easeOutCubic(v)))),
    );
  } else {
    yield* tween(0.4, v => labelRef().opacity(easeOutCubic(v)));
  }

  // 3. Hold for the remainder, leaving 0.35s for fade-out
  const animIn = 0.35 + (sublabel ? 0.4 + 0.15 : 0.4);
  yield* waitFor(Math.max(0, duration - animIn - 0.35));

  // 4. Fade out the whole scene
  yield* tween(0.35, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
