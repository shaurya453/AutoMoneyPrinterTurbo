import '../global.css';
import {blur, makeScene2D, Rect, Txt, Video} from '@revideo/2d';
import {createRef, easeInOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Timeline Card — a chronology/date marker (e.g. "1969" or "Three years
// later"), full-replace like infographic/list. Deliberately minimal, one
// or two lines — a chapter break, not a chart.

export default makeScene2D('timeline-card', function* (view) {
  const vars = useScene().variables;

  const label     = String(vars.get('label', '')());
  const sublabel  = String(vars.get('sublabel', '')());
  const duration  = Number(vars.get('duration', 4)());
  const bgVideo   = String(vars.get('bgVideo', '')());

  const hasSublabel = sublabel.length > 0;

  const FADE_IN  = 0.4;
  const FADE_OUT = 0.35;
  const RULE_W   = 120;

  const rootRef = createRef<Rect>();
  const ruleRef = createRef<Rect>();

  view.add(
    <Rect ref={rootRef} width={1920} height={1080} layout={false} opacity={0}>
      {bgVideo
        ? <Video src={bgVideo} width={1920} height={1080} opacity={0.5} filters={[blur(18)]} loop play />
        : <Rect width={1920} height={1080} fill={'#0a0a0a'} />
      }
      <Rect width={1920} height={1080} fill={'rgba(0,0,0,0.55)'} />
      <Rect
        ref={ruleRef}
        width={0}
        height={3}
        y={hasSublabel ? -70 : -40}
        fill={'#ffffff'}
      />
      <Txt
        text={label}
        x={0}
        y={0}
        width={1600}
        fontSize={96}
        fontWeight={700}
        fontFamily={'Stack Sans Text, sans-serif'}
        fill={'#ffffff'}
        textAlign={'center'}
        justifyContent={'center'}
        textWrap={true}
      />
      {hasSublabel && (
        <Txt
          text={sublabel}
          x={0}
          y={90}
          width={1400}
          fontSize={36}
          fontWeight={400}
          fontFamily={'Stack Sans Text, sans-serif'}
          fill={'#c9c9c9'}
          textAlign={'center'}
          justifyContent={'center'}
          textWrap={true}
        />
      )}
    </Rect>,
  );

  yield* tween(FADE_IN, v => {
    const t = easeInOutCubic(v);
    rootRef().opacity(t);
    ruleRef().width(RULE_W * t);
  });
  const holdDur = Math.max(0, duration - FADE_IN - FADE_OUT);
  if (holdDur > 0) {
    yield* waitFor(holdDur);
  }
  yield* tween(FADE_OUT, v => rootRef().opacity(1 - easeInOutCubic(v)));
});
