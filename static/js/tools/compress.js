// Compress: hit a target file size (max / optional min KB) in JPG, WebP or AVIF.

import { h, num, fmtBytes, append } from '../dom.js';
import { state, defaults, FORMAT_LABEL, LOSSY, fmtKb } from '../state.js';
import { toolHead, group, segmented, numberField, quickChips, note } from '../controls.js';

const CHIPS = [20, 50, 100, 200, 500, 1024];
const FORMATS = ['jpeg', 'webp', 'avif'];

let r = {};

export default {
  id: 'compress',
  title: 'Compress',
  icon: 'compress',

  isActive: () => state.compress.on,
  reset() {
    state.compress = defaults.compress();
  },

  render(root, app) {
    r = {};
    const c = state.compress;
    const set = (k, v) => {
      c[k] = v;
      c.on = num(c.maxKb) > 0 || num(c.minKb) > 0;
      app.changed();
    };
    r.max = numberField({ label: 'Max size', value: c.maxKb, placeholder: 'e.g. 200', suffix: 'KB', step: 1, min: 1, onChange: (v) => set('maxKb', v) });
    r.min = numberField({ label: 'Min size (optional)', value: c.minKb, placeholder: 'none', suffix: 'KB', step: 1, min: 1, onChange: (v) => set('minKb', v) });
    const chips = quickChips(CHIPS.map((v) => ({ value: v, label: fmtKb(v), active: num(c.maxKb) === v })), (v) => {
      r.max.input.value = v;
      set('maxKb', v);
    }, { label: 'Common size limits' });

    r.format = segmented({
      label: 'Format', cols: 3, value: state.output.format,
      options: FORMATS.map((f) => ({ value: f, label: FORMAT_LABEL[f], disabled: !app.canOutput(f) })),
      onChange: (v) => app.setFormat(v),
    });
    r.fmtNote = note('', 'info');
    r.rangeNote = note('The minimum is larger than the maximum, so it is ignored.', 'warn');
    r.result = h('div', { class: 'readout', 'aria-live': 'polite' });

    append(root,
      toolHead('Compress', 'Shrink photos to fit an upload limit — forms, job portals, email.', { onReset: () => app.resetTool(this), active: this.isActive() }),
      group(null, r.max, chips, r.min, r.rangeNote, h('p', { class: 'hint' }, 'The minimum is best effort: heyp raises quality but never pads files with junk.')),
      group(null, r.format, r.fmtNote),
      r.result,
    );
    this.sync(app);
  },

  sync(app) {
    if (!r.result) return;
    const fmt = state.output.format;
    r.format.set(fmt);
    r.fmtNote.hidden = LOSSY.has(fmt);
    r.fmtNote.querySelector('span').textContent = `Exporting as ${FORMAT_LABEL[fmt] || fmt}: heyp reduces colours and size instead of quality. JPG, WebP or AVIF compress far better.`;
    r.result.replaceChildren();
    const p = app.lastPreview();
    const c = state.compress;
    r.rangeNote.hidden = !(num(c.minKb) > 0 && num(c.maxKb) > 0 && num(c.minKb) > num(c.maxKb));
    if (!p || !c.on) return;
    const max = num(c.maxKb);
    const min = num(c.minKb);
    const kb = p.size_bytes / 1024;
    const okMax = !(max > 0) || kb <= max + 1e-9;
    const okMin = !(min > 0) || kb >= min - 1e-9;
    append(r.result,
      h('div', { class: 'readout-row' }, h('span', { class: 'muted' }, 'Result'),
        h('strong', { class: okMax && okMin ? 'ok' : 'warn' }, `${fmtBytes(p.size_bytes)}`, okMax && okMin ? ' ✓' : '')),
      p.encode?.quality != null ? h('div', { class: 'readout-row' }, h('span', { class: 'muted' }, 'Quality used'), h('span', {}, `q ${p.encode.quality}`)) : null,
      h('div', { class: 'readout-row' }, h('span', { class: 'muted' }, 'Dimensions'),
        h('span', {}, `${p.width} × ${p.height} px`, p.encode?.scale < 0.999 ? h('span', { class: 'muted' }, ` · scaled to ${Math.round(p.encode.scale * 100)} %`) : null)),
    );
  },
};
