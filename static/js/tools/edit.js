// Edit: rotate / flip / straighten and tone adjustments (whole batch).

import { h, fmtNum, append } from '../dom.js';
import { state, rotateQuarter, flip } from '../state.js';
import { toolHead, group, slider, toggle, iconButton } from '../controls.js';

const TONE = [
  ['brightness', 'Brightness'],
  ['contrast', 'Contrast'],
  ['saturation', 'Saturation'],
  ['sharpness', 'Sharpness'],
];

const pct = (v) => {
  const d = Math.round((v - 1) * 100);
  return d === 0 ? '0' : `${d > 0 ? '+' : ''}${d}`;
};

let r = {};

export default {
  id: 'edit',
  title: 'Edit',
  icon: 'edit',

  isActive() {
    const e = state.edit;
    return Boolean(e.quarter || e.flipH || e.flipV || e.straighten || e.grayscale || e.autoContrast ||
      e.brightness !== 1 || e.contrast !== 1 || e.saturation !== 1 || e.sharpness !== 1);
  },
  reset() {
    const e = state.edit;
    while (e.quarter) rotateQuarter(e.quarter === 270 ? 1 : -1);
    if (e.flipH) flip('h');
    if (e.flipV) flip('v');
    Object.assign(e, { straighten: 0, cropEdges: true, brightness: 1, contrast: 1, saturation: 1, sharpness: 1, grayscale: false, autoContrast: false });
  },

  render(root, app) {
    r = {};
    const e = state.edit;
    const orient = h('div', { class: 'btn-row btn-row-4' },
      iconButton({ label: 'Rotate left', iconName: 'rotateLeft', onClick: () => { rotateQuarter(-1); app.changed({ panel: true }); } }),
      iconButton({ label: 'Rotate right (R)', iconName: 'rotateRight', onClick: () => { rotateQuarter(1); app.changed({ panel: true }); } }),
      iconButton({ label: 'Flip horizontal', iconName: 'flipH', pressed: e.flipH, onClick: () => { flip('h'); app.changed({ panel: true }); } }),
      iconButton({ label: 'Flip vertical', iconName: 'flipV', pressed: e.flipV, onClick: () => { flip('v'); app.changed({ panel: true }); } }));

    const straighten = slider({
      label: 'Straighten', min: -45, max: 45, step: 0.5, value: e.straighten, resetTo: 0,
      format: (v) => `${v > 0 ? '+' : ''}${fmtNum(v, 1)}°`,
      onInput: (v) => { e.straighten = v; app.changed(); },
    });
    const cropEdges = toggle({ label: 'Crop empty corners', checked: e.cropEdges, onChange: (v) => { e.cropEdges = v; app.changed(); } });

    const tone = TONE.map(([k, label]) => slider({
      label, min: 0, max: 2, step: 0.05, value: e[k], resetTo: 1, format: pct,
      onInput: (v) => { e[k] = v; app.changed(); },
    }));

    append(root,
      toolHead('Edit', 'Rotate, flip, straighten and adjust tone. Applies to every file.', { onReset: () => app.resetTool(this), active: this.isActive() }),
      group('Orientation', orient, h('p', { class: 'hint' }, `Now: ${e.quarter ? `${e.quarter}° clockwise` : 'as shot'}${e.flipH || e.flipV ? ', mirrored' : ''}. EXIF orientation is already applied.`)),
      group('Straighten', straighten, cropEdges),
      group('Tone', ...tone,
        toggle({ label: 'Auto contrast', checked: e.autoContrast, onChange: (v) => { e.autoContrast = v; app.changed(); } }),
        toggle({ label: 'Black & white', checked: e.grayscale, onChange: (v) => { e.grayscale = v; app.changed(); } })),
    );
  },
};
