import '../global.css';
import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {createRef, easeInOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant A — Minimal Fade
// Title and subtitle fade in sequentially, centered on a dark background.

export default makeScene2D('title-card', function* (view) {
  const vars     = useScene().variables;
  const title    = String(vars.get('title', 'Untitled')());
  const subtitle = String(vars.get('subtitle', '')());
  const duration = Number(vars.get('duration', 5)());

  const hasSub  = subtitle.length > 0;
  // When subtitle present: title sits 80px above centre, subtitle 80px below.
  // When no subtitle: title is exactly at centre (y=0).
  const TITLE_Y  = hasSub ? -80 : 0;
  const ANIM_OUT = 0.45;

  const containerRef = createRef<Rect>();
  const titleRef     = createRef<Txt>();
  const subRef       = createRef<Txt>();

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#080808'} opacity={1} layout={false}>
      <Txt
        ref={titleRef}
        text={title}
        x={0}
        y={TITLE_Y}
        fontSize={88}
        fontWeight={700}
        fontFamily={'Inter, sans-serif'}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        textWrap={true}
        width={1600}
      />
      <Txt
        ref={subRef}
        text={subtitle}
        x={0}
        y={80}
        fontSize={44}
        fontWeight={300}
        fontFamily={'Inter, sans-serif'}
        fill={'#aaaaaa'}
        opacity={0}
        textAlign={'center'}
        textWrap={true}
        width={1400}
      />
    </Rect>,
  );

  yield* tween(0.8, v => titleRef().opacity(easeInOutCubic(v)));
  if (hasSub) {
    yield* waitFor(0.3);
    yield* tween(0.7, v => subRef().opacity(easeInOutCubic(v)));
  }
  const animIn = hasSub ? 1.8 : 0.8;
  yield* waitFor(Math.max(0, duration - animIn - ANIM_OUT));
  yield* tween(ANIM_OUT, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
