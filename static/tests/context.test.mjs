// Passport context view geometry (contract v1.2): the pre-passport photo laid out under a fixed frame.
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  contextPlacement, contextFrame, zoomPivot, zoomShift, movedGuides, isSourceFrame, guideModel, CONTEXT_FILL,
} from '../js/canvas.js';
import { state, defaults, prePassportOps, framingOf, buildOps } from '../js/state.js';

/** CSS matrix(a, b, c, d, e, f) with transform-origin 0 0 applied to element point (x, y). */
const apply = ([a, b, c, d, e, f], x, y) => [a * x + c * y + e, b * x + d * y + f];

/** Stage point where source pixel (sx, sy) is drawn. */
const shown = (pl, sx, sy) => apply(pl.matrix, sx * pl.scale, sy * pl.scale);

/**
 * The contract's mapping: output pixel (u, v) of an out_w × out_h passport comes from source point
 * center + R(angle)·((u − out_w/2)·fw/out_w, (v − out_h/2)·fh/out_h), R(θ) = [[cos, −sin], [sin, cos]].
 */
function sourceOf(src, outW, outH, u, v) {
  const t = (src.angle * Math.PI) / 180;
  const x = ((u - outW / 2) * src.size[0]) / outW;
  const y = ((v - outH / 2) * src.size[1]) / outH;
  return [src.center[0] + Math.cos(t) * x - Math.sin(t) * y, src.center[1] + Math.sin(t) * x + Math.cos(t) * y];
}

const close = (got, want, tol, msg) => {
  assert.ok(Math.abs(got[0] - want[0]) <= tol && Math.abs(got[1] - want[1]) <= tol,
    `${msg}: got (${got[0].toFixed(3)}, ${got[1].toFixed(3)}), want (${want[0].toFixed(3)}, ${want[1].toFixed(3)})`);
};

/** Every output pixel that matters lands on the matching point of the frame rectangle. */
function assertFrameLands(src, outW, outH, frame, pl, tol = 1e-6) {
  const pts = [[0, 0], [outW, 0], [0, outH], [outW, outH], [outW / 2, outH / 2], [outW * 0.3, outH * 0.8], [17, 5]];
  for (const [u, v] of pts) {
    const [sx, sy] = sourceOf(src, outW, outH, u, v);
    const want = [frame.left + (u / outW) * frame.width, frame.top + (v / outH) * frame.height];
    close(shown(pl, sx, sy), want, tol, `output (${u}, ${v})`);
  }
}

test('context: angle 0 — the frame region of the photo lands exactly on the frame rectangle', () => {
  const src = { width: 4000, height: 3000, center: [2100, 1250], size: [1000, 1000], angle: 0 };
  const frame = { left: 140, top: 60, width: 300, height: 300 };
  const pl = contextPlacement(src, frame);
  assert.equal(pl.scale, 0.3);
  assert.equal(pl.width, 1200);
  assert.equal(pl.height, 900);
  assertFrameLands(src, 600, 600, frame, pl);
  // unrotated: a pure scale + translate, and the CSS string says the same
  assert.deepEqual(pl.matrix.slice(0, 4).map((v) => v + 0), [1, 0, 0, 1]);
  assert.equal(pl.transform, "matrix(1, 0, 0, 1, -340, -165)");
  // the photo's corner sits up-left of the frame, the whole photo around it
  close(shown(pl, 0, 0), [frame.left + 150 - 2100 * 0.3, frame.top + 150 - 1250 * 0.3], 1e-9, 'photo origin');
});

test('context: a levelled (rotated) frame — the photo is counter-rotated under the upright frame', () => {
  // India-style 630×810 output, head tilted 7.5° → the frame is rotated 7.5° clockwise on the photo
  const src = { width: 3024, height: 4032, center: [1480.25, 1702.5], size: [700, 900], angle: 7.5 };
  const frame = { left: 97, top: 31, width: 210, height: 270 };
  const pl = contextPlacement(src, frame);
  assertFrameLands(src, 630, 810, frame, pl);
  // counter-clockwise on screen: rotate(−7.5deg) → b = sin(−7.5°) < 0
  assert.ok(pl.matrix[1] < 0 && pl.matrix[2] > 0);
  const t = (-7.5 * Math.PI) / 180;
  assert.ok(Math.abs(pl.matrix[0] - Math.cos(t)) < 1e-12 && Math.abs(pl.matrix[1] - Math.sin(t)) < 1e-12);
  // the CSS string carries the same numbers (rounded for the style attribute)
  const nums = pl.transform.match(/-?[\d.]+(e-?\d+)?/g).map(Number);
  pl.matrix.forEach((v, i) => assert.ok(Math.abs(nums[i] - v) < 1e-3, `matrix[${i}]`));
});

test('context: a pending drag moves the photo like the re-framed preview will (lands within 1 px)', () => {
  const src = { width: 3024, height: 4032, center: [1480, 1700], size: [700, 900], angle: -6 };
  const frame = { left: 100, top: 40, width: 210, height: 270 };
  // offset_x += 0.1, offset_y −= 0.05 (subject right / up): the server moves the frame the other way on the photo
  const ox = 0.1;
  const oy = -0.05;
  const live = contextPlacement(src, frame, { dx: ox * frame.width, dy: oy * frame.height });
  const t = (src.angle * Math.PI) / 180;
  const ddx = -ox * src.size[0];
  const ddy = -oy * src.size[1];
  const next = { ...src, center: [src.center[0] + Math.cos(t) * ddx - Math.sin(t) * ddy, src.center[1] + Math.sin(t) * ddx + Math.cos(t) * ddy] };
  const relaid = contextPlacement(next, frame);
  for (const [sx, sy] of [[0, 0], [3024, 4032], [1500, 1600], [800, 2900]]) {
    close(shown(live, sx, sy), shown(relaid, sx, sy), 1e-6, `photo point (${sx}, ${sy})`);
  }
  // and the dragged-to point is where the subject now sits in the new frame
  assertFrameLands(next, 630, 810, frame, live);
});

test('context: zoom feedback scales about the pivot exactly like the re-laid-out preview', () => {
  const src = { width: 4000, height: 3000, center: [2000, 1400], size: [1200, 1200], angle: 4 };
  const frame = { left: 120, top: 50, width: 300, height: 300 };
  const out = 600;
  // US-style eye band: the eye point (output 300, 260) stays put while the head grows by r
  const pivot = { u: 300, v: 260 };
  const r = 1.1;
  const live = contextPlacement(src, frame, {
    r, px: frame.left + (pivot.u / out) * frame.width, py: frame.top + (pivot.v / out) * frame.height,
  });
  // server: frame shrinks by r on the photo, about the eye point E
  const E = sourceOf(src, out, out, pivot.u, pivot.v);
  const next = {
    ...src,
    size: [src.size[0] / r, src.size[1] / r],
    center: [E[0] + (src.center[0] - E[0]) / r, E[1] + (src.center[1] - E[1]) / r],
  };
  const relaid = contextPlacement(next, frame);
  for (const [sx, sy] of [[0, 0], [4000, 3000], [2000, 1400], [E[0], E[1]], [2600, 900]]) {
    close(shown(live, sx, sy), shown(relaid, sx, sy), 1e-6, `photo point (${sx}, ${sy})`);
  }
  // the eye point did not move, everything else spread out by r
  const before = contextPlacement(src, frame);
  close(shown(live, ...E), shown(before, ...E), 1e-9, 'pivot');
  const a = shown(before, 0, 0);
  const b = shown(live, 0, 0);
  const p = shown(before, ...E);
  close([b[0] - p[0], b[1] - p[1]], [r * (a[0] - p[0]), r * (a[1] - p[1])], 1e-6, 'scaled about the pivot');
  // zoom + drag together (any order gives the same picture)
  const both = contextPlacement(src, frame, { r, dx: 12, dy: -7, px: p[0], py: p[1] });
  close(shown(both, 0, 0), [b[0] + 12, b[1] - 7], 1e-6, 'zoom then drag');
});

test('context: the frame takes ~62 % of the stage in the output aspect, centred, never larger than the plain view', () => {
  const f = contextFrame(1000, 600, 1, Infinity);
  assert.equal(f.height, Math.round(600 * CONTEXT_FILL));
  assert.equal(f.width, f.height);
  assert.equal(f.left, Math.round((1000 - f.width) / 2));
  assert.equal(f.top, Math.round((600 - f.height) / 2));
  const tall = contextFrame(400, 900, 630 / 810);
  assert.equal(tall.width, Math.round(400 * CONTEXT_FILL)); // width-bound on a narrow (phone) stage
  assert.ok(Math.abs(tall.width / tall.height - 630 / 810) < 0.01);
  assert.ok(tall.height <= 900 * CONTEXT_FILL);
  const tiny = contextFrame(1000, 800, 1, 120); // a small output is not blown up past the plain view
  assert.equal(tiny.width, 120);
  assert.equal(tiny.height, 120);
});

test('context: zoom pivot follows the server framing rule (eye line, crown, hair, centre)', () => {
  const lines = { crown: 100, eye: 260, chin: 420 };
  const eyeBand = { eye_min: 0.5625, eye_max: 0.6875, head_min: 0.5, head_max: 0.69 };
  const eye = zoomPivot({ height: 600, lines, hair_top: 90, bands: eyeBand }, 0.1);
  assert.equal(eye.fx, 0.6);
  assert.equal(eye.fy, 260 / 600);
  assert.deepEqual(eye.eye, { cae: 170 / 600, mid: 0.375, minTop: 0.04, lowest: 0.4375 });
  // the crown band keeps the crown: no extra move
  const top = zoomPivot({ height: 600, lines, bands: { top_min: 0.1, top_max: 0.15 } }, 0);
  assert.deepEqual(top, { fx: 0.5, fy: 100 / 600 });
  assert.equal(zoomShift(top, 1.15), 0);
  // no band: the hair top is re-placed (45/55 share); without hair_top the crown stands in for it
  const head = zoomPivot({ height: 600, lines, hair_top: 80, bands: {} }, -0.05);
  assert.ok(Math.abs(head.fx - 0.45) < 1e-12);
  assert.equal(head.fy, 80 / 600);
  assert.deepEqual(head.hair, { vh: 340 / 600, th: 320 / 600 });
  assert.equal(zoomPivot({ height: 600, lines, bands: {} }).fy, 100 / 600);
  // no face: the centred crop scales about the photo centre, which sits at frame centre + offsets
  assert.deepEqual(zoomPivot({ height: 600, face_found: false, lines: null, bands: eyeBand }, 0.2, -0.1), { fx: 0.7, fy: 0.4 });
  assert.deepEqual(zoomPivot(null), { fx: 0.5, fy: 0.5 });
  assert.equal(zoomShift(null, 1.1), 0);
  assert.equal(zoomShift(eye, 1), 0);
});

test('context: the eye line is lowered like the server does when a bigger head would crowd the top', () => {
  const pp = { height: 600, lines: { crown: 51.4, eye: 243, chin: 407.6 }, hair_top: 51.4,
    bands: { eye_min: 0.5625, eye_max: 0.6875, head_min: 0.5, head_max: 0.6875, top_min: null, top_max: null } };
  const pivot = zoomPivot(pp, 0.05, 0.03);
  // room above the hair: zooming out (or a little in) keeps the eye line
  assert.equal(zoomShift(pivot, 0.85), 0);
  assert.equal(zoomShift(pivot, 1.02), 0);
  // ×1.15: the hair would be 4.7 px from the top, so the eye line drops to keep 4 % (24 px) above it
  const s = zoomShift(pivot, 1.15) * 600;
  assert.ok(Math.abs(s - (0.04 * 600 + (243 - 51.4) * 1.15 - 0.375 * 600)) < 1e-9, `shift ${s}`);
  // … but never below the lowest allowed eye line
  assert.ok(Math.abs(zoomShift(pivot, 3) * 600 - (0.4375 - 0.375) * 600) < 1e-9);
});

// Real /api/preview answers (astronaut fixture, offsets 0.05 / 0.03, levelled 3°) at zoom 1, 1.15 and 0.85:
// starting from the zoom-1 preview, the live feedback must land where the re-framed preview puts the photo.
const SERVED = {
  us: { height: 600, width: 600, bands: { eye_min: 0.5625, eye_max: 0.6875, head_min: 0.5, head_max: 0.6875, top_min: null, top_max: null },
    at: {
      1: { lines: { crown: 51.4, chin: 407.6, eye: 243 }, hair_top: 51.4, source: { width: 512, height: 512, center: [208.8, 127.12], size: [269.96, 269.96], angle: 3 } },
      1.15: { source: { width: 512, height: 512, center: [211.13, 116.31], size: [234.75, 234.75], angle: 3 } },
      0.85: { source: { width: 512, height: 512, center: [206.19, 131.52], size: [317.61, 317.61], angle: 3 } },
    } },
  uk: { height: 531, width: 413, bands: { head_min: 0.6444444444444445, head_max: 0.7555555555555555, eye_min: null, eye_max: null, top_min: null, top_max: null },
    at: {
      1: { lines: { crown: 101.3, chin: 473, eye: 289.7 }, hair_top: 76.4, source: { width: 512, height: 512, center: [215.8, 92], size: [166.91, 214.6], angle: 3 } },
      1.15: { source: { width: 512, height: 512, center: [216.92, 91.5], size: [145.14, 186.61], angle: 3 } },
      0.85: { source: { width: 512, height: 512, center: [214.29, 92.68], size: [196.36, 252.47], angle: 3 } },
    } },
  jp: { height: 531, width: 413, bands: { head_min: 0.7111111111111111, head_max: 0.8, eye_min: null, eye_max: null, top_min: 0.044444444444444446, top_max: 0.13333333333333333 },
    at: {
      1: { lines: { crown: 63.1, chin: 464.3, eye: 278.9 }, hair_top: 63.1, source: { width: 512, height: 512, center: [215.67, 96.43], size: [165.01, 212.15], angle: 3 } },
      1.15: { source: { width: 512, height: 512, center: [217.29, 85.95], size: [143.48, 184.48], angle: 3 } },
      0.85: { source: { width: 512, height: 512, center: [213.47, 110.6], size: [194.12, 249.59], angle: 3 } },
    } },
  // no face (1200×900 grid, offsets 0.1 / −0.08, uk 35×45 frame)
  none: { height: 531, width: 413, face_found: false, bands: {}, offsets: [0.1, -0.08],
    at: {
      1: { lines: null, source: { width: 1200, height: 900, center: [530, 522], size: [700, 900], angle: 0 } },
      1.15: { source: { width: 1200, height: 900, center: [539.13, 512.61], size: [608.7, 782.61], angle: 0 } },
      0.85: { source: { width: 1200, height: 900, center: [517.65, 534.71], size: [823.53, 1058.82], angle: 0 } },
    } },
};

test('context: live head-size feedback matches the server re-framing (eye, hair, crown rules and no face)', () => {
  for (const [name, c] of Object.entries(SERVED)) {
    const [ox, oy] = c.offsets || [0.05, 0.03];
    const pp = { height: c.height, width: c.width, face_found: c.face_found ?? true, bands: c.bands, ...c.at[1] };
    const frame = { left: 0, top: 0, width: c.width, height: c.height }; // output px
    const pivot = zoomPivot(pp, ox, oy);
    for (const z of [1.15, 0.85]) {
      const live = contextPlacement(c.at[1].source, frame, {
        r: z, dy: zoomShift(pivot, z) * frame.height, px: pivot.fx * frame.width, py: pivot.fy * frame.height,
      });
      const relaid = contextPlacement(c.at[z].source, frame);
      const s = c.at[1].source;
      for (const [sx, sy] of [[s.center[0], s.center[1]], [0, 0], [s.width, s.height], [s.center[0] - 60, s.center[1] + 90]]) {
        // the served numbers are rounded (lines 0.1 px, frame 0.01 source px): well under 1 output px
        close(shown(live, sx, sy), shown(relaid, sx, sy), 0.6, `${name} ×${z} photo point (${sx}, ${sy})`);
      }
    }
  }
});

test('context: guides while moving — bands stay with the frame, measured lines ride with the photo', () => {
  // eye range = 31.25–43.75 % from the top (187.5–262.5 px of 600)
  const pp = { height: 600, face_found: true, lines: { crown: 40, eye: 120, chin: 360 }, bands: { eye_min: 0.5625, eye_max: 0.6875, head_min: 0.5, head_max: 0.69 } };
  const eyeLine = (p) => guideModel(p).find((g) => g.name === 'eye' && g.kind === 'line');
  const eyeBand = (p) => guideModel(p).find((g) => g.name === 'eye' && g.kind === 'band');
  assert.equal(eyeLine(pp).ok, false); // eyes too high
  const down = movedGuides(pp, { dy: 100 });
  assert.equal(down.lines.eye, 220);
  assert.equal(down.lines.crown, 140);
  assert.equal(eyeLine(down).ok, true); // dragged into the band, the line turns "ok" live
  assert.deepEqual(eyeBand(down), eyeBand(pp));
  const zoomed = movedGuides(pp, { r: 1.1, py: 120 });
  assert.equal(zoomed.lines.eye, 120);
  assert.ok(Math.abs((zoomed.lines.chin - zoomed.lines.crown) - 1.1 * 320) < 1e-9);
  assert.equal(movedGuides(pp, {}), pp);
  assert.equal(movedGuides(null, { dy: 5 }), null);
});

test('context: only a complete passport.source is used', () => {
  const ok = { width: 10, height: 20, center: [5, 5], size: [4, 4], angle: 0 };
  assert.equal(isSourceFrame(ok), true);
  assert.equal(isSourceFrame({ ...ok, angle: undefined }), true);
  assert.equal(isSourceFrame(null), false);
  assert.equal(isSourceFrame({ ...ok, size: [0, 4] }), false);
  assert.equal(isSourceFrame({ ...ok, center: [5] }), false);
  assert.equal(isSourceFrame({ ...ok, width: '10' }), false);
  assert.equal(isSourceFrame({ ...ok, angle: NaN }), false);
});

test('context: the pre-passport ops are exactly the ops entering the passport op', () => {
  Object.assign(state, {
    files: [], presets: [{ id: 'us', name: 'United States', country: 'US', width_mm: 50.8, height_mm: 50.8, dpi: 300, width_px: 600, height_px: 600 }],
    edit: defaults.edit(), resize: defaults.resize(), background: defaults.background(), passport: defaults.passport(),
    output: defaults.output(), compress: defaults.compress(), features: { heic: true, avif: true, face: true, segmentation: true },
  });
  const f = { id: 'a', name: 'a.jpg', crop: { kind: 'box', x: 0.1, y: 0.1, w: 0.5, h: 0.5, ratio: 'free' }, info: { width: 1000, height: 800, has_alpha: false } };
  assert.equal(prePassportOps(f), null); // passport off
  Object.assign(state.passport, { on: true, preset: 'us', zoom: 1.05, offsetX: 0.02, offsetY: -0.01 });
  assert.deepEqual(prePassportOps(f), []); // crop doesn't apply under passport: the original is the base
  state.edit.quarter = 90;
  state.background = { mode: 'replace', color: '#ffffff', autoFrom: null };
  const pre = prePassportOps(f);
  assert.deepEqual(pre.map((o) => o.op), ['rotate', 'background']);
  const fr = framingOf(buildOps(f));
  assert.deepEqual({ zoom: fr.zoom, offsetX: fr.offsetX, offsetY: fr.offsetY }, { zoom: 1.05, offsetX: 0.02, offsetY: -0.01 });
  assert.equal(fr.baseKey, JSON.stringify(pre));
  state.passport.layout = 'sheet'; // a sheet after the passport op doesn't change what enters it
  assert.equal(framingOf(buildOps(f)).baseKey, JSON.stringify(pre));
  assert.equal(framingOf([{ op: 'resize', mode: 'scale', scale: 50 }]), null);
});
