// Blur faces, licence plates and any area: suggested boxes from the detectors (/api/detect), plus boxes drawn
// on the photo. Boxes belong to each photo; the style (blur / pixelate / solid) applies to all.

import { h, append } from '../dom.js';
import { state, defaults, activeFile, activeRegions, PIXELATE_MIN } from '../state.js';
import { toolHead, group, segmented, slider, swatches, note } from '../controls.js';
import { icon } from '../icons.js';

const COLORS = [
  { value: '#141210', label: 'Black' },
  { value: '#ffffff', label: 'White' },
  { value: '#808080', label: 'Grey' },
];
const KIND_LABEL = { face: 'Face', plate: 'Plate', manual: 'Area' };

/** The slider starts at PIXELATE_MIN for pixelate (img-06); blur keeps the full range — it is also used just to soften. */
export { PIXELATE_MIN };
export const minStrength = (mode) => (mode === 'pixelate' ? PIXELATE_MIN : 0.1);

/** What the styles really do — Solid is the only one that removes the detail by construction (img-06, UIH-12). */
export const BLUR_HINT = 'The style applies to every photo. Blur and pixelate hide detail from casual viewers but are not guaranteed to be irreversible, especially at low strength. Use Solid for text, licence plates or anything that must stay secret.';

let r = {};

/** Readable list rows: "Face 1 · 97 %". */
export function regionRows(regions) {
  const n = { face: 0, plate: 0, manual: 0 };
  return (regions || []).map((g) => {
    const k = KIND_LABEL[g.source] ? g.source : 'manual';
    n[k] += 1;
    const score = g.score != null && g.source !== 'manual' ? ` · ${Math.round(g.score * 100)} %` : '';
    return { id: g.id, label: `${KIND_LABEL[k]} ${n[k]}${score}`, on: g.on !== false };
  });
}

export default {
  id: 'blur',
  title: 'Blur',
  icon: 'blur',
  kicker: 'Privacy',

  isActive: () => state.files.some((f) => activeRegions(f).length > 0),
  reset() {
    for (const f of state.files) f.regions = [];
    state.blur = defaults.blur();
  },

  render(root, app) {
    r = {};
    const b = state.blur;
    const f = activeFile();
    append(root, toolHead('Blur faces & plates', 'Hide faces, licence plates or anything else. Draw boxes on the photo, or let heyp suggest them.', { onReset: () => app.resetTool(this), active: this.isActive() }));
    if (!f) {
      append(root, note('Add a photo, then draw a box over what you want hidden.', 'info'));
      return;
    }
    const busy = app.detectBusy();
    const canFaces = state.features.face !== false;
    const canPlates = state.features.plates !== false;
    const findBtn = (what, label, iconName, ok) => h('button', {
      type: 'button', class: 'btn btn-icon-text', disabled: busy || !ok, 'aria-busy': busy === what ? 'true' : null,
      title: ok ? null : 'Not available on this server',
      onclick: () => app.detect({ what: what === 'both' ? ['faces', 'plates'].filter((k) => (k === 'faces' ? canFaces : canPlates)) : [what], all: false }),
    }, busy === what ? h('span', { class: 'spinner' }) : icon(iconName, 16), h('span', {}, label));
    r.find = h('div', { class: 'btn-row' },
      findBtn('faces', 'Find faces', 'face', canFaces),
      findBtn('plates', 'Find plates', 'target', canPlates));
    const allBtn = state.files.length > 1
      ? h('button', { type: 'button', class: 'link-btn', disabled: busy || !(canFaces || canPlates), onclick: () => app.detect({ what: ['faces', 'plates'].filter((k) => (k === 'faces' ? canFaces : canPlates)), all: true }) }, `Find faces & plates in all ${state.files.length} photos`)
      : null;
    append(root, group('Suggestions', r.find, allBtn,
      h('p', { class: 'hint' }, 'Suggestions are pre-selected: untick any you want to keep visible. Detection only finds faces — it never tells who they are.')));

    r.list = h('ul', { class: 'region-list', 'aria-label': 'Areas to hide on this photo' });
    r.clear = h('button', { type: 'button', class: 'link-btn', onclick: () => { f.regions = []; app.changed({ panel: true }); } }, 'Remove all boxes on this photo');
    append(root, group('On this photo', r.list, h('p', { class: 'hint' }, 'Drag on the photo to draw a box. Select a box to move or resize it; Delete removes it.'), r.clear));

    const mode = segmented({
      label: 'Style', cols: 3, value: b.mode,
      options: [{ value: 'blur', label: 'Blur' }, { value: 'pixelate', label: 'Pixelate' }, { value: 'solid', label: 'Solid' }],
      onChange: (v) => { b.mode = v; if (v === 'pixelate') b.strength = Math.max(PIXELATE_MIN, b.strength); app.changed({ panel: true }); },
    });
    const strength = b.mode === 'solid' ? null : slider({ label: 'Strength', min: minStrength(b.mode), max: 1, step: 0.05, value: Math.max(minStrength(b.mode), b.strength), resetTo: 0.6, format: (v) => `${Math.round(v * 100)}%`, onInput: (v) => { b.strength = v; app.changed(); } });
    const color = b.mode === 'solid' ? swatches({ label: 'Colour', colors: COLORS, value: b.color, defaultValue: '#141210', onChange: (v) => { b.color = v; app.changed(); } }) : null;
    append(root, group('Style', mode, strength, color, h('p', { class: 'hint' }, BLUR_HINT)));
    this.sync(app);
  },

  sync(app) {
    if (!r.list) return;
    const f = activeFile();
    if (!f) return;
    const rows = regionRows(f.regions);
    const sel = app.selectedItem();
    r.list.replaceChildren(...rows.map((row) => h('li', { class: `region-row${sel === row.id ? ' is-selected' : ''}` },
      h('label', { class: 'region-on' },
        h('input', { type: 'checkbox', checked: row.on, onchange: (e) => { const g = f.regions.find((x) => x.id === row.id); if (g) g.on = e.target.checked; app.changed(); } }),
        h('span', {}, row.label)),
      // picks the box on the photo (to move or resize it) — it never un-hides anything, so it isn't called "Show"
      h('button', { type: 'button', class: 'link-btn', 'aria-label': `Select ${row.label} on the photo`, onclick: () => app.selectItem(row.id) }, 'Select'),
      h('button', { type: 'button', class: 'chip-x', 'aria-label': `Remove ${row.label}`, title: 'Remove', onclick: () => { f.regions = f.regions.filter((x) => x.id !== row.id); app.changed({ panel: true }); } }, icon('x', 12)))));
    if (!rows.length) r.list.append(h('li', { class: 'region-empty muted' }, 'No boxes yet.'));
    r.clear.hidden = !rows.length;
  },
};
