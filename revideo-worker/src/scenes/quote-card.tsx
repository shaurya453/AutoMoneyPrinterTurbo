import '../global.css';
import {blur, makeScene2D, Rect, Txt, Video} from '@revideo/2d';
import {createRef, easeInOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Quote Card — attributed quote, full-replace (like infographic/list, not
// composited over footage). Restrained: one big serif quote mark, the quote
// itself, and a muted attribution line — no chart/list chrome to compete
// with the words.

export default makeScene2D('quote-card', function* (view) {
  const vars = useScene().variables;

  const quote       = String(vars.get('quote', '')());
  const attribution = String(vars.get('attribution', '')());
  const duration    = Number(vars.get('duration', 6)());
  const bgVideo     = String(vars.get('bgVideo', '')());

  const hasAttribution = attribution.length > 0;

  const FADE_IN  = 0.45;
  const FADE_OUT = 0.35;

  const rootRef = createRef<Rect>();

  view.add(
    <Rect ref={rootRef} width={1920} height={1080} layout={false} opacity={0}>
      {bgVideo
        ? <Video src={bgVideo} width={1920} height={1080} opacity={0.5} filters={[blur(18)]} loop play />
        : <Rect width={1920} height={1080} fill={'#0a0a0a'} />
      }
      <Rect width={1920} height={1080} fill={'rgba(0,0,0,0.55)'} />
      <Txt
        text={'“'}
        x={0}
        y={-260}
        fontSize={220}
        fontWeight={700}
        fontFamily={'Playfair Display, serif'}
        fill={'#ffffff'}
        opacity={0.18}
      />
      <Txt
        text={quote}
        x={0}
        y={-40}
        width={1400}
        fontSize={64}
        fontWeight={600}
        fontFamily={'Playfair Display, serif'}
        fill={'#ffffff'}
        textAlign={'center'}
        justifyContent={'center'}
        textWrap={true}
      />
      {hasAttribution && (
        <Txt
          text={`— ${attribution}`}
          x={0}
          y={230}
          width={1400}
          fontSize={34}
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

  yield* tween(FADE_IN, v => rootRef().opacity(easeInOutCubic(v)));
  const holdDur = Math.max(0, duration - FADE_IN - FADE_OUT);
  if (holdDur > 0) {
    yield* waitFor(holdDur);
  }
  yield* tween(FADE_OUT, v => rootRef().opacity(1 - easeInOutCubic(v)));
});
