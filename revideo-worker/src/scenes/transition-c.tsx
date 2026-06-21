import '../global.css';
import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant C — Corner Brackets
// Two L-shaped brackets animate in simultaneously from the top-left and
// bottom-right corners; the section label and sublabel fade in between them.
// Everything dissolves out together.

export default makeScene2D('transition-c', function* (view) {
  const vars = useScene().variables;
  const label    = String(vars.get('label',    '')());
  const sublabel = String(vars.get('sublabel', '')());
  const duration = Number(vars.get('duration', 3.0)());

  const hasSub = sublabel.length > 0;

  const ARM_LEN = 160;
  const ARM_W   = 2;
  const TL_X    = -880;
  const TL_Y    = -490;
  const BR_X    = 880;
  const BR_Y    = 490;

  const LABEL_Y = hasSub ? -46 : -18;
  const SUB_Y   = 36;

  const containerRef = createRef<Rect>();
  const tlHRef = createRef<Rect>();
  const tlVRef = createRef<Rect>();
  const brHRef = createRef<Rect>();
  const brVRef = createRef<Rect>();
  const labelRef = createRef<Txt>();
  const subRef   = createRef<Txt>();

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#080808'} opacity={1}>
      <Rect ref={tlHRef} width={0} height={ARM_W} fill={'#ffffff'} opacity={0.85} x={TL_X} y={TL_Y} />
      <Rect ref={tlVRef} width={ARM_W} height={0} fill={'#ffffff'} opacity={0.85} x={TL_X} y={TL_Y} />
      <Rect ref={brHRef} width={0} height={ARM_W} fill={'#ffffff'} opacity={0.85} x={BR_X} y={BR_Y} />
      <Rect ref={brVRef} width={ARM_W} height={0} fill={'#ffffff'} opacity={0.85} x={BR_X} y={BR_Y} />
      <Txt
        ref={labelRef}
        text={label}
        y={LABEL_Y}
        fontSize={80}
        fontWeight={700}
        fontFamily={'Inter, sans-serif'}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1600}
        letterSpacing={4}
      />
      <Txt
        ref={subRef}
        text={sublabel}
        y={SUB_Y}
        fontSize={34}
        fontWeight={300}
        fontFamily={'Inter, sans-serif'}
        fill={'#888888'}
        opacity={0}
        textAlign={'center'}
        maxWidth={1400}
        letterSpacing={2}
      />
    </Rect>,
  );

  yield* all(
    tween(0.40, v => {
      const w = easeInOutCubic(v) * ARM_LEN;
      tlHRef().width(w);
      tlHRef().x(TL_X + w / 2);
    }),
    tween(0.40, v => {
      const h = easeInOutCubic(v) * ARM_LEN;
      tlVRef().height(h);
      tlVRef().y(TL_Y + h / 2);
    }),
    tween(0.40, v => {
      const w = easeInOutCubic(v) * ARM_LEN;
      brHRef().width(w);
      brHRef().x(BR_X - w / 2);
    }),
    tween(0.40, v => {
      const h = easeInOutCubic(v) * ARM_LEN;
      brVRef().height(h);
      brVRef().y(BR_Y - h / 2);
    }),
  );

  const LABEL_IN = hasSub ? 0.78 : 0.48;
  if (hasSub) {
    yield* all(
      tween(0.48, v => labelRef().opacity(easeOutCubic(v))),
      chain(waitFor(0.26), tween(0.42, v => subRef().opacity(easeOutCubic(v)))),
    );
  } else {
    yield* tween(0.48, v => labelRef().opacity(easeOutCubic(v)));
  }

  const ANIM_IN  = 0.40 + LABEL_IN;
  const ANIM_OUT = 0.36;
  yield* waitFor(Math.max(0, duration - ANIM_IN - ANIM_OUT));
  yield* tween(ANIM_OUT, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
