// Convert: the big output-format grid (bound to Export › Format), PDF combine + advanced encoder options.
// Quality and metadata live only in Export (SPEC §10.1).

import { h, append } from '../dom.js';
import { state, defaults, ALPHA_FORMATS, FORMAT_LABEL, activeFile, colorName } from '../state.js';
import { toolHead, group, segmented, toggle, swatches, selectField, textField, note } from '../controls.js';

// every Export choice has a cell here, so the grid and the Export select never disagree
const FORMATS = ['jpeg', 'png', 'webp', 'avif', 'heic', 'tiff', 'pdf', 'gif', 'bmp', 'original'];
const ENCODER_KEYS = ['format', 'lossless', 'combinePdf', 'colorspace', 'background', 'progressive', 'chroma', 'filename'];
const FILL = [
  { value: '#ffffff', label: 'White' },
  { value: '#000000', label: 'Black' },
  { value: '#f2f2f2', label: 'Off-white' },
];

let r = {};

export default {
  id: 'convert',
  title: 'Convert',
  icon: 'convert',

  /** Edited = format or encoder options changed (Export's quality and metadata switches don't count). */
  isActive() {
    const o = state.output;
    const d = defaults.output();
    return ENCODER_KEYS.some((k) => o[k] !== d[k]);
  },

  reset() {
    const o = state.output;
    state.output = { ...defaults.output(), quality: o.quality, stripMetadata: o.stripMetadata, stripGps: o.stripGps };
  },

  render(root, app) {
    const o = state.output;
    r = {};
    r.summary = h('p', { class: 'convert-summary' });
    r.format = segmented({
      label: 'Format', cols: 4, size: 'lg', value: o.format,
      options: FORMATS.map((f) => ({
        value: f, label: f === 'original' ? 'Original' : FORMAT_LABEL[f], disabled: !app.canOutput(f),
        title: f === 'original' ? 'Keep each photo’s own format' : app.canOutput(f) ? null : 'Not available in this build',
      })),
      onChange: (v) => app.setFormat(v),
    });
    r.lossless = toggle({ label: 'Lossless', checked: o.lossless, onChange: (v) => { o.lossless = v; app.changed(); } });
    r.combine = toggle({ label: 'Combine into one PDF', checked: o.combinePdf, hint: 'One page per photo, in strip order.', onChange: (v) => { o.combinePdf = v; app.changed(); } });
    r.alphaNote = note('', 'info');

    r.colorspace = segmented({
      label: 'Colour', value: o.colorspace, cols: 2,
      options: [{ value: 'srgb', label: 'Convert to sRGB' }, { value: 'keep', label: 'Keep profile' }],
      onChange: (v) => { o.colorspace = v; app.changed(); },
    });
    r.fill = swatches({ label: 'Fill transparent areas with', colors: FILL, value: o.background, onChange: (v) => { o.background = v; app.changed(); } });
    r.progressive = toggle({ label: 'Progressive JPEG', checked: o.progressive, onChange: (v) => { o.progressive = v; app.changed(); } });
    r.chroma = selectField({
      label: 'Chroma subsampling', value: o.chroma,
      options: [
        { value: 'auto', label: 'Auto (4:4:4 at quality ≥ 90)' },
        { value: '444', label: '4:4:4 — sharpest colour edges' },
        { value: '422', label: '4:2:2' },
        { value: '420', label: '4:2:0 — smallest' },
      ],
      onChange: (v) => { o.chroma = v; app.changed(); },
    });
    r.filename = textField({ label: 'File name (single file)', value: o.filename, placeholder: 'Same as original', onChange: (v) => { o.filename = v; app.changed({ preview: false }); } });
    r.jpegOnly = h('div', { class: 'stack' }, r.progressive, r.chroma);

    append(root,
      toolHead('Convert', 'Change the file format. iPhone HEIC photos become JPG or PNG that open anywhere.', { onReset: () => app.resetTool(this), active: this.isActive() }),
      r.summary,
      group(null, r.format, r.lossless,
        h('p', { class: 'hint convert-hint' }, 'Quality and metadata are set once, in ', h('b', {}, 'Export'), ' below.')),
      r.combine,
      r.alphaNote,
      h('details', { class: 'disclosure' },
        h('summary', {}, 'Advanced'),
        h('div', { class: 'stack' }, r.colorspace, r.fill, r.jpegOnly, r.filename)),
    );
    this.sync(app);
  },

  sync(app) {
    if (!r.format) return;
    const o = state.output;
    const f = activeFile();
    const fmt = o.format;
    r.format.set(fmt);
    r.lossless.hidden = !(fmt === 'webp' || fmt === 'avif');
    r.lossless.set(o.lossless);
    r.combine.hidden = !(fmt === 'pdf' && state.files.length > 1);
    r.jpegOnly.hidden = fmt !== 'jpeg';
    const target = fmt === 'original' ? 'original format' : FORMAT_LABEL[fmt];
    r.summary.replaceChildren();
    if (f) {
      // a mixed batch names every source format (JPG + PNG), never just the active photo's
      const label = (x) => FORMAT_LABEL[x.info.format] || x.info.format?.toUpperCase() || '?';
      const srcs = [...new Set([label(f), ...state.files.map(label)])];
      append(r.summary, h('span', { class: 'fmt-pill' }, srcs.length > 3 ? 'Mixed' : srcs.join(' + ')), ' → ', h('span', { class: 'fmt-pill fmt-pill-accent' }, target));
      if (state.files.length > 1) append(r.summary, h('span', { class: 'muted' }, ` · all ${state.files.length} files`));
    }
    const hasAlpha = state.files.some((x) => x.info.has_alpha) || state.background.mode === 'remove';
    const loses = fmt !== 'original' && !ALPHA_FORMATS.has(fmt);
    r.alphaNote.hidden = !(hasAlpha && loses);
    r.alphaNote.querySelector('span').textContent = `${FORMAT_LABEL[fmt]} has no transparency — transparent areas are filled with ${colorName(o.background)?.toLowerCase() || o.background}. Change under Advanced, or pick PNG/WebP.`;
  },
};
