import '../global.css';
import {makeScene2D, Rect, Txt} from '@revideo/2d';
import {all, chain, createRef, easeInOutCubic, easeOutCubic, tween, useScene, waitFor} from '@revideo/core';

// Variant D — Card Grid
// Items are laid out in a 2-column grid of bordered cards, each with a coloured
// accent number badge in the top-left corner. Cards fade in column-by-column.
// Falls back to single-column for ≤3 items. Background: deep charcoal (#0d0d0d).

const COLORS = [
  '#4f8ef7', '#f7964f', '#4fd1a0', '#f74f7e',
  '#b44ff7', '#f7e14f', '#4fcef7', '#f74fb3',
];

export default makeScene2D('list-d', function* (view) {
  const vars = useScene().variables;

  const title    = String(vars.get('title', '')());
  const rawItems = vars.get('items', ['Item 1', 'Item 2', 'Item 3'])();
  const items    = Array.from(rawItems as string[]);
  const duration = Number(vars.get('duration', 8)());

  const n        = Math.min(items.length, 6);
  const hasTitle = title.length > 0;

  // Layout: 2 columns for 4-6 items, 1 column for 1-3
  const useTwoCols = n >= 4;
  const COLS       = useTwoCols ? 2 : 1;
  const ROWS       = Math.ceil(n / COLS);

  const CARD_W  = useTwoCols ? 840 : 1100;
  const CARD_H  = ROWS <= 2 ? 220 : 160;
  const COL_GAP = 40;
  const ROW_GAP = 32;

  const gridW = COLS * CARD_W + (COLS - 1) * COL_GAP;
  const gridH = ROWS * CARD_H + (ROWS - 1) * ROW_GAP;

  const GRID_TOP  = hasTitle ? -gridH / 2 + 60 : -gridH / 2;
  const TITLE_Y   = hasTitle ? GRID_TOP - CARD_H / 2 - 56 : 0;

  // Card positions (top-left corner, converted to centre for Revideo)
  const cardCentres = Array.from({length: n}, (_, i) => {
    const col = i % COLS;
    const row = Math.floor(i / COLS);
    const x = -gridW / 2 + col * (CARD_W + COL_GAP) + CARD_W / 2;
    const y = GRID_TOP + row * (CARD_H + ROW_GAP) + CARD_H / 2;
    return {x, y};
  });

  const STAGGER  = 0.18;
  const FADE_DUR = 0.45;
  const ANIM_OUT = 0.35;

  const containerRef = createRef<Rect>();
  const titleRef     = createRef<Txt>();
  const cardRefs     = Array.from({length: n}, () => createRef<Rect>());
  const numRefs      = Array.from({length: n}, () => createRef<Txt>());
  const txtRefs      = Array.from({length: n}, () => createRef<Txt>());

  view.add(
    <Rect ref={containerRef} width={1920} height={1080} fill={'#0d0d0d'} opacity={1} layout={false}>
      <Txt
        ref={titleRef}
        text={title}
        y={TITLE_Y}
        fontSize={52}
        fontWeight={700}
        fontFamily={'Inter, sans-serif'}
        fill={'#ffffff'}
        opacity={0}
        textAlign={'center'}
        width={1600}
        textWrap={true}
      />

      {Array.from({length: n}, (_, i) => {
        const {x, y} = cardCentres[i];
        return (
          <Rect
            ref={cardRefs[i]}
            width={CARD_W}
            height={CARD_H}
            fill={'#161620'}
            stroke={COLORS[i % COLORS.length]}
            lineWidth={1.5}
            opacity={0}
            radius={8}
            x={x}
            y={y}
          />
        );
      })}
      {Array.from({length: n}, (_, i) => {
        const {x, y} = cardCentres[i];
        const numSize = CARD_H <= 160 ? 32 : 40;
        return (
          <Txt
            ref={numRefs[i]}
            text={String(i + 1).padStart(2, '0')}
            x={x - CARD_W / 2 + 32}
            y={y - CARD_H / 2 + numSize / 2 + 14}
            fontSize={numSize}
            fontWeight={800}
            fontFamily={'Inter, sans-serif'}
            fill={COLORS[i % COLORS.length]}
            opacity={0}
            textAlign={'left'}
          />
        );
      })}
      {Array.from({length: n}, (_, i) => {
        const {x, y} = cardCentres[i];
        const txtSize = CARD_H <= 160 ? 26 : 32;
        return (
          <Txt
            ref={txtRefs[i]}
            text={items[i]}
            x={x}
            y={y + 8}
            fontSize={txtSize}
            fontWeight={400}
            fontFamily={'Inter, sans-serif'}
            fill={'#d8d8d8'}
            opacity={0}
            textAlign={'center'}
            width={CARD_W - 48}
            textWrap={true}
          />
        );
      })}
    </Rect>,
  );

  const titleTime = hasTitle ? 0.40 + 0.12 : 0;
  if (hasTitle) {
    yield* tween(0.40, v => titleRef().opacity(easeInOutCubic(v)));
    yield* waitFor(0.12);
  }

  // Cards fade in column-by-column, left column first
  yield* all(
    ...Array.from({length: n}, (_, i) =>
      chain(
        waitFor(i * STAGGER),
        tween(FADE_DUR, v => {
          const t = easeOutCubic(v);
          cardRefs[i]().opacity(t);
          numRefs[i]().opacity(t);
          txtRefs[i]().opacity(t);
        }),
      ),
    ),
  );

  const animIn = titleTime + (n - 1) * STAGGER + FADE_DUR;
  yield* waitFor(Math.max(0, duration - animIn - ANIM_OUT));
  yield* tween(ANIM_OUT, v => containerRef().opacity(1 - easeInOutCubic(v)));
});
