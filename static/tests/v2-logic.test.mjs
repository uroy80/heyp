// heyp v2 UI: pure logic of the new sections (ops JSON, canvas geometry, detections, schema fitting, URLs).
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  state, defaults, buildOps, blurOp, overlayItemJSON, overlayOp, watermarkOp, memeOp, filterOp, frameOp, upscaleOp,
  upscalePlan, quarterApplies, setRotateOnly, rotateQuarter, flip, preCropOps, describeEdits, hasHeavyOps, STAGES,
} from '../js/state.js';
import { FONTS, fontId, fontCss, filterCss, FILTERS, FRAMES, SHAPES, adoptEnums, zoneOf, positionOf } from '../js/catalog.js';
import {
  normalizeDetections, mergeRegions, loupeRegion, watermarkGeom, memeLayout, wrapWords, iou, MEME,
} from '../js/stagekit.js';
import {
  normAngle, moveItem, resizeItem, rotationTowards, boxFromPoints, toCornerBox, fromCornerBox, simplifyStroke, strokeBounds,
} from '../js/overlay.js';
import { changesFrame, stageBaseOps, snapWatermark, shiftStroke, EDIT_STAGE } from '../js/editing.js';
import { resolveProp, fitOptions, enumOf, rangeOf } from '../js/schema.js';
import { normalizeUrl, htmlOptions } from '../js/tools/html.js';
import { gifOptions } from '../js/tools/gif.js';
import { regionRows } from '../js/tools/blur.js';

const close = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) <= eps, `${a} ≉ ${b}`);
/** Deterministic text measurement: 0.5 em per character, 1 line per \n. */
const fakeMeasure = (text, font, px, { uppercase = false, lineHeight = 1.15 } = {}) => {
  const lines = String(text).split('\n').map((l) => (uppercase ? l.toUpperCase() : l));
  return { w: Math.max(...lines.map((l) => l.length * px * 0.5)), h: lines.length * px * lineHeight, lines };
};

function fresh() {
  state.files = [];
  state.activeId = null;
  state.features = { segmentation: true, face: true };
  state.limits = null;
  for (const k of ['edit', 'resize', 'background', 'passport', 'compress', 'output', 'upscale', 'watermark', 'meme', 'blur', 'editor', 'gif', 'html']) {
    state[k] = defaults[k]();
  }
}
const file = (id, w = 400, h = 300, extra = {}) => ({ id, name: `${id}.jpg`, info: { width: w, height: h, format: 'jpeg' }, crop: null, regions: [], items: [], ...extra });

// ── ops JSON matches the server's models ────────────────────────────────────

test('v2 ops come in the pipeline order and previews leave the heavy upscale out', () => {
  fresh();
  const f = file('a');
  f.regions = [{ id: 'r1', source: 'face', on: true, cx: 0.5, cy: 0.5, w: 0.2, h: 0.2, shape: 'ellipse' }];
  f.items = [{ id: 'o1', kind: 'text', text: 'hi', cx: 0.5, cy: 0.5, size: 0.1, font: 'anton', color: '#fff', rotation: 0, opacity: 1 }];
  state.edit.quarter = 90;
  state.editor.filter = 'vivid';
  state.editor.frame = 'line';
  state.upscale.factor = 2;
  state.meme.top = 'top';
  state.watermark.text = '© me';
  const ops = buildOps(f).map((o) => o.op);
  assert.deepEqual(ops, ['blur_regions', 'rotate', 'filter', 'upscale', 'frame', 'meme', 'overlay', 'watermark']);
  assert.ok(!buildOps(f, { preview: true }).some((o) => o.op === 'upscale'));
  assert.deepEqual(buildOps(f, { until: 'overlay', preview: true }).map((o) => o.op), ['blur_regions', 'rotate', 'filter', 'frame', 'meme']);
  assert.deepEqual(buildOps(f, { until: 'blur' }), []);
  assert.equal(hasHeavyOps([f]), true);
  assert.equal(STAGES[0], 'blur');
});

test('blur_regions: corner boxes clipped to the photo, the shape kept, unticked boxes left out', () => {
  fresh();
  const f = file('a');
  f.regions = [
    { id: 'r1', on: true, cx: 0.05, cy: 0.5, w: 0.2, h: 0.2, shape: 'ellipse' },
    { id: 'r2', on: false, cx: 0.5, cy: 0.5, w: 0.1, h: 0.1 },
    { id: 'r3', on: true, cx: 0.5, cy: 0.5, w: 0, h: 0.1 },
  ];
  state.blur.mode = 'solid';
  const op = blurOp(f);
  assert.equal(op.regions.length, 1);
  assert.deepEqual(op.regions[0], { x: 0, y: 0.4, w: 0.15, h: 0.2, shape: 'ellipse' });
  assert.equal(op.mode, 'solid');
  assert.equal(op.color, state.blur.color);
  f.regions = [];
  assert.equal(blurOp(f), null);
});

test('overlay items: sticker by id and width, shapes with stroke/fill, text font ids the server knows', () => {
  assert.deepEqual(overlayItemJSON({ kind: 'sticker', sticker: 'heyp/smiley', cx: 0.25, cy: 0.75, w: 0.2, h: 0.3, rotation: 12.34567, opacity: 0.5 }),
    { type: 'sticker', sticker: 'heyp/smiley', x: 0.25, y: 0.75, rotation: 12.346, opacity: 0.5, w: 0.2 });
  // the server's ShapeItem (app/imaging/ops_ext/edit.py): color = outline, fill null = none, no other keys
  const shape = overlayItemJSON({ kind: 'shape', shape: 'ellipse', cx: 0.5, cy: 0.5, w: 0.3, h: 0.2, color: '#e0262e', fill: null });
  assert.equal(shape.color, '#e0262e');
  assert.equal(shape.fill, null);
  assert.equal('stroke' in shape, false);
  assert.equal(overlayItemJSON({ kind: 'shape', shape: 'rect', cx: 0.5, cy: 0.5, w: 0.3, h: 0.2, color: '#000', fill: '#fff' }).fill, '#fff');
  assert.equal(shape.type, 'shape');
  const text = overlayItemJSON({ kind: 'text', text: 'x'.repeat(900), font: 'libre-franklin', cx: 0.5, cy: 0.5, size: 0.08, color: '#000' });
  assert.equal(text.font, 'libre_franklin');
  assert.equal(text.text.length, 500);
  const brush = overlayItemJSON({ kind: 'brush', points: [[0.1, 0.1], [0.2, 0.2]], color: '#000', width: 0.01 });
  assert.equal(brush.type, 'brush');
  assert.equal(overlayItemJSON({ kind: 'nope' }), null);
  assert.equal(overlayOp({ items: [{ kind: 'text', text: '   ' }, { kind: 'brush', points: [] }] }), null);
});

test('watermark: text / logo / custom position / mosaic', () => {
  fresh();
  assert.equal(watermarkOp(), null);
  state.watermark.text = '  © heyp  ';
  let op = watermarkOp();
  assert.equal(op.text, '© heyp');
  assert.equal(op.font, 'libre_franklin');
  assert.equal(op.position, 'bottom-right');
  assert.equal(op.x, undefined);
  Object.assign(state.watermark, { position: 'custom', x: 0.123456, y: 0.9 });
  op = watermarkOp();
  assert.equal(op.position, 'custom');
  assert.equal(op.x, 0.1235);
  state.watermark.mosaic = true;
  op = watermarkOp();
  assert.equal(op.position, 'mosaic');
  assert.equal(op.x, undefined);
  Object.assign(state.watermark, { kind: 'image', imageId: null });
  assert.equal(watermarkOp(), null, 'a logo watermark needs its image');
  state.watermark.imageId = 'f'.repeat(32);
  op = watermarkOp();
  assert.equal(op.image_id, 'f'.repeat(32));
  assert.equal(op.text, undefined);
});

test('meme, filter, frame, upscale ops', () => {
  fresh();
  assert.equal(memeOp(), null);
  Object.assign(state.meme, { top: ' hello ', outline: 'none' });
  assert.deepEqual(memeOp(), { op: 'meme', top: 'hello', bottom: '', font: 'anton', size: 'auto', color: '#ffffff', outline: 'transparent', uppercase: true });
  state.meme.autoSize = false;
  assert.equal(memeOp().size, 0.1);
  assert.equal(filterOp(), null);
  state.editor.filter = 'noir';
  state.editor.intensity = 0;
  assert.equal(filterOp(), null, 'zero intensity is no filter');
  state.editor.intensity = 0.5;
  assert.deepEqual(filterOp(), { op: 'filter', name: 'noir', intensity: 0.5 });
  state.editor.frame = 'film';
  assert.deepEqual(frameOp(), { op: 'frame', style: 'film' }, 'no colour or width chosen: the style’s own (a dark film strip)');
  Object.assign(state.editor, { frame: 'polaroid', frameColor: '#141210', frameWidth: 0.0625 });
  assert.deepEqual(frameOp(), { op: 'frame', style: 'polaroid', color: '#141210', width: 0.063 });
  state.upscale.factor = 4;
  assert.deepEqual(upscaleOp(), { op: 'upscale', factor: 4, model: 'balanced' });
  state.features.upscale = false;
  assert.equal(upscaleOp(), null, 'no model on the server, no op');
});

test('upscale plan respects the server pixel limit', () => {
  const p = upscalePlan({ w: 4000, h: 3000 }, 4, { max_pixels: 50_000_000 });
  assert.equal(p.w, 16000);
  assert.equal(p.ok, false);
  assert.equal(p.maxMp, 50);
  assert.equal(upscalePlan({ w: 1000, h: 1000 }, 2, { max_pixels: 50_000_000 }).ok, true);
  assert.equal(upscalePlan({ w: 1000, h: 1000 }, 0), null);
  const big = upscalePlan({ w: 4000, h: 2000 }, 2, { max_pixels: 50_000_000, upscale_max_input_pixels: 6_000_000 });
  assert.equal(big.ok, false, 'an 8 MP photo is over the 6 MP input limit even though 2× fits the output limit');
  assert.equal(big.inOk, false);
  assert.equal(big.maxInMp, 6);
  assert.equal(upscalePlan({ w: 2000, h: 1500 }, 2, { max_pixels: 50_000_000, upscale_max_input_pixels: 6_000_000 }).ok, true);
  assert.equal(upscalePlan(null, 2), null);
});

test('rotate batch filter: only landscape / portrait photos turn; crops of changed photos reset', () => {
  fresh();
  const land = file('l', 400, 300);
  const port = file('p', 300, 400);
  const square = file('s', 300, 300);
  state.files = [land, port, square];
  state.activeId = 'l';
  state.edit.only = 'landscape';
  assert.equal(quarterApplies(land), true);
  assert.equal(quarterApplies(port), false);
  assert.equal(quarterApplies(square), false, 'squares are neither');
  rotateQuarter(1);
  assert.deepEqual(preCropOps(land).map((o) => o.op), ['rotate']);
  assert.deepEqual(preCropOps(port), []);
  flip('h');
  assert.deepEqual(preCropOps(port).map((o) => o.direction), ['horizontal'], 'photos the turn skips keep the plain flip');
  assert.ok(describeEdits(land).some((e) => /landscape only/.test(e.label)));
  port.crop = { kind: 'box', x: 0.1, y: 0.1, w: 0.5, h: 0.5, ratio: 'free' };
  const reset = setRotateOnly('portrait');
  assert.equal(reset, 1);
  assert.equal(port.crop, null);
  assert.equal(setRotateOnly('<script>'), 0);
  assert.equal(state.edit.only, 'all', 'unknown filter values fall back to all');
});

// ── catalogue ───────────────────────────────────────────────────────────────

test('catalogue ids are the server ids; old hyphenated font ids still resolve', () => {
  assert.equal(fontId('libre-franklin'), 'libre_franklin');
  assert.equal(fontId('ibm-plex-mono'), 'plex_mono');
  assert.equal(fontId('nonsense'), FONTS[0].id);
  assert.ok(fontCss('anton').startsWith('Anton'));
  assert.ok(FILTERS.some((f) => f.id === 'teal_orange'));
  assert.ok(FRAMES.some((f) => f.id === 'mat'));
  assert.deepEqual(SHAPES.map((s) => s.id), ['rect', 'ellipse', 'line', 'arrow']);
  assert.equal(filterCss('none'), 'none');
  assert.equal(filterCss('mono', 0.5), 'grayscale(0.500)');
  assert.equal(filterCss('mono', 'junk'), 'grayscale(0.000)');
  const adopted = adoptEnums(FILTERS, ['none', 'vivid', 'brand_new']);
  assert.deepEqual(adopted.map((f) => f.id), ['none', 'vivid', 'brand_new']);
  assert.equal(adopted[2].label, 'Brand new');
  assert.equal(adoptEnums(FILTERS, null), FILTERS);
  assert.deepEqual(zoneOf('top-left'), { row: 'top', col: 'left' });
  assert.equal(positionOf({ row: 'middle', col: 'center' }), 'center');
});

// ── canvas geometry ─────────────────────────────────────────────────────────

test('overlay geometry: move clamps and snaps, resize keeps the opposite corner, rotation snaps to 15°', () => {
  const it = { cx: 0.5, cy: 0.5, w: 0.2, h: 0.1, rotation: 0 };
  assert.deepEqual(moveItem(it, 0.7, -0.9).item, { ...it, cx: 1, cy: 0 });
  const snapped = moveItem(it, 0.004, 0.2, { snap: { x: 0.01, y: 0.01 } });
  assert.equal(snapped.item.cx, 0.5);
  assert.equal(snapped.snapped.x, true);
  // dragging the SE handle by (+20, +10) px on 200×100: NW corner stays at (80, 45)
  const r = resizeItem(it, 'se', 20, 10, { W: 200, H: 100 });
  close((r.item.cx - r.item.w / 2) * 200, 80);
  close((r.item.cy - r.item.h / 2) * 100, 45);
  close(r.scale, 60 / 40);
  const ratio = resizeItem(it, 'e', 40, 0, { W: 200, H: 100, keepRatio: true });
  close(ratio.item.w / ratio.item.h, it.w / it.h);
  const tiny = resizeItem(it, 'se', -500, -500, { W: 200, H: 100 });
  assert.ok(tiny.item.w * 200 >= 8 && tiny.item.h * 100 >= 8, 'never smaller than a grab-able box');
  assert.equal(rotationTowards(it, 100, 0, { W: 200, H: 100 }), 0);
  assert.equal(rotationTowards(it, 200, 50, { W: 200, H: 100 }), 90);
  assert.equal(rotationTowards(it, 190, 12, { W: 200, H: 100, snap: true }) % 15, 0);
  assert.equal(normAngle(540), 180);
  assert.equal(normAngle(-190), 170);
  assert.equal(normAngle('x'), 0);
});

test('boxes and strokes: drawn boxes clip to the photo; strokes thin out and move as a whole', () => {
  const b = boxFromPoints(-50, 20, 120, 80, { W: 100, H: 100 });
  assert.deepEqual(b, { cx: 0.5, cy: 0.5, w: 1, h: 0.6 });
  const corner = toCornerBox({ cx: 0.95, cy: 0.5, w: 0.2, h: 0.2 });
  assert.deepEqual(corner, { x: 0.85, y: 0.4, w: 0.15, h: 0.2 });
  assert.deepEqual(fromCornerBox({ x: 0.1, y: 0.2, w: 0.3, h: 0.4 }), { cx: 0.25, cy: 0.4, w: 0.3, h: 0.4 });
  const pts = [[0, 0], [0.001, 0.001], [0.002, 0.002], [0.5, 0.5]];
  const s = simplifyStroke(pts, { W: 100, H: 100 });
  assert.deepEqual(s, [[0, 0], [0.5, 0.5]]);
  const bounds = strokeBounds([[0.1, 0.2], [0.3, 0.6]], 0.02, { W: 200, H: 100 });
  close(bounds.w, 0.22);
  close(bounds.h, 0.44);
  assert.deepEqual(shiftStroke([[0.1, 0.1], [0.95, 0.5]], 0.1, -0.2), [[0.2, 0], [1, 0.3]]);
});

// ── detections → blur boxes ─────────────────────────────────────────────────

test('detections: heyp /api/detect boxes are used as given (already grown), selection kept', () => {
  const res = {
    file_id: 'a'.repeat(32), width: 1000, height: 500, level: 'recommended',
    regions: [
      { kind: 'face', x: 0.1, y: 0.2, w: 0.2, h: 0.4, shape: 'ellipse', score: 0.97, selected: true },
      { kind: 'plate', x: 0.6, y: 0.7, w: 0.2, h: 0.1, shape: 'rect', score: 0.41, selected: false },
      { kind: 'face', x: 'nan', y: 0, w: 0.1, h: 0.1 },
      { kind: 'face', x: 0.1, y: 0.1, w: -1, h: 0.1 },
      null,
    ],
  };
  const out = normalizeDetections(res, { w: 1000, h: 500 });
  assert.equal(out.length, 2);
  assert.deepEqual(out[0], { source: 'face', cx: 0.2, cy: 0.4, w: 0.2, h: 0.4, score: 0.97, on: true, shape: 'ellipse' });
  assert.equal(out[1].source, 'plate');
  assert.equal(out[1].on, false);
});

test('detections: other shapes (pixel boxes, x1y1x2y2, bare lists) are normalised and padded', () => {
  const px = normalizeDetections({ faces: [{ box: [100, 100, 100, 100], score: 0.9 }] }, { w: 1000, h: 1000 });
  close(px[0].w, 0.1 * 1.24, 1e-5);
  const corners = normalizeDetections([{ x1: 0, y1: 0, x2: 50, y2: 25, label: 'plate' }], { w: 100, h: 100 });
  assert.equal(corners[0].source, 'plate');
  assert.equal(corners[0].cx > 0, true);
  assert.deepEqual(normalizeDetections({ faces: [{ box: [10, 10, 10, 10] }] }, null), [], 'pixel boxes need the image size');
  assert.deepEqual(normalizeDetections('garbage'), []);
});

test('merging detections skips boxes already there and caps the count', () => {
  let n = 0;
  const id = () => `r${++n}`;
  const a = { cx: 0.5, cy: 0.5, w: 0.2, h: 0.2 };
  close(iou(a, a), 1);
  assert.equal(iou(a, { cx: 0.9, cy: 0.9, w: 0.1, h: 0.1 }), 0);
  const { list, added } = mergeRegions([{ id: 'x', ...a }], [{ ...a, cx: 0.51 }, { cx: 0.1, cy: 0.1, w: 0.1, h: 0.1 }], id);
  assert.equal(added, 1);
  assert.equal(list.length, 2);
  const many = Array.from({ length: 100 }, (_, i) => ({ cx: (i % 10) / 10 + 0.05, cy: Math.floor(i / 10) / 10 + 0.05, w: 0.05, h: 0.05 }));
  const capped = mergeRegions([], many, id, 64);
  assert.equal(capped.list.length, 64);
  assert.equal(capped.added, 64);
});

test('blur list rows name and number each kind', () => {
  const rows = regionRows([{ id: 'a', source: 'face', score: 0.973 }, { id: 'b', source: 'face', on: false }, { id: 'c', source: 'manual' }, { id: 'd', source: 'plate', score: 0.5 }]);
  assert.deepEqual(rows.map((r) => r.label), ['Face 1 · 97 %', 'Face 2', 'Area 1', 'Plate 1 · 50 %']);
  assert.equal(rows[1].on, false);
});

// ── watermark / meme / loupe placement ──────────────────────────────────────

test('watermark placement mirrors the server: size share of the height, margin of the shorter side', () => {
  const wm = { ...defaults.watermark(), text: 'abcd', size: 0.1, margin: 0.05 };
  const g = watermarkGeom(wm, 1000, 500, { measure: fakeMeasure });
  // px = 0.1 × 500 = 50; text w = 4 × 25 = 100 (+ 0.2 px padding = 110); margin 25
  close(g.px, 50);
  close(g.cx * 1000, 1000 - 25 - 55);
  close(g.cy * 500, 500 - 25 - (50 * 1.15) / 2);
  const logo = watermarkGeom({ ...wm, kind: 'image', imageScale: 0.2, position: 'top-left' }, 1000, 500, { logoAspect: 2 });
  close(logo.w, 0.2);
  close(logo.h * 500, 100);
  close(logo.cx * 1000, 25 + 100);
});

test('watermark text on a portrait photo is sized from the shorter side at weight 600, like the server (img-10)', () => {
  const wm = { ...defaults.watermark(), text: 'abcd', size: 0.1, margin: 0.05, position: 'bottom-right' };
  const seen = [];
  const spy = (text, font, px, opts = {}) => { seen.push(opts.weight); return fakeMeasure(text, font, px, opts); };
  const portrait = watermarkGeom(wm, 900, 1600, { measure: spy }); // 9:16 phone screenshot
  close(portrait.px, 90); // 0.1 × min(900, 1600), not 0.1 × 1600
  assert.deepEqual(seen, [600]);
  const landscape = watermarkGeom(wm, 1600, 900, { measure: fakeMeasure });
  close(landscape.px, portrait.px); // same mark on both orientations of the same photo
  close(portrait.w * 900, landscape.w * 1600);
});

test('dragging the watermark snaps to a nine-grid position nearby, else places it by hand', () => {
  const geomAt = (pos) => ({ 'bottom-right': { cx: 0.9, cy: 0.9 }, center: { cx: 0.5, cy: 0.5 } }[pos] || { cx: -1, cy: -1 });
  assert.deepEqual(snapWatermark(0.905, 0.895, geomAt), { position: 'bottom-right' });
  assert.deepEqual(snapWatermark(0.7, 0.3, geomAt), { position: 'custom', x: 0.7, y: 0.3 });
  assert.deepEqual(snapWatermark(1.4, -2, geomAt), { position: 'custom', x: 1, y: 0 });
});

test('meme captions: auto size fits the width, long captions wrap to at most three lines inside the band', () => {
  const m = { ...defaults.meme(), top: 'short', bottom: '' };
  const lay = memeLayout(m, 1000, 1000, fakeMeasure);
  assert.equal(lay.bottom, undefined);
  close(lay.top.px, 140, 1e-6); // capped at 14 % of the height
  assert.deepEqual(lay.top.lines, ['SHORT']);
  const long = memeLayout({ ...m, top: 'one does not simply walk into mordor without a very long caption on top' }, 1000, 1000, fakeMeasure);
  assert.ok(long.top.lines.length > 1 && long.top.lines.length <= MEME.maxLines);
  assert.ok(long.top.h <= MEME.maxShare + 1e-9);
  assert.ok(long.top.cy - long.top.h / 2 >= MEME.edge - 1e-9);
  assert.deepEqual(wrapWords('a bb ccc', 'anton', 10, 25, fakeMeasure), ['a bb', 'ccc']);
  assert.deepEqual(wrapWords('line\nbreak', 'anton', 10, 1000, fakeMeasure, { uppercase: true }), ['LINE', 'BREAK']);
});

test('meme captions never run wider than the photo or taller than 30 % of it (img-03)', () => {
  const m = { ...defaults.meme(), bottom: '' };
  const long = 'when you finally fix the bug at three in the morning and then realise the test was wrong all along';
  for (const [W, H] of [[600, 1000], [300, 300], [1080, 1920], [100, 2000]]) {
    for (const top of [long, `${long} ${long}`, 'W'.repeat(11)]) {
      const lay = memeLayout({ ...m, top }, W, H, fakeMeasure).top;
      const widest = Math.max(...lay.lines.map((l) => fakeMeasure(l, 'anton', lay.px).w)) * (1 + 2 * MEME.stroke);
      assert.ok(widest <= MEME.width * W + 1e-6, `${W}×${H} ${top.length}: ${widest} > ${MEME.width * W}`);
      assert.ok(lay.lines.length * lay.px * MEME.lineSpacing <= MEME.maxShare * H + 1e-6, `${W}×${H} ${top.length}`);
    }
  }
  const four = memeLayout({ ...m, top: 'ONE\nTWO\nTHREE\nFOUR' }, 800, 600, fakeMeasure).top;
  assert.deepEqual(four.lines, ['ONE', 'TWO', 'THREE', 'FOUR']);
  assert.ok(four.lines.length * four.px * MEME.lineSpacing <= MEME.maxShare * 600 + 1e-6);
});

test('upscale detail square: 18 % of the short side, 48–200 px, always inside the photo', () => {
  const r = loupeRegion({ cx: 1, cy: 1 }, 4000, 3000);
  assert.equal(r.side, 200);
  close(r.x + r.w, 1);
  close(r.y + r.h, 1);
  const small = loupeRegion(null, 100, 60);
  assert.equal(small.side, 48);
  assert.ok(small.x >= 0 && small.y >= 0 && small.x + small.w <= 1 + 1e-9);
});

// ── which picture the layer draws on ────────────────────────────────────────

test('stage base: only frame-changing edits after a section force its input image', () => {
  assert.equal(changesFrame({ op: 'rotate', angle: 90 }), true);
  assert.equal(changesFrame({ op: 'crop_aspect', ratio: 1 }), true);
  assert.equal(changesFrame({ op: 'resize', mode: 'fit', width: 100 }), false);
  assert.equal(changesFrame({ op: 'resize', mode: 'pad', width: 100, height: 100 }), true);
  assert.equal(changesFrame({ op: 'watermark' }), false);
  assert.equal(changesFrame(null), false);
  const before = [];
  assert.equal(stageBaseOps(null, 'blur', { ops: [{ op: 'blur_regions' }, { op: 'adjust' }], before }), null);
  assert.deepEqual(stageBaseOps(null, 'blur', { ops: [{ op: 'blur_regions' }, { op: 'rotate' }], before }), []);
  const pre = [{ op: 'rotate' }];
  assert.equal(stageBaseOps(null, 'watermark', { ops: [...pre, { op: 'watermark' }], before: pre }), null, 'edits before the stage are part of its input');
  assert.deepEqual(Object.keys(EDIT_STAGE).sort(), ['blur', 'editor', 'meme', 'upscale', 'watermark']);
});

// ── registry options fitted to the server schema ────────────────────────────

const GIF_SCHEMA = {
  properties: {
    delay_ms: { type: 'integer', minimum: 20, maximum: 60000, default: 500 },
    loop: { type: 'integer', minimum: 0, default: 0 },
    size: { anyOf: [{ type: 'integer', enum: [0, 1080, 720, 480] }, { type: 'null' }], default: 0 },
    format: { type: 'string', enum: ['gif', 'webp'] },
  },
};

test('schema fitting: first known name wins, types coerced, ranges clamped, enums kept valid', () => {
  assert.deepEqual(resolveProp(GIF_SCHEMA, GIF_SCHEMA.properties.size).enum, [0, 1080, 720, 480]);
  const out = fitOptions(GIF_SCHEMA, [
    { names: ['delay', 'delay_ms'], value: '5' },
    { names: ['size'], value: 700 },
    { names: ['format'], value: 'WEBP' },
    { names: ['missing'], value: 1 },
  ]);
  assert.deepEqual(out, { delay_ms: 20, size: 720, format: 'webp' });
  assert.deepEqual(fitOptions(null, [{ names: ['a', 'b'], value: 1 }, { names: ['c'], value: undefined }]), { a: 1 });
  const secs = fitOptions({ properties: { delay_s: { type: 'number' } } }, [{ names: ['delay_s'], value: 1500, unit: 'ms' }]);
  assert.deepEqual(secs, { delay_s: 1.5 });
  assert.deepEqual(enumOf(GIF_SCHEMA, ['nope', 'format']), ['gif', 'webp']);
  assert.equal(enumOf(GIF_SCHEMA, ['delay_ms']), null);
  assert.deepEqual(rangeOf(GIF_SCHEMA, ['delay_ms']), { name: 'delay_ms', min: 20, max: 60000, default: 500 });
  const withRef = { $defs: { Fmt: { type: 'string', enum: ['png'] } }, properties: { format: { $ref: '#/$defs/Fmt' } } };
  assert.deepEqual(fitOptions(withRef, [{ names: ['format'], value: 'gif' }]), { format: 'png' });
});

test('Make GIF options: delay clamped, loop as count or flag, frame order', () => {
  const g = { ...defaults.gif(), delay: 5, loop: false, size: 480, format: 'webp' };
  const o = gifOptions(GIF_SCHEMA, g, [{}, {}, {}]);
  assert.equal(o.delay_ms, 20);
  assert.equal(o.loop, 1, 'integer loop: 1 = play once');
  assert.equal(o.size, 480);
  const bare = gifOptions(null, { ...g, loop: true }, [{}, {}]);
  assert.equal(bare.loop, true);
  assert.deepEqual(bare.order, [0, 1]);
});

// ── HTML → image: the address field ─────────────────────────────────────────

test('web page addresses: https added, only public http(s) pages', () => {
  assert.deepEqual(normalizeUrl('example.com/a?b=1'), { ok: true, url: 'https://example.com/a?b=1' });
  assert.equal(normalizeUrl('http://example.com').url, 'http://example.com/');
  for (const bad of ['', '   ', 'javascript:alert(1)', 'file:///etc/passwd', 'ftp://example.com', 'http://localhost:8080', 'http://127.0.0.1/',
    'http://10.0.0.1', 'http://192.168.1.1', 'http://172.16.0.1', 'http://169.254.169.254/latest/meta-data', 'http://[::1]/', 'http://[fd00::1]/',
    'http://user:pw@example.com', 'http://intranet', 'http://printer.local', `https://example.com/${'a'.repeat(2100)}`, 'http://100.64.0.1']) {
    assert.equal(normalizeUrl(bad).ok, false, bad);
    assert.ok(normalizeUrl(bad).error);
  }
  const o = htmlOptions({ properties: { url: { type: 'string' }, width: { type: 'integer', enum: [1920, 1440, 390] }, full_page: { type: 'boolean' }, format: { type: 'string', enum: ['jpg', 'png'] }, delay_ms: { type: 'integer', minimum: 0, maximum: 10000 } } },
    { ...defaults.html(), width: 1000, delay: 2.5 }, 'https://example.com/');
  assert.deepEqual(o, { url: 'https://example.com/', width: 1440, full_page: true, format: 'jpg', delay_ms: 2500 });
});
