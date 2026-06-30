import '../global.css';
import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {createRef, easeInOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Lower Third — left-anchored entity label with semi-transparent blurred backdrop.
// Fades in over stock footage, holds for the clip duration, fades out.
// Used for named entities only when a visual identifier adds genuine viewer value.

export default makeScene2D('lower-third', function* (view) {
  const vars     = useScene().variables;
  const label    = String(vars.get('label', '')());
  const duration = Number(vars.get('duration', 5)());

  // --- Layout constants ---
  const FONT_SIZE = 36;
  const PAD_H     = 28;   // horizontal padding inside backdrop
  const PAD_V     = 14;   // vertical padding inside backdrop
  // Approximate text width: ~0.58× fontSize per character for Inter SemiBold
  const textW     = Math.max(80, Math.round(label.length * FONT_SIZE * 0.58));
  const backdropW = textW + PAD_H * 2;
  const backdropH = FONT_SIZE + PAD_V * 2;

  // Position in lower third, above subtitle zone.
  // Revideo coords: center (0,0), frame ±960 / ±540.
  // Subtitles at "bottom" render at y ≈ 486 (1026px from top in 1080p).
  // We sit at y = 370 (~910px from top) — comfortably above.
  const LEFT_MARGIN = 80;
  const ANCHOR_X    = -960 + LEFT_MARGIN + backdropW / 2;
  const ANCHOR_Y    = 370;

  const ANIM_IN  = 0.40;
  const ANIM_OUT = 0.35;

  const backdropRef = createRef<Rect>();

  view.add(
    <Rect width={1920} height={1080} fill={'#000000'} layout={false}>
      <Rect
        ref={backdropRef}
        x={ANCHOR_X}
        y={ANCHOR_Y}
        width={backdropW}
        height={backdropH}
        fill={'rgba(0,0,0,0.78)'}
        radius={5}
        shadowBlur={32}
        shadowColor={'rgba(0,0,0,0.6)'}
        opacity={0}
        layout={false}
      >
        <Txt
          text={label}
          x={-backdropW / 2 + PAD_H}
          offsetX={-1}
          fontSize={FONT_SIZE}
          fontWeight={600}
          fontFamily={'Inter, sans-serif'}
          fill={'#ffffff'}
          textAlign={'left'}
          textWrap={false}
        />
      </Rect>
    </Rect>
  );

  // Fade in
  yield* tween(ANIM_IN, v => backdropRef().opacity(easeInOutCubic(v) * 0.92));
  // Hold
  yield* waitFor(Math.max(0, duration - ANIM_IN - ANIM_OUT));
  // Fade out
  yield* tween(ANIM_OUT, v => backdropRef().opacity(0.92 * (1 - easeInOutCubic(v))));
});
