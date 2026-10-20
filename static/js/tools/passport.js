// Passport / visa / ID photos: preset framing, background, fine-tune, print sheets.

import { h, fmtNum, posInt, append } from '../dom.js';
import { state, defaults, findPreset, passportDims, shortPresetName } from '../state.js';
import { toolHead, group, segmented, slider, toggle, selectField, fillSelect, numberField, swatches, note, quickChips } from '../controls.js';
import { icon } from '../icons.js';

/** Copies per print sheet the server accepts (schemas.SheetOp.copies). */
const MAX_COPIES = 200;

/** One-click documents offered above the full list (the viewer's own country goes first). */
const POPULAR = ['us', 'uk', 'schengen', 'in', 'ca', 'au', 'cn', 'jp'];
const QUICK_PICKS = 6;

const BACKGROUNDS = [
  { value: '#ffffff', label: 'White' },
  { value: '#f2f2f2', label: 'Off-white' },
  { value: '#dbe9f7', label: 'Light blue' },
  { value: '#e5e7eb', label: 'Light grey' },
];

let r = {};
let query = '';

const regionNames = (() => {
  try {
    return new Intl.DisplayNames(['en'], { type: 'region' });
  } catch {
    return null;
  }
})();

export function countryName(code) {
  if (!code) return 'Other';
  if (code === 'EU') return 'European Union';
  try {
    return regionNames?.of(code) || code;
  } catch {
    return code;
  }
}

/** Pick a sensible default preset from the browser locale (en-IN → India). */
export function guessPreset(presets) {
  const langs = navigator.languages?.length ? navigator.languages : [navigator.language || ''];
  for (const l of langs) {
    const region = /[-_]([A-Za-z]{2})\b/.exec(l)?.[1]?.toUpperCase();
    if (!region) continue;
    const hit = presets.find((p) => p.country === region && /passport/i.test(p.name)) || presets.find((p) => p.country === region);
    if (hit) return hit.id;
  }
  return (presets.find((p) => p.id === 'us') || presets[0])?.id || null;
}

function groups(q) {
  const needle = q.trim().toLowerCase();
  const byCountry = new Map();
  for (const p of state.presets) {
    const cname = countryName(p.country);
    const hay = `${p.name} ${cname} ${p.country} ${p.id}`.toLowerCase();
    if (needle && !hay.includes(needle)) continue;
    if (!byCountry.has(cname)) byCountry.set(cname, []);
    byCountry.get(cname).push({ value: p.id, label: p.name });
  }
  const out = [...byCountry.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([label, options]) => ({ label, options: options.sort((a, b) => a.label.localeCompare(b.label)) }));
  if (!needle || 'custom size'.includes(needle)) out.push({ label: 'Other', options: [{ value: 'custom', label: 'Custom size…' }] });
  return out;
}

/**
 * What the preset <select> shows for a search: the filtered groups, and the current preset selected
 * when it is among them. When the search hides it, a disabled placeholder is selected instead, so the
 * dropdown never displays a country other than the one that is applied and picking any match fires `change`.
 */
export function presetSelectModel(q, presetId) {
  const g = groups(q);
  const listed = g.some((x) => x.options.some((o) => o.value === presetId));
  if (listed || !presetId) return { groups: g, value: presetId, placeholder: undefined };
  const cur = presetId === 'custom' ? 'custom size' : shortPresetName(findPreset(presetId) || { name: presetId });
  const n = g.reduce((k, x) => k + x.options.length, 0);
  return { groups: g, value: '', placeholder: n ? `Pick a match · now: ${cur}` : `No match · now: ${cur}` };
}

/** Passport defaults that depend on the preset's rules (C3): no background replacement where it's forbidden. */
export const replaceAllowed = (preset) => preset?.background_edit_allowed !== false;

/** "<Country> rules don't allow digitally altered backgrounds …" for presets that forbid it, else null. */
export function backgroundRule(preset) {
  if (replaceAllowed(preset)) return null;
  return `${countryName(preset.country)} rules don’t allow digitally altered backgrounds — photograph against a plain light wall instead.`;
}

/** Point a passport state object at a preset, applying the preset's background rule. */
export function choosePreset(p, id) {
  p.preset = id;
  p.background = null;
  p.replace = replaceAllowed(findPreset(id));
}

const pct = (v) => `${Math.round(v * 100)} %`;
const range = (a, b) => (a == null || b == null ? null : `${Math.round(a * 100)}–${Math.round(b * 100)} %`);

/** Measured head / eye / tilt rows for the panel from the preview's `passport` block (contract v1.1 C2). */
export function measurements(pp) {
  if (!pp) return [];
  const b = pp.bands || {};
  const rows = [];
  if (pp.face_found === false) return [{ label: 'Face', value: 'not found — centred crop', ok: false }];
  if (pp.head_ratio != null) {
    const ok = b.head_min == null || (pp.head_ratio >= b.head_min - 0.005 && pp.head_ratio <= b.head_max + 0.005);
    rows.push({ label: 'Head height', value: pct(pp.head_ratio), spec: range(b.head_min, b.head_max), ok });
  }
  if (pp.eye_from_bottom != null && b.eye_min != null) {
    const ok = pp.eye_from_bottom >= b.eye_min - 0.005 && pp.eye_from_bottom <= b.eye_max + 0.005;
    rows.push({ label: 'Eyes from bottom', value: pct(pp.eye_from_bottom), spec: range(b.eye_min, b.eye_max), ok });
  }
  if (pp.roll_deg != null && Math.abs(pp.roll_deg) >= 0.5) {
    const deg = Math.abs(pp.roll_deg);
    rows.push({ label: 'Head tilt', value: `${fmtNum(deg, 1)}°`, spec: deg > 15 ? 'too tilted to level' : state.passport.level ? 'levelled' : 'not levelled', note: true, ok: deg <= 15 && state.passport.level !== false });
  }
  return rows;
}

function safeUrl(u) {
  try {
    const url = new URL(u);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
  } catch {
    return null;
  }
}

export default {
  id: 'passport',
  title: 'Passport',
  icon: 'passport',

  isActive: () => state.passport.on,
  reset() {
    const keep = state.passport.preset;
    state.passport = { ...defaults.passport(), preset: keep, replace: replaceAllowed(findPreset(keep)) };
  },

  render(root, app) {
    r = {};
    const p = state.passport;
    append(root, toolHead('Passport photo', 'Face-aware framing to official head-size rules, a clean background and print-ready sheets.', { onReset: () => app.resetTool(this), active: this.isActive() }));

    if (state.presetsError) {
      append(root, note(`Couldn’t load presets: ${state.presetsError}`, 'warn'),
        h('button', { type: 'button', class: 'btn', onclick: () => app.loadPresets() }, 'Try again'));
      return;
    }
    if (!state.presets.length) {
      append(root, h('p', { class: 'muted' }, 'Loading presets…'));
      return;
    }

    const pick = (id) => {
      if (!id) return;
      if (id !== p.preset) choosePreset(p, id);
      p.on = true;
      p.dismissed = false;
      query = '';
      app.changed({ panel: true });
    };
    const matches = () => r.select.select.querySelectorAll('option:not([disabled])').length;
    const fill = () => {
      fillSelect(r.select.select, presetSelectModel(query, p.preset));
      r.empty.hidden = matches() > 0;
      if (r.make) r.make.disabled = !r.select.select.value;
    };

    const search = h('input', {
      type: 'search', class: 'input input-search', placeholder: 'Search country or document…', value: query,
      'aria-label': 'Search presets', 'aria-controls': 'pp-select', autocomplete: 'off',
      oninput: () => {
        query = search.value;
        fill();
      },
      onkeydown: (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const first = r.select.select.querySelector('option:not([disabled])');
          if (first) pick(first.value);
        }
      },
    });
    r.select = selectField({ label: 'Country & document', id: 'pp-select', ...presetSelectModel(query, p.preset), onChange: pick });
    r.empty = h('p', { class: 'hint', hidden: matches() > 0 }, 'No presets match — try a country name or “visa”.');

    const preset = p.preset === 'custom' ? null : findPreset(p.preset);
    const dims = passportDims();
    // the size at a glance stays under the picker; the long rules fold away at the bottom
    const dimsLine = preset ? h('p', { class: 'preset-dims' },
      h('strong', {}, `${fmtNum(preset.width_mm, 1)} × ${fmtNum(preset.height_mm, 1)} mm`),
      dims ? h('span', { class: 'muted' }, ` · ${dims.w}\u00a0×\u00a0${dims.h}\u00a0px · ${preset.dpi}\u00a0DPI`) : null) : null;
    const rules = preset && (preset.notes || safeUrl(preset.source)) ? h('details', { class: 'disclosure pp-rules' },
      h('summary', {}, 'Rules for this document'),
      preset.notes ? h('p', { class: 'preset-notes' }, preset.notes) : null,
      safeUrl(preset.source) ? h('a', { class: 'ext-link', href: safeUrl(preset.source), target: '_blank', rel: 'noopener noreferrer' }, 'Official spec', h('span', { 'aria-hidden': 'true' }, ' ↗'), h('span', { class: 'sr-only' }, ' (opens in a new tab)')) : null) : null;
    let custom = null;
    if (p.preset === 'custom') {
      const c = p.custom;
      const upd = (k) => (v) => { c[k] = v; p.on = true; p.dismissed = false; app.changed(); };
      custom = h('div', { class: 'preset-card' }, h('div', { class: 'grid-3' },
        numberField({ label: 'Width', value: c.width_mm, suffix: 'mm', min: 10, max: 300, onChange: upd('width_mm') }),
        numberField({ label: 'Height', value: c.height_mm, suffix: 'mm', min: 10, max: 300, onChange: upd('height_mm') }),
        numberField({ label: 'DPI', value: c.dpi, step: 1, min: 72, max: 2400, onChange: upd('dpi') })),
      h('p', { class: 'hint' }, 'Head height defaults to about 50–69 % of the photo, like most ICAO-style specs.'));
      r.customHint = note('Enter a width, height and DPI to apply the custom size.', 'warn');
      append(custom, r.customHint);
    }

    // one-click documents: picking one starts the passport photo straight away
    const own = guessPreset(state.presets);
    const quick = [...new Set([own, ...POPULAR])].map(findPreset).filter(Boolean).slice(0, QUICK_PICKS);
    const chipLabel = (x) => (x.country === 'EU' ? 'Schengen / EU' : countryName(x.country) || shortPresetName(x));
    const chips = quick.length ? h('div', { class: 'pp-quick' }, h('p', { class: 'hint' }, 'Popular'),
      quickChips(quick.map((x) => ({ value: x.id, label: chipLabel(x), active: false })), pick, { label: 'Popular documents' })) : null;

    if (!p.on) {
      // the choice comes first: pick a document (chip or list) — or confirm the one shown — and the photo is framed
      r.make = h('button', { type: 'button', class: 'btn btn-primary btn-block pp-make', disabled: !r.select.select.value, onclick: () => pick(r.select.select.value) },
        icon('passport', 18), h('span', {}, 'Make passport photo'));
      // picker + action first so they sit above the pinned Export block; quick picks and search below
      append(root,
        group('Choose your document', r.select, dimsLine, custom, r.make, chips, search, r.empty),
        rules);
      this.sync(app);
      return;
    }

    const defBg = preset?.background || '#ffffff';
    const seg = state.features.segmentation;
    const rule = backgroundRule(preset);
    const bg = swatches({ label: 'Colour', colors: BACKGROUNDS, value: p.background ?? defBg, defaultValue: defBg, onChange: (v) => { p.background = v; app.changed(); } });
    const replace = toggle({
      label: 'Replace background', checked: p.replace && seg, disabled: !seg,
      hint: !seg ? 'Needs the segmentation model, which isn’t available in this build — the original background is kept.'
        : rule ? 'Off by default for this document.' : 'Cuts out the person and puts them on the colour above.',
      onChange: (v) => { p.replace = v; app.changed(); },
    });
    const ruleNote = rule && seg ? note(rule, 'warn') : null;

    const vpos = (v) => (Math.abs(v) < 0.001 ? 'Centred' : `${v > 0 ? '↑' : '↓'} ${Math.round(Math.abs(v) * 100)}%`);
    const hpos = (v) => (Math.abs(v) < 0.001 ? 'Centred' : `${v > 0 ? '→' : '←'} ${Math.round(Math.abs(v) * 100)}%`);
    r.zoom = slider({ label: 'Head size', min: 0.85, max: 1.15, step: 0.005, value: p.zoom, resetTo: 1, format: (v) => `${Math.round(v * 100)}%`, onInput: (v) => { p.zoom = v; app.changed(); } });
    r.vpos = slider({ label: 'Vertical position', min: -0.25, max: 0.25, step: 0.005, value: -p.offsetY, resetTo: 0, format: vpos, onInput: (v) => { p.offsetY = -v; app.changed(); } });
    r.hpos = slider({ label: 'Horizontal position', min: -0.25, max: 0.25, step: 0.005, value: p.offsetX, resetTo: 0, format: hpos, onInput: (v) => { p.offsetX = v; app.changed(); } });
    const tune = group('Fine-tune',
      h('p', { class: 'hint pp-drag-hint' }, 'Drag the photo to move it under the frame, scroll or pinch to resize the head, double-click to reset.'),
      r.zoom, r.vpos, r.hpos,
      toggle({ label: 'Level head', checked: p.level !== false, hint: 'Straightens a tilted head (1–15°) using the eye line.', onChange: (v) => { p.level = v; app.changed(); } }));

    r.guides = toggle({ label: 'Show guides', checked: p.guides !== false, hint: 'Crown, chin and eye lines with the allowed ranges, drawn over the photo.', onChange: (v) => { p.guides = v; app.changed({ preview: false }); } });
    r.measure = h('div', { class: 'readout pp-measure', 'aria-live': 'polite' });
    const check = group('Check', r.measure, r.guides);

    const layout = segmented({
      label: 'Output', hideLabel: true, cols: 2, value: p.layout,
      options: [{ value: 'single', label: 'Single photo' }, { value: 'sheet', label: 'Print sheet' }],
      onChange: (v) => { p.layout = v; app.changed({ panel: true }); },
    });
    const out = group('Output', layout);
    if (p.layout === 'sheet') {
      append(out,
        selectField({ label: 'Paper', value: p.paper, options: state.papers.map((x) => ({ value: x.id, label: x.name })), onChange: (v) => { p.paper = v; app.changed(); } }),
        numberField({ label: 'Copies', value: p.copies || null, placeholder: 'As many as fit', step: 1, min: 0, max: MAX_COPIES, onChange: (v) => { p.copies = Math.min(MAX_COPIES, posInt(v) || 0); app.changed(); } }),
        toggle({ label: 'Cut lines', checked: p.cutLines, onChange: (v) => { p.cutLines = v; app.changed(); } }),
      );
    }

    // most-used first: document, what to print, background; checks and fine-tuning below; the rules fold away
    append(root,
      group('Document', r.select, dimsLine, custom),
      out,
      group('Background', bg, replace, ruleNote),
      check, tune, rules,
      h('button', { type: 'button', class: 'link-btn', onclick: () => { p.on = false; p.dismissed = true; app.changed({ panel: true }); } }, 'Turn off passport framing'));
    this.sync(app);
  },

  sync(app) {
    if (r.customHint) r.customHint.hidden = Boolean(passportDims());
    const p = state.passport; // drag / wheel / keys on the photo change these too — keep the sliders in step
    r.zoom?.set(p.zoom);
    r.vpos?.set(-p.offsetY);
    r.hpos?.set(p.offsetX);
    if (!r.measure) return;
    const pp = app?.lastPreview()?.passport;
    const single = state.passport.layout === 'single';
    const rows = single ? measurements(pp) : [];
    r.guides.hidden = !single;
    r.measure.replaceChildren(...rows.map((x) => h('div', { class: 'readout-row' },
      h('span', { class: 'muted' }, x.label),
      h('span', {}, h('strong', { class: x.ok ? 'ok' : 'warn' }, x.value, x.ok ? ' ✓' : ''), x.spec ? h('span', { class: 'muted' }, `\u2002·\u2002${x.note ? '' : 'spec '}${x.spec}`) : null))));
    if (!single) append(r.measure, h('p', { class: 'readout-line' }, 'Measurements and guides show for a single photo.'));
  },
};
