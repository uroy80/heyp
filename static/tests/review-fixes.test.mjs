// Regression tests for the v2 review findings fixed in heyp's UI (img-01, img-05, img-06, UIH-01…UIH-12,
// OPS-4, OPS-5, OPS-14, COPY-HEYP). Pure modules only — no DOM.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';

import {
  state, defaults, buildOps, blurOp, planUploads, requestChunkBytes, DEFAULT_REQUEST_MB, JOB_OPS, exportNeedsJob, hasJobOps,
  upscaleProblems, upscaleInputDims, stillAnimations, PIXELATE_MIN,
} from '../js/state.js';
import { copyFor, sourceLink, OFFLINE } from '../js/config.js';
import { api, edgeMessage, ApiError } from '../js/api.js';
import { deleteKeyAction } from '../js/keys.js';
import { keepFiles, savingRows, compressMode, keepOptions, KEEP_FORMATS } from '../js/tools/compress.js';
import { BLUR_HINT, minStrength } from '../js/tools/blur.js';

const MiB = 1024 * 1024;
const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8');

function fresh() {
  state.files = [];
  state.activeId = null;
  state.features = { segmentation: true, face: true, matting: true };
  state.limits = null;
  state.tools = [];
  for (const k of ['edit', 'resize', 'background', 'passport', 'compress', 'output', 'upscale', 'watermark', 'meme', 'blur', 'editor', 'gif', 'html']) {
    state[k] = defaults[k]();
  }
}
const file = (id, w = 400, h = 300, extra = {}) => ({ id, name: `${id}.jpg`, info: { width: w, height: h, format: 'jpeg' }, crop: null, regions: [], items: [], ...extra });

// ── img-01 / UIH-01 / OPS-5: slow exports go through the img.process job ──────────────────────────────────────

test('img-01/OPS-5: background removal and upscale count as job work; several photos always go as a job', () => {
  fresh();
  assert.ok(JOB_OPS.has('upscale') && JOB_OPS.has('background'));
  state.files = [file('a')];
  assert.equal(exportNeedsJob(), false, 'one photo, light edits: a direct download');
  state.background.mode = 'remove';
  assert.ok(hasJobOps() && exportNeedsJob(), 'background removal is slow (BiRefNet)');
  state.background.mode = 'off';
  state.upscale.factor = 2;
  state.features.upscale = true;
  assert.ok(exportNeedsJob(), 'upscale');
  state.upscale.factor = 0;
  state.files = [file('a'), file('b')];
  assert.ok(exportNeedsJob(), 'a batch: progress and no proxy timeout');
});

test('UIH-01: proxy and CDN replies that are not heyp JSON get readable messages; X-Heyp-Use-Job is kept', async () => {
  assert.match(edgeMessage(524), /took too long/);
  assert.match(edgeMessage(502), /took too long/);
  assert.match(edgeMessage(413), /larger than the site accepts/);
  assert.equal(edgeMessage(404), null);
  const real = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response('<html>A timeout occurred</html>', { status: 524, headers: { 'Content-Type': 'text/html' } });
    await assert.rejects(api.process({ ids: ['x'] }), (e) => e instanceof ApiError && e.status === 524 && /took too long/.test(e.detail));
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'On this server upscale runs as a background job' }), {
      status: 422, headers: { 'Content-Type': 'application/json', 'X-Heyp-Use-Job': 'img.process' },
    });
    await assert.rejects(api.process({ ids: ['x'] }), (e) => e.useJob === 'img.process' && e.status === 422);
    globalThis.fetch = async () => { throw new TypeError('offline'); };
    await assert.rejects(api.process({ ids: ['x'] }), (e) => e.status === 0 && e.detail === OFFLINE && !/container/.test(e.detail));
  } finally {
    globalThis.fetch = real;
  }
});

// ── UIH-02 / OPS-4: requests stay under Cloudflare's 100 MB body cap ─────────────────────────────────────────

test('OPS-4/UIH-02: uploads are chunked below the request cap; 20 phone photos are not one 120 MB request', () => {
  const f = (name, mb) => ({ name, size: mb * MiB });
  const twenty = Array.from({ length: 20 }, (_, i) => f(`p${i}.jpg`, 6));
  const plan = planUploads(twenty, { max_upload_mb: 100, max_files: 200 });
  assert.ok(plan.chunks.length >= 2, `${plan.chunks.length} request(s)`);
  for (const c of plan.chunks) assert.ok(c.reduce((n, x) => n + x.size, 0) <= requestChunkBytes(null), 'every request under the default cap');
  assert.ok(requestChunkBytes(null) < 100 * 1000 * 1000, 'below 100 MB (decimal), multipart framing included');
  assert.equal(DEFAULT_REQUEST_MB, 90);
  const fifteen = Array.from({ length: 15 }, (_, i) => f(`q${i}.jpg`, 8));
  assert.ok(planUploads(fifteen, { max_upload_mb: 90, max_request_mb: 95 }).chunks.length >= 2);
  // a file larger than a chunk goes alone; one above the published request cap is refused up front
  const solo = planUploads([f('a', 1), f('big', 92), f('b', 1)], { max_upload_mb: 100 });
  assert.deepEqual(solo.chunks.map((c) => c.map((x) => x.name)), [['a'], ['big'], ['b']]);
  const capped = planUploads([f('huge', 96)], { max_upload_mb: 100, max_request_mb: 95 });
  assert.equal(capped.chunks.length, 0);
  assert.match(capped.rejected[0].detail, /95 MB this site accepts in one upload/);
});

// ── UIH-03: Delete never drops the photo while a box / layer is being edited ─────────────────────────────────

test('UIH-03: Delete after drawing a blur box removes the box, never the photo', () => {
  const editingBlur = { active: true, selected: 'r1', removable: true };
  assert.equal(deleteKeyAction({ hasPhoto: true, target: { body: true }, editing: editingBlur }), 'layer');
  assert.equal(deleteKeyAction({ hasPhoto: true, target: { body: true }, editing: { active: true, selected: null } }), null);
  assert.equal(deleteKeyAction({ hasPhoto: true, target: { body: true }, editing: { active: true, selected: 'wm', removable: false } }), null);
  // a focused thumbnail still removes its photo; outside the editing sections the page focus does too
  assert.equal(deleteKeyAction({ hasPhoto: true, target: { inStrip: true }, editing: editingBlur }), 'photo');
  assert.equal(deleteKeyAction({ hasPhoto: true, target: { body: true }, editing: { active: false } }), 'photo');
  assert.equal(deleteKeyAction({ hasPhoto: true, target: {}, editing: { active: false } }), null, 'a slider keeps the key');
  assert.equal(deleteKeyAction({ hasPhoto: false, target: { body: true }, editing: { active: false } }), null);
});

// ── UIH-04: Make GIF applies the edits by default ────────────────────────────────────────────────────────────

test('UIH-04: Make GIF frames get the other sections’ edits unless switched off', () => {
  fresh();
  assert.equal(defaults.gif().applyEdits, true);
  const src = read('../js/tools/gif.js');
  assert.ok(!src.includes('then use “Continue with › Make GIF”'), 'the old note sent people round in a loop');
  assert.match(src, /app\.makeGif/);
});

// ── UIH-05 / COPY-HEYP: only verifiable copy ──────────────────────────────────────────────────────

const BANNED = /local only|local edition|late edition|web edition|this machine|never the internet|never sent|no third part|your own server|container|keeps nothing|private by design|can’t be recovered/i;

test('COPY-HEYP: copy in both modes is built from /api/config and makes no unverifiable claim', () => {
  const hosted = copyFor({ mode: 'hosted', ttl_minutes: 60, hard_ttl_minutes: 240, source_url: 'https://github.com/uroy80/heyp' });
  const local = copyFor({ mode: 'local', ttl_minutes: 90, hard_ttl_minutes: 0 });
  for (const c of [hosted, local, copyFor(null)]) assert.ok(!BANNED.test(JSON.stringify(c)), JSON.stringify(c));
  assert.match(hosted.bgHint, /isn’t sent to an AI service/);
  assert.match(hosted.consent, /agree/);
  assert.equal(local.consent, null, 'self-hosted copies print no consent line');
});

// ── COPY-PASS: how long files are kept is told on /privacy only, never in the app's UI ─────────────────────

const RETENTION = /auto-delete|deleted (after|automatically|\d)|files are deleted|after (you )?last use|kept for|\b\d+ (minutes?|hours?)\b|no longer on the server|may remain in our server logs/i;
/** A source file with its comments removed: only what can reach the page is checked. */
const code = (src) => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '').replace(/^\s*\/\/.*$/gm, '').replace(/\s\/\/ .*$/gm, '');

test('COPY-PASS: the page copy never states how long files are kept, whatever the configured TTLs', () => {
  for (const cfg of [null, { mode: 'local', ttl_minutes: 60 }, { mode: 'hosted', ttl_minutes: 60, hard_ttl_minutes: 240 }]) {
    const c = JSON.stringify(copyFor(cfg));
    assert.ok(!RETENTION.test(c), c);
    assert.ok(!/ttl|60|240/i.test(c.replace(/AGPL-3\.0|uroy80/g, '')), c);
  }
});

test('COPY-PASS: no retention wording in index.html, the stylesheet or any UI module, and no UI code reads the TTLs', () => {
  const files = ['../index.html', '../styles.css', '../app.js', ...readdirSync(new URL('../js/', import.meta.url)).filter((f) => f.endsWith('.js')).map((f) => `../js/${f}`),
    ...readdirSync(new URL('../js/tools/', import.meta.url)).map((f) => `../js/tools/${f}`)];
  for (const f of files) {
    const src = code(read(f));
    assert.ok(!RETENTION.test(src), `${f}: ${src.match(RETENTION)?.[0]}`);
    assert.ok(!/ttl_minutes|hard_ttl/.test(src), `${f} reads the configured TTLs`);
  }
  const html = read('../index.html');
  assert.doesNotMatch(html, /local-pill|dateline-edition|dateline-date/, 'no badge, retention line or date in the masthead');
});

test('UIH-06: closing the tab still deletes this page\'s uploads and results', () => {
  assert.match(read('../app.js'), /for \(const id of resultFiles\) if \(!state\.files\.some/, 'pagehide deletes job results too');
});

test('UIH-05: no edition labels or "Local only" pill ship in the page or the stylesheet', () => {
  const css = read('../styles.css');
  assert.ok(!/--edition|Late edition|Web edition|Local edition/.test(css), 'edition labels in styles.css');
  const html = read('../index.html');
  assert.ok(!BANNED.test(html.replace(/<!--[\s\S]*?-->/g, '')), 'index.html copy');
});

test('COPY-PASS: panels carry a title and at most one short line; explanations sit behind an "i" button', () => {
  for (const f of readdirSync(new URL('../js/tools/', import.meta.url))) {
    const src = read(`../js/tools/${f}`);
    for (const m of src.matchAll(/toolHead\('[^']*', '([^']*)'/g)) {
      assert.ok(m[1].split(/\s+/).length <= 6, `${f}: standfirst "${m[1]}" is longer than six words`);
    }
    assert.doesNotMatch(src, /kicker:/, `${f}: long kickers are gone (the number is the kicker)`);
  }
  const controls = read('../js/controls.js');
  assert.match(controls, /'aria-describedby': id/, 'the tip button is described by its note');
  assert.match(controls, /e\.key !== 'Escape'/, 'Esc closes a tip');
});

// ── UIH-07 / OPS-14 / UIH-08 / UIH-09: links ─────────────────────────────────────────────────────────────────

test('OPS-14: the footer source link points at the running commit when the server reports it', () => {
  assert.equal(sourceLink({ source_url: 'https://github.com/uroy80/heyp', revision: '5a122f3c9d' }), 'https://github.com/uroy80/heyp/tree/5a122f3c9d');
  assert.equal(sourceLink({ source_url: 'https://github.com/uroy80/heyp/', revision: 'main; rm' }), 'https://github.com/uroy80/heyp');
  assert.equal(sourceLink({ source_url: 'https://git.example.org/heyp', revision: '5a122f3c9d' }), 'https://git.example.org/heyp');
  const c = copyFor({ mode: 'hosted', ttl_minutes: 60, source_url: 'https://github.com/uroy80/heyp', revision: 'abcdef1' });
  assert.equal(c.footer.at(-1).href, 'https://github.com/uroy80/heyp/tree/abcdef1');
});

test('UIH-08: the policy links open in a new tab, and leaving with photos always asks', () => {
  const app = read('../app.js');
  assert.match(app, /const doc = \(href, label\) => h\('a', \{ href, target: '_blank', rel: 'noopener' \}, label\)/);
  assert.match(app, /if \(!state\.files\.length\) return;\n {4}e\.preventDefault\(\);/);
  assert.match(read('../index.html'), /<a href="\/privacy" target="_blank" rel="noopener">Privacy<\/a>/);
});

test('UIH-09 (split): heypdf is a plain link — no file ids, no hand-over storage', () => {
  const app = read('../app.js');
  assert.doesNotMatch(app, /sessionStorage|\?files=|handOff/);
  assert.match(read('../index.html'), /<a href="#" class="sibling-link" data-app="heypdf" hidden>/);
});

// ── UIH-10 / UIH-11 ───────────────────────────────────────────────────────────────────────────────────────────

test('UIH-10: animated photos exported to a still format are listed (GIF/WebP/original keep them)', () => {
  fresh();
  const gif = file('g', 60, 40, { info: { width: 60, height: 40, format: 'gif', frames: 3 } });
  state.files = [file('a'), gif];
  assert.deepEqual(stillAnimations(state.files, 'jpeg').map((f) => f.id), ['g']);
  for (const fmt of ['gif', 'webp', 'original']) assert.equal(stillAnimations(state.files, fmt).length, 0, fmt);
  assert.equal(stillAnimations(state.files, 'png').length, 1, 'a GIF saved as PNG is a still');
});

test('UIH-11: every photo of a batch is checked against the upscale limits before sending', () => {
  fresh();
  const limits = { max_pixels: 50_000_000, upscale_max_input_pixels: 6_000_000 };
  const small = file('small', 512, 512);
  const big = file('big', 3000, 2000);
  const bad = upscaleProblems([small, big], upscaleInputDims, 4, limits);
  assert.deepEqual(bad.map((x) => x.file.id), ['big']);
  assert.equal(upscaleProblems([small, big], upscaleInputDims, 2, limits).length, 0);
  // a photo resized before the upscale has an unknown input size: left to the server rather than refused on a guess
  state.resize = { ...defaults.resize(), on: true, mode: 'percent', percent: 25 };
  assert.equal(upscaleInputDims(big), null);
  assert.deepEqual(upscaleInputDims({ ...big, preview: { width: 750, height: 500 } }), { w: 750, h: 500 });
});

// ── img-05: compress in the original format ───────────────────────────────────────────────────────────────────

test('img-05: Compress offers "Keep format" through img.compress when the server has it', () => {
  fresh();
  assert.equal(defaults.compress().mode, 'keep');
  assert.equal(compressMode(), 'target', 'no img.compress on the server: the size-limit mode');
  state.tools = [{ id: 'img.compress', available: true, max_files: 100 }];
  assert.equal(compressMode(), 'keep');
  assert.deepEqual(keepOptions(), { level: 'recommended', png_quantize: true });
  const files = ['jpeg', 'png', 'gif', 'svg', 'webp', 'heic', 'tiff'].map((fmt) => file(fmt, 10, 10, { info: { width: 10, height: 10, format: fmt } }));
  assert.deepEqual(keepFiles(files).map((f) => f.id), [...KEEP_FORMATS]);
  const rows = savingRows({ files: [{ source: 'bot.gif', before: 414000, after: 140000, saved: 0.6618, changed: true }] });
  assert.deepEqual(rows[0], { name: 'bot.gif', before: 414000, after: 140000, saved: 66, changed: true });
  // keep-format doesn't change the export
  state.compress.maxKb = 100;
  state.compress.on = false;
  assert.equal(buildOps(file('a')).length, 0);
});

// ── img-06 / UIH-12: no overclaiming on blur ──────────────────────────────────────────────────────────────────

test('img-06/UIH-12: the blur hint doesn’t promise irreversibility, and pixelate never goes below 40 %', () => {
  fresh();
  assert.ok(!/can’t be recovered|cannot be recovered|unrecoverable/i.test(BLUR_HINT));
  assert.match(BLUR_HINT, /Use Solid for text, licence plates/);
  assert.equal(minStrength('pixelate'), PIXELATE_MIN);
  assert.equal(minStrength('blur'), 0.1);
  state.blur = { mode: 'pixelate', strength: 0.1, color: '#000000' };
  const f = file('a', 400, 300, { regions: [{ id: 'r', cx: 0.5, cy: 0.5, w: 0.2, h: 0.2, on: true }] });
  assert.equal(blurOp(f).strength, PIXELATE_MIN);
  state.blur.mode = 'blur';
  assert.equal(blurOp(f).strength, 0.1);
});
