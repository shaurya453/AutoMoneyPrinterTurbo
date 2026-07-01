import '../global.css';
import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {useScene, waitFor} from '@revideo/core';

// Lower Third — white text on a solid black frame.
// The pipeline composites this via `screen` blend over footage:
//   screen(footage, black=0) = footage  →  black background disappears
//   screen(footage, white=1) = white    →  text stays white
// The feathered blob PNG is overlaid separately by FFmpeg before the text.
// Fade in/out is applied to the video (RGB) by FFmpeg, not here.

export default makeScene2D('lower-third', function* (view) {
  const vars     = useScene().variables;
  const label    = String(vars.get('label', '')());
  const duration = Number(vars.get('duration', 5)());

  // Text left edge at screen x=108px (80 left margin + 28 pad), y=910px center.
  // Revideo centre-origin coords: x = 108 - 960 = -852, y = 910 - 540 = 370.
  const TEXT_X = -852;
  const TEXT_Y = 350;

  view.add(
    <Rect width={1920} height={1080} fill={'#000000'} layout={false}>
      <Txt
        text={label}
        x={TEXT_X}
        y={TEXT_Y}
        offsetX={-1}
        fontSize={58}
        fontWeight={600}
        fontFamily={'Playfair Display, serif'}
        fill={'#ffffff'}
        textAlign={'left'}
        textWrap={false}
        layout={false}
      />
    </Rect>
  );

  yield* waitFor(duration);
});
