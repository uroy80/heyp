// Regression tests for the UI state/op builders (run: node --test static/tests/).
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  state, defaults, cropRatioFor, cropPx, rotateQuarter, exportRequest, qualityApplies, setBackgroundMode,
  resetBackground, planUploads, exportLimitError, warningSummary, buildOps, describeEdits, ratioValue, CROP_RATIOS,
} from '../js/state.js';
import { fitRatio, snapBox } from '../js/cropbox.js';
import cropTool from '../js/tools/crop.js';
import resizeTool from '../js/tools/resize.js';
import convertTool from '../js/tools/convert.js';

const file = (id, extra = {}) => ({
  id, name: `${id}.jpg`, crop: null, info: { width: 1001, height: 667, format: 'jpeg', has_alpha: false }, ...extra,
});

function fresh() {
  Object.assign(state, {
    tool: 'convert', files: [], activeId: null, presets: [{ id: 'us', name: 'United States — passport (2×2 in)', country: 'US', width_mm: 50.8, height_mm: 50.8, dpi: 300, width_px: 600, height_px: 600 }],
    edit: defaults.edit(), resize: defaults.resize(), background: defaults.background(), passport: defaults.passport(),
    compress: defaults.compress(), output: defaults.output(), cropRatio: 'free', features: { heic: true, avif: true, face: true, segmentation: true },
    outputFormats: ['jpeg', 'png', 'webp', 'avif', 'heic', 'tiff', 'bmp', 'gif', 'pdf'],
  });
}

// Python's round() as the server's crop op uses it (ties to even).
const pyRound = (v) => {
  const f = Math.floor(v);
  const d = v - f;
  if (Math.abs(d - 0.5) < 1e-9) return f % 2 === 0 ? f : f + 1;
  return Math.round(v);
};
const round6 = (v) => Math.round(v * 1e6) / 1e6;
const serverCrop = (b, W, H) => {
  const l = pyRound(b.x * W);
  const t = pyRound(b.y * H);
  return { w: Math.max(pyRound((b.x + b.w) * W), l + 1) - l, h: Math.max(pyRound((b.y + b.h) * H), t + 1) - t };
};

test('UI-3: the ratio lock comes from the photo itself, never from the previous photo', () => {
  fresh();
  assert.equal(cropRatioFor(file('a')), 'free');
  assert.equal(cropRatioFor(file('a', { crop: { kind: 'box', x: 0, y: 0, w: 0.5, h: 0.5, ratio: '16:9' } })), '16:9');
  assert.equal(cropRatioFor(file('a', { crop: { kind: 'trim', tolerance: 12, padding: 0 } })), 'free');
  assert.equal(cropRatioFor(file('a', { crop: { kind: 'aspect', ratio: 1.4142, anchor: 'smart' } })), 'free');
  assert.equal(cropRatioFor(file('a', { crop: { kind: 'box', x: 0, y: 0, w: 1, h: 0.6, ratio: '5:4' } })), '5:4');
  assert.equal(cropRatioFor(null), 'free');
});

test('UI-4: rotating swaps any a:b lock together with the box (4:5 and 35:45 included)', () => {
  for (const [r, swapped] of [['4:5', '5:4'], ['35:45', '45:35'], ['16:9', '9:16']]) {
    fresh();
    const f = file('a', { crop: { kind: 'box', x: 0.2, y: 0, w: 0.6, h: 1, ratio: r } });
    state.files = [f];
    state.cropRatio = r;
    rotateQuarter(1);
    assert.equal(state.cropRatio, swapped);
    assert.equal(f.crop.ratio, swapped);
    rotateQuarter(-1);
    assert.equal(state.cropRatio, r);
    assert.equal(f.crop.ratio, r);
  }
  fresh();
  state.cropRatio = 'original';
  rotateQuarter(1);
  assert.equal(state.cropRatio, 'original');
});

test('UI-10: snapped crop boxes give the exact size the server crops, for every chip ratio', () => {
  for (const [W, H] of [[1001, 667], [667, 1001], [400, 400], [4032, 3024]]) {
    for (const r of CROP_RATIOS.filter((x) => x !== 'free')) {
      const px = snapBox(fitRatio(ratioValue(r, W / H), W, H), W, H);
      const frac = { x: round6(px.x / W), y: round6(px.y / H), w: round6(px.w / W), h: round6(px.h / H) };
      const label = cropPx(frac, W, H);
      assert.deepEqual(serverCrop(frac, W, H), { w: label.w, h: label.h }, `${r} on ${W}×${H}`);
      assert.deepEqual({ w: label.w, h: label.h }, { w: px.w, h: px.h });
    }
  }
  // the old label (Math.round(w·W)) disagreed with the server for 4:5 on 1001×667
  const raw = fitRatio(4 / 5, 1001, 667);
  const frac = { x: raw.x / 1001, y: 0, w: raw.w / 1001, h: 1 };
  assert.notEqual(Math.round(frac.w * 1001), serverCrop(frac, 1001, 667).w);
  assert.equal(cropPx(frac, 1001, 667).w, serverCrop(frac, 1001, 667).w);
});

test('UI-10: the crop chip label uses server rounding', () => {
  fresh();
  const raw = fitRatio(4 / 5, 1001, 667);
  const f = file('a', { crop: { kind: 'box', x: raw.x / 1001, y: 0, w: raw.w / 1001, h: 1, ratio: '4:5' } });
  state.files = [f];
  state.activeId = 'a';
  const label = describeEdits(f).find((e) => e.key === 'crop').label;
  assert.equal(label, `Crop ${serverCrop(f.crop, 1001, 667).w}×667`);
});

test('C1 / UI-1 / UI-15: a batch with per-file crops is one request; only differing files get ops_by_id', () => {
  fresh();
  const a = file('a');
  const b = file('b', { crop: { kind: 'box', x: 0.1, y: 0.1, w: 0.5, h: 0.5, ratio: 'free' } });
  const c = file('c');
  state.files = [a, b, c];
  const req = exportRequest(state.files);
  assert.deepEqual(req.ids, ['a', 'b', 'c']);
  assert.deepEqual(req.ops, []);
  assert.deepEqual(Object.keys(req.ops_by_id), ['b']);
  assert.equal(req.ops_by_id.b[0].op, 'crop');
  assert.equal(req.output.filename, null);

  const uniform = exportRequest([a, c]);
  assert.equal(uniform.ops_by_id, undefined);

  state.output.filename = 'mine';
  assert.equal(exportRequest([a]).output.filename, 'mine');
});

test('C1: the shared ops are the ones most files use', () => {
  fresh();
  const crop = { kind: 'box', x: 0, y: 0, w: 0.5, h: 0.5, ratio: 'free' };
  state.files = [file('a'), file('b', { crop: { ...crop } }), file('c', { crop: { ...crop } })];
  const req = exportRequest(state.files);
  assert.equal(req.ops[0].op, 'crop');
  assert.deepEqual(Object.keys(req.ops_by_id), ['a']);
  assert.deepEqual(req.ops_by_id.a, []);
});

test('UI-5: quality applies to "Same as original" when a source is lossy', () => {
  fresh();
  state.output.format = 'original';
  state.files = [file('a')];
  assert.equal(qualityApplies(), true);
  state.files = [file('p', { info: { width: 10, height: 10, format: 'png', has_alpha: false } })];
  assert.equal(qualityApplies(), false);
  state.files = [file('i', { info: { width: 10, height: 10, format: 'ico', has_alpha: false } })];
  assert.equal(qualityApplies(), true);
  state.output.format = 'webp';
  state.output.lossless = true;
  assert.equal(qualityApplies(), false);
  state.output.format = 'png';
  assert.equal(qualityApplies(), false);
});

test('UI-13: leaving Remove restores the format it replaced, unless the user changed it', () => {
  fresh();
  assert.match(setBackgroundMode('remove'), /JPG to PNG/);
  assert.equal(state.output.format, 'png');
  assert.match(setBackgroundMode('off'), /back to JPG/);
  assert.equal(state.output.format, 'jpeg');

  setBackgroundMode('remove');
  state.output.format = 'webp'; // user's own choice
  assert.equal(setBackgroundMode('replace'), null);
  assert.equal(state.output.format, 'webp');

  state.output.format = 'jpeg';
  setBackgroundMode('remove');
  assert.match(resetBackground(), /back to JPG/);
  assert.equal(state.output.format, 'jpeg');
  assert.equal(state.background.mode, 'off');

  state.output.format = 'png';
  assert.equal(setBackgroundMode('remove'), null); // already keeps alpha: nothing to restore later
  assert.equal(setBackgroundMode('off'), null);
  assert.equal(state.output.format, 'png');
});

test('C7: uploads are pre-validated against /api/health limits', () => {
  const MB = 1024 * 1024;
  const f = (name, mb) => ({ name, size: mb * MB });
  const { chunks, rejected } = planUploads([f('a', 1), f('big', 150), f('b', 2), f('c', 3)], { max_upload_mb: 100, max_files: 2 });
  assert.deepEqual(rejected.map((r) => r.name), ['big']);
  assert.match(rejected[0].detail, /100 MB/);
  assert.deepEqual(chunks.map((c) => c.map((x) => x.name)), [['a', 'b'], ['c']]);
  const none = planUploads([f('a', 500)], null);
  assert.equal(none.rejected.length, 0);
  assert.equal(none.chunks.length, 1);
});

test('C7: an export with more photos than the server takes per request is refused before sending', () => {
  assert.equal(exportLimitError(200, { max_files: 200 }), null);
  assert.match(exportLimitError(203, { max_files: 200 }), /max 200.*remove 3/);
  assert.equal(exportLimitError(5000, null), null); // limits unknown: let the server decide
});

test('UI-15: export warnings are summarised in one toast line', () => {
  assert.equal(warningSummary(['a.jpg: above 50 KB']), 'a.jpg: above 50 KB');
  assert.equal(warningSummary(['x', 'x', 'y']), 'x · y');
  assert.equal(warningSummary(['a', 'b', 'c', 'd']), '4 warnings — a · b …');
});

test('C4: the passport op carries the Level head setting', () => {
  fresh();
  state.passport.on = true;
  state.passport.preset = 'us';
  const op = () => buildOps(file('a')).find((o) => o.op === 'passport');
  assert.equal(op().level, true);
  state.passport.level = false;
  assert.equal(op().level, false);
});

test('UI-12: Crop and Resize are not marked as edited while Passport overrides them', () => {
  fresh();
  const f = file('a', { crop: { kind: 'box', x: 0, y: 0, w: 0.5, h: 0.5, ratio: 'free' } });
  state.files = [f];
  state.activeId = 'a';
  state.resize.on = true;
  assert.equal(cropTool.isActive(), true);
  assert.equal(resizeTool.isActive(), true);
  state.passport.on = true;
  state.passport.preset = 'us';
  assert.equal(cropTool.isActive(), false);
  assert.equal(resizeTool.isActive(), false);
});

test('SPEC §10.1: Convert counts format/encoder changes, not Export quality or metadata', () => {
  fresh();
  state.output.quality = 70;
  state.output.stripMetadata = false;
  assert.equal(convertTool.isActive(), false);
  state.output.format = 'png';
  assert.equal(convertTool.isActive(), true);
  convertTool.reset();
  assert.equal(state.output.format, 'jpeg');
  assert.equal(state.output.quality, 70); // Export's own settings survive a Convert reset
});

test('QA: edit chips name swatch colours instead of printing hex codes', () => {
  fresh();
  const f = file('a');
  state.files = [f];
  state.activeId = 'a';
  setBackgroundMode('replace');
  state.background.color = '#dbe9f7';
  assert.equal(describeEdits(f).find((e) => e.key === 'background').label, 'Light blue background');
  state.background.color = '#123456';
  assert.equal(describeEdits(f).find((e) => e.key === 'background').label, 'Background #123456');
});
