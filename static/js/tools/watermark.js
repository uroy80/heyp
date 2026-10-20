// Watermark: text or a logo, on one of nine positions or tiled (mosaic). Drag the mark on the photo to move it
// between the positions; the size handle and the rotation knob work too.

import { h, append, uid } from '../dom.js';
import { state, defaults, watermarkActive } from '../state.js';
import { toolHead, group, segmented, slider, toggle, selectField, textField, swatches, note } from '../controls.js';
import { icon } from '../icons.js';
import { FONTS, POSITIONS } from '../catalog.js';

const COLORS = [
  { value: '#ffffff', label: 'White' },
  { value: '#141210', label: 'Ink' },
  { value: '#b4141b', label: 'Red' },
  { value: '#808080', label: 'Grey' },
];

let r = {};

/** 3×3 grid of radio cells for the nine positions. */
function positionGrid(value, onChange, disabled) {
  const name = uid('pos');
  const fs = h('fieldset', { class: 'pos-grid', disabled }, h('legend', { class: 'label' }, 'Position'));
  const grid = h('div', { class: 'pos-cells' });
  const inputs = POSITIONS.map(([v, label]) => {
    const input = h('input', { type: 'radio', name, value: v, checked: v === value, 'aria-label': label, onchange: () => input.checked && onChange(v) });
    grid.append(h('label', { class: 'pos-cell', title: label }, input, h('span', { 'aria-hidden': 'true' })));
    return input;
  });
  fs.append(grid);
  fs.set = (v) => inputs.forEach((i) => { i.checked = i.value === v; });
  return fs;
}

export default {
  id: 'watermark',
  title: 'Watermark',
  icon: 'watermark',
  kicker: 'Stamp & sign',

  isActive: () => watermarkActive(),
  reset() {
    const w = state.watermark;
    state.watermark = { ...defaults.watermark(), imageId: w.imageId, imageName: w.imageName, imageW: w.imageW, imageH: w.imageH };
    state.watermark.kind = 'text';
  },

  render(root, app) {
    r = {};
    const w = state.watermark;
    append(root, toolHead('Watermark', 'Stamp your name or logo on every photo — in a corner, the middle, or tiled across.', { onReset: () => app.resetTool(this), active: this.isActive() }));
    const kind = segmented({
      label: 'Watermark', cols: 2, value: w.kind,
      options: [{ value: 'text', label: 'Text', icon: 'text' }, { value: 'image', label: 'Logo', icon: 'image' }],
      onChange: (v) => { w.kind = v; app.changed({ panel: true }); },
    });
    append(root, group(null, kind));

    if (w.kind === 'text') {
      r.text = textField({ label: 'Text', value: w.text, placeholder: '© Your name 2026', onChange: (v) => { w.text = v.slice(0, 200); app.changed(); } });
      r.text.input.maxLength = 200;
      const font = selectField({ label: 'Font', value: w.font, options: FONTS.map((f) => ({ value: f.id, label: f.label })), onChange: (v) => { w.font = v; app.changed(); } });
      const color = swatches({ label: 'Colour', colors: COLORS, value: w.color, defaultValue: '#ffffff', onChange: (v) => { w.color = v; app.changed(); } });
      r.size = slider({ label: 'Size', min: 0.01, max: 0.3, step: 0.005, value: w.size, resetTo: 0.05, format: pct, onInput: (v) => { w.size = v; app.changed(); } });
      append(root, group('Text', r.text, font, color, r.size));
    } else {
      r.file = h('input', { type: 'file', accept: 'image/png,image/webp,image/svg+xml,image/jpeg,image/gif,image/avif,.png,.webp,.svg,.jpg,.jpeg', hidden: true, onchange: () => pickLogo(app, r.file.files?.[0]) });
      const pick = h('button', { type: 'button', class: 'btn btn-icon-text', onclick: () => { r.file.value = ''; r.file.click(); } }, icon('upload', 16), h('span', {}, w.imageId ? 'Replace logo…' : 'Choose logo…'));
      const logo = w.imageId
        ? h('div', { class: 'logo-chip' },
          h('img', { src: app.thumbUrl(w.imageId), alt: '', onerror: () => app.auxExpired?.('watermark') }),
          h('span', { class: 'logo-name', title: w.imageName }, w.imageName || 'Logo'),
          h('button', { type: 'button', class: 'chip-x', 'aria-label': 'Remove logo', title: 'Remove', onclick: () => { app.dropAux(w.imageId); Object.assign(w, { imageId: null, imageName: '', imageW: 0, imageH: 0 }); app.changed({ panel: true }); } }, icon('x', 12)))
        : null;
      r.scale = slider({ label: 'Size', min: 0.03, max: 1, step: 0.01, value: w.imageScale, resetTo: 0.25, format: pct, onInput: (v) => { w.imageScale = v; app.changed(); } });
      append(root, group('Logo', logo, h('div', { class: 'btn-row' }, pick), r.file,
        h('p', { class: 'hint' }, 'PNG or SVG with transparency works best. Size is a share of the photo’s width.'), r.scale));
    }

    r.opacity = slider({ label: 'Opacity', min: 0.05, max: 1, step: 0.01, value: w.opacity, resetTo: 0.6, format: pct, onInput: (v) => { w.opacity = v; app.changed(); } });
    r.rotation = slider({ label: 'Rotation', min: -180, max: 180, step: 1, value: w.rotation, resetTo: 0, format: (v) => `${v}°`, onInput: (v) => { w.rotation = v; app.changed(); } });
    r.mosaic = toggle({ label: 'Tile across the photo (mosaic)', checked: w.mosaic, onChange: (v) => { w.mosaic = v; app.changed({ panel: true }); } });
    r.pos = positionGrid(w.position, (v) => { w.position = v; app.changed({ panel: true }); }, w.mosaic);
    r.custom = h('p', { class: 'hint', hidden: w.mosaic || w.position !== 'custom' }, 'Placed by hand on the photo. Pick a cell to snap it back to a corner, edge or the centre.');
    r.margin = slider({ label: 'Margin', min: 0, max: 0.2, step: 0.005, value: w.margin, resetTo: 0.03, format: pct, onInput: (v) => { w.margin = v; app.changed(); } });
    append(root,
      group('Placement', r.mosaic, r.pos, r.custom, r.margin),
      group('Look', r.opacity, r.rotation),
      h('p', { class: 'hint' }, 'Drag the watermark on the photo to place it anywhere (it snaps to the nine positions), drag a corner to resize it, the knob to turn it. The same mark goes on every photo, at the same relative place.'));
    if (!app.hasFiles()) append(root, note('Add photos to see the watermark on them.', 'info'));
  },

  sync() {
    if (!r.opacity) return;
    const w = state.watermark;
    r.pos?.set(w.position);
    if (r.custom) r.custom.hidden = w.mosaic || w.position !== 'custom';
    r.rotation.set(w.rotation);
    r.size?.set(w.size);
    r.scale?.set(w.imageScale);
  },
};

const pct = (v) => `${Math.round(v * 1000) / 10}%`;

async function pickLogo(app, file) {
  if (!file) return;
  const info = await app.uploadAux(file);
  if (!info) return;
  const w = state.watermark;
  if (w.imageId && w.imageId !== info.id) app.dropAux(w.imageId);
  Object.assign(w, { imageId: info.id, imageName: info.name, imageW: info.width || 0, imageH: info.height || 0, kind: 'image' });
  app.changed({ panel: true });
}
