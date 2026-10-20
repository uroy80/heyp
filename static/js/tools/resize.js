// Resize: pixel dimensions, percent, long edge, print size, or DPI only.

import { h, posInt, num, fmtNum, append } from '../dom.js';
import { state, defaults, activeFile, preResizeDims, passportActive, UNIT_MM } from '../state.js';
import { toolHead, segmented, slider, toggle, selectField, numberField, dimsPair, quickChips, note } from '../controls.js';

const PRESETS = [
  { label: 'Screens', items: [
    { id: '4k', label: '4K UHD — 3840×2160', w: 3840, h: 2160, fit: 'fit' },
    { id: 'fhd', label: 'Full HD — 1920×1080', w: 1920, h: 1080, fit: 'fit' },
    { id: 'hd', label: 'HD — 1280×720', w: 1280, h: 720, fit: 'fit' },
  ] },
  { label: 'Social', items: [
    { id: 'ig-sq', label: 'Instagram square — 1080×1080', w: 1080, h: 1080, fit: 'fill' },
    { id: 'ig-pt', label: 'Instagram portrait — 1080×1350', w: 1080, h: 1350, fit: 'fill' },
    { id: 'ig-st', label: 'Instagram story — 1080×1920', w: 1080, h: 1920, fit: 'fill' },
    { id: 'x', label: 'X / Twitter post — 1600×900', w: 1600, h: 900, fit: 'fill' },
    { id: 'li', label: 'LinkedIn banner — 1584×396', w: 1584, h: 396, fit: 'fill' },
    { id: 'yt', label: 'YouTube thumbnail — 1280×720', w: 1280, h: 720, fit: 'fill' },
  ] },
  { label: 'Print (300 DPI)', items: [
    { id: 'a4', label: 'A4 — 210×297 mm', print: true, w: 210, h: 297, unit: 'mm', dpi: 300, fit: 'fill' },
    { id: '4x6', label: '4×6 in photo', print: true, w: 6, h: 4, unit: 'in', dpi: 300, fit: 'fill' },
  ] },
];

const MODES = [
  { value: 'dimensions', label: 'Dimensions' },
  { value: 'percent', label: 'Percent' },
  { value: 'long_edge', label: 'Long edge' },
  { value: 'print', label: 'Print size' },
  { value: 'dpi', label: 'DPI only' },
];

const FIT = [
  { value: 'fill', label: 'Fill', title: 'Cover the box, crop the overflow' },
  { value: 'fit', label: 'Fit', title: 'Fit inside the box, keep everything' },
  { value: 'pad', label: 'Pad', title: 'Fit inside, pad to the exact size' },
  { value: 'exact', label: 'Stretch', title: 'Stretch to the exact size' },
];

let r = {};

function aspect() {
  const d = preResizeDims(activeFile());
  return d ? d.w / d.h : null;
}

/** Physical size of `px` pixels at `dpi`, as "6.67 × 5 in · 16.9 × 12.7 cm". */
export function printSize(w, hh, dpi) {
  if (!(w && hh && dpi)) return '';
  const wi = w / dpi;
  const hi = hh / dpi;
  return `${fmtNum(wi)} × ${fmtNum(hi)} in · ${fmtNum(wi * 2.54, 1)} × ${fmtNum(hi * 2.54, 1)} cm`;
}

export default {
  id: 'resize',
  title: 'Resize',
  icon: 'resize',

  // resize is ignored while Passport sets the size, so it isn't shown as an edit then
  isActive: () => state.resize.on && !passportActive(),
  reset() {
    state.resize = defaults.resize();
  },

  render(root, app) {
    const rs = state.resize;
    r = {};
    const touch = () => { rs.on = true; app.changed(); };
    const dims = preResizeDims(activeFile());

    const presetSel = selectField({
      label: 'Presets', placeholder: 'Choose a preset…', value: '',
      groups: PRESETS.map((g) => ({ label: g.label, options: g.items.map((i) => ({ value: i.id, label: i.label })) })),
      onChange: (id) => {
        const p = PRESETS.flatMap((g) => g.items).find((i) => i.id === id);
        if (!p) return;
        rs.on = true;
        rs.lock = false;
        rs.fit = p.fit;
        if (p.print) {
          const portrait = dims ? dims.h >= dims.w : true;
          const [a, b] = [Math.min(p.w, p.h), Math.max(p.w, p.h)];
          Object.assign(rs, { mode: 'print', unit: p.unit, dpi: p.dpi, printW: portrait ? a : b, printH: portrait ? b : a });
        } else {
          Object.assign(rs, { mode: 'dimensions', width: p.w, height: p.h });
        }
        app.changed({ panel: true });
      },
    });

    const mode = segmented({
      label: 'Mode', options: MODES, value: rs.mode, cols: 3,
      onChange: (v) => { rs.mode = v; rs.on = true; app.changed({ panel: true }); },
    });

    const body = h('div', { class: 'stack' });
    const ph = (v) => (v ? String(v) : '');

    if (rs.mode === 'dimensions') {
      const pair = dimsPair({
        w: rs.width, h: rs.height, unit: 'px', locked: rs.lock, step: 1,
        wPlaceholder: ph(dims?.w), hPlaceholder: ph(dims?.h),
        onW: (v) => {
          rs.width = v;
          rs.driver = 'width';
          if (rs.lock) { rs.height = v && aspect() ? Math.max(1, Math.round(v / aspect())) : null; pair.h.set(rs.height); }
          touch();
        },
        onH: (v) => {
          rs.height = v;
          rs.driver = 'height';
          if (rs.lock) { rs.width = v && aspect() ? Math.max(1, Math.round(v * aspect())) : null; pair.w.set(rs.width); }
          touch();
        },
        onLock: (v) => { rs.lock = v; app.changed({ panel: true }); },
      });
      append(body, pair);
      if (rs.lock) {
        append(body, h('p', { class: 'hint' }, state.files.length > 1 ? 'The other side follows each photo’s own aspect ratio.' : 'The other side follows the aspect ratio.'));
      } else {
        r.fit = segmented({ label: 'When the shape differs', options: FIT, value: rs.fit, cols: 4, onChange: (v) => { rs.fit = v; syncEnlargeHint(); touch(); } });
        append(body, r.fit);
      }
    } else if (rs.mode === 'percent') {
      const s = slider({ label: 'Scale', min: 1, max: 200, step: 1, value: num(rs.percent) ?? 50, resetTo: 100, format: (v) => `${v}%`, onInput: (v) => { rs.percent = v; touch(); } });
      append(body, s, quickChips([25, 50, 75, 150, 200].map((v) => ({ value: v, label: `${v}%`, active: rs.percent === v })), (v) => { rs.percent = v; s.set(v); touch(); }, { label: 'Quick percentages' }));
    } else if (rs.mode === 'long_edge') {
      const f = numberField({ label: 'Long edge', value: rs.longEdge, placeholder: ph(dims && Math.max(dims.w, dims.h)), suffix: 'px', step: 1, min: 1, onChange: (v) => { rs.longEdge = v; touch(); } });
      append(body, f, quickChips([1080, 1600, 2048, 2560, 3840].map((v) => ({ value: v, label: String(v), active: rs.longEdge === v })), (v) => { rs.longEdge = v; f.input.value = v; touch(); }, { label: 'Common long-edge sizes' }));
    } else if (rs.mode === 'print') {
      const unit = segmented({ label: 'Unit', cols: 3, value: rs.unit, options: [{ value: 'in', label: 'in' }, { value: 'cm', label: 'cm' }, { value: 'mm', label: 'mm' }], onChange: (v) => {
        const k = UNIT_MM[rs.unit] / UNIT_MM[v];
        const conv = (x) => (num(x) ? Number((num(x) * k).toFixed(v === 'mm' ? 1 : 3)) : x);
        rs.printW = conv(rs.printW);
        rs.printH = conv(rs.printH);
        rs.unit = v;
        touch();
        app.refreshPanel();
      } });
      const dpi = posInt(rs.dpi) || 300;
      const physPh = (px) => (px ? fmtNum((px / dpi) * (25.4 / UNIT_MM[rs.unit]), 2) : '');
      const pair = dimsPair({
        w: rs.printW, h: rs.printH, unit: rs.unit, locked: rs.lock,
        wPlaceholder: physPh(dims?.w), hPlaceholder: physPh(dims?.h),
        onW: (v) => {
          rs.printW = v;
          rs.printDriver = 'width';
          if (rs.lock) { rs.printH = v && aspect() ? Number((v / aspect()).toFixed(3)) : null; pair.h.set(rs.printH); }
          touch();
        },
        onH: (v) => {
          rs.printH = v;
          rs.printDriver = 'height';
          if (rs.lock) { rs.printW = v && aspect() ? Number((v * aspect()).toFixed(3)) : null; pair.w.set(rs.printW); }
          touch();
        },
        onLock: (v) => { rs.lock = v; app.changed({ panel: true }); },
      });
      append(body, unit, pair, dpiField(app, touch));
      if (!rs.lock) {
        r.fit = segmented({ label: 'When the shape differs', options: FIT, value: rs.fit, cols: 4, onChange: (v) => { rs.fit = v; touch(); } });
        append(body, r.fit);
      }
    } else if (rs.mode === 'dpi') {
      append(body, dpiField(app, touch), note('Pixels stay exactly the same — only the print size recorded in the file changes.', 'info'));
    }

    r.enlarge = toggle({ label: 'Don’t enlarge', checked: rs.noEnlarge, hint: enlargeHint(), onChange: (v) => { rs.noEnlarge = v; touch(); } });
    r.enlarge.hidden = rs.mode === 'dpi';
    r.readout = h('div', { class: 'readout', 'aria-live': 'polite' });

    const fs = h('fieldset', { class: 'plain-fieldset', disabled: passportActive() },
      h('legend', { class: 'sr-only' }, 'Resize options'),
      presetSel, mode, body, r.enlarge);
    append(root,
      toolHead('Resize', 'Set pixel dimensions, print size or DPI. Applies to every file.', { onReset: () => app.resetTool(this), active: this.isActive() }),
      passportActive() ? note('Passport framing sets the size. Turn off Passport to resize manually.', 'warn') : null,
      fs,
      r.readout,
    );
    this.sync(app);
  },

  sync(app) {
    if (!r.readout) return;
    const p = app.lastPreview();
    const rs = state.resize;
    r.readout.replaceChildren();
    if (!p) return;
    const dpi = p.dpi?.[0] || (rs.on && rs.mode !== 'dimensions' ? posInt(rs.dpi) : null);
    append(r.readout,
      h('div', { class: 'readout-row' }, h('span', { class: 'muted' }, 'Output'), h('strong', {}, `${p.width} × ${p.height} px`)),
      dpi ? h('div', { class: 'readout-row' }, h('span', { class: 'muted' }, `Prints at ${Math.round(dpi)} DPI`), h('span', {}, printSize(p.width, p.height, dpi))) : null,
    );
  },
};

/** Pad keeps the exact canvas even when the photo is not enlarged (PLAN §3 resize, upscale=false). */
function padding() {
  const rs = state.resize;
  return !rs.lock && rs.fit === 'pad' && (rs.mode === 'dimensions' || rs.mode === 'print');
}

function enlargeHint() {
  return padding()
    ? 'Photo is not enlarged; the canvas is still padded to the exact size.'
    : 'Leave photos that are already smaller untouched.';
}

function syncEnlargeHint() {
  const hint = r.enlarge?.querySelector('.hint');
  if (hint) hint.textContent = enlargeHint();
}

function dpiField(app, touch) {
  const rs = state.resize;
  const f = numberField({ label: 'Resolution', value: rs.dpi, suffix: 'DPI', step: 1, min: 1, max: 2400, onChange: (v) => { rs.dpi = v; touch(); } });
  return h('div', { class: 'stack-sm' }, f,
    quickChips([72, 150, 300, 600].map((v) => ({ value: v, label: String(v), active: posInt(rs.dpi) === v })), (v) => { rs.dpi = v; f.input.value = v; touch(); }, { label: 'Common DPI values' }));
}
