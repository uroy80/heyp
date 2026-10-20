// Background: remove (transparent) or replace with a colour. v2 servers cut out any subject (BiRefNet) or
// people only (U²-Net); older ones only people.

import { h, append } from '../dom.js';
import { state, ALPHA_FORMATS, FORMAT_LABEL, passportActive, setBackgroundMode, resetBackground } from '../state.js';
import { toolHead, group, segmented, swatches, note } from '../controls.js';

const COLORS = [
  { value: '#ffffff', label: 'White' },
  { value: '#f2f2f2', label: 'Off-white' },
  { value: '#dbe9f7', label: 'Light blue' },
  { value: '#e5e7eb', label: 'Light grey' },
  { value: '#18181b', label: 'Near black' },
];

let r = {};

export default {
  id: 'background',
  title: 'Background',
  icon: 'background',

  isActive: () => state.background.mode !== 'off',
  reset(app) {
    const msg = resetBackground();
    if (msg) app?.toast(msg);
  },

  render(root, app) {
    r = {};
    const b = state.background;
    const seg = state.features.segmentation;
    append(root, toolHead('Background', null, { onReset: () => app.resetTool(this), active: this.isActive() }));
    if (!seg) {
      append(root, note('Not available on this server.', 'warn'));
      return;
    }
    const mode = segmented({
      label: 'Background', cols: 3, value: b.mode, tip: app.copy?.().bgHint,
      options: [{ value: 'off', label: 'Keep' }, { value: 'remove', label: 'Remove' }, { value: 'replace', label: 'Replace' }],
      onChange: (v) => {
        const msg = setBackgroundMode(v);
        app.changed({ panel: true });
        if (msg) app.toast(msg);
      },
    });
    append(root, group(null, mode));
    if (state.features.matting && b.mode !== 'off') {
      append(root, group(null, segmented({
        label: 'Subject', cols: 3, value: b.engine || 'auto',
        options: [{ value: 'auto', label: 'Auto' }, { value: 'object', label: 'Any object' }, { value: 'person', label: 'Person' }],
        onChange: (v) => { b.engine = v; app.changed(); },
        tip: 'Auto finds any subject: pets, products, people. Person is faster, for portraits.',
      })));
    }
    if (b.mode === 'replace') {
      append(root, group(null, swatches({ label: 'Colour', colors: COLORS, value: b.color, onChange: (v) => { b.color = v; app.changed(); } })));
    } else if (b.mode === 'remove') {
      r.alpha = note('', 'info');
      append(root, r.alpha);
    }
    if (passportActive() && state.passport.replace) {
      append(root, note('Passport already replaces the background.', 'info'));
    }
    this.sync();
  },

  sync() {
    if (!r.alpha) return;
    const fmt = state.output.format;
    const ok = ALPHA_FORMATS.has(fmt);
    r.alpha.hidden = ok;
    r.alpha.querySelector('span').textContent = ok ? '' : `${FORMAT_LABEL[fmt] || fmt} can’t keep transparency — choose PNG or WebP.`;
  },
};
