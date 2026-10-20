// Regression tests for passport panel logic, guides, live-region updates and undoable removal.
import test from 'node:test';
import assert from 'node:assert/strict';

import { state, defaults } from '../js/state.js';
import { presetSelectModel, choosePreset, backgroundRule, measurements } from '../js/tools/passport.js';
import { guideModel } from '../js/canvas.js';
import { replaceIfChanged } from '../js/dom.js';
import { Pending } from '../js/pending.js';

const PRESETS = [
  { id: 'us', name: 'United States — passport & visa (2×2 in)', country: 'US', background_edit_allowed: true },
  { id: 'jp', name: 'Japan — passport (35×45 mm)', country: 'JP' },
  { id: 'uk', name: 'United Kingdom — passport (35×45 mm)', country: 'GB', background_edit_allowed: false },
];

test('UI-2: a search that hides the current preset selects a placeholder, never another country', () => {
  state.presets = PRESETS;
  const hidden = presetSelectModel('japan', 'us');
  assert.equal(hidden.value, '');
  assert.match(hidden.placeholder, /United States 2×2 in/);
  assert.deepEqual(hidden.groups.flatMap((g) => g.options.map((o) => o.value)), ['jp']);

  const shown = presetSelectModel('united', 'us');
  assert.equal(shown.value, 'us');
  assert.equal(shown.placeholder, undefined);

  const all = presetSelectModel('', 'us');
  assert.equal(all.value, 'us');

  const none = presetSelectModel('zzz', 'us');
  assert.equal(none.value, '');
  assert.match(none.placeholder, /^No match/);
});

test('C3: presets that forbid background edits default "Replace background" off and explain why', () => {
  state.presets = PRESETS;
  const p = defaults.passport();
  choosePreset(p, 'uk');
  assert.equal(p.replace, false);
  assert.match(backgroundRule(PRESETS[2]), /United Kingdom rules don’t allow digitally altered backgrounds/);
  choosePreset(p, 'us');
  assert.equal(p.replace, true);
  assert.equal(backgroundRule(PRESETS[0]), null);
  choosePreset(p, 'jp'); // field missing (older server) → allowed
  assert.equal(p.replace, true);
});

test('a new document starts from its own framing: the fine-tune made for the previous one is dropped', () => {
  state.presets = PRESETS;
  const p = { ...defaults.passport(), preset: 'us', zoom: 1.2, offsetX: 0.1, offsetY: 0.07 };
  choosePreset(p, 'uk');
  assert.deepEqual({ zoom: p.zoom, offsetX: p.offsetX, offsetY: p.offsetY }, { zoom: 1, offsetX: 0, offsetY: 0 });
});

const PP = {
  preset: 'us', width: 600, height: 600, head_ratio: 0.594, eye_from_bottom: 0.62, face_found: true,
  lines: { crown: 52, chin: 408.4, eye: 228 },
  bands: { head_min: 0.5, head_max: 0.69, eye_min: 0.5625, eye_max: 0.6875, top_min: null, top_max: null },
  background_edit_allowed: true,
};

test('VIS-9 / UI-17: guides map the passport block to bands and measured lines', () => {
  const g = guideModel(PP);
  const band = (n) => g.find((x) => x.kind === 'band' && x.name === n);
  const line = (n) => g.find((x) => x.kind === 'line' && x.name === n);
  assert.ok(Math.abs(band('chin').from - (52 + 300) / 600) < 1e-9);
  assert.ok(Math.abs(band('chin').to - (52 + 414) / 600) < 1e-9);
  assert.ok(Math.abs(band('eye').from - (1 - 0.6875)) < 1e-9);
  assert.ok(Math.abs(band('eye').to - (1 - 0.5625)) < 1e-9);
  assert.equal(band('top'), undefined);
  assert.equal(line('chin').ok, true);
  assert.equal(line('eye').ok, true);
  assert.equal(line('crown').ok, null);
  assert.ok(g.some((x) => x.kind === 'center'));

  const low = guideModel({ ...PP, lines: { crown: 52, chin: 300, eye: 450 } });
  assert.equal(low.find((x) => x.name === 'chin' && x.kind === 'line').ok, false);
  assert.equal(low.find((x) => x.name === 'eye' && x.kind === 'line').ok, false);

  const noFace = guideModel({ ...PP, face_found: false, lines: null });
  assert.deepEqual(noFace.filter((x) => x.kind === 'line'), []);
  assert.ok(noFace.some((x) => x.name === 'eye'));
  assert.deepEqual(guideModel(null), []);
});

test('VIS-9 / UI-17: the panel reports measured head and eye positions against the spec', () => {
  state.passport = defaults.passport();
  const rows = measurements(PP);
  assert.deepEqual(rows.map((r) => [r.label, r.value, r.spec, r.ok]), [
    ['Head height', '59 %', '50–69 %', true],
    ['Eyes from bottom', '62 %', '56–69 %', true],
  ]);
  const out = measurements({ ...PP, head_ratio: 0.45, roll_deg: 20 });
  assert.equal(out[0].ok, false);
  assert.equal(out.at(-1).spec, 'too tilted to level');
  assert.equal(out.at(-1).note, true); // a status word, printed without the "spec" prefix
  assert.equal(measurements({ ...PP, face_found: false })[0].ok, false);
  assert.deepEqual(measurements(null), []);
});

test('UI-11: live regions are only rewritten when their content changes', () => {
  let writes = 0;
  const target = { replaceChildren: () => { writes++; } };
  const span = (t) => ({ outerHTML: `<span>${t}</span>` });
  assert.equal(replaceIfChanged(target, [span('a.jpg'), span('400×400')]), true);
  for (let i = 0; i < 10; i++) replaceIfChanged(target, [span('a.jpg'), span('400×400')]);
  assert.equal(writes, 1);
  replaceIfChanged(target, [span('a.jpg'), span('600×600')]);
  assert.equal(writes, 2);
  replaceIfChanged(target, []);
  replaceIfChanged(target, []);
  assert.equal(writes, 3);
});

test('UI-7: a removal waits for the undo window; Undo cancels it, page exit fires it', () => {
  const timers = new Map();
  let n = 0;
  const pend = new Pending(8000, { setTimer: (fn) => { timers.set(++n, fn); return n; }, clearTimer: (t) => timers.delete(t) });
  const deleted = [];
  pend.add('a', { index: 0 }, (_, opts) => deleted.push(['a', opts]));
  pend.add('b', { index: 1 }, (_, opts) => deleted.push(['b', opts]));
  assert.deepEqual(pend.take('a'), { index: 0 }); // Undo
  assert.equal(pend.take('a'), null);
  for (const fn of [...timers.values()]) fn(); // the window expires
  assert.deepEqual(deleted, [['b', undefined]]);
  pend.add('c', {}, (_, opts) => deleted.push(['c', opts]));
  pend.flush({ keepalive: true });
  assert.deepEqual(deleted.at(-1), ['c', { keepalive: true }]);
  assert.equal(pend.has('c'), false);
});

test('UI-7: Pending calls its timers unbound (browser timers throw "Illegal invocation" otherwise)', () => {
  const calls = [];
  function strictTimer(fn, ms) {
    if (this !== undefined && this !== globalThis) throw new TypeError('Illegal invocation');
    calls.push(ms);
    return calls.length;
  }
  function strictClear() {
    if (this !== undefined && this !== globalThis) throw new TypeError('Illegal invocation');
  }
  const pend = new Pending(8000, { setTimer: strictTimer, clearTimer: strictClear });
  pend.add('a', { index: 0 }, () => {});
  assert.deepEqual(calls, [8000]);
  assert.deepEqual(pend.take('a'), { index: 0 });
});
