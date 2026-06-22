import '../global.css';
import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant B — Panel Sweep
// A deep-navy panel sweeps in from the left edge, covering the full frame. The
// section label and accent rule fade in on the panel, then content fades out and
// the panel sweeps off to the right — a clean cinematic wipe.

export default makeScene2D('transition-b', function* (view) {
  const vars = useScene().variables;
  const label    = String(vars.get('label',    '')());
  const sublabel = String(vars.get('sublabel', '')());
  const duration = Number(vars.get('duration', 3.0)());

  const hasSub = sublabel.length > 0;

  const LABEL_Y   = hasSub ? -46 : -16;
  const RULE_Y    = hasSub ? -106 : -76;
  const SUB_Y     = 36;

  const PANEL_X_IN  = -1920;
  const PANEL_X_MID = 0;
  const PANEL_X_OUT = 1920;

  const containerRef = createRef<Rect>();
  const panelRef     = createRef<Rect>();
  const ruleRef      = createRef<Rect>();
  const labelRef     = createRef<Txt>();
  const subRef       = createRef<Txt>();

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#080808'} opacity={1} layout={false}>
      <Rect
        ref={panelRef}
        width={1920}
        height={1080}
        fill={'#0d0d1c'}
        x={PANEL_X_IN}
        y={0}
      />
      <Rect
        ref={ruleRef}
        width={0}
        height={2}
        fill={'#ffffff'}
        opacity={0.55}
        x={0}
        y={RULE_Y}
      />
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
        width={1600}
        textWrap={true}
        letterSpacing={4}
      />
      <Txt
        ref={subRef}
        text={sublabel}
        y={SUB_Y}
        fontSize={32}
        fontWeight={300}
        fontFamily={'Inter, sans-serif'}
        fill={'#888888'}
        opacity={0}
        textAlign={'center'}
        width={1400}
        textWrap={true}
        letterSpacing={2}
      />
    </Rect>,
  );

  const SWEEP_DUR = 0.42;
  yield* tween(SWEEP_DUR, v => {
    panelRef().x(PANEL_X_IN + (PANEL_X_MID - PANEL_X_IN) * easeInOutCubic(v));
  });

  const CONTENT_IN = hasSub ? 0.78 : 0.48;
  yield* all(
    tween(0.36, v => ruleRef().width(easeInOutCubic(v) * 340)),
    chain(waitFor(0.10), tween(0.42, v => labelRef().opacity(easeOutCubic(v)))),
    ...(hasSub
      ? [chain(waitFor(0.36), tween(0.42, v => subRef().opacity(easeOutCubic(v))))]
      : []),
  );

  const CONTENT_OUT = 0.24;
  const SWEEP_OUT   = 0.44;
  yield* waitFor(Math.max(0, duration - SWEEP_DUR - CONTENT_IN - CONTENT_OUT - SWEEP_OUT));

  yield* all(
    tween(CONTENT_OUT, v => labelRef().opacity(1 - easeInOutCubic(v))),
    tween(CONTENT_OUT, v => ruleRef().opacity(0.55 * (1 - easeInOutCubic(v)))),
    ...(hasSub ? [tween(CONTENT_OUT, v => subRef().opacity(1 - easeInOutCubic(v)))] : []),
  );

  yield* tween(SWEEP_OUT, v => {
    panelRef().x(PANEL_X_MID + (PANEL_X_OUT - PANEL_X_MID) * easeInOutCubic(v));
  });
});
