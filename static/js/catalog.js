// Option catalogues for the v2 image tools: fonts, filters, frames, shapes, watermark positions, page widths.
// Ids are the values the server's ops accept (`app/imaging/ops_ext/edit.py`); labels and the CSS used for the
// instant on-canvas previews are ours. `adoptEnums()` narrows a list to what the running server offers.

/**
 * Fonts shipped for server-side text (app/imaging/text.py FONTS — ids are the server's); `css` is the stack the
 * on-canvas preview uses (the page itself ships Fraunces, Libre Franklin and Plex Mono).
 */
export const FONTS = [
  { id: 'libre_franklin', label: 'Libre Franklin', css: '"Libre Franklin", "Franklin Gothic Book", Arial, sans-serif' },
  { id: 'fraunces', label: 'Fraunces', css: 'Fraunces, Georgia, serif' },
  { id: 'anton', label: 'Anton', css: 'Anton, Impact, "Arial Narrow Bold", sans-serif' },
  { id: 'plex_mono', label: 'IBM Plex Mono', css: '"IBM Plex Mono", Menlo, monospace' },
  { id: 'dejavu_sans', label: 'DejaVu Sans', css: '"DejaVu Sans", Verdana, sans-serif' },
  { id: 'dejavu_serif', label: 'DejaVu Serif', css: '"DejaVu Serif", Georgia, serif' },
  { id: 'caveat', label: 'Caveat (handwriting)', css: 'Caveat, "Segoe Print", cursive' },
  { id: 'dancing_script', label: 'Dancing Script', css: '"Dancing Script", "Brush Script MT", cursive' },
  { id: 'great_vibes', label: 'Great Vibes', css: '"Great Vibes", "Brush Script MT", cursive' },
  { id: 'homemade_apple', label: 'Homemade Apple', css: '"Homemade Apple", "Segoe Print", cursive' },
];

const FONT_ALIAS = { 'libre-franklin': 'libre_franklin', 'ibm-plex-mono': 'plex_mono', 'dejavu-sans': 'dejavu_sans', 'dancing-script': 'dancing_script', 'great-vibes': 'great_vibes' };
/** A font id the server knows (old hyphenated ids are mapped; unknown → the default). */
export const fontId = (id) => (FONTS.some((f) => f.id === id) ? id : FONT_ALIAS[id] || FONTS[0].id);
export const fontCss = (id) => (FONTS.find((f) => f.id === fontId(id)) || FONTS[0]).css;

/**
 * Named filters (app/imaging/editor.py FILTERS). `css` approximates the look on the small swatches only (the
 * real filter runs on the server), scaled by intensity in `filterCss()`.
 */
export const FILTERS = [
  { id: 'none', label: 'Original', css: [] },
  { id: 'vivid', label: 'Vivid', css: [['saturate', 1.35], ['contrast', 1.08]] },
  { id: 'warm', label: 'Warm', css: [['sepia', 0.2], ['saturate', 1.15], ['hue-rotate', -6]] },
  { id: 'cool', label: 'Cool', css: [['saturate', 0.95], ['hue-rotate', 10], ['brightness', 1.02]] },
  { id: 'golden', label: 'Golden hour', css: [['sepia', 0.35], ['saturate', 1.25], ['brightness', 1.04]] },
  { id: 'fade', label: 'Fade', css: [['contrast', 0.82], ['brightness', 1.08], ['saturate', 0.78]] },
  { id: 'vintage', label: 'Vintage', css: [['sepia', 0.45], ['contrast', 0.9], ['brightness', 1.03]] },
  { id: 'chrome', label: 'Chrome', css: [['contrast', 1.2], ['saturate', 1.15], ['hue-rotate', 4]] },
  { id: 'dramatic', label: 'Dramatic', css: [['contrast', 1.4], ['saturate', 1.1], ['brightness', 0.92]] },
  { id: 'lomo', label: 'Lomo', css: [['saturate', 1.3], ['contrast', 1.22], ['brightness', 0.95]] },
  { id: 'teal_orange', label: 'Teal & orange', css: [['saturate', 1.2], ['contrast', 1.05], ['hue-rotate', -10]] },
  { id: 'mono', label: 'Mono', css: [['grayscale', 1]] },
  { id: 'noir', label: 'Noir', css: [['grayscale', 1], ['contrast', 1.45], ['brightness', 0.92]] },
  { id: 'sepia', label: 'Sepia', css: [['sepia', 0.9]] },
  { id: 'broadsheet', label: 'Broadsheet', css: [['grayscale', 1], ['sepia', 0.25], ['contrast', 1.15]] },
  { id: 'cyanotype', label: 'Cyanotype', css: [['grayscale', 1], ['sepia', 1], ['hue-rotate', 175], ['saturate', 2.2]] },
  { id: 'halftone', label: 'Halftone', css: [['grayscale', 1], ['contrast', 1.6]] },
  { id: 'pencil', label: 'Pencil', css: [['grayscale', 1], ['contrast', 1.8], ['brightness', 1.25]] },
  { id: 'invert', label: 'Invert', css: [['invert', 1]] },
  // the CSS chains the server reproduces for these looks (editor.py), so swatch and result agree
  { id: 'matte', label: 'Matte', css: [['contrast', 0.85], ['saturate', 0.9], ['brightness', 1.06]] },
  { id: 'polaroid', label: 'Instant', css: [['sepia', 0.18], ['contrast', 1.08], ['saturate', 1.2], ['brightness', 1.05]] },
  { id: 'kodachrome', label: 'Slide film', css: [['sepia', 0.2], ['saturate', 1.45], ['contrast', 1.15], ['hue-rotate', -5]] },
  { id: 'technicolor', label: 'Cinema', css: [['saturate', 1.75], ['contrast', 1.2], ['hue-rotate', 4]] },
  { id: 'bright', label: 'Bright', css: [['brightness', 1.2], ['contrast', 1.05]] },
  // no CSS equivalent: a hint of the look on the swatch only
  { id: 'sharpen', label: 'Sharpen', css: [['contrast', 1.12]] },
  { id: 'soft', label: 'Soft focus', css: [['blur', 0.8], ['brightness', 1.03]] },
];

/** Readable name of a filter or frame id (catalogue label; unknown ids are spelled out). */
export function catalogLabel(list, id) {
  const hit = list.find((x) => x.id === id);
  return hit ? hit.label : String(id || '').replace(/[-_]+/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

/** CSS filter string for a named filter at `intensity` (0…1), e.g. for swatches. */
export function filterCss(id, intensity = 1) {
  const f = FILTERS.find((x) => x.id === id);
  if (!f || !f.css.length) return 'none';
  const k = Math.max(0, Math.min(1, Number(intensity) || 0));
  return f.css.map(([fn, v]) => {
    if (fn === 'hue-rotate') return `hue-rotate(${(v * k).toFixed(1)}deg)`;
    if (fn === 'blur') return `blur(${(v * k).toFixed(2)}px)`;
    // identity is 0 for grayscale/sepia/invert, 1 for the others
    const id0 = fn === 'grayscale' || fn === 'sepia' || fn === 'invert' ? 0 : 1;
    return `${fn}(${(id0 + (v - id0) * k).toFixed(3)})`;
  }).join(' ');
}

/** Frame styles (app/imaging/editor.py FRAMES): default width (share of the shorter side) and colour. */
export const FRAMES = [
  { id: 'none', label: 'None' },
  { id: 'line', label: 'Line', width: 0.03, color: '#1d1b19' },
  { id: 'double', label: 'Double rule', width: 0.05, color: '#1d1b19' },
  { id: 'polaroid', label: 'Instant photo', width: 0.05, color: '#ffffff' },
  { id: 'mat', label: 'Mat board', width: 0.08, color: '#f2ecdf' },
  { id: 'rounded', label: 'Rounded corners', width: 0.06, color: 'transparent' },
  { id: 'shadow', label: 'Drop shadow', width: 0.05, color: '#00000073' },
  { id: 'film', label: 'Film strip', width: 0.08, color: '#141414' },
  { id: 'stamp', label: 'Postage stamp', width: 0.05, color: '#ffffff' },
  { id: 'circle', label: 'Circle', width: 0, color: '#1d1b19' },
  { id: 'vignette', label: 'Vignette', width: 0.04, color: '#000000' },
];

/** Shapes (`overlay` op, type shape — app/imaging/editor.py SHAPES). */
export const SHAPES = [
  { id: 'rect', label: 'Rectangle' },
  { id: 'ellipse', label: 'Ellipse' },
  { id: 'line', label: 'Line' },
  { id: 'arrow', label: 'Arrow' },
];

/** The 3×3 watermark grid, row by row (op value, label), plus mosaic. */
export const POSITIONS = [
  ['top-left', 'Top left'], ['top', 'Top'], ['top-right', 'Top right'],
  ['left', 'Left'], ['center', 'Centre'], ['right', 'Right'],
  ['bottom-left', 'Bottom left'], ['bottom', 'Bottom'], ['bottom-right', 'Bottom right'],
];

/** Position value ↔ {row, col} of the 3×3 grid. */
export function zoneOf(pos) {
  const i = POSITIONS.findIndex(([v]) => v === pos);
  if (i < 0) return { row: 'bottom', col: 'right' };
  return { row: ['top', 'middle', 'bottom'][Math.floor(i / 3)], col: ['left', 'center', 'right'][i % 3] };
}

export function positionOf({ row, col }) {
  const r = { top: 0, middle: 1, bottom: 2 }[row] ?? 2;
  const c = { left: 0, center: 1, right: 2 }[col] ?? 2;
  return POSITIONS[r * 3 + c][0];
}

/** Screen widths for HTML → image (iLoveIMG offers 1920 / 1440 / 1200 / 768 / 320; we add a modern phone). */
export const PAGE_WIDTHS = [
  { value: 1920, label: '1920', title: 'Large desktop' },
  { value: 1440, label: '1440', title: 'Laptop' },
  { value: 1200, label: '1200', title: 'Small laptop' },
  { value: 768, label: '768', title: 'Tablet' },
  { value: 390, label: '390', title: 'Phone' },
];

/** Blank canvases for memes without a photo (px). */
export const BLANK_CANVASES = [
  { id: 'square', label: 'Square', w: 1080, h: 1080 },
  { id: 'landscape', label: 'Landscape', w: 1200, h: 675 },
  { id: 'portrait', label: 'Portrait', w: 1080, h: 1350 },
];

/**
 * Keep the entries of `list` whose id the server's enum allows (unknown server values are appended with a
 * readable label). Returns `list` unchanged when the enum is missing.
 */
export function adoptEnums(list, values) {
  if (!Array.isArray(values) || !values.length) return list;
  const known = new Map(list.map((x) => [x.id, x]));
  return values.map((v) => known.get(v) || { id: v, label: catalogLabel([], v), css: [] });
}
