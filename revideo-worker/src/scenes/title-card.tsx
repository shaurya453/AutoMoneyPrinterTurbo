import '../global.css';
import {makeScene2D, Rect, Txt, Layout} from '@revideo/2d';
import {all, createRef, easeInOutCubic, tween, useScene, waitFor} from '@revideo/core';

export default makeScene2D('title-card', function* (view) {
  const vars = useScene().variables;
  const title = vars.get('title', 'Untitled')();
  const subtitle = vars.get('subtitle', '')();
  const duration = Number(vars.get('duration', 5)());

  const titleRef = createRef<Txt>();
  const subtitleRef = createRef<Txt>();

  view.add(
    <Rect width={1920} height={1080} fill={'#080808'}>
      <Layout
        direction={'column'}
        alignItems={'center'}
        justifyContent={'center'}
        gap={56}
        width={'100%'}
        height={'100%'}
      >
        <Txt
          ref={titleRef}
          text={title as string}
          fontSize={88}
          fontWeight={700}
          fontFamily={'Inter, sans-serif'}
          fill={'#ffffff'}
          opacity={0}
          textAlign={'center'}
          maxWidth={1600}
        />
        <Txt
          ref={subtitleRef}
          text={subtitle as string}
          fontSize={44}
          fontWeight={300}
          fontFamily={'Inter, sans-serif'}
          fill={'#aaaaaa'}
          opacity={0}
          textAlign={'center'}
          maxWidth={1400}
        />
      </Layout>
    </Rect>,
  );

  yield* tween(0.8, v => titleRef().opacity(easeInOutCubic(v)));
  yield* waitFor(0.4);
  if (subtitle) {
    yield* tween(0.8, v => subtitleRef().opacity(easeInOutCubic(v)));
  }
  yield* waitFor(Math.max(0, duration - 2.0));
});
