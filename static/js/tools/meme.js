// Meme: top and bottom captions in the classic outlined poster type, on your photo or a blank canvas.

import { h, append } from '../dom.js';
import { state, defaults, memeActive } from '../state.js';
import { toolHead, group, slider, toggle, selectField, swatches } from '../controls.js';
import { icon } from '../icons.js';
import { FONTS, BLANK_CANVASES } from '../catalog.js';

const COLORS = [
  { value: '#ffffff', label: 'White' },
  { value: '#141210', label: 'Black' },
  { value: '#ffd60a', label: 'Yellow' },
];
const OUTLINES = [
  { value: '#000000', label: 'Black' },
  { value: '#ffffff', label: 'White' },
  { value: 'none', label: 'None' },
];
const MAX_CHARS = 300;

let r = {};

function captionField(label, value, onChange) {
  const id = `meme-${label.toLowerCase()}`;
  const ta = h('textarea', {
    id, class: 'input textarea', rows: 2, maxLength: MAX_CHARS, spellcheck: 'true', placeholder: label === 'Top' ? 'When the code works first time' : 'Nobody: …',
    oninput: () => onChange(ta.value.slice(0, MAX_CHARS)),
  });
  ta.value = value;
  const wrap = h('div', { class: 'field' }, h('label', { htmlFor: id, class: 'label' }, `${label} text`), ta);
  wrap.input = ta;
  return wrap;
}

export default {
  id: 'meme',
  title: 'Meme',
  icon: 'meme',

  isActive: () => memeActive(),
  reset() {
    state.meme = defaults.meme();
  },

  render(root, app) {
    r = {};
    const m = state.meme;
    append(root, toolHead('Meme', null, { onReset: () => app.resetTool(this), active: this.isActive() }));
    r.top = captionField('Top', m.top, (v) => { m.top = v; app.changed(); });
    r.bottom = captionField('Bottom', m.bottom, (v) => { m.bottom = v; app.changed(); });
    append(root, group(null, r.top, r.bottom,
      toggle({ label: 'ALL CAPS', checked: m.uppercase, onChange: (v) => { m.uppercase = v; app.changed(); } })));

    const font = selectField({ label: 'Font', value: m.font, options: FONTS.map((f) => ({ value: f.id, label: f.label })), onChange: (v) => { m.font = v; app.changed(); } });
    const auto = toggle({ label: 'Auto-fit text', checked: m.autoSize, onChange: (v) => { m.autoSize = v; app.changed({ panel: true }); } });
    const size = m.autoSize ? null : slider({ label: 'Text size', min: 0.03, max: 0.25, step: 0.005, value: m.size, resetTo: 0.1, format: (v) => `${Math.round(v * 1000) / 10}%`, onInput: (v) => { m.size = v; app.changed(); } });
    const color = swatches({ label: 'Text colour', colors: COLORS, value: m.color, defaultValue: '#ffffff', onChange: (v) => { m.color = v; app.changed(); } });
    const outline = swatches({ label: 'Outline', colors: OUTLINES.map((o) => ({ ...o, value: o.value })), allowCustom: false, value: m.outline, defaultValue: '#000000', onChange: (v) => { m.outline = v; app.changed(); } });
    // "None" is a word, not a colour: draw it as a struck-through paper chip
    outline.querySelectorAll('.swatch').forEach((s, i) => { if (OUTLINES[i].value === 'none') { s.style.background = ''; s.classList.add('swatch-none'); } });
    append(root, group('Type', font, auto, size, color, outline));

    r.blank = h('div', { class: 'btn-row' }, BLANK_CANVASES.map((c) => h('button', {
      type: 'button', class: 'btn btn-icon-text', title: `${c.w} × ${c.h} px`,
      onclick: () => app.newBlank(c, '#ffffff'),
    }, icon('image', 16), h('span', {}, c.label))));
    append(root, group('Blank canvas', r.blank));
  },

  sync() {
    if (!r.top) return;
    const m = state.meme;
    if (document.activeElement !== r.top.input && r.top.input.value !== m.top) r.top.input.value = m.top;
    if (document.activeElement !== r.bottom.input && r.bottom.input.value !== m.bottom) r.bottom.input.value = m.bottom;
  },
};
