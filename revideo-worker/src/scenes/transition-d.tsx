import '../global.css';
import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant D — Crosshair
// Lines grow outward from centre at full opacity, then fade to a whisper (12%).
// Text materialises on the now-dim grid — clearly legible, with the crosshair as
// a textured background rather than a competing foreground element.

export default makeScene2D('transition-d', function* (view) {
  const vars = useScene().variables;

  const label    = String(vars.get('label',    '')());
  const sublabel = String(vars.get('sublabel', '')());
  const duration = Number(vars.get('duration', 3.0)());

  const hasSub = sublabel.length > 0;

  const LABEL_Y    = hasSub ? -52 : -26;
  const SUBLABEL_Y = 32;

  const containerRef = createRef<Rect>();
  const hRef         = createRef<Rect>();
  const vRef         = createRef<Rect>();
  const labelRef     = createRef<Txt>();
  const subRef       = createRef<Txt>();

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#0c0c0c'} opacity={1} layout={false}>
      {/* Crosshair lines — rendered behind the label, dimmed before text appears */}
      <Rect ref={hRef} width={0}   height={2} fill={'#ffffff'} opacity={0} />
      <Rect ref={vRef} width={2}   height={0} fill={'#ffffff'} opacity={0} />

      <Txt
        ref={labelRef}
        text={label}
        y={LABEL_Y}
        fontSize={78}
        fontWeight={700}
        fontFamily={'Inter, sans-serif'}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        width={1500}
        textWrap={true}
        letterSpacing={4}
      />
      <Txt
        ref={subRef}
        text={sublabel}
        y={SUBLABEL_Y}
        fontSize={32}
        fontWeight={300}
        fontFamily={'Inter, sans-serif'}
        fill={'#777777'}
        opacity={0}
        textAlign={'center'}
        width={1400}
        textWrap={true}
        letterSpacing={2}
      />
    </Rect>,
  );

  // Phase 1 — lines grow to full extent at high opacity
  yield* all(
    tween(0.06, v => { hRef().opacity(0.78 * easeOutCubic(v)); vRef().opacity(0.78 * easeOutCubic(v)); }),
    tween(0.34, v => hRef().width(easeInOutCubic(v) * 1720)),
    chain(
      waitFor(0.06),
      tween(0.30, v => vRef().height(easeInOutCubic(v) * 620)),
    ),
  );

  // Phase 2 — lines fade to near-invisible
  yield* all(
    tween(0.25, v => hRef().opacity(0.78 - 0.66 * easeInOutCubic(v))),  // 0.78 → 0.12
    tween(0.25, v => vRef().opacity(0.78 - 0.66 * easeInOutCubic(v))),
  );

  // Phase 3 — text fades in on the dim grid
  if (hasSub) {
    yield* all(
      tween(0.32, v => labelRef().opacity(easeOutCubic(v))),
      chain(waitFor(0.18), tween(0.28, v => subRef().opacity(easeOutCubic(v)))),
    );
  } else {
    yield* tween(0.32, v => labelRef().opacity(easeOutCubic(v)));
  }

  const animIn = 0.40 + 0.25 + 0.32 + (hasSub ? 0.18 : 0);
  yield* waitFor(Math.max(0, duration - animIn - 0.35));
  yield* tween(0.35, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
