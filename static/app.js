// heyp UI entry point: wires state, tools, canvas, preview, intake and export together.

import { h, $, clamp, debounce, fmtBytes, isTyping, replaceIfChanged } from './js/dom.js';
import { icon } from './js/icons.js';
import { api, imageRetryDelay } from './js/api.js';
import {
  state, activeFile, buildOps, buildOutput, preCropOps, prePassportOps, framingOf, describeEdits, baseDims, ratioValue,
  passportActive, rotateQuarter, cropRatioFor, exportRequest, exportLimitError, warningSummary, qualityApplies,
  planUploads, FORMAT_LABEL, newId, toolEntry, hasHeavyOps, upscaleActive, exportNeedsJob, hasJobOps, upscaleProblems,
  upscaleInputDims, stillAnimations,
} from './js/state.js';
import { deleteKeyAction } from './js/keys.js';
import { Canvas, isSourceFrame, zoomPivot } from './js/canvas.js';
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
import upscaleTool from './js/tools/upscale.js';
import watermarkTool from './js/tools/watermark.js';
import blurTool from './js/tools/blur.js';
import editorTool from './js/tools/editor.js';
import memeTool from './js/tools/meme.js';
import gifTool, { GIF_TOOL } from './js/tools/gif.js';
import htmlTool from './js/tools/html.js';
import { Editing, EDIT_STAGE, stageBaseOps } from './js/editing.js';
import { normalizeDetections, mergeRegions, loupeRegion } from './js/stagekit.js';
import { jobs } from './js/jobs.js';
import { startRun, getRun, resultFile, continueMenu } from './js/jobui.js';
import { copyFor, expiryReason, siblingUrl } from './js/config.js';
import { continueTargets, outputKind, HEYP_CONTINUE } from './js/continue.js';
import { FILTERS, FRAMES, adoptEnums } from './js/catalog.js';

/** Sections in rail order; 1–9 switch to the first nine, the "?" dialog lists them all. */
const TOOLS = [
  convertTool, resizeTool, cropTool, passportTool, compressTool, editTool, backgroundTool, upscaleTool, watermarkTool,
  blurTool, editorTool, memeTool, gifTool, htmlTool, infoTool,
];
const SHORTCUTS = 9;
/** Section kickers above each tool headline ("04 · Passport & ID"). */
const KICKER = {
  convert: 'File format', resize: 'Size & print', crop: 'Framing', passport: 'Passport & ID',
  compress: 'File size', edit: 'Orientation & tone', background: 'Cut-out', info: 'Inside the file',
};
const kickerOf = (t) => t.kicker || KICKER[t.id] || t.title;
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

/** The v2 editing layer (blur boxes, watermark, meme captions, editor layers, upscale detail square). */
const editing = new Editing(canvas, {
  changed: (opts) => changed(opts),
  selection: () => {
    if (state.tool === 'editor') refreshPanel();
    else syncPanel();
  },
  activate: (it) => {
    if (it?.kind === 'text') editorTool.focusText();
  },
  logoSrc: () => (state.watermark.imageId ? api.previewUrl(state.watermark.imageId, 800) : ''),
  stickerSrc: (id) => stickerUrl(id),
  loupeDims: () => upscaleInput(),
  notify: (msg) => toast(msg, 'warn'),
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
  // v2 surface (see the v2 section below)
  copy: () => copyFor(state.config),
  hasFiles: () => state.files.length > 0,
  hasEdits: () => state.files.some((f) => buildOps(f).length > 0),
  thumbUrl: (id, max = 240) => api.previewUrl(id, max),
  catalog: () => editorCatalog(),
  uploadAux,
  dropAux,
  auxExpired,
  newBlank,
  detect,
  detectBusy: () => detectBusy,
  selectedItem: () => editing.selected,
  selectItem,
  addItem,
  removeItem: (id) => editing.remove(id),
  moveItem,
  moveFileTo,
  upscaleInput,
  loupeView,
  runTool,
  makeGif,
  gifAppliesEdits,
  canApplyEdits: () => Boolean(processTool()),
  resultView,
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
      type: 'button', class: 'rail-btn', dataset: { tool: t.id }, title: i < SHORTCUTS ? `${t.title} (${i + 1})` : t.title,
      onclick: () => setTool(t.id),
    }, icon(t.icon, 18), h('span', { class: 'rail-label' }, t.rail || t.title), h('span', { class: 'rail-dot', 'aria-hidden': 'true' }), h('kbd', { class: 'rail-kbd', 'aria-hidden': 'true' }, String(i + 1).padStart(2, '0'))))));
  buildSectionIndex();
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
    // the name starts with the visible label (WCAG 2.5.3): "Web page (HTML → image)", "Resize (has edits)"
    const name = t.rail && t.rail !== t.title ? `${t.rail} (${t.title})` : t.title;
    btn.setAttribute('aria-label', `${name}${active ? ' (has edits)' : ''}`);
  }
}

function setTool(id) {
  if (!TOOLS.some((t) => t.id === id) || state.tool === id) return;
  if (comparing) setCompare(false);
  currentTool().leave?.(app);
  state.tool = id;
  editing.selected = null;
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
  el.panel.querySelector('.tool-head')?.prepend(h('p', { class: 'tool-kicker' }, `${String(TOOLS.indexOf(tool) + 1).padStart(2, '0')} · ${kickerOf(tool)}`));
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

/**
 * A server render of the ops before a tool's own op, kept on the file in `f[slot]` (keyed by the ops):
 * the crop tool draws on the pre-crop image ('cropBase'), the passport context view shows the image
 * entering the passport op ('passportBase'). No ops → the original preview. Null while it is fetched.
 */
function opsBase(f, slot, ops) {
  if (!ops.length) return { src: api.previewUrl(f.id), w: f.info.width, h: f.info.height };
  const key = JSON.stringify(ops);
  if (f[slot]?.key === key) return f[slot];
  if (f[`${slot}Failed`] !== key) ensureBase(f, slot, ops, key);
  return null;
}

/** Image the crop tool draws on: original preview, or a server render of the pre-crop ops. */
const cropBase = (f) => opsBase(f, 'cropBase', preCropOps(f));

let baseSeq = 0;
let baseCtrl = null;
async function ensureBase(f, slot, ops, key) {
  const pending = `${slot}Pending`;
  if (f[pending] === key) return;
  baseCtrl?.abort();
  baseCtrl = new AbortController();
  const my = ++baseSeq;
  f[pending] = key;
  setBasePending(true);
  try {
    const res = await api.preview({ ids: [f.id], ops, output: { format: 'jpeg', quality: 85, strip_metadata: true } }, baseCtrl.signal);
    if (my !== baseSeq) return;
    f[slot] = { key, src: res.image, w: res.width, h: res.height };
    if (f.id === state.activeId) {
      renderCanvas();
      syncPanel();
    }
  } catch (e) {
    if (e?.name === 'AbortError' || my !== baseSeq) return;
    if (slot === 'passportBase' && e?.status !== 404) {
      // only the surroundings are missing: keep the plain view, and don't ask again for these edits
      // unless the server was merely busy
      if (e?.status !== 503) f[`${slot}Failed`] = key;
      return;
    }
    handleFileError(e, f);
  } finally {
    if (f[pending] === key) f[pending] = null;
    if (my === baseSeq) setBasePending(false);
  }
}

function renderCanvas() {
  const f = activeFile();
  document.body.classList.toggle('has-files', state.files.length > 0);
  el.compare.hidden = !f || cropBoxMode();
  if (!panFor(f)) canvas.setPan(null);
  if (!f || comparing || cropBoxMode()) editing.update({ tool: null });
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
  if (renderStageBase(f)) return;
  let badge = '';
  if (state.tool === 'crop' && f.crop && !passportActive()) badge = f.crop.kind === 'trim' ? 'Auto trim' : `${f.crop.anchor === 'face' ? 'Face' : 'Smart'} crop`;
  canvas.setBadge(badge);
  const p = f.preview;
  const src = p?.image || api.previewUrl(f.id);
  if (!guidesFor(f)) canvas.setGuides(null);
  canvas.show(src, p ? { w: p.width, h: p.height } : infoDims(f), f.name, { context: contextFor(f) }).then(() => {
    if (canvas.src === src && activeFile() === f && !comparing) canvas.setGuides(guidesFor(f));
    if (canvas.src === src && activeFile() === f && !comparing) syncEditing(f, previewFresh(f));
  });
  if (panFor(f)) canvas.setPan(PAN);
}

// ── drag-to-align (passport) ─────────────────────────────────────────────────

const PAN_LIMIT = 0.25; // same range as the position sliders
const ZOOM_MIN = 0.85; // same range as the head-size slider
const ZOOM_MAX = 1.15;
const round3 = (v) => Math.round(v * 1000) / 1000;
const PAN = {
  onCommit(fx, fy) {
    const p = state.passport;
    p.offsetX = round3(clamp(p.offsetX + fx, -PAN_LIMIT, PAN_LIMIT));
    p.offsetY = round3(clamp(p.offsetY + fy, -PAN_LIMIT, PAN_LIMIT));
    changed();
  },
  /** The part of a drag (fractions of the frame) that stays within the position limits. */
  clampMove(fx, fy) {
    const p = state.passport;
    return [clamp(p.offsetX + fx, -PAN_LIMIT, PAN_LIMIT) - p.offsetX, clamp(p.offsetY + fy, -PAN_LIMIT, PAN_LIMIT) - p.offsetY];
  },
  /** Head size + step; false when that changes nothing (at the limit, or a step too small to register). */
  onZoom(step) {
    const p = state.passport;
    const zoom = round3(clamp(p.zoom + step, ZOOM_MIN, ZOOM_MAX));
    if (zoom === p.zoom) return false;
    p.zoom = zoom;
    changed();
    return true;
  },
  /** Touch pinch: head size × ratio. */
  onZoomBy(ratio) {
    const p = state.passport;
    p.zoom = round3(clamp(p.zoom * ratio, ZOOM_MIN, ZOOM_MAX));
    changed();
  },
  clampScale(ratio) {
    const z = state.passport.zoom;
    return clamp(z * ratio, ZOOM_MIN, ZOOM_MAX) / z;
  },
  onReset() {
    Object.assign(state.passport, { offsetX: 0, offsetY: 0, zoom: 1 });
    changed();
  },
};

/**
 * Passport context view (whole photo around a fixed frame) for the shown preview, or null for the plain
 * view: needs `passport.source` from the server and the pre-passport image the preview was made from.
 * `pending` is what the settings changed since that preview (drag, keys, wheel, sliders), shown live.
 */
function contextFor(f) {
  if (!panFor(f)) return null;
  const pp = f.preview.passport;
  const fr = f.previewFraming;
  if (!fr || !isSourceFrame(pp.source)) return null;
  const ops = prePassportOps(f);
  if (!ops || JSON.stringify(ops) !== fr.baseKey) return null; // edits before the frame changed: wait for both renders
  const base = opsBase(f, 'passportBase', ops);
  if (!base) return null;
  const p = state.passport;
  return {
    src: base.src,
    source: pp.source,
    out: { w: pp.width, h: pp.height },
    pending: { fx: p.offsetX - fr.offsetX, fy: p.offsetY - fr.offsetY, r: fr.zoom > 0 ? p.zoom / fr.zoom : 1 },
    pivot: zoomPivot(pp, fr.offsetX, fr.offsetY),
  };
}

/** The photo can be dragged into place while the shown preview is a single passport frame. */
function panFor(f) {
  return Boolean(f?.preview?.passport && !comparing && state.tool === 'passport' && passportActive() && state.passport.layout === 'single');
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
  return { ids: [f.id], ops: buildOps(f, { preview: true }), output }; // heavy ops (upscale) run on export only
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
    f.previewFraming = framingOf(req.ops); // what the passport context view measures pending moves against
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
  toast(`${what} no longer on the server (${expiryReason(state.config)}). Add ${list.length === 1 ? 'it' : 'them'} again to keep working.`, 'warn', { timeout: 10000 });
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
        }, { accept: 'image' }); // anything but an image (an HTML page, a PDF) is refused with a reason
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
    state.files.push(fileRecord(info));
  }
  if (added.length) {
    state.activeId = null;
    selectFile(added[0].id);
  }
  reportUploadErrors(errors);
}

function reportUploadErrors(errors) {
  if (!errors.length) return;
  // a PDF isn't "damaged": it belongs in heypdf, a separate site (a plain link — nothing is sent there)
  const pdfs = errors.filter((e) => /\.pdf$/i.test(String(e.name || '')));
  if (pdfs.length) {
    errors = errors.filter((e) => !pdfs.includes(e));
    const sibling = siblingUrl(state.config);
    const what = pdfs.length === 1 ? `“${pdfs[0].name}” is a PDF` : `${pdfs.length} files are PDFs`;
    toast(`${what} — heyp opens images only.${sibling ? ' heypdf, a separate site, has the PDF tools: add the file there.' : ''}`, 'warn', {
      timeout: 10000,
      action: sibling ? { label: 'Open heypdf', onClick: () => window.open(sibling, '_blank', 'noopener') } : undefined,
    });
    if (!errors.length) return;
  }
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
    exportUi.keepMeta, exportUi.keepGps, exportUi.btn, exportUi.meta, exportUi.cont = h('div', { class: 'export-continue' }));
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
  const up = upscaleActive() ? state.upscale.factor : 0; // previews leave the upscale out: say what the file will be
  const facts = !p ? '' : up ? `${p.width * up}×${p.height * up} · upscaled ${up}× on download` : [`${p.width}×${p.height}`, fmtBytes(p.size_bytes), achievedQ(p)].filter(Boolean).join(' · ');
  const notes = exportNotes();
  setText(exportUi.meta, [!n ? 'Add photos to export.' : p && n === 1 ? facts : p ? `This photo: ${facts}` : '', ...notes].filter(Boolean).join(' · '));
  exportUi.meta.classList.toggle('is-warn', notes.length > 0);
  syncContinue();
}

/** What the export will do that the preview of one photo doesn't show: photos too large for the upscale, animations kept as stills. */
function exportNotes() {
  const notes = [];
  const files = state.files;
  if (upscaleActive() && files.length > 1) {
    const bad = upscaleProblems(files, upscaleInputDims);
    if (bad.length) notes.push(`${bad.length} photo${bad.length > 1 ? 's are' : ' is'} too large for ${state.upscale.factor}×`);
  }
  const stills = stillAnimations(files);
  // named when it isn't the photo on screen ("This photo: …" sits right before this note)
  const which = stills.length > 1 ? `${stills.length} animations` : files.length > 1 ? `“${stills[0]?.name}”` : 'The animation';
  if (stills.length) notes.push(`${which} will keep only the first frame in ${FORMAT_LABEL[state.output.format] || state.output.format} — choose GIF, WebP or Same as original to keep ${stills.length > 1 ? 'them' : 'it'} moving`);
  return notes;
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
  if (upscaleActive()) {
    // the panel checks the photo on screen; the server refuses the whole batch for one photo that's too large
    const bad = upscaleProblems(files, upscaleInputDims);
    if (bad.length) {
      const names = bad.slice(0, 3).map((x) => `“${x.file.name}”`).join(', ') + (bad.length > 3 ? ` and ${bad.length - 3} more` : '');
      toast(`${names} ${bad.length > 1 ? 'are' : 'is'} too large for a ${state.upscale.factor}× upscale. Choose 2×, or crop or resize ${bad.length > 1 ? 'them' : 'it'} first.`, 'error', { timeout: 10000 });
      return;
    }
  }
  const req = exportRequest(files);
  beginExport(exportBusyText(files));
  try {
    // several photos or slow edits run as a job: progress, and no proxy cuts the request off after 60–100 s
    if (processTool() && exportNeedsJob(files)) {
      await exportViaJob(req, files);
    } else {
      try {
        const res = await api.process(req);
        saveBlob(res.blob, res.filename || fallbackName(files, req.output));
        if (res.warnings.length) toast(warningSummary(res.warnings), 'warn', { timeout: 10000 });
      } catch (e) {
        if (!(e?.useJob === PROCESS_TOOL && processTool())) throw e;
        await exportViaJob(req, files); // the server runs this one as a job (hosted mode, a long export)
      }
    }
    exportUi.done = true;
    syncContinue();
  } catch (e) {
    if (e?.name === 'AbortError' || e?.cancelled) return;
    if (e?.status === 404) await findExpired(files, e);
    else toast(e?.detail || 'Export failed', 'error');
  } finally {
    endExport();
  }
}

/** Registry tool that runs the export pipeline as a job (SSE progress). */
const PROCESS_TOOL = 'img.process';

/** The img.process registry entry when the server offers it (and it is available), else null. */
function processTool() {
  const t = toolEntry(PROCESS_TOOL);
  return t && t.available !== false ? t : null;
}

/** img.process options for a ProcessRequest (`ids` become the job's file_ids). */
const processOptions = (req) => ({ ops: req.ops, ...(req.ops_by_id ? { ops_by_id: req.ops_by_id } : {}), output: req.output });

/** Job outputs this page created and hasn't handed on: deleted with the page (pagehide) like the uploads. */
const resultFiles = new Set();

function trackResults(res) {
  for (const f of res?.files || []) if (f?.id) resultFiles.add(f.id);
  if (res?.zip?.id) resultFiles.add(res.zip.id);
}

/** Run the export pipeline as a job with the progress on the Download button; resolves the job result. */
async function runProcessJob(req, { label = 'Exporting', signal } = {}) {
  const res = await jobs.run(
    { tool: PROCESS_TOOL, file_ids: req.ids, options: processOptions(req) },
    {
      signal,
      onQueued: (pos) => setText(exportUi.label, pos > 0 ? `Waiting — ${pos} ahead…` : 'Starting…'),
      onProgress: (p, msg) => setText(exportUi.label, `${msg || label} ${Math.round(Math.min(1, p) * 100)}%`),
    },
  );
  trackResults(res);
  return res;
}

async function exportViaJob(req, files) {
  const res = await runProcessJob(req, { label: hasHeavyOps(files) ? 'Upscaling' : 'Exporting' });
  const out = res.zip || res.files?.[0];
  if (!out?.id) throw new Error('The export finished without a file');
  // a ZIP or combined PDF gets the name a direct download would have had
  const name = res.zip || files.length > 1 ? fallbackName(files, req.output) : out.name || fallbackName(files, req.output);
  const a = h('a', { href: api.downloadUrl(out.id), download: name, hidden: true });
  document.body.append(a);
  a.click();
  a.remove();
  if (res.warnings?.length) toast(warningSummary(res.warnings), 'warn', { timeout: 10000 });
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
    if (!typing && editing.active && e.target.closest?.('.ov-layer')) return; // the editing layer handles its own keys
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
    if (/^[1-9]$/.test(e.key) && Number(e.key) <= Math.min(SHORTCUTS, TOOLS.length)) {
      e.preventDefault();
      setTool(TOOLS[Number(e.key) - 1].id);
    } else if (key === 'r') {
      e.preventDefault();
      rotateQuarter(e.shiftKey ? -1 : 1);
      changed({ panel: state.tool === 'edit' || state.tool === 'crop' });
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      // only from "neutral" focus (page, canvas, strip) so a stray key on a slider never drops a photo; while a
      // section edits on the photo (blur boxes, layers) the key belongs to the selected layer, never the photo
      const t = e.target;
      const action = deleteKeyAction({
        hasPhoto: Boolean(state.activeId),
        target: { body: t === document.body || Boolean(t.closest?.('.compare-btn')), inStrip: Boolean(t.closest?.('.thumbs')) },
        editing: { active: editing.active, selected: editing.selected, removable: state.tool === 'blur' || state.tool === 'editor' },
      });
      if (!action) return;
      e.preventDefault();
      if (action === 'layer') editing.remove(editing.selected);
      else removeFile(state.activeId, { undo: true });
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

// ── v2: files, editing layer, detection, upscale check, jobs, continue, hosted copy ──────────────────────

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

/** A photo in the strip (upload or job result). */
function fileRecord(info) {
  return {
    id: info.id, name: info.name, info, crop: null, preview: null, previewKey: null, previewFraming: null, previewError: null,
    cropBase: null, cropBasePending: null, passportBase: null, passportBasePending: null, full: null, regions: [], items: [],
  };
}

const isImageInfo = (info) => !info?.kind || info.kind === 'image';

/** The shown preview is the render of exactly the current settings. */
function previewFresh(f) {
  return Boolean(f && !previewPending && !f.previewError && f.previewKey === JSON.stringify(previewRequest(f)));
}

/** Point the editing layer at the active photo for the open section (after the picture is laid out). */
function syncEditing(f, settled) {
  if (!f || comparing || cropBoxMode() || f !== activeFile()) {
    editing.update({ tool: null });
    return;
  }
  editing.update({ tool: EDIT_STAGE[state.tool] ? state.tool : null, file: f, settled });
}

const STAGE_BADGE = { blur: 'Before rotate & crop', watermark: 'Before the sheet layout', meme: 'Before later edits', editor: 'Before later edits', upscale: 'Before later edits' };

/**
 * Sections that draw on the picture: when later edits move the frame, show the image entering the section's
 * stage instead of the finished preview, so boxes and layers line up. Returns true when it took over the canvas.
 */
function renderStageBase(f) {
  const stage = EDIT_STAGE[state.tool];
  if (!stage) return false;
  const ops = stageBaseOps(f, stage);
  if (!ops) return false;
  canvas.setGuides(null);
  canvas.setBadge(STAGE_BADGE[state.tool] || 'Before later edits');
  const base = opsBase(f, `${stage}Base`, ops);
  if (!base) {
    editing.update({ tool: null });
    const p = f.preview;
    canvas.show(p?.image || api.previewUrl(f.id), p ? { w: p.width, h: p.height } : infoDims(f), f.name);
    return true;
  }
  canvas.show(base.src, { w: base.w, h: base.h }, `${f.name}, before later edits`).then(() => {
    if (canvas.src === base.src && activeFile() === f && !comparing) syncEditing(f, false);
  });
  return true;
}

function exportBusyText(files) {
  if (hasHeavyOps(files)) return files.length > 1 ? `Upscaling ${files.length} photos… this takes a while` : 'Upscaling… this can take a minute';
  if (hasJobOps(files)) return files.length > 1 ? `Working on ${files.length} photos… this takes a while` : 'Removing the background…';
  return files.length > 1 ? `Exporting ${files.length} photos…` : 'Exporting…';
}

// ── sections index (help dialog) ─────────────────────────────────────────────

function buildSectionIndex() {
  const dlg = el.help;
  let list = dlg.querySelector('.section-index');
  if (!list) {
    list = h('ol', { class: 'section-index', 'aria-label': 'Every section' });
    dlg.append(h('div', { class: 'section-index-wrap' }, h('h3', { class: 'group-title' }, 'Every section'), list));
  }
  list.replaceChildren(...TOOLS.map((t, i) => h('li', {},
    h('button', {
      type: 'button', class: 'section-link', onclick: () => { dlg.close(); setTool(t.id); },
    }, h('span', { class: 'section-no' }, String(i + 1).padStart(2, '0')), h('span', { class: 'section-name' }, t.title),
    i < SHORTCUTS ? h('kbd', {}, String(i + 1)) : null))));
}

// ── editor catalogue (filters, frames, stickers from the server) ─────────────

function editorCatalog() {
  const cat = state.catalog;
  const ids = (list) => (Array.isArray(list) ? list.map((x) => x?.id).filter((x) => typeof x === 'string') : null);
  const fIds = ids(cat?.filters);
  const frIds = ids(cat?.frames);
  const stickers = (Array.isArray(cat?.stickers) ? cat.stickers : [])
    .filter((s) => typeof s?.id === 'string' && /^[\w-]+(\/[\w-]+)*$/.test(s.id))
    .map((s) => ({ ...s, name: String(s.name || s.id), url: stickerUrl(s.id) }));
  return {
    filters: fIds ? adoptEnums(FILTERS, ['none', ...fIds.filter((x) => x !== 'none')]) : FILTERS,
    frames: frIds ? adoptEnums(FRAMES, ['none', ...frIds.filter((x) => x !== 'none')]) : FRAMES,
    stickers,
    stickersLoading: cat === undefined,
    stickerCredit: stickers.some((s) => s.source === 'openmoji') ? 'Emoji stickers by OpenMoji (CC BY-SA 4.0) — see Credits.' : '',
  };
}

/** Same-origin URL of a bundled sticker's SVG (catalogue `url` when the server gives one). */
function stickerUrl(id) {
  const s = Array.isArray(state.catalog?.stickers) ? state.catalog.stickers.find((x) => x?.id === id) : null;
  if (typeof s?.url === 'string' && /^\/[^\s"'<>]*$/.test(s.url)) return s.url;
  return `/api/assets/stickers/${String(id).split('/').map(encodeURIComponent).join('/')}.svg`;
}

// ── side files (watermark logo): uploaded, never in the strip ───────────────

const auxFiles = new Set();
const LOGO_TYPES = /^image\/(png|webp|svg\+xml|jpeg|gif|avif)$/;

async function uploadAux(file) {
  if (!file) return null;
  if (file.type && !LOGO_TYPES.test(file.type)) {
    toast('Choose a PNG, SVG, WebP, JPG, GIF or AVIF image for the logo.', 'error');
    return null;
  }
  const max = state.limits?.max_upload_mb > 0 ? state.limits.max_upload_mb : 25;
  if (file.size > Math.min(max, 25) * 1024 * 1024) {
    toast(`That logo is too large (max ${Math.min(max, 25)} MB).`, 'error');
    return null;
  }
  try {
    const res = await api.upload([file], null, { accept: 'image' });
    const info = res.files[0];
    if (!info) {
      toast(`Couldn’t use “${file.name}”: ${res.errors[0]?.detail || 'unsupported file'}`, 'error');
      return null;
    }
    auxFiles.add(info.id);
    return info;
  } catch (e) {
    toast(e?.detail || 'Upload failed', 'error');
    return null;
  }
}

function dropAux(id) {
  if (!id || !auxFiles.has(id)) return;
  auxFiles.delete(id);
  api.deleteFile(id);
}

function auxExpired(kind) {
  if (kind !== 'watermark' || !state.watermark.imageId) return;
  auxFiles.delete(state.watermark.imageId);
  Object.assign(state.watermark, { imageId: null, imageName: '', imageW: 0, imageH: 0 });
  toast('The watermark logo is no longer on the server. Choose it again.', 'warn');
  changed({ panel: true });
}

/** A blank canvas (meme without a photo) is made here and added like any other picture. */
function newBlank(c, color = '#ffffff') {
  const cv = document.createElement('canvas');
  cv.width = c.w;
  cv.height = c.h;
  const g = cv.getContext('2d');
  g.fillStyle = color;
  g.fillRect(0, 0, c.w, c.h);
  cv.toBlob((b) => {
    if (b) addFiles([new File([b], `blank-${c.id}.png`, { type: 'image/png' })]);
    else toast('Couldn’t make a blank canvas in this browser.', 'error');
  }, 'image/png');
}

// ── blur: detection ─────────────────────────────────────────────────────────

let detectBusy = null;

async function detect({ what, all = false }) {
  const kinds = (what || []).filter((k) => k === 'faces' || k === 'plates');
  if (detectBusy || !kinds.length) return;
  const files = all ? state.files.slice() : [activeFile()].filter(Boolean);
  if (!files.length) return;
  detectBusy = kinds.length > 1 ? 'both' : kinds[0];
  refreshPanel();
  const found = { face: 0, plate: 0 };
  const gone = [];
  const missing = new Set();
  let failed = null;
  try {
    for (const f of files) {
      try {
        let res = null;
        for (let attempt = 0; !res; attempt++) {
          try {
            res = await api.detect(f.id, kinds);
          } catch (e) {
            if (e?.status === 503 && e.retryAfter && attempt < 2) {
              await sleep(Math.min(10, e.retryAfter) * 1000);
              continue;
            }
            throw e;
          }
        }
        for (const u of res.unavailable || []) missing.add(u);
        const { list, added } = mergeRegions(f.regions, normalizeDetections(res, infoDims(f)), () => newId('r'));
        f.regions = list;
        for (const r of list.slice(list.length - added)) found[r.source] = (found[r.source] || 0) + 1;
      } catch (e) {
        if (e?.status === 404) gone.push(f);
        else failed = e;
      }
    }
  } finally {
    detectBusy = null;
  }
  if (gone.length) expire(gone);
  changed({ panel: true });
  const parts = [];
  if (kinds.includes('faces')) parts.push(`${found.face} face${found.face === 1 ? '' : 's'}`);
  if (kinds.includes('plates')) parts.push(`${found.plate} plate${found.plate === 1 ? '' : 's'}`);
  if (failed) toast(`Detection failed: ${failed.detail || 'unknown error'}`, 'error');
  else if (missing.size) toast(`${[...missing].map((m) => (m === 'faces' ? 'Face' : 'Plate')).join(' and ')} detection isn’t installed on this server.`, 'warn');
  else toast(found.face + found.plate ? `Found ${parts.join(' and ')}${all ? ` in ${files.length} photos` : ''}. Untick any you want to keep visible.` : `No new ${parts.map((p) => p.replace(/^\d+ /, '')).join(' or ')} found — draw a box over anything else.`);
}

// ── editor layers ───────────────────────────────────────────────────────────

const TAB_OF = { text: 'text', sticker: 'stickers', shape: 'shapes', brush: 'brush' };

function selectItem(id, { tab = false } = {}) {
  const f = activeFile();
  if (!f) return;
  const it = (f.items || []).find((x) => x.id === id) || (f.regions || []).find((x) => x.id === id);
  if (!it) return;
  editing.selected = id;
  if (tab && state.tool === 'editor' && TAB_OF[it.kind] && state.editor.tab !== TAB_OF[it.kind]) state.editor.tab = TAB_OF[it.kind];
  changed({ panel: true, preview: false });
  editing.select(id, { focus: true });
}

function addItem(item, { focusText = false } = {}) {
  const f = activeFile();
  if (!f) return;
  f.items ||= [];
  if (f.items.length >= 100) {
    toast('At most 100 layers per photo.', 'warn');
    return;
  }
  const id = newId('o');
  const ratio = item.kind === 'sticker' ? (item.ratio || 1) : undefined;
  f.items.push({ cx: 0.5, cy: 0.5, w: 0.2, h: 0.1, rotation: 0, opacity: 1, ...item, ...(ratio ? { ratio } : {}), id });
  editing.selected = id;
  changed({ panel: true });
  if (focusText) requestAnimationFrame(() => editorTool.focusText());
}

function moveItem(id, dir) {
  const f = activeFile();
  const list = f?.items || [];
  const i = list.findIndex((x) => x.id === id);
  const j = i + (dir > 0 ? 1 : -1);
  if (i < 0 || j < 0 || j >= list.length) return;
  [list[i], list[j]] = [list[j], list[i]];
  changed({ panel: true });
}

function moveFileTo(id, index) {
  const i = state.files.findIndex((f) => f.id === id);
  if (i < 0) return;
  const to = clamp(index, 0, state.files.length - 1);
  if (to === i) return;
  const [f] = state.files.splice(i, 1);
  state.files.splice(to, 0, f);
  changed({ panel: true, preview: false });
}

// ── upscale: input size and the detail check ────────────────────────────────

function upscaleInput() {
  const f = activeFile();
  if (!f) return null;
  const ops = stageBaseOps(f, 'upscale');
  if (ops) {
    const b = opsBase(f, 'upscaleBase', ops);
    return b ? { w: b.w, h: b.h } : null;
  }
  const p = f.preview;
  if (p?.width && p?.height) return { w: p.width, h: p.height };
  return buildOps(f, { preview: true }).length ? null : infoDims(f);
}

const loupe = { key: null, src: null, error: null, pending: false, ctrl: null, timer: 0 };

/** Before/after of the detail square: the browser's enlargement vs. the model's, rendered by the server. */
function loupeView() {
  const f = activeFile();
  const dims = upscaleInput();
  if (!f || !dims || !(state.upscale.factor > 1) || passportActive()) return null;
  const region = loupeRegion(state.upscale.loupe, dims.w, dims.h);
  const ops = stageBaseOps(f, 'upscale');
  const base = ops ? opsBase(f, 'upscaleBase', ops) : null;
  const beforeSrc = base?.src || f.preview?.image || api.previewUrl(f.id);
  const pre = buildOps(f, { until: 'upscale', preview: true });
  const req = {
    ids: [f.id],
    ops: [...pre, { op: 'crop', unit: 'fraction', x: region.x, y: region.y, width: region.w, height: region.h },
      { op: 'upscale', factor: state.upscale.factor, model: state.upscale.model }],
    output: { format: 'png', strip_metadata: true },
  };
  const key = JSON.stringify(req);
  if (key !== loupe.key) {
    loupe.key = key;
    loupe.src = null;
    loupe.error = null;
    loupe.pending = true;
    clearTimeout(loupe.timer);
    loupe.ctrl?.abort();
    loupe.timer = setTimeout(() => fetchLoupe(req, key), 450);
  }
  return { region, beforeSrc, afterSrc: loupe.src, pending: loupe.pending, error: loupe.error };
}

async function fetchLoupe(req, key) {
  loupe.ctrl = new AbortController();
  try {
    const res = await api.preview(req, loupe.ctrl.signal, 800);
    if (loupe.key !== key) return;
    loupe.src = res.image;
  } catch (e) {
    if (e?.name === 'AbortError' || loupe.key !== key) return;
    loupe.error = e?.detail || 'unknown error';
  }
  loupe.pending = false;
  if (state.tool === 'upscale') syncPanel();
}

// ── jobs (Make GIF, HTML → image) and their results ─────────────────────────

function runTool(key, toolId, ids, options, { addToStrip = false, exec = null } = {}) {
  const title = TOOLS.find((t) => t.id === key)?.title || 'The job';
  const run = exec || (({ signal, onQueued, onProgress }) => jobs.run({ tool: toolId, file_ids: ids, options }, { signal, onQueued, onProgress }));
  startRun(key, (ctl) => run(ctl).then((res) => { trackResults(res); return res; }))
    .then(async (res) => {
      if (!res) {
        const e = getRun(key).error;
        if (e?.status === 404 && ids.length) {
          const gone = [];
          await Promise.all(state.files.filter((f) => ids.includes(f.id)).map((f) => api.fileInfo(f.id).catch((err) => { if (err?.status === 404) gone.push(f); })));
          if (gone.length) expire(gone);
        }
        if (e && state.tool !== key) toast(`${title} failed: ${e.detail || 'unknown error'}`, 'error');
        return;
      }
      if (res.warnings?.length) toast(warningSummary(res.warnings), 'warn', { timeout: 10000 });
      const files = (res.files || []).filter(isImageInfo);
      if (addToStrip && files.length) addResultFiles(files, { select: true });
      else if (state.tool !== key) toast(`${title} is ready.`, 'info', { action: { label: 'Open', onClick: () => setTool(key) } });
    });
}

/**
 * Make GIF (UIH-04): unless switched off, frames get the other sections' edits first — the img.process job renders
 * them (PNG), the GIF is made from those renders, and the renders are deleted again. Without edits (or without
 * img.process) the frames are the photos as uploaded.
 */
function makeGif(frames, options) {
  const ids = frames.map((f) => f.id);
  if (!gifAppliesEdits(frames)) {
    runTool('gif', GIF_TOOL, ids, options);
    return;
  }
  runTool('gif', GIF_TOOL, ids, options, {
    exec: async ({ signal, onQueued, onProgress }) => {
      const req = exportRequest(frames);
      req.output = { ...req.output, format: 'png', max_kb: null, min_kb: null, filename: null, combine_pdf: false };
      const baked = await jobs.run({ tool: PROCESS_TOOL, file_ids: req.ids, options: processOptions(req) }, {
        signal, onQueued, onProgress: (p) => onProgress(Math.min(1, p) * 0.6, 'Applying your edits to the frames'),
      });
      const tmp = (baked.files || []).map((f) => f.id);
      try {
        if (tmp.length !== ids.length) throw new Error('Some frames could not be prepared');
        const res = await jobs.run({ tool: GIF_TOOL, file_ids: tmp, options }, {
          signal, onQueued, onProgress: (p, msg) => onProgress(0.6 + Math.min(1, p) * 0.4, msg),
        });
        return { ...res, warnings: [...new Set([...(baked.warnings || []), ...(res.warnings || [])])] };
      } finally {
        for (const id of tmp) api.deleteFile(id); // intermediate renders: only the GIF is kept
      }
    },
  });
}

/** Whether Make GIF will apply the other sections' edits to these frames. */
function gifAppliesEdits(frames) {
  return state.gif.applyEdits !== false && Boolean(processTool()) && frames.some((f) => buildOps(f).length > 0);
}

/** Put job results into the strip so every section can work on them. */
function addResultFiles(infos, { select = true } = {}) {
  const fresh = infos.filter((i) => isImageInfo(i) && !state.files.some((f) => f.id === i.id));
  for (const info of fresh) state.files.push(fileRecord(info));
  if (select && infos[0]) {
    state.activeId = null;
    selectFile(infos[0].id);
  }
  changed({ panel: true });
  return fresh.length;
}

/** heyp sections that take `kind` results (Make GIF needs at least two frames). */
function continueSections(kind) {
  if (kind !== 'image') return [];
  return TOOLS.filter((t) => HEYP_CONTINUE.includes(t.id))
    .map((t) => ({ id: t.id, title: t.title, accepts: (k, n) => k === 'image' && (t.id !== 'gif' || n >= 2) }));
}

function resultTargets(files, exclude) {
  const first = files[0] || {};
  const fmt = String(first.format || first.name?.split('.').pop() || '').toLowerCase();
  const kind = first.kind || (fmt === 'pdf' ? 'pdf' : 'image');
  return continueTargets({ kind, count: files.length },
    { heypSections: continueSections(kind), exclude, sibling: siblingUrl(state.config) });
}

function resultView(res, { source } = {}) {
  const files = res.files || [];
  if (!files.length) return h('p', { class: 'hint' }, 'The job finished without a file.');
  const list = h('div', { class: 'job-results' }, files.slice(0, 12).map((info) => resultFile(info)));
  const more = files.length > 12 ? h('p', { class: 'hint' }, `…and ${files.length - 12} more in the ZIP.`) : null;
  const actions = h('div', { class: 'btn-row job-actions' });
  if (res.zip?.id) {
    actions.append(h('a', { class: 'btn btn-icon-text', href: api.downloadUrl(res.zip.id), download: res.zip.name || '' }, icon('download', 16), h('span', {}, `All ${files.length} · ZIP`)));
  }
  const images = files.filter(isImageInfo);
  if (images.length && !images.every((i) => state.files.some((f) => f.id === i.id))) {
    actions.append(h('button', { type: 'button', class: 'btn btn-icon-text', onclick: () => { const n = addResultFiles(images); if (n) toast(`Added ${n} to your photos.`); } }, icon('plus', 16), h('span', {}, 'Add to photos')));
  }
  const menu = continueMenu({ targets: resultTargets(files, source), onPick: (t) => continueWith(t, files) });
  if (menu) actions.append(menu);
  return h('div', { class: 'job-result' }, h('p', { class: 'group-title' }, files.length > 1 ? `Results · ${files.length}` : 'Result'), list, more, actions);
}

/** "Continue with" a heyp section: it works on the files in the strip (job results are added to it first). */
function continueWith(t, files) {
  if (files) addResultFiles(files);
  setTool(t.id);
}

// export → continue: heyp sections switch in place, with the same photos and edits
let continueKey = '';

function syncContinue() {
  const box = exportUi.cont;
  if (!box) return;
  const n = state.files.length;
  const fmt = state.output.format === 'original' ? (activeFile()?.info?.format || 'jpeg') : state.output.format;
  const combined = fmt === 'pdf' && state.output.combinePdf && n > 1;
  const out = outputKind(fmt);
  const sibling = siblingUrl(state.config);
  const key = JSON.stringify([n, fmt, combined, Boolean(exportUi.done), sibling, state.tool]);
  if (key === continueKey) return;
  continueKey = key;
  if (!n || !exportUi.done) {
    box.replaceChildren();
    return;
  }
  const targets = continueTargets({ kind: out.kind, count: combined ? 1 : n },
    { heypSections: continueSections(out.kind), exclude: state.tool, sibling });
  // heyp sections keep editing the same photos with the same edits (Make GIF applies them to its frames)
  const menu = continueMenu({ targets, up: true, onPick: (t) => continueWith(t) });
  box.replaceChildren(...(menu ? [menu] : []));
}

// ── v2 boot: config (hosted copy), registry, editor catalogue ────

function applyCopy(c) {
  document.body.classList.remove('copy-pending', 'copy-unknown');
  document.body.classList.toggle('is-hosted', c.mode === 'hosted');
  const pill = $('.local-pill');
  if (pill) {
    pill.textContent = c.pill;
    pill.title = c.pillTitle;
  }
  // the dateline's one factual line (how long files are kept), from the configured TTLs
  const edition = $('.dateline-edition');
  if (edition) edition.textContent = c.dateline;
  // the "heyp · heypdf" switch: a plain link to the sibling site when this deployment names one
  const sibling = siblingUrl(state.config);
  const link = $('.sibling-link[data-app="heypdf"]');
  if (link && sibling) link.href = sibling;
  for (const node of document.querySelectorAll('.sibling-dot, .sibling-link[data-app="heypdf"]')) node.hidden = !sibling;
  const lede = $('.dz-lede');
  if (lede) lede.textContent = c.lede;
  // the policy pages open in a new tab: leaving this page deletes the photos it holds (UIH-08)
  const doc = (href, label) => h('a', { href, target: '_blank', rel: 'noopener' }, label);
  const priv = $('.dz-privacy');
  if (priv) {
    // one run of text (the line is a flex row: the shield, then the words)
    priv.replaceChildren(icon('shield', 15), h('span', {}, `${c.privacy}. `,
      ...(c.privacyNote ? [`${c.privacyNote} `, doc('/terms', 'Terms'), ' and ', doc('/privacy', 'Privacy policy'), '.']
        : [doc('/privacy', 'Privacy')])));
  }
  const foot = $('#colophon-links');
  if (foot) foot.replaceChildren(...c.footer.map((l) => doc(l.href, l.label)));
  const ver = $('#colophon-version');
  if (ver) ver.textContent = c.version ? `v${c.version}` : '';
  const op = $('#colophon-operator');
  const name = state.config?.operator?.name;
  if (op) op.textContent = c.mode === 'hosted' && typeof name === 'string' && name.length < 120 ? `Run by ${name}` : '';
}

async function loadV2() {
  const [cfg, tools, cat] = await Promise.allSettled([api.config(), api.tools(), api.editorCatalog()]);
  state.config = cfg.status === 'fulfilled' && cfg.value && typeof cfg.value === 'object' ? cfg.value : null;
  if (state.config?.features && typeof state.config.features === 'object') {
    for (const [k, v] of Object.entries(state.config.features)) if (typeof v === 'boolean') state.features[k] = v;
  }
  if (cfg.status === 'rejected' && cfg.reason?.status !== 404) {
    // couldn't ask (offline, server error): make no claim either way rather than risk a false "Local only"
    document.body.classList.remove('copy-pending');
    document.body.classList.add('copy-unknown');
  } else {
    applyCopy(copyFor(state.config)); // 404 = a server older than /api/config, which is always self-hosted
  }
  state.tools = tools.status === 'fulfilled' && Array.isArray(tools.value?.tools) ? tools.value.tools : [];
  state.catalog = cat.status === 'fulfilled' && cat.value && typeof cat.value === 'object' ? cat.value : null;
  refreshPanel();
  changed({ preview: false });
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
  loadV2();
  // a text font that finished loading changes measured caption / watermark sizes: lay the layer out again
  document.fonts?.addEventListener?.('loadingdone', () => editing.refresh());
  // leaving deletes every upload (below), so any navigation away with photos asks first (UIH-08)
  window.addEventListener('beforeunload', (e) => {
    if (!state.files.length) return;
    e.preventDefault();
    e.returnValue = '';
  });
  // privacy: when the tab really goes away, delete this page's uploads and job results right away (TTL is the fallback)
  window.addEventListener('pagehide', (e) => {
    if (e.persisted) return;
    removals.flush({ keepalive: true });
    for (const f of state.files) api.deleteFile(f.id, { keepalive: true });
    for (const id of auxFiles) api.deleteFile(id, { keepalive: true });
    for (const id of resultFiles) if (!state.files.some((f) => f.id === id)) api.deleteFile(id, { keepalive: true });
  });
}

boot();
