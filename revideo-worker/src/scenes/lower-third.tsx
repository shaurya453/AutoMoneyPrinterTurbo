import '../global.css';
import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {useScene, waitFor} from '@revideo/core';

// Lower Third — white text on a dynamically-sized dark rect backdrop.
// The pipeline composites this via colorkey (black→transparent) over footage:
//   colorkey removes pure black (#000000) → footage shows through
//   dark rect (#1a1a1a = rgb 26,26,26, distance ~0.18 from black) is well outside
//   the key+blend threshold (0.01+0.05=0.06) → stays opaque as the text backdrop
//   white text (#ffffff) → clearly opaque
// The backdrop rect auto-sizes to the label text via layout+padding.
// Fade in/out is applied to the video (RGB) by FFmpeg, not here.

export default makeScene2D('lower-third', function* (view) {
  const vars     = useScene().variables;
  const label    = String(vars.get('label', '')());
  const duration = Number(vars.get('duration', 5)());

  // Backdrop rect left edge at screen x=84px (text starts at 84+24=108px).
  // Revideo centre-origin coords: left edge x = 84 - 960 = -876.
  // BOX_BOTTOM_Y: bottom of a single-line box in Revideo coords.
  //   centre y=350 → screen y=890; half single-line height = (58+2×12)/2 = 41 → bottom = 391.
  //   Screen: 540+391 = 931px from top, leaving 149px margin at the bottom.
  // offsetY={1} bottom-anchors the box so long labels wrap upward, not into the frame edge.
  const BOX_LEFT_X  = -876; // left edge of backdrop rect
  const BOX_BOTTOM_Y = 391; // bottom edge of backdrop rect (centre-origin)

  // Padding inside the backdrop rect (px).
  const PAD_X = 24; // left and right
  const PAD_Y = 12; // top and bottom

  // Max width: frame(1920) - left margin(84) - right margin(80) = 1756 → 1750.
  // Long labels wrap to a second line rather than running off-screen.
  const MAX_W = 1750;

  view.add(
    <Rect width={1920} height={1080} fill={'#000000'} layout={false}>
      <Rect
        x={BOX_LEFT_X}
        y={BOX_BOTTOM_Y}
        offsetX={-1}
        offsetY={1}
        maxWidth={MAX_W}
        fill={'#1a1a1a'}
        padding={[PAD_Y, PAD_X]}
        radius={8}
        layout={true}
      >
        <Txt
          text={label}
          fontSize={58}
          fontWeight={600}
          fontFamily={'Playfair Display, serif'}
          fill={'#ffffff'}
          textAlign={'left'}
          textWrap={true}
        />
      </Rect>
    </Rect>
  );

  yield* waitFor(duration);
});
