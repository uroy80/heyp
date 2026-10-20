// Crop panel. The interactive box lives on the canvas (cropbox.js); this panel drives
// ratio, auto modes (trim / smart / face) and shows the box size.

import { h, append } from '../dom.js';
import { state, activeFile, baseDims, passportActive, ratioLabel, cropPx, CROP_RATIOS } from '../state.js';
import { toolHead, group, segmented, slider, numberField, note, infoTip } from '../controls.js';
import { icon } from '../icons.js';

const RATIO_LABEL = { free: 'Free', original: 'Original' };

let r = {};

export default {
  id: 'crop',
  title: 'Crop',
  icon: 'crop',

  // a crop is ignored while Passport frames the photo, so it isn't shown as an edit then
  isActive: () => Boolean(activeFile()?.crop) && !passportActive(),
  reset() {
    const f = activeFile();
    if (f) f.crop = null;
    state.cropRatio = 'free';
  },

  render(root, app) {
    r = {};
    const f = activeFile();
    const crop = f?.crop;
    const pp = passportActive();

    append(root, toolHead('Crop', 'Set per photo.', { onReset: () => app.resetTool(this), active: this.isActive() }));
    if (pp) {
      append(root, note('Passport sets the crop — turn it off to crop.', 'warn'));
      return;
    }
    if (!f) {
      append(root, note('Add a photo first.', 'info'));
      return;
    }

    r.ratio = segmented({
      label: 'Aspect ratio', cols: 3, value: state.cropRatio, size: 'sm',
      // a lock that isn't a chip (5:4 after rotating a 4:5 crop) gets its own cell, so it stays visible and re-selectable
      options: [...CROP_RATIOS, ...(CROP_RATIOS.includes(state.cropRatio) ? [] : [state.cropRatio])].map((v) => ({ value: v, label: RATIO_LABEL[v] || v })),
      onChange: (v) => app.crop.setRatio(v),
    });

    const autoBtn = (kind, label, iconName, title) => h('button', {
      type: 'button', class: 'btn btn-icon-text', title,
      'aria-pressed': String(isAuto(crop, kind)),
      onclick: () => app.crop.auto(kind),
    }, icon(iconName, 16), h('span', {}, label));
    const faceTitle = state.features.face ? 'Centre the crop on the largest face (uses the selected ratio)' : 'Face detection is unavailable — falls back to Smart';
    r.auto = h('div', { class: 'btn-row' },
      autoBtn('trim', 'Auto trim', 'trim', 'Remove uniform borders around the photo'),
      autoBtn('smart', 'Smart', 'sparkle', 'Keep the most detailed region (uses the selected ratio)'),
      autoBtn('face', 'Face', 'face', faceTitle));

    r.size = h('p', { class: 'readout-line', 'aria-live': 'polite' });
    const parts = [
      group(null, r.ratio),
      group({ title: 'Auto', tip: 'Smart and Face use the selected ratio (the box’s shape with Free, else 1:1).' }, r.auto),
    ];

    if (crop?.kind === 'trim') {
      const tol = slider({ label: 'Tolerance', min: 0, max: 100, step: 1, value: crop.tolerance, resetTo: 12, onInput: (v) => { crop.tolerance = v; app.changed(); } });
      const pad = numberField({ label: 'Padding', value: crop.padding, suffix: 'px', step: 1, min: 0, max: 2000, onChange: (v) => { crop.padding = Math.max(0, Math.round(v || 0)); app.changed(); } });
      parts.push(group('Trim', tol, pad, manualLink(app)));
    } else if (crop?.kind === 'aspect') {
      parts.push(group(null, manualLink(app)));
    }

    r.resetBtn = h('button', { type: 'button', class: 'btn', onclick: () => app.resetTool(this), disabled: !crop }, 'Reset crop');
    r.applyBtn = state.files.length > 1 ? h('button', { type: 'button', class: 'btn', onclick: () => app.crop.applyAll(), disabled: !crop }, `Apply to all ${state.files.length}`) : null;
    const actions = h('div', { class: 'btn-row' }, r.resetBtn, r.applyBtn);

    append(root, ...parts, h('div', { class: 'readout-tip' }, r.size,
      infoTip('Drag outside the box to draw a new one. Keys: arrows nudge, Shift ×10, Alt resizes, Esc reverts.', { label: 'Crop box tips' })), actions);
    this.sync(app);
  },

  sync() {
    if (!r.size) return;
    const f = activeFile();
    if (!f) return;
    r.ratio.set(state.cropRatio);
    const c = f.crop;
    // a box drawn after the panel rendered enables Reset / Apply to all without a re-render
    r.resetBtn.disabled = !c;
    if (r.applyBtn) r.applyBtn.disabled = !c;
    const { w, h: hh } = baseDims(f);
    let text = c?.kind === 'trim' ? 'Auto trim' : c?.kind === 'aspect' ? `${c.anchor === 'face' ? 'Face' : 'Smart'} crop · ${ratioLabel(c.ratio)}` : '';
    if (!c || c.kind === 'box') {
      const px = c ? cropPx(c, w, hh) : { w, h: hh };
      text = `${px.w} × ${px.h} px${c ? ` of ${w} × ${hh}` : ' · full image'}`;
    }
    if (r.size.textContent !== text) r.size.textContent = text; // live region: only announce real changes
  },
};

function isAuto(c, kind) {
  if (!c) return false;
  if (kind === 'trim') return c.kind === 'trim';
  return c.kind === 'aspect' && c.anchor === kind;
}

function manualLink(app) {
  return h('button', { type: 'button', class: 'link-btn', onclick: () => app.crop.manual() }, 'Adjust by hand');
}
