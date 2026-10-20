// Background: remove (transparent) or replace with a colour, via person segmentation.

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
    append(root, toolHead('Background', 'Cut out the person and remove or replace what’s behind them. Works best on portraits.', { onReset: () => app.resetTool(this), active: this.isActive() }));
    if (!seg) {
      append(root, note('Background removal needs the person-segmentation model, which isn’t available in this heyp build.', 'warn'));
      return;
    }
    const mode = segmented({
      label: 'Background', cols: 3, value: b.mode,
      options: [{ value: 'off', label: 'Keep' }, { value: 'remove', label: 'Remove' }, { value: 'replace', label: 'Replace' }],
      onChange: (v) => {
        const msg = setBackgroundMode(v);
        app.changed({ panel: true });
        if (msg) app.toast(msg);
      },
    });
    append(root, group(null, mode));
    if (b.mode === 'replace') {
      append(root, group(null, swatches({ label: 'Colour', colors: COLORS, value: b.color, onChange: (v) => { b.color = v; app.changed(); } })));
    } else if (b.mode === 'remove') {
      r.alpha = note('', 'info');
      append(root, r.alpha);
    }
    if (passportActive() && state.passport.replace) {
      append(root, note('Passport framing already replaces the background — you usually don’t need both.', 'info'));
    }
    append(root, h('p', { class: 'hint' }, 'Runs a segmentation model on this machine. The first preview can take a few seconds.'));
    this.sync();
  },

  sync() {
    if (!r.alpha) return;
    const fmt = state.output.format;
    const ok = ALPHA_FORMATS.has(fmt);
    r.alpha.querySelector('span').textContent = ok
      ? `Transparent background — exporting as ${FORMAT_LABEL[fmt]} keeps it.`
      : `${FORMAT_LABEL[fmt] || fmt} can’t store transparency — the background will be filled. Choose PNG or WebP to keep it.`;
  },
};
