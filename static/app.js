// heyp UI entry point: wires state, tools, canvas, preview, intake and export together.

import { h, $, debounce, fmtBytes, isTyping, replaceIfChanged } from './js/dom.js';
import { icon } from './js/icons.js';
import { api, imageRetryDelay } from './js/api.js';
import {
  state, activeFile, buildOps, buildOutput, preCropOps, describeEdits, baseDims, ratioValue,
  passportActive, rotateQuarter, cropRatioFor, exportRequest, exportLimitError, warningSummary, qualityApplies,
  planUploads, FORMAT_LABEL,
} from './js/state.js';
import { Canvas } from './js/canvas.js';
import { fitRatio, snapBox } from './js/cropbox.js';
import { toast, initTheme } from './js/ui.js';
import { Pending } from './js/pending.js';
import { slider, toggle, selectField } from './js/controls.js';
import convertTool from './js/tools/convert.js';
import resizeTool from './js/tools/resize.js';
import cropTool from './js/tools/crop.js';
import passportTool, { guessPreset, choosePreset } from './js/tools/passport.js';
import compressTool from './js/tools/compress.js';
import editTool from './js/tools/edit.js';
import backgroundTool from './js/tools/background.js';
import infoTool from './js/tools/info.js';

const TOOLS = [convertTool, resizeTool, cropTool, passportTool, compressTool, editTool, backgroundTool, infoTool];
/** Section kickers above each tool headline ("04 · Passport & ID"). */
const KICKER = {
  convert: 'File format', resize: 'Size & print', crop: 'Framing', passport: 'Passport & ID',
  compress: 'File size', edit: 'Orientation & tone', background: 'Cut-out', info: 'Inside the file',
};
const UNDO_MS = 8000;
const NARROW = window.matchMedia('(max-width: 780px)');
const REDUCE_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)');
const IS_MAC = /mac|iphone|ipad|ipod/i.test(navigator.userAgentData?.platform || navigator.platform || navigator.userAgent);
const EXT = { jpeg: 'jpg', png: 'png', webp: 'webp', avif: 'avif', heic: 'heic', tiff: 'tif', bmp: 'bmp', gif: 'gif', pdf: 'pdf' };

const el = {
  toolList: $('#tool-list'),
  panel: $('#tool-panel'),
  exportBox: $('#export'),
  status: $('#status'),
  warnings: $('#warnings'),
  chips: $('#chips'),
  thumbs: $('#thumbs'),
  uploadBar: $('#upload-bar'),
  uploadText: $('#upload-text'),
  uploadFill: $('#upload-fill'),
  fileInput: $('#file-input'),
  dropveil: $('#dropveil'),
  compare: $('#compare-btn'),
  help: $('#help-dialog'),
};

const canvas = new Canvas($('#canvas'), {
  onCropChange: (box) => cropFromBox(box, { live: true }),
  onCropCommit: (box) => cropFromBox(box),
});

let comparing = false;
let previewPending = false;
let basePending = false;
let cropSnapshot = null; // {id, crop, ratio} — what Esc reverts to
let renderedTool = null;
const exportUi = {};

// ── public surface for tool modules ─────────────────────────────────────────

const app = {
  changed,
  refreshPanel,
  setTool,
  setFormat,
  resetTool,
  toast,
  loadPresets,
  fileFull,
  canOutput,
  lastPreview: () => activeFile()?.preview || null,
  crop: {
    setRatio: cropSetRatio,
    auto: cropAuto,
    manual: () => {
      const f = activeFile();
      if (f) f.crop = null;
      changed({ panel: true });
    },
    applyAll: cropApplyAll,
  },
};

const currentTool = () => TOOLS.find((t) => t.id === state.tool) || TOOLS[0];

function canOutput(fmt) {
  if (fmt === 'original') return true;
  if (!state.outputFormats.includes(fmt)) return false;
  if (fmt === 'avif' && state.features.avif === false) return false;
  if (fmt === 'heic' && state.features.heic === false) return false;
  return true;
}

/** Any setting changed: refresh dependent UI and schedule a live preview. */
function changed({ panel = false, preview = true } = {}) {
  if (panel) refreshPanel();
  else syncPanel();
  syncRail();
  renderChips();
  renderThumbs();
  syncExport();
  renderCanvas();
  renderStatus();
  if (preview) schedulePreview();
}

function resetTool(tool) {
  tool.reset(app);
  if (tool.id === 'crop') canvas.crop.setRatio(null);
  changed({ panel: true });
}

function setFormat(fmt) {
  if (!canOutput(fmt)) return;
  state.output.format = fmt;
  state.background.autoFrom = null; // the user chose this format: leaving Background › Remove must not undo it
  changed();
}

// ── rail & panel ────────────────────────────────────────────────────────────

function buildRail() {
  el.toolList.replaceChildren(...TOOLS.map((t, i) => h('li', {},
    h('button', {
      type: 'button', class: 'rail-btn', dataset: { tool: t.id }, title: `${t.title} (${i + 1})`,
      onclick: () => setTool(t.id),
    }, icon(t.icon, 18), h('span', { class: 'rail-label' }, t.title), h('span', { class: 'rail-dot', 'aria-hidden': 'true' }), h('kbd', { class: 'rail-kbd', 'aria-hidden': 'true' }, String(i + 1))))));
  syncRail();
}

function syncRail() {
  for (const btn of el.toolList.querySelectorAll('.rail-btn')) {
    const t = TOOLS.find((x) => x.id === btn.dataset.tool);
    const on = t.id === state.tool;
    if (on) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
    const active = t.isActive();
    btn.classList.toggle('has-edit', active);
    btn.setAttribute('aria-label', `${t.title}${active ? ' (active)' : ''}`);
  }
}

function setTool(id) {
  if (!TOOLS.some((t) => t.id === id) || state.tool === id) return;
  if (comparing) setCompare(false);
  state.tool = id;
  if (id === 'crop') snapshotCrop();
  else cropSnapshot = null;
  syncRail();
  refreshPanel();
  el.toolList.querySelector(`[data-tool="${id}"]`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  renderCanvas();
  renderStatus();
  revealPanel();
}

/** Phones stack picture and panel: after a tool switch, bring the tool's controls up under the sticky picture. */
function revealPanel() {
  if (!NARROW.matches || !state.files.length) return;
  const top = el.panel.getBoundingClientRect().top;
  const canvasBottom = $('#canvas').getBoundingClientRect().bottom;
  // the panel's first controls belong right under the (sticky) picture; leave it alone when it already is
  if (top < canvasBottom - 1 || top > canvasBottom + 48) {
    el.panel.scrollIntoView({ block: 'start', behavior: REDUCE_MOTION.matches ? 'auto' : 'smooth' });
  }
}

/**
 * Phones: the caption, chips, warnings and strip sit between the sticky picture and the panel. When they change
 * height while tucked under the picture (the reader is in the panel), scroll by the same amount so the panel stays
 * put instead of the strip peeking out between picture and controls (CSS turns scroll anchoring off there).
 */
function pinPanelOnPhones() {
  if (typeof ResizeObserver !== 'function') return;
  const parts = [$('.below'), $('#strip')];
  const total = () => parts.reduce((s, x) => s + x.getBoundingClientRect().height, 0);
  let last = total();
  const ro = new ResizeObserver(() => {
    const now = total();
    const delta = now - last;
    last = now;
    if (!delta || !NARROW.matches || !state.files.length || window.scrollY <= 0) return;
    if (parts[0].getBoundingClientRect().top < $('#canvas').getBoundingClientRect().bottom - 1) window.scrollBy(0, delta);
  });
  for (const p of parts) ro.observe(p);
}

const FOCUSABLE = 'input, select, textarea, button, summary, a[href]';

/** Rebuild the panel, keeping scroll, open <details> and focus position when the tool is unchanged. */
function refreshPanel() {
  const tool = currentTool();
  const same = renderedTool === tool.id;
  const focusables = () => [...el.panel.querySelectorAll(FOCUSABLE)];
  const idx = same && el.panel.contains(document.activeElement) ? focusables().indexOf(document.activeElement) : -1;
  const scroll = same ? el.panel.scrollTop : 0;
  const open = same ? [...el.panel.querySelectorAll('details')].map((d) => d.open) : [];
  el.panel.replaceChildren();
  el.panel.setAttribute('aria-labelledby', 'tool-title');
  tool.render(el.panel, app);
  el.panel.querySelector('.tool-head')?.prepend(h('p', { class: 'tool-kicker' }, `${String(TOOLS.indexOf(tool) + 1).padStart(2, '0')} · ${KICKER[tool.id]}`));
  renderedTool = tool.id;
  el.panel.querySelectorAll('details').forEach((d, i) => { if (open[i]) d.open = true; });
  el.panel.scrollTop = scroll;
  if (idx >= 0) focusables()[idx]?.focus({ preventScroll: true });
}

function syncPanel() {
  const tool = currentTool();
  tool.sync?.(app);
  const reset = el.panel.querySelector('[data-role="reset"]');
  if (reset) reset.hidden = !tool.isActive();
}

// ── canvas ──────────────────────────────────────────────────────────────────

const infoDims = (f) => ({ w: f.info.width, h: f.info.height });

function cropBoxMode() {
  const f = activeFile();
  return Boolean(state.tool === 'crop' && f && !passportActive() && (!f.crop || f.crop.kind === 'box'));
}

/** Image the crop tool draws on: original preview, or a server render of the pre-crop ops. */
function cropBase(f) {
  const ops = preCropOps(f);
  if (!ops.length) return { src: api.previewUrl(f.id), w: f.info.width, h: f.info.height };
  const key = JSON.stringify(ops);
  if (f.cropBase?.key === key) return f.cropBase;
  ensureCropBase(f, ops, key);
  return null;
}

let baseSeq = 0;
let baseCtrl = null;
async function ensureCropBase(f, ops, key) {
  if (f.cropBasePending === key) return;
  baseCtrl?.abort();
  baseCtrl = new AbortController();
  const my = ++baseSeq;
  f.cropBasePending = key;
  setBasePending(true);
  try {
    const res = await api.preview({ ids: [f.id], ops, output: { format: 'jpeg', quality: 85, strip_metadata: true } }, baseCtrl.signal);
    if (my !== baseSeq) return;
    f.cropBase = { key, src: res.image, w: res.width, h: res.height };
    if (f.id === state.activeId) {
      renderCanvas();
      syncPanel();
    }
  } catch (e) {
    if (e?.name === 'AbortError' || my !== baseSeq) return;
    handleFileError(e, f);
  } finally {
    if (f.cropBasePending === key) f.cropBasePending = null;
    if (my === baseSeq) setBasePending(false);
  }
}

function renderCanvas() {
  const f = activeFile();
  document.body.classList.toggle('has-files', state.files.length > 0);
  el.compare.hidden = !f || cropBoxMode();
  if (!f) {
    canvas.crop.show(false);
    canvas.setBadge('');
    canvas.setGuides(null);
    canvas.show(null);
    return;
  }
  if (comparing) {
    canvas.crop.show(false);
    canvas.setGuides(null);
    canvas.setBadge('Original');
    canvas.show(api.previewUrl(f.id), infoDims(f), `${f.name}, original`);
    return;
  }
  if (cropBoxMode()) {
    canvas.setBadge('');
    canvas.setGuides(null);
    const base = cropBase(f);
    if (!base) {
      // pre-crop render in flight: show this file's latest result meanwhile, never another file's image
      canvas.crop.show(false);
      const p = f.preview;
      canvas.show(p?.image || api.previewUrl(f.id), p ? { w: p.width, h: p.height } : infoDims(f), f.name);
      return;
    }
    const cb = canvas.crop;
    cb.setImageSize(base.w, base.h);
    cb.setRatio(ratioValue(state.cropRatio, base.w / base.h));
    if (!cb.drag) cb.setBox(f.crop?.kind === 'box' ? f.crop : null);
    canvas.show(base.src, { w: base.w, h: base.h }, `${f.name}, crop`).then(() => {
      if (cropBoxMode() && !comparing && canvas.src === base.src) cb.show(true);
    });
    return;
  }
  canvas.crop.show(false);
  let badge = '';
  if (state.tool === 'crop' && f.crop && !passportActive()) badge = f.crop.kind === 'trim' ? 'Auto trim' : `${f.crop.anchor === 'face' ? 'Face' : 'Smart'} crop`;
  canvas.setBadge(badge);
  const p = f.preview;
  const src = p?.image || api.previewUrl(f.id);
  if (!guidesFor(f)) canvas.setGuides(null);
  canvas.show(src, p ? { w: p.width, h: p.height } : infoDims(f), f.name).then(() => {
    if (canvas.src === src && activeFile() === f && !comparing) canvas.setGuides(guidesFor(f));
  });
}

/** Passport guides for the shown preview: Passport tool open, "Show guides" on, single photo measured. */
function guidesFor(f) {
  const pp = f?.preview?.passport;
  if (!pp || comparing || state.tool !== 'passport' || !passportActive() || state.passport.guides === false) return null;
  return state.passport.layout === 'single' ? pp : null;
}

function setCompare(on) {
  if (on === comparing) return;
  if (on && (!activeFile() || cropBoxMode())) return;
  comparing = on;
  el.compare.classList.toggle('is-on', on);
  el.compare.setAttribute('aria-pressed', String(on));
  renderCanvas();
  renderStatus();
}

// ── live preview ────────────────────────────────────────────────────────────

let pvSeq = 0;
let pvCtrl = null;
let pvRetry = 0; // timer: re-run a preview the server refused as busy (503 + Retry-After)
const schedulePreview = debounce(runPreview, 250);

function setPreviewPending(v) {
  if (previewPending === v) return;
  previewPending = v;
  canvas.loading(previewPending || basePending);
  el.status.classList.toggle('is-stale', v);
  renderStatus();
}

function setBasePending(v) {
  basePending = v;
  canvas.loading(previewPending || basePending);
}

function previewRequest(f) {
  const output = buildOutput();
  output.filename = null;
  return { ids: [f.id], ops: buildOps(f), output };
}

async function runPreview() {
  clearTimeout(pvRetry);
  const f = activeFile();
  if (!f) {
    if (previewPending) setPreviewPending(false); // e.g. a busy-retry outlived the last file
    return;
  }
  const req = previewRequest(f);
  const key = JSON.stringify(req);
  if (f.previewKey === key) {
    // already rendered (or failed) for exactly these settings; drop any in-flight stale request
    if (previewPending) {
      pvCtrl?.abort();
      pvSeq++;
      setPreviewPending(false);
    }
    return;
  }
  pvCtrl?.abort();
  pvCtrl = new AbortController();
  const my = ++pvSeq;
  setPreviewPending(true);
  let busy = false;
  try {
    const res = await api.preview(req, pvCtrl.signal);
    if (my !== pvSeq) return;
    f.preview = res;
    f.previewKey = key;
    f.previewError = null;
    if (f.id === state.activeId) {
      renderCanvas();
      renderStatus();
      syncPanel();
      syncExport();
    }
  } catch (e) {
    if (e?.name === 'AbortError' || my !== pvSeq) return;
    if (e?.status === 404) {
      expire([f]);
      return;
    }
    if (e?.status === 503 && e.retryAfter) {
      // busy, not broken: keep the current picture (still marked updating) and ask again when told to
      busy = true;
      pvRetry = setTimeout(runPreview, e.retryAfter * 1000);
      return;
    }
    // a rejected request (4xx) won't succeed on retry; network/server errors may
    if (e?.status >= 400 && e.status < 500) f.previewKey = key;
    f.previewError = e?.detail || 'Preview failed';
    if (f.id === state.activeId) renderStatus();
  } finally {
    if (my === pvSeq && !busy) setPreviewPending(false);
  }
}

function previewNow() {
  schedulePreview.cancel();
  runPreview();
}

// ── status, warnings, chips ─────────────────────────────────────────────────

const fmtName = (f) => FORMAT_LABEL[f] || String(f || '').toUpperCase();

/** "q 72" when the target-size encoder picked the quality (contract v1.1 C2). */
const achievedQ = (p) => (p?.encode?.quality != null ? `q ${p.encode.quality}` : null);

function renderStatus() {
  const f = activeFile();
  if (!f) {
    replaceIfChanged(el.status, []);
    replaceIfChanged(el.warnings, []);
    return;
  }
  const fig = h('span', { class: 'status-fig' }, `Fig. ${state.files.indexOf(f) + 1}`);
  const name = h('span', { class: 'status-name', title: f.name }, f.name);
  const src = `${f.info.width}×${f.info.height}`;
  const sep = () => h('span', { class: 'sep', 'aria-hidden': 'true' }, '·');
  const facts = h('span', { class: 'status-facts' });
  if (comparing) {
    facts.append(h('strong', {}, 'Original'), sep(), src, sep(), fmtBytes(f.info.size_bytes), sep(), fmtName(f.info.format));
    replaceIfChanged(el.status, [fig, name, facts]);
    return;
  }
  const p = f.preview;
  if (p) {
    const out = `${p.width}×${p.height}`;
    facts.append(src, h('span', { class: 'arrow', 'aria-label': 'becomes' }, '→'), h('strong', {}, out), sep(), h('strong', {}, fmtBytes(p.size_bytes)), sep(), fmtName(p.format));
    if (achievedQ(p)) facts.append(sep(), achievedQ(p));
    if (p.dpi?.[0]) facts.append(sep(), `${Math.round(p.dpi[0])} DPI`);
  } else {
    facts.append(src, sep(), fmtBytes(f.info.size_bytes), sep(), fmtName(f.info.format));
  }
  // busy state is announced through aria-busy on the canvas, not by rewriting this line
  if (previewPending) facts.append(h('span', { class: 'status-busy', 'aria-hidden': 'true' }, h('span', { class: 'spinner' })));
  replaceIfChanged(el.status, [fig, name, facts]);

  const warns = [];
  if (f.previewError) warns.push({ text: f.previewError, kind: 'error' });
  if (p && !f.previewError) for (const w of new Set(p.warnings || [])) warns.push({ text: w, kind: 'warn' });
  replaceIfChanged(el.warnings, warns.map((w) => h('li', { class: `warning warning-${w.kind}` }, icon('alert', 14), h('span', {}, w.text))));
}

function renderChips() {
  const edits = describeEdits(activeFile());
  el.chips.replaceChildren(...edits.map((e, i) => h('li', { class: 'chip' },
    h('button', { type: 'button', class: 'chip-label', title: `Open ${TOOLS.find((t) => t.id === e.tool)?.title}`, onclick: () => setTool(e.tool) }, e.label),
    h('button', {
      type: 'button', class: 'chip-x', 'aria-label': `Remove ${e.label}`, title: 'Remove',
      onclick: () => {
        const msg = e.clear();
        if (e.key === 'crop') canvas.crop.setRatio(null);
        changed({ panel: true });
        if (typeof msg === 'string') toast(msg);
        const xs = el.chips.querySelectorAll('.chip-x');
        (xs[i] || xs[i - 1])?.focus();
      },
    }, icon('x', 12)))));
}

// ── files: thumbs, selection, removal ───────────────────────────────────────

const thumbEls = new Map();

function renderThumbs() {
  const focused = document.activeElement;
  const items = state.files.map((f) => {
    let li = thumbEls.get(f.id);
    if (!li) {
      li = h('li', { class: 'thumb' },
        h('button', {
          type: 'button', class: 'thumb-btn', title: f.name, 'aria-label': `${f.name}, ${f.info.width} by ${f.info.height}`,
          onclick: () => selectFile(f.id),
          onkeydown: (e) => thumbKeys(e, f.id),
        }, h('img', { src: api.previewUrl(f.id, 240), alt: '', loading: 'lazy', decoding: 'async', draggable: 'false', onerror: (e) => retryThumb(e.currentTarget, f.id) }),
        h('span', { class: 'thumb-badge', 'aria-hidden': 'true' }, icon('crop', 11))),
        h('button', { type: 'button', class: 'thumb-x', 'aria-label': `Remove ${f.name}`, title: 'Remove', onclick: () => removeFile(f.id, { undo: true }) }, icon('x', 12)));
      thumbEls.set(f.id, li);
    }
    const on = f.id === state.activeId;
    li.classList.toggle('is-active', on);
    const btn = li.firstElementChild;
    if (on) btn.setAttribute('aria-current', 'true');
    else btn.removeAttribute('aria-current');
    li.classList.toggle('has-crop', Boolean(f.crop) && !passportActive());
    return li;
  });
  for (const id of [...thumbEls.keys()]) if (!state.files.some((f) => f.id === id)) thumbEls.delete(id);
  const add = el.thumbs.querySelector('.thumb-add') || h('li', { class: 'thumb thumb-add' },
    h('button', { type: 'button', class: 'thumb-add-btn', 'aria-label': 'Add photos', title: 'Add photos', onclick: openPicker }, icon('plus', 20)));
  el.thumbs.replaceChildren(...items, add);
  if (focused?.isConnected && focused !== document.activeElement) focused.focus({ preventScroll: true });
}

const THUMB_TRIES = 4;

/** A thumbnail refused while the server was busy (503) is requested again after its Retry-After. */
async function retryThumb(img, id) {
  const tries = Number(img.dataset.tries || 0) + 1;
  if (tries >= THUMB_TRIES) return;
  img.dataset.tries = String(tries);
  const url = `${api.previewUrl(id, 240)}&retry=${tries}`;
  const wait = await imageRetryDelay(url);
  if (wait == null) return;
  setTimeout(() => { if (img.isConnected) img.src = url; }, wait);
}

function thumbKeys(e, id) {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  e.preventDefault();
  const i = state.files.findIndex((f) => f.id === id);
  const next = state.files[i + (e.key === 'ArrowRight' ? 1 : -1)];
  if (!next) return;
  selectFile(next.id);
  thumbEls.get(next.id)?.firstElementChild.focus();
}

function selectFile(id) {
  if (state.activeId === id) return;
  if (comparing) setCompare(false);
  state.activeId = id;
  // the ratio lock belongs to the photo: set it before the Esc snapshot so neither carries another photo's ratio
  state.cropRatio = cropRatioFor(activeFile());
  if (state.tool === 'crop') snapshotCrop();
  refreshPanel();
  changed({ preview: false });
  thumbEls.get(id)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  previewNow();
}

function stepFile(dir) {
  const i = state.files.findIndex((f) => f.id === state.activeId);
  const next = state.files[i + dir];
  if (next) selectFile(next.id);
}

function dropFiles(ids) {
  const i = state.files.findIndex((f) => f.id === state.activeId);
  state.files = state.files.filter((f) => !ids.includes(f.id));
  if (ids.includes(state.activeId)) {
    state.activeId = state.files[Math.min(Math.max(i, 0), state.files.length - 1)]?.id ?? null;
    state.cropRatio = cropRatioFor(activeFile());
    if (state.tool === 'crop') snapshotCrop();
  }
  if (comparing) setCompare(false);
  refreshPanel();
  changed({ preview: false });
  previewNow();
}

// removed photos stay on the server for UNDO_MS so "Undo" can bring them back with their crop
const removals = new Pending(UNDO_MS);

/** Remove a photo. With `undo`, the server copy is deleted only after the Undo toast expires. */
function removeFile(id, { undo = false } = {}) {
  const index = state.files.findIndex((f) => f.id === id);
  if (index < 0) return;
  const file = state.files[index];
  const wasFocusedThumb = thumbEls.get(id)?.contains(document.activeElement);
  dropFiles([id]);
  if (wasFocusedThumb) (thumbEls.get(state.activeId)?.firstElementChild || el.thumbs.querySelector('.thumb-add-btn'))?.focus();
  if (!undo) {
    api.deleteFile(id);
    return;
  }
  removals.add(id, { file, index }, (_, opts) => api.deleteFile(id, opts));
  toast(`Removed “${file.name}”.`, 'info', { timeout: UNDO_MS, action: { label: 'Undo', onClick: () => restoreFile(id) } });
}

function restoreFile(id) {
  const kept = removals.take(id);
  if (!kept) return;
  const { file, index } = kept;
  state.files.splice(Math.min(index, state.files.length), 0, file);
  thumbEls.delete(id);
  state.activeId = null;
  selectFile(id);
  thumbEls.get(id)?.firstElementChild.focus({ preventScroll: true });
}

/** Server forgot these files (TTL / restart): drop them and tell the user to re-add. */
function expire(files) {
  const list = files.filter((f) => state.files.includes(f));
  if (!list.length) return;
  dropFiles(list.map((f) => f.id));
  const what = list.length === 1 ? `“${list[0].name}” is` : `${list.length} photos are`;
  toast(`${what} no longer on the server (removed after inactivity or a restart). Add ${list.length === 1 ? 'it' : 'them'} again to keep working.`, 'warn', { timeout: 10000 });
}

function handleFileError(e, f) {
  if (e?.status === 404) expire([f]);
  else toast(e?.detail || 'Something went wrong', 'error');
}

async function fileFull(f) {
  if (f.full) return f.full;
  try {
    f.full = await api.fileInfo(f.id);
    return f.full;
  } catch (e) {
    handleFileError(e, f);
    return null;
  }
}

// ── intake: picker, drop, paste, upload ─────────────────────────────────────

function openPicker() {
  el.fileInput.value = '';
  el.fileInput.click();
}

let uploads = 0;

function showUpload(text, frac) {
  el.uploadBar.hidden = false;
  el.uploadText.textContent = text;
  el.uploadFill.style.transform = `scaleX(${Math.max(0.02, Math.min(1, frac))})`;
  document.body.classList.add('is-uploading');
}

function hideUpload() {
  if (uploads) return;
  el.uploadBar.hidden = true;
  document.body.classList.remove('is-uploading');
}

async function addFiles(list) {
  const picked = [...(list || [])].filter((f) => f && f.size > 0);
  if (!picked.length) {
    toast('Nothing to add — drop or choose image files.', 'warn');
    return;
  }
  // files over the server's limit are refused here instead of after uploading them
  const { chunks, rejected } = planUploads(picked, state.limits);
  const errors = [...rejected];
  const files = chunks.flat();
  if (!files.length) {
    reportUploadErrors(errors);
    return;
  }

  const total = files.reduce((n, f) => n + f.size, 0);
  const noun = `${files.length} photo${files.length > 1 ? 's' : ''}`;
  let sent = 0;
  const added = [];
  uploads++;
  showUpload(`Uploading ${noun}…`, 0);
  try {
    for (const chunk of chunks) {
      const size = chunk.reduce((n, f) => n + f.size, 0);
      try {
        const res = await api.upload(chunk, (frac) => {
          const all = (sent + frac * size) / total;
          showUpload(all >= 0.999 ? `Reading ${noun}…` : `Uploading ${noun}… ${Math.round(all * 100)}%`, all);
        });
        added.push(...res.files);
        errors.push(...res.errors);
      } catch (e) {
        if (Array.isArray(e.body?.errors) && e.body.errors.length) errors.push(...e.body.errors);
        else errors.push(...chunk.map((f) => ({ name: f.name, detail: e.detail })));
      }
      sent += size;
    }
  } finally {
    uploads--;
    hideUpload();
  }

  for (const info of added) {
    if (state.files.some((f) => f.id === info.id)) continue;
    state.files.push({ id: info.id, name: info.name, info, crop: null, preview: null, previewKey: null, previewError: null, cropBase: null, cropBasePending: null, full: null });
  }
  if (added.length) {
    state.activeId = null;
    selectFile(added[0].id);
  }
  reportUploadErrors(errors);
}

function reportUploadErrors(errors) {
  if (!errors.length) return;
  const details = [...new Set(errors.map((e) => e.detail || 'unsupported file'))];
  if (errors.length === 1) {
    toast(`Couldn’t add “${errors[0].name}”: ${details[0]}`, 'error');
  } else {
    const names = errors.slice(0, 3).map((e) => `“${e.name}”`).join(', ') + (errors.length > 3 ? ` and ${errors.length - 3} more` : '');
    toast(`Couldn’t add ${errors.length} files (${names}): ${details.slice(0, 2).join('; ')}`, 'error');
  }
}

function initIntake() {
  el.fileInput.addEventListener('change', () => addFiles(el.fileInput.files));
  $('#add-btn').addEventListener('click', openPicker);
  $('#dz-pick').addEventListener('click', (e) => {
    e.stopPropagation();
    openPicker();
  });
  $('#dropzone').addEventListener('click', (e) => {
    if (e.target === e.currentTarget || e.target.closest('.dz-inner') && !e.target.closest('button')) openPicker();
  });

  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth++;
    el.dropveil.hidden = false;
  });
  window.addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  window.addEventListener('dragleave', (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) el.dropveil.hidden = true;
  });
  window.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    el.dropveil.hidden = true;
    addFiles(e.dataTransfer.files);
  });

  document.addEventListener('paste', (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return; // plain text paste goes to the focused field
    e.preventDefault();
    addFiles(files.map((f, i) => (f.name && f.name !== 'image.png' ? f : new File([f], `pasted-${Date.now()}${i ? `-${i}` : ''}.${(f.type.split('/')[1] || 'png').replace('jpeg', 'jpg')}`, { type: f.type }))));
  });
}

// ── crop controller ─────────────────────────────────────────────────────────

function snapshotCrop() {
  const f = activeFile();
  cropSnapshot = f ? { id: f.id, crop: f.crop ? { ...f.crop } : null, ratio: state.cropRatio } : null;
}

function revertCrop() {
  const f = activeFile();
  if (!f || !cropSnapshot || cropSnapshot.id !== f.id) return;
  f.crop = cropSnapshot.crop ? { ...cropSnapshot.crop } : null;
  state.cropRatio = cropSnapshot.ratio;
  changed({ panel: true });
}

const cropDims = (f) => baseDims(f);

function cropFromBox(box, { live = false } = {}) {
  const f = activeFile();
  if (!f || passportActive()) return;
  const full = box.x < 1e-4 && box.y < 1e-4 && box.w > 0.9999 && box.h > 0.9999;
  f.crop = full ? null : { kind: 'box', x: box.x, y: box.y, w: box.w, h: box.h, ratio: state.cropRatio };
  if (live) {
    syncPanel();
    return;
  }
  changed();
}

function cropSetRatio(v) {
  const f = activeFile();
  state.cropRatio = v;
  if (!f) return;
  if (f.crop?.kind === 'aspect' && v !== 'free') {
    f.crop = { ...f.crop, ratio: v }; // re-run the auto crop with the new ratio
    changed({ panel: true });
    return;
  }
  const { w, h: hh } = cropDims(f);
  const r = ratioValue(v, w / hh);
  if (f.crop && f.crop.kind !== 'box') f.crop = null;
  const cb = canvas.crop;
  cb.setImageSize(w, hh);
  cb.setBox(f.crop?.kind === 'box' ? f.crop : null);
  if (r) {
    cb.applyRatio(r); // commits through onCropCommit → changed()
  } else {
    cb.setRatio(null);
    if (f.crop) f.crop.ratio = 'free';
    changed();
  }
}

function cropAuto(kind) {
  const f = activeFile();
  if (!f) return;
  const c = f.crop;
  const same = c && (kind === 'trim' ? c.kind === 'trim' : c.kind === 'aspect' && c.anchor === kind);
  if (same) {
    f.crop = null;
  } else if (kind === 'trim') {
    f.crop = { kind: 'trim', tolerance: 12, padding: 0 };
  } else {
    let ratio = state.cropRatio;
    if (ratio === 'free' && c?.kind === 'box') {
      const { w, h: hh } = cropDims(f);
      ratio = Math.round(((c.w * w) / (c.h * hh)) * 1e4) / 1e4;
    } else if (ratio === 'free' || ratio === 'original') {
      ratio = '1:1'; // the whole frame at its own ratio would be a no-op
      state.cropRatio = ratio;
    }
    f.crop = { kind: 'aspect', ratio, anchor: kind };
  }
  changed({ panel: true });
}

function cropApplyAll() {
  const src = activeFile();
  if (!src) return;
  const c = src.crop;
  for (const f of state.files) {
    if (f === src) continue;
    if (!c) f.crop = null;
    else if (c.kind === 'box' && c.ratio !== 'free') {
      // keep the ratio exact on photos with a different shape
      const { w, h: hh } = cropDims(f);
      const r = ratioValue(c.ratio, w / hh);
      const b = snapBox(fitRatio(r, w, hh, (c.x + c.w / 2) * w, (c.y + c.h / 2) * hh), w, hh);
      f.crop = { ...c, x: b.x / w, y: b.y / hh, w: b.w / w, h: b.h / hh };
    } else f.crop = { ...c };
  }
  toast(`Crop applied to all ${state.files.length} photos.`);
  changed({ panel: true });
}

// ── export ──────────────────────────────────────────────────────────────────

const EXPORT_FORMATS = ['jpeg', 'png', 'webp', 'avif', 'heic', 'tiff', 'bmp', 'gif', 'pdf', 'original'];

function buildExport() {
  const o = state.output;
  exportUi.format = selectField({
    label: 'Format', value: o.format,
    options: EXPORT_FORMATS.map((f) => ({ value: f, label: f === 'original' ? 'Same as original' : FORMAT_LABEL[f], disabled: !canOutput(f) })),
    onChange: (v) => setFormat(v),
  });
  exportUi.quality = slider({ label: 'Quality', min: 1, max: 100, step: 1, value: o.quality, onInput: (v) => { o.quality = v; changed(); } });
  exportUi.autoQ = h('p', { class: 'auto-q' }, 'Auto · target size');
  exportUi.qualityAuto = h('div', { class: 'field' }, h('span', { class: 'label' }, 'Quality'), exportUi.autoQ);
  // one positive metadata control (SPEC §10.2); GPS can only be kept while metadata is kept
  exportUi.keepMeta = toggle({
    label: 'Keep metadata (camera, date)', checked: !o.stripMetadata,
    onChange: (v) => { o.stripMetadata = !v; if (!v) o.stripGps = true; changed(); },
  });
  exportUi.keepGps = toggle({
    label: 'Keep location (GPS)', checked: !o.stripGps,
    onChange: (v) => { o.stripGps = !v; changed(); },
  });
  exportUi.keepGps.classList.add('switch-sub');
  exportUi.label = h('span', {}, 'Download');
  exportUi.btn = h('button', { type: 'button', class: 'btn btn-primary btn-block btn-download', onclick: download },
    h('span', { class: 'btn-ico' }, icon('download', 18)), exportUi.label);
  exportUi.meta = h('p', { class: 'export-meta', 'aria-live': 'polite' });
  el.exportBox.replaceChildren(
    h('h2', { class: 'export-title', id: 'export-title' }, 'Export'),
    h('div', { class: 'export-grid' }, exportUi.format, exportUi.quality, exportUi.qualityAuto),
    exportUi.keepMeta, exportUi.keepGps, exportUi.btn, exportUi.meta);
  syncExport();
}

let exporting = false;

const setText = (node, text) => { if (node.textContent !== text) node.textContent = text; };

function syncExport() {
  if (!exportUi.btn) return;
  const o = state.output;
  const n = state.files.length;
  exportUi.format.select.value = o.format;
  for (const opt of exportUi.format.select.options) opt.disabled = !canOutput(opt.value);
  const lossy = qualityApplies();
  const p = activeFile()?.preview;
  exportUi.quality.set(o.quality);
  exportUi.quality.hidden = !lossy || state.compress.on;
  exportUi.qualityAuto.hidden = !(lossy && state.compress.on);
  setText(exportUi.autoQ, achievedQ(p) ? `Auto · ${achievedQ(p)}` : 'Auto · target size');
  exportUi.keepMeta.set(!o.stripMetadata);
  exportUi.keepGps.set(!o.stripGps);
  exportUi.keepGps.hidden = o.stripMetadata;
  if (exporting) return;
  exportUi.btn.disabled = !n || uploads > 0;
  const fmt = o.format === 'original' ? '' : ` ${FORMAT_LABEL[o.format]}`;
  let label = `Download${fmt}`;
  if (n > 1) label = o.format === 'pdf' && o.combinePdf ? `Download PDF · ${n} pages` : `Download ${n} files · ZIP`;
  setText(exportUi.label, label);
  const facts = p ? [`${p.width}×${p.height}`, fmtBytes(p.size_bytes), achievedQ(p)].filter(Boolean).join(' · ') : '';
  setText(exportUi.meta, !n ? 'Add photos to export.' : p && n === 1 ? facts : p ? `This photo: ${facts}` : '');
}

/** Export busy state: only download() starts and ends it, so nothing can leave the button stuck. */
function beginExport(text) {
  exporting = true;
  exportUi.btn.disabled = true;
  exportUi.btn.classList.add('is-busy');
  exportUi.btn.setAttribute('aria-busy', 'true');
  exportUi.btn.querySelector('.btn-ico').replaceChildren(h('span', { class: 'spinner' }));
  exportUi.label.textContent = text;
}

function endExport() {
  exporting = false;
  exportUi.btn.classList.remove('is-busy');
  exportUi.btn.setAttribute('aria-busy', 'false');
  exportUi.btn.querySelector('.btn-ico').replaceChildren(icon('download', 18));
  syncExport();
}

function fallbackName(files, output) {
  if (files.length > 1) return output.format === 'pdf' && output.combine_pdf ? 'heyp-photos.pdf' : `heyp-${files.length}-files.zip`;
  const f = files[0];
  const base = output.filename || f.name.replace(/\.[^.]+$/, '') || 'photo';
  const ext = EXT[output.format] || EXT[f.info.format] || 'jpg';
  return `${base}.${ext}`;
}

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = h('a', { href: url, download: name, hidden: true });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

/** One request for the whole batch: per-file crops travel in ops_by_id (contract v1.1 C1). */
async function download() {
  if (exporting || uploads) return;
  const files = state.files.slice();
  if (!files.length) {
    toast('Add a photo first.', 'warn');
    return;
  }
  const tooMany = exportLimitError(files.length, state.limits);
  if (tooMany) {
    toast(tooMany, 'error');
    return;
  }
  const req = exportRequest(files);
  beginExport(files.length > 1 ? `Exporting ${files.length} photos…` : 'Exporting…');
  try {
    const res = await api.process(req);
    saveBlob(res.blob, res.filename || fallbackName(files, req.output));
    if (res.warnings.length) toast(warningSummary(res.warnings), 'warn', { timeout: 10000 });
  } catch (e) {
    if (e?.status === 404) await findExpired(files, e);
    else toast(e?.detail || 'Export failed', 'error');
  } finally {
    endExport();
  }
}

async function findExpired(files, err) {
  const gone = [];
  await Promise.all(files.map((f) => api.fileInfo(f.id).catch((e) => { if (e?.status === 404) gone.push(f); })));
  if (gone.length) expire(gone);
  else toast(err?.detail || 'Export failed', 'error');
}

// ── presets & capabilities ──────────────────────────────────────────────────

async function loadPresets() {
  state.presetsError = null;
  if (state.tool === 'passport') refreshPanel();
  try {
    const res = await api.presets();
    state.presets = Array.isArray(res.presets) ? res.presets : [];
    state.papers = Array.isArray(res.papers) ? res.papers : [];
    if (!state.passport.preset || !state.presets.some((p) => p.id === state.passport.preset)) {
      const id = guessPreset(state.presets);
      if (id) choosePreset(state.passport, id);
    }
    if (state.papers.length && !state.papers.some((p) => p.id === state.passport.paper)) state.passport.paper = state.papers[0].id;
  } catch (e) {
    state.presetsError = e?.detail || 'unknown error';
  }
  if (state.tool === 'passport') refreshPanel();
  renderChips();
}

async function loadCapabilities() {
  const [health, formats] = await Promise.allSettled([api.health(), api.formats()]);
  if (health.status === 'fulfilled') {
    state.features = { ...state.features, ...(health.value?.features || {}) };
    if (health.value?.limits && typeof health.value.limits === 'object') state.limits = health.value.limits;
  } else {
    toast(health.reason?.detail || 'Can’t reach heyp.', 'error', { timeout: 0 });
  }
  if (formats.status === 'fulfilled' && Array.isArray(formats.value?.output)) state.outputFormats = formats.value.output;
  if (!canOutput(state.output.format)) state.output.format = 'jpeg';
  refreshPanel();
  changed({ preview: false });
}

// ── keyboard ────────────────────────────────────────────────────────────────

function spaceBelongsToTarget(t) {
  if (!t || t === document.body) return false;
  if (t.matches?.('input[type="checkbox"], input[type="radio"], select, textarea')) return true;
  // a keyboard-focused button keeps Space; after a mouse click (no focus ring) Space compares
  return Boolean(t.matches?.('button, summary, a[href]') && t.matches(':focus-visible'));
}

function initKeyboard() {
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented) return;
    const mod = IS_MAC ? e.metaKey : e.ctrlKey;
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (mod && !e.altKey && !e.shiftKey && key === 's') {
      e.preventDefault();
      download();
      return;
    }
    if (mod && !e.altKey && !e.shiftKey && key === 'o') {
      e.preventDefault();
      openPicker();
      return;
    }
    if (el.help.open) return;
    const typing = isTyping(e.target);
    if (e.key === 'Escape') {
      if (state.tool === 'crop' && !typing) revertCrop();
      return;
    }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === ' ') {
      if (spaceBelongsToTarget(e.target)) return;
      e.preventDefault();
      if (!e.repeat) setCompare(true);
      return;
    }
    if (/^[1-8]$/.test(e.key)) {
      e.preventDefault();
      setTool(TOOLS[Number(e.key) - 1].id);
    } else if (key === 'r') {
      e.preventDefault();
      rotateQuarter(e.shiftKey ? -1 : 1);
      changed({ panel: state.tool === 'edit' || state.tool === 'crop' });
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      // only from "neutral" focus (page, canvas, strip) so a stray key on a slider never drops a photo
      const t = e.target;
      if (!state.activeId || !(t === document.body || t.closest?.('.thumbs, .compare-btn'))) return;
      e.preventDefault();
      removeFile(state.activeId, { undo: true });
    } else if (e.key === '[' || e.key === ']') {
      e.preventDefault();
      stepFile(e.key === ']' ? 1 : -1);
    } else if (e.key === '?') {
      e.preventDefault();
      el.help.showModal();
    }
  });
  document.addEventListener('keyup', (e) => {
    if (e.key === ' ' && comparing) setCompare(false);
  });
  window.addEventListener('blur', () => setCompare(false));

  // press-and-hold compare button (mouse, touch, keyboard)
  const b = el.compare;
  b.setAttribute('aria-pressed', 'false');
  b.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    b.setPointerCapture(e.pointerId);
    setCompare(true);
  });
  for (const ev of ['pointerup', 'pointercancel', 'lostpointercapture']) b.addEventListener(ev, () => setCompare(false));
  b.addEventListener('keydown', (e) => {
    if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault();
      if (!e.repeat) setCompare(true);
    }
  });
  b.addEventListener('keyup', (e) => {
    if (e.key === ' ' || e.key === 'Enter') setCompare(false);
  });
  b.addEventListener('contextmenu', (e) => e.preventDefault());
}

// ── boot ────────────────────────────────────────────────────────────────────

function decorate() {
  $('.add-icon').replaceWith(icon('plus', 16));
  $('.dz-icon').append(icon('image', 40));
  $('.dz-shield').replaceWith(icon('shield', 15));
  $('.compare-icon').replaceWith(icon('compare', 16));
  $('#help-btn').append(icon('keyboard', 18));
  $('#help-close').append(icon('x', 16));
  $('#help-btn').addEventListener('click', () => el.help.showModal());
  $('#help-close').addEventListener('click', () => el.help.close());
  el.help.addEventListener('click', (e) => { if (e.target === el.help) el.help.close(); });
  if (IS_MAC) for (const k of document.querySelectorAll('.kbd-mod')) k.textContent = '⌘';
  const today = new Date();
  const date = $('#dateline-date');
  date.textContent = new Intl.DateTimeFormat(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }).format(today);
  date.dateTime = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  initTheme($('#theme-btn'));
}

function boot() {
  decorate();
  buildRail();
  buildExport();
  refreshPanel();
  initIntake();
  initKeyboard();
  pinPanelOnPhones();
  changed({ preview: false });
  loadCapabilities();
  loadPresets();
  // leaving deletes every upload (below), so warn first when crops or edits would be lost
  window.addEventListener('beforeunload', (e) => {
    if (!state.files.length || !(state.files.some((f) => f.crop) || describeEdits(activeFile()).length)) return;
    e.preventDefault();
    e.returnValue = '';
  });
  // privacy: when the tab really goes away, delete this session's uploads right away (TTL is the fallback)
  window.addEventListener('pagehide', (e) => {
    if (e.persisted) return;
    removals.flush({ keepalive: true });
    for (const f of state.files) api.deleteFile(f.id, { keepalive: true });
  });
}

boot();
