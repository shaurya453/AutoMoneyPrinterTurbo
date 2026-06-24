import '../global.css';
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

  const LINE_W = 120;
  const LINE_Y = sublabel ? -108 : -88;
  const LABEL_Y = sublabel ? -48 : -28;
  const SUBLABEL_Y = 28;

  const containerRef = createRef<Rect>();
  const lineRef = createRef<Rect>();
  const labelRef = createRef<Txt>();
  const sublabelRef = createRef<Txt>();

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#080808'} opacity={1} layout={false}>
      <Rect
        ref={lineRef}
        width={0}
        height={2}
        fill={'#ffffff'}
        y={LINE_Y}
      />
      <Txt
        ref={labelRef}
        text={label}
        x={0}
        y={LABEL_Y}
        fontSize={80}
        fontWeight={700}
        fontFamily={'Inter, sans-serif'}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        justifyContent={'center'}
        width={1600}
        textWrap={true}
        letterSpacing={4}
      />
      <Txt
        ref={sublabelRef}
        text={sublabel}
        x={0}
        y={SUBLABEL_Y}
        fontSize={34}
        fontWeight={300}
        fontFamily={'Inter, sans-serif'}
        fill={'#888888'}
        opacity={0}
        textAlign={'center'}
        justifyContent={'center'}
        width={1400}
        textWrap={true}
        letterSpacing={2}
      />
    </Rect>,
  );

  yield* tween(0.35, v => lineRef().width(easeInOutCubic(v) * LINE_W));

  if (sublabel) {
    yield* all(
      tween(0.4, v => labelRef().opacity(easeOutCubic(v))),
      chain(waitFor(0.15), tween(0.35, v => sublabelRef().opacity(easeOutCubic(v)))),
    );
  } else {
    yield* tween(0.4, v => labelRef().opacity(easeOutCubic(v)));
  }

  const animIn = 0.35 + (sublabel ? 0.4 + 0.15 : 0.4);
  yield* waitFor(Math.max(0, duration - animIn - 0.35));
  yield* tween(0.35, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
