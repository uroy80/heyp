// Compress, two ways:
// - "Keep format" (img.compress job, when the server has it): every JPG, PNG, GIF, SVG and WebP is made smaller in its
//   own format — animations keep their frames, SVGs stay vectors (img-05). Results come back as files.
// - "Fit a size limit": the export hits a target file size (max / optional min KB) in JPG, WebP or AVIF.

import { h, num, fmtBytes, append } from '../dom.js';
import { state, defaults, FORMAT_LABEL, LOSSY, fmtKb, toolEntry } from '../state.js';
import { toolHead, group, segmented, numberField, quickChips, note, toggle } from '../controls.js';
import { jobCard } from '../jobui.js';

const CHIPS = [20, 50, 100, 200, 500, 1024];
const FORMATS = ['jpeg', 'webp', 'avif'];
export const COMPRESS_TOOL = 'img.compress';
/** Formats img.compress optimises in place (the server skips others with a warning). */
export const KEEP_FORMATS = new Set(['jpeg', 'png', 'gif', 'svg', 'webp']);
const LEVELS = [
  { value: 'low', label: 'Light', hint: 'Light keeps the most detail.' },
  { value: 'recommended', label: 'Recommended', hint: 'Recommended balances size and detail.' },
  { value: 'extreme', label: 'Strongest', hint: 'Strongest makes the smallest files; fine detail may suffer.' },
];

let r = {};

/** The img.compress registry entry when this server offers it, else null. */
export function keepFormatTool() {
  const t = toolEntry(COMPRESS_TOOL);
  return t && t.available !== false ? t : null;
}

/** The mode in effect: 'keep' only when the server can do it. */
export const compressMode = (c = state.compress) => (c.mode === 'keep' && keepFormatTool() ? 'keep' : 'target');

/** img.compress options from the panel state. */
export const keepOptions = (c = state.compress) => ({ level: c.level || 'recommended', png_quantize: c.pngQuantize !== false });

/** Strip photos img.compress can work on (by recorded format). */
export const keepFiles = (files = state.files) => files.filter((f) => KEEP_FORMATS.has(String(f?.info?.format || '').toLowerCase()));

/** "2.4 MB → 610 KB (−75 %)" rows for the job's data.files. */
export function savingRows(data) {
  return (Array.isArray(data?.files) ? data.files : []).map((row) => ({
    name: String(row.source || row.name || ''),
    before: Number(row.before) || 0,
    after: Number(row.after) || 0,
    saved: Math.round((Number(row.saved) || 0) * 100),
    changed: row.changed !== false,
  }));
}

export default {
  id: 'compress',
  title: 'Compress',
  icon: 'compress',

  // only the size-limit mode changes the export; keep-format runs as its own job
  isActive: () => state.compress.on && compressMode() === 'target',
  reset() {
    state.compress = defaults.compress();
  },

  render(root, app) {
    r = {};
    const c = state.compress;
    const keep = keepFormatTool();
    const mode = compressMode();
    append(root, toolHead('Compress', null, { onReset: () => app.resetTool(this), active: this.isActive() }));
    if (keep) {
      append(root, group(null, segmented({
        label: 'How', cols: 2, value: mode, tip: 'Keep format: PNG stays PNG, GIFs keep moving. Size limit: hit a target size in JPG, WebP or AVIF.',
        options: [{ value: 'keep', label: 'Keep format' }, { value: 'target', label: 'Fit a size limit' }],
        onChange: (v) => {
          c.mode = v;
          // the size limit only applies in its own mode; its numbers are kept for when you switch back
          c.on = v === 'target' && (num(c.maxKb) > 0 || num(c.minKb) > 0);
          app.changed({ panel: true });
        },
      })));
    }
    if (mode === 'keep') this.renderKeep(root, app, keep);
    else this.renderTarget(root, app);
  },

  renderKeep(root, app, tool) {
    const c = state.compress;
    const files = keepFiles();
    const skipped = state.files.length - files.length;
    const level = segmented({
      label: 'Compression', cols: 3, value: c.level || 'recommended', tip: LEVELS.map((l) => l.hint).join(' '),
      options: LEVELS.map((l) => ({ value: l.value, label: l.label })),
      onChange: (v) => { c.level = v; app.changed({ panel: true, preview: false }); },
    });
    const pngQ = toggle({
      label: 'Reduce PNG colours', checked: c.pngQuantize !== false,
      hint: 'Much smaller PNGs with fewer colours. Off keeps them lossless.',
      onChange: (v) => { c.pngQuantize = v; app.changed({ preview: false }); },
    });
    append(root,
      group(null, level, pngQ),
      skipped > 0 ? note(`${skipped} photo${skipped > 1 ? 's' : ''} will be skipped — use “Fit a size limit” for ${skipped > 1 ? 'them' : 'it'}.`, 'info') : null,
      app.hasEdits() ? note('Your edits aren’t applied here — use “Fit a size limit” to keep them.', 'info') : null);
    const max = Math.min(Number(tool?.max_files) || 100, Number(tool?.limits?.max_files) || 100);
    append(root, jobCard('compress', {
      startLabel: `Compress ${files.length} file${files.length === 1 ? '' : 's'}`,
      startIcon: 'compress',
      disabled: () => {
        if (!files.length) return state.files.length ? 'None of these photos is JPG, PNG, GIF, SVG or WebP.' : 'Add photos first.';
        if (files.length > max) return `At most ${max} files at a time.`;
        return null;
      },
      onStart: () => app.runTool('compress', COMPRESS_TOOL, keepFiles().map((f) => f.id), keepOptions(), { kind: 'image' }),
      renderResult: (res) => h('div', {}, this.savings(res), app.resultView(res, { kind: 'image', source: 'compress' })),
    }));
  },

  /** Before → after per file, from the job's data. */
  savings(res) {
    const rows = savingRows(res?.data);
    if (!rows.length) return null;
    const total = res.data;
    const list = h('ul', { class: 'savings' }, rows.slice(0, 20).map((row) => h('li', { class: 'readout-row' },
      h('span', { class: 'muted', title: row.name }, row.name),
      h('span', {}, row.changed ? `${fmtBytes(row.before)} → ${fmtBytes(row.after)} (−${row.saved} %)` : `${fmtBytes(row.before)} · already minimal`))));
    const sum = Number(total?.before) > 0 ? h('p', { class: 'hint' }, `Total: ${fmtBytes(total.before)} → ${fmtBytes(total.after)} (−${Math.round((Number(total.saved) || 0) * 100)} %)`) : null;
    return h('div', { class: 'readout' }, list, sum);
  },

  renderTarget(root, app) {
    const c = state.compress;
    const set = (k, v) => {
      c[k] = v;
      c.on = num(c.maxKb) > 0 || num(c.minKb) > 0;
      app.changed();
    };
    r.max = numberField({ label: 'Max size', value: c.maxKb, placeholder: 'e.g. 200', suffix: 'KB', step: 1, min: 1, onChange: (v) => set('maxKb', v) });
    r.min = numberField({ label: 'Min size (optional)', tip: 'Best effort: quality goes up, files are never padded.', value: c.minKb, placeholder: 'none', suffix: 'KB', step: 1, min: 1, onChange: (v) => set('minKb', v) });
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
    r.rangeNote = note('Min is above max, so it’s ignored.', 'warn');
    r.result = h('div', { class: 'readout', 'aria-live': 'polite' });

    append(root,
      group(null, r.max, chips, r.min, r.rangeNote),
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
    r.fmtNote.querySelector('span').textContent = `${FORMAT_LABEL[fmt] || fmt} shrinks poorly — pick JPG, WebP or AVIF.`;
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
