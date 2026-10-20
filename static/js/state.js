// Single source of truth + the op builder (PLAN.md §7: fixed order
// rotate/flip → crop → background → passport → resize → adjust → sheet).

import { posInt, num, fmtNum } from './dom.js';

export const FORMAT_LABEL = {
  jpeg: 'JPG', png: 'PNG', webp: 'WebP', avif: 'AVIF', heic: 'HEIC', tiff: 'TIFF',
  bmp: 'BMP', gif: 'GIF', pdf: 'PDF', original: 'Original', heif: 'HEIF', ico: 'ICO', jp2: 'JPEG 2000',
};
export const LOSSY = new Set(['jpeg', 'webp', 'avif', 'heic']);
export const ALPHA_FORMATS = new Set(['png', 'webp', 'avif', 'heic', 'tiff', 'gif']);
export const CROP_RATIOS = ['free', 'original', '1:1', '4:5', '3:2', '16:9', '9:16', '35:45', '2:3'];
export const UNIT_MM = { in: 25.4, cm: 10, mm: 1 };
/** Names of the swatch colours the panels offer, so chips and notes never print a bare hex code. */
const COLOR_NAMES = { '#ffffff': 'White', '#000000': 'Black', '#f2f2f2': 'Off-white', '#dbe9f7': 'Light blue', '#e5e7eb': 'Light grey', '#18181b': 'Near black' };
export const colorName = (hex) => COLOR_NAMES[String(hex || '').toLowerCase()] || null;

export const defaults = {
  edit: () => ({
    quarter: 0, flipH: false, flipV: false, straighten: 0, cropEdges: true,
    brightness: 1, contrast: 1, saturation: 1, sharpness: 1, grayscale: false, autoContrast: false,
  }),
  resize: () => ({
    on: false, mode: 'dimensions', width: null, height: null, driver: 'width', lock: true, fit: 'fill',
    percent: 50, longEdge: null, printW: null, printH: null, printDriver: 'width', unit: 'in', dpi: 300,
    noEnlarge: false,
  }),
  /** autoFrom: the export format Remove replaced with PNG (restored when leaving Remove). */
  background: () => ({ mode: 'off', color: '#ffffff', autoFrom: null }),
  passport: () => ({
    on: false, preset: null, background: null, replace: true, level: true, guides: true,
    zoom: 1, offsetX: 0, offsetY: 0,
    layout: 'single', paper: '4x6', copies: 0, cutLines: true,
    custom: { width_mm: 35, height_mm: 45, dpi: 300 },
  }),
  compress: () => ({ on: false, maxKb: null, minKb: null }),
  output: () => ({
    format: 'jpeg', quality: 90, lossless: false, progressive: true, chroma: 'auto',
    stripMetadata: true, stripGps: true, colorspace: 'srgb', background: '#ffffff',
    combinePdf: true, filename: '',
  }),
};

export const state = {
  tool: 'convert',
  /** @type {Array<{id:string,name:string,info:object,crop:object|null,preview:object|null,previewKey:string|null,previewFraming:object|null,cropBase:object|null,passportBase:object|null,full:object|null}>} */
  files: [],
  activeId: null,
  features: { heic: true, avif: true, face: true, segmentation: false },
  outputFormats: ['jpeg', 'png', 'webp', 'avif', 'heic', 'tiff', 'bmp', 'gif', 'pdf'],
  presets: [],
  papers: [],
  presetsError: null,
  /** Server limits from /api/health (max_upload_mb, max_files, max_pixels); null until known. */
  limits: null,
  edit: defaults.edit(),
  resize: defaults.resize(),
  background: defaults.background(),
  passport: defaults.passport(),
  compress: defaults.compress(),
  output: defaults.output(),
  cropRatio: 'free',
};

export const activeFile = () => state.files.find((f) => f.id === state.activeId) || null;
export const findPreset = (id) => state.presets.find((p) => p.id === id) || null;

// ── geometry ────────────────────────────────────────────────────────────────

/** "4:5" | "original" | number → width/height ratio (null for free). */
export function ratioValue(r, original = 1) {
  if (r == null || r === 'free') return null;
  if (r === 'original') return original;
  if (typeof r === 'number') return r > 0 ? r : null;
  const [a, b] = String(r).split(/[:/]/).map(Number);
  return a > 0 && b > 0 ? a / b : null;
}

/** A crop-ratio setting the Crop tool can show and lock to: a chip value or any "a:b" (e.g. 5:4 after a rotate). */
export const isCropRatio = (r) => CROP_RATIOS.includes(r) || (typeof r === 'string' && /^\d+(\.\d+)?:\d+(\.\d+)?$/.test(r));

/** The ratio lock that belongs to `file`: its own box/auto-crop ratio, else free (never another photo's). */
export function cropRatioFor(file) {
  const c = file?.crop;
  return c && c.kind !== 'trim' && isCropRatio(c.ratio) ? c.ratio : 'free';
}

/** Integer pixel edges of a fractional crop box on a W×H image, rounded exactly like the server's crop op. */
export function cropPx(c, W, H) {
  const x0 = Math.round(c.x * W);
  const y0 = Math.round(c.y * H);
  return { x: x0, y: y0, w: Math.max(1, Math.round((c.x + c.w) * W) - x0), h: Math.max(1, Math.round((c.y + c.h) * H) - y0) };
}

/** Largest centred axis-aligned box (scale k of w×h) inside a w×h image rotated by `deg` without expanding. */
export function inscribedScale(w, h, deg) {
  const t = (Math.abs(deg) * Math.PI) / 180;
  const c = Math.cos(t);
  const s = Math.sin(t);
  const k = Math.min(w / (w * c + h * s), h / (w * s + h * c));
  return Math.max(0.05, k - 2 / Math.min(w, h)); // shave 1 px per side: no fill-coloured fringe
}

/** Image size after EXIF orientation + quarter turns. */
export function orientedDims(file) {
  const { width: w, height: h } = file.info;
  return state.edit.quarter % 180 ? { w: h, h: w } : { w, h };
}

function straightenScale(file) {
  const e = state.edit;
  if (!e.straighten || !e.cropEdges || !file) return 1;
  const { w, h } = orientedDims(file);
  return inscribedScale(w, h, e.straighten);
}

/** Size of the image the crop tool works on (after rotate/flip/straighten); exact once the server rendered it. */
export function baseDims(file) {
  const cb = file.cropBase;
  if (cb && cb.key === JSON.stringify(preCropOps(file))) return { w: cb.w, h: cb.h };
  const { w, h } = orientedDims(file);
  const k = straightenScale(file);
  return { w: Math.max(1, Math.round(w * k)), h: Math.max(1, Math.round(h * k)) };
}

export function passportDims() {
  const p = state.passport;
  if (p.preset === 'custom') {
    const { width_mm: wm, height_mm: hm, dpi } = p.custom;
    if (!(wm > 0 && hm > 0 && dpi > 0)) return null;
    return { w: Math.round((wm / 25.4) * dpi), h: Math.round((hm / 25.4) * dpi) };
  }
  const pr = findPreset(p.preset);
  if (!pr) return null;
  const w = pr.width_px ?? pr.pixel_width ?? Math.round((pr.width_mm / 25.4) * pr.dpi);
  const h = pr.height_px ?? pr.pixel_height ?? Math.round((pr.height_mm / 25.4) * pr.dpi);
  return { w, h };
}

/** Best estimate of the size that reaches the resize step (exact except after trim). */
export function preResizeDims(file) {
  if (!file) return null;
  if (state.passport.on) return passportDims();
  let { w, h } = baseDims(file);
  const c = file.crop;
  if (c?.kind === 'box') {
    w *= c.w;
    h *= c.h;
  } else if (c?.kind === 'aspect') {
    const r = ratioValue(c.ratio, w / h);
    if (r) {
      if (w / h > r) w = h * r;
      else h = w / r;
    }
  }
  return { w: Math.max(1, Math.round(w)), h: Math.max(1, Math.round(h)) };
}

// ── ops ─────────────────────────────────────────────────────────────────────

/** Ops that precede crop (what the crop tool shows). */
export function preCropOps(file) {
  const e = state.edit;
  const ops = [];
  if (e.quarter) ops.push({ op: 'rotate', angle: e.quarter });
  if (e.flipH) ops.push({ op: 'flip', direction: 'horizontal' });
  if (e.flipV) ops.push({ op: 'flip', direction: 'vertical' });
  if (e.straighten) {
    const fill = file?.info?.has_alpha ? 'transparent' : '#ffffff';
    ops.push({ op: 'rotate', angle: e.straighten, expand: false, fill });
    const k = straightenScale(file);
    if (k < 1) {
      const o = round6((1 - k) / 2);
      ops.push({ op: 'crop', unit: 'fraction', x: o, y: o, width: round6(k), height: round6(k) });
    }
  }
  return ops;
}

const round6 = (v) => Math.round(v * 1e6) / 1e6;

function cropOp(c, file) {
  if (c.kind === 'box') {
    return { op: 'crop', unit: 'fraction', x: round6(c.x), y: round6(c.y), width: round6(c.w), height: round6(c.h) };
  }
  if (c.kind === 'trim') return { op: 'trim', tolerance: c.tolerance, padding: c.padding };
  if (c.kind === 'aspect') {
    let ratio = c.ratio;
    if (ratio === 'original' || ratio === 'free') {
      const { w, h } = baseDims(file);
      ratio = round6(w / h);
    }
    return { op: 'crop_aspect', ratio, anchor: c.anchor };
  }
  return null;
}

export function resizeOp() {
  const r = state.resize;
  if (!r.on) return null;
  const upscale = !r.noEnlarge;
  switch (r.mode) {
    case 'dimensions': {
      const w = posInt(r.width);
      const h = posInt(r.height);
      if (r.lock || !(w && h)) {
        const useH = h && (r.driver === 'height' || !w);
        if (useH) return { op: 'resize', mode: 'fit', height: h, upscale };
        if (w) return { op: 'resize', mode: 'fit', width: w, upscale };
        return null;
      }
      const op = { op: 'resize', mode: r.fit, width: w, height: h, upscale };
      if (r.fit === 'pad') op.background = state.output.background;
      return op;
    }
    case 'percent': {
      const s = num(r.percent);
      return s > 0 ? { op: 'resize', mode: 'scale', scale: s, upscale } : null;
    }
    case 'long_edge': {
      const s = posInt(r.longEdge);
      return s ? { op: 'resize', mode: 'long_edge', size: s, upscale } : null;
    }
    case 'print': {
      const w = num(r.printW);
      const h = num(r.printH);
      const dpi = posInt(r.dpi) || 300;
      const base = { op: 'resize', unit: r.unit, dpi, upscale };
      if (r.lock || !(w > 0 && h > 0)) {
        const useH = h > 0 && (r.printDriver === 'height' || !(w > 0));
        if (useH) return { ...base, mode: 'fit', height: h };
        if (w > 0) return { ...base, mode: 'fit', width: w };
        return null;
      }
      const op = { ...base, mode: r.fit, width: w, height: h };
      if (r.fit === 'pad') op.background = state.output.background;
      return op;
    }
    default:
      return null; // 'dpi' only touches output.dpi
  }
}

function adjustOp() {
  const e = state.edit;
  const changed = e.brightness !== 1 || e.contrast !== 1 || e.saturation !== 1 || e.sharpness !== 1 || e.grayscale || e.autoContrast;
  if (!changed) return null;
  return {
    op: 'adjust', brightness: e.brightness, contrast: e.contrast, saturation: e.saturation,
    sharpness: e.sharpness, grayscale: e.grayscale, auto_contrast: e.autoContrast,
  };
}

function passportOp() {
  const p = state.passport;
  if (!p.preset) return null;
  const op = {
    op: 'passport', preset: p.preset, background: p.background,
    replace_background: Boolean(p.replace && state.features.segmentation),
    level: p.level !== false,
    zoom: p.zoom, offset_x: p.offsetX, offset_y: p.offsetY,
  };
  if (p.preset === 'custom') op.custom = { ...p.custom };
  return op;
}

/** Passport framing applies (on, a preset chosen, and custom sizes valid). */
export const passportActive = () => state.passport.on && Boolean(state.passport.preset) && (state.passport.preset !== 'custom' || Boolean(passportDims()));

/**
 * The passport op's framing in an op list ({zoom, offsetX, offsetY}) plus `baseKey`, the JSON of the ops
 * before it (the image the passport frame is placed on); null without a passport op.
 */
export function framingOf(ops) {
  const i = (ops || []).findIndex((o) => o?.op === 'passport');
  if (i < 0) return null;
  const o = ops[i];
  return { zoom: o.zoom ?? 1, offsetX: o.offset_x ?? 0, offsetY: o.offset_y ?? 0, baseKey: JSON.stringify(ops.slice(0, i)) };
}

/** Ops before the passport op (what the passport context view shows around the frame); null when inactive. */
export function prePassportOps(file) {
  const ops = buildOps(file);
  const i = ops.findIndex((o) => o.op === 'passport');
  return i < 0 ? null : ops.slice(0, i);
}

export function buildOps(file) {
  const ops = preCropOps(file);
  const pp = passportActive();
  if (!pp && file?.crop) {
    const c = cropOp(file.crop, file);
    if (c) ops.push(c);
  }
  if (state.background.mode !== 'off' && state.features.segmentation) {
    ops.push(state.background.mode === 'replace'
      ? { op: 'background', mode: 'replace', color: state.background.color }
      : { op: 'background', mode: 'remove' });
  }
  if (pp) ops.push(passportOp());
  if (!pp) {
    const r = resizeOp();
    if (r) ops.push(r);
  }
  const a = adjustOp();
  if (a) ops.push(a);
  if (pp && state.passport.layout === 'sheet') {
    ops.push({ op: 'sheet', paper: state.passport.paper, copies: state.passport.copies, cut_lines: state.passport.cutLines });
  }
  return ops;
}

export function outputDpi() {
  const r = state.resize;
  if (passportActive() || !r.on || r.mode !== 'dpi') return null;
  return posInt(r.dpi);
}

export function buildOutput() {
  const o = state.output;
  const c = state.compress;
  const lossless = (o.format === 'webp' || o.format === 'avif') && o.lossless;
  const maxKb = c.on ? num(c.maxKb) : null;
  const minKb = c.on ? num(c.minKb) : null;
  return {
    format: o.format,
    quality: o.quality,
    lossless,
    progressive: o.progressive,
    chroma: o.chroma,
    dpi: outputDpi(),
    strip_metadata: o.stripMetadata,
    strip_gps: o.stripGps,
    colorspace: o.colorspace,
    background: o.background,
    max_kb: maxKb > 0 ? maxKb : null,
    min_kb: minKb > 0 && !(maxKb > 0 && minKb > maxKb) ? minKb : null, // inverted range: the panel explains
    combine_pdf: o.combinePdf,
    filename: sanitizeFilename(o.filename),
  };
}

/**
 * One ProcessRequest for a whole export (contract v1.1 C1): the ops list most files share goes in
 * `ops`; files whose ops differ (per-file crops) get their own list in `ops_by_id`.
 */
export function exportRequest(files) {
  const output = buildOutput();
  const lists = files.map((f) => buildOps(f));
  const keys = lists.map((l) => JSON.stringify(l));
  const counts = new Map();
  for (const k of keys) counts.set(k, (counts.get(k) || 0) + 1);
  let shared = keys[0];
  for (const k of keys) if (counts.get(k) > counts.get(shared)) shared = k;
  const req = {
    ids: files.map((f) => f.id),
    ops: lists[keys.indexOf(shared)] || [],
    output: { ...output, filename: files.length === 1 ? output.filename : null },
  };
  const byId = {};
  files.forEach((f, i) => { if (keys[i] !== shared) byId[f.id] = lists[i]; });
  if (Object.keys(byId).length) req.ops_by_id = byId;
  return req;
}

/** Output format a file really gets ('original' → its own format if writable, else JPEG / PNG with alpha). */
export function resolvedFormat(fmt, file) {
  if (fmt !== 'original' || !file) return fmt;
  const src = file.info?.format;
  if (src === 'heif') return 'heic';
  if (state.outputFormats.includes(src)) return src;
  return file.info?.has_alpha ? 'png' : 'jpeg';
}

/** True when the export quality setting affects at least one file (lossy encoder, not lossless WebP/AVIF). */
export function qualityApplies() {
  const o = state.output;
  if ((o.format === 'webp' || o.format === 'avif') && o.lossless) return false;
  if (o.format !== 'original') return LOSSY.has(o.format);
  return state.files.some((f) => LOSSY.has(resolvedFormat('original', f)));
}

/**
 * Switch the Background mode. Entering Remove moves an export format without transparency to PNG;
 * leaving Remove restores it unless the user changed the format meanwhile. Returns a toast or null.
 */
export function setBackgroundMode(mode) {
  const b = state.background;
  const o = state.output;
  b.mode = mode;
  if (mode === 'remove') {
    if (ALPHA_FORMATS.has(o.format)) return null;
    const from = o.format;
    b.autoFrom = from;
    o.format = 'png';
    return `Export switched from ${FORMAT_LABEL[from] || from} to PNG to keep the transparency.`;
  }
  const from = b.autoFrom;
  b.autoFrom = null;
  if (from && o.format === 'png') {
    o.format = from;
    return `Export switched back to ${FORMAT_LABEL[from] || from}.`;
  }
  return null;
}

/** Background back to defaults (restoring an auto-switched export format). Returns a toast or null. */
export function resetBackground() {
  const msg = setBackgroundMode('off');
  state.background = defaults.background();
  return msg;
}

/** Why an export of `n` photos can't be sent (the server takes at most `max_files` ids per request), else null. */
export function exportLimitError(n, limits) {
  const max = limits?.max_files;
  if (!(max > 0) || n <= max) return null;
  return `Too many photos for one export: ${n} (the limit is ${max}). Remove ${n - max} and export again.`;
}

/** One toast line for export warnings: all of them when few, else a count and the first two. */
export function warningSummary(list) {
  const uniq = [...new Set(list)];
  if (uniq.length <= 2) return uniq.join(' · ');
  return `${uniq.length} warnings — ${uniq.slice(0, 2).join(' · ')} …`;
}

/** Split picked files into upload requests within the server's limits; oversize files are rejected up front. */
export function planUploads(files, limits, { chunkFiles = 20, chunkBytes = 200 * 1024 * 1024 } = {}) {
  const maxBytes = limits?.max_upload_mb > 0 ? limits.max_upload_mb * 1024 * 1024 : Infinity;
  const perRequest = Math.max(1, Math.min(chunkFiles, limits?.max_files > 0 ? limits.max_files : chunkFiles));
  const rejected = [];
  const chunks = [];
  let cur = [];
  let bytes = 0;
  for (const f of files) {
    if (f.size > maxBytes) {
      rejected.push({ name: f.name, detail: `larger than the ${fmtNum(limits.max_upload_mb)} MB upload limit` });
      continue;
    }
    if (cur.length && (cur.length >= perRequest || bytes + f.size > chunkBytes)) {
      chunks.push(cur);
      cur = [];
      bytes = 0;
    }
    cur.push(f);
    bytes += f.size;
  }
  if (cur.length) chunks.push(cur);
  return { chunks, rejected };
}

export function sanitizeFilename(s) {
  const n = String(s || '').replace(/\.[a-z0-9]{2,5}$/i, '').replace(/[\\/:*?"<>|\u0000-\u001f]+/g, '-').trim();
  return n ? n.slice(0, 120) : null;
}

// ── edits that also move per-file crop boxes ────────────────────────────────

const swapRatio = (r) => (typeof r === 'string' && r.includes(':') ? r.split(':').reverse().join(':') : r);

/** Rotate the whole batch by ±90°, keeping flips and crop boxes visually consistent. */
export function rotateQuarter(dir) {
  const e = state.edit;
  e.quarter = (((e.quarter + (dir > 0 ? 90 : 270)) % 360) + 360) % 360;
  // flip-after-rotate is canonical: R90 ∘ Fh = Fv ∘ R90, so swap the flips.
  [e.flipH, e.flipV] = [e.flipV, e.flipH];
  for (const f of state.files) {
    const c = f.crop;
    if (c?.kind !== 'box') continue;
    const nb = dir > 0
      ? { x: 1 - c.y - c.h, y: c.x, w: c.h, h: c.w }
      : { x: c.y, y: 1 - c.x - c.w, w: c.h, h: c.w };
    f.crop = { ...c, ...nb, ratio: swapRatio(c.ratio) };
  }
  state.cropRatio = swapRatio(state.cropRatio); // any a:b, so 4:5 → 5:4 and 35:45 → 45:35 stay locked to the box's shape
}

export function flip(axis) {
  const e = state.edit;
  if (axis === 'h') e.flipH = !e.flipH;
  else e.flipV = !e.flipV;
  for (const f of state.files) {
    const c = f.crop;
    if (c?.kind !== 'box') continue;
    f.crop = axis === 'h' ? { ...c, x: 1 - c.x - c.w } : { ...c, y: 1 - c.y - c.h };
  }
}

// ── human-readable edits (chips) ────────────────────────────────────────────

const RESIZE_FIT = { fill: 'fill', fit: 'fit', pad: 'pad', exact: 'stretch' };

export function resizeLabel() {
  const r = state.resize;
  if (!r.on) return null;
  if (r.mode === 'dpi') {
    const d = posInt(r.dpi);
    return d ? `${d} DPI` : null;
  }
  const op = resizeOp();
  if (!op) return null;
  if (op.mode === 'scale') return `Resize ${fmtNum(op.scale)}%`;
  if (op.mode === 'long_edge') return `Long edge ${op.size} px`;
  const u = op.unit && op.unit !== 'px' ? op.unit : 'px';
  const at = u === 'px' ? '' : ` @ ${op.dpi} DPI`;
  if (!(op.width && op.height)) return op.width ? `Width ${fmtNum(op.width)} ${u}${at}` : `Height ${fmtNum(op.height)} ${u}${at}`;
  const fit = op.mode !== 'fit' ? ` · ${RESIZE_FIT[op.mode]}` : '';
  return `Resize ${fmtNum(op.width)}×${fmtNum(op.height)} ${u}${at}${fit}`;
}

/** Active edits for the chip row: [{key, tool, label, clear()}]. */
export function describeEdits(file) {
  const out = [];
  const e = state.edit;
  const pp = passportActive();

  const rot = [];
  if (e.quarter) rot.push({ 90: '90° right', 180: '180°', 270: '90° left' }[e.quarter]);
  if (e.straighten) rot.push(`${e.straighten > 0 ? '+' : ''}${fmtNum(e.straighten, 1)}°`);
  if (rot.length) {
    out.push({
      key: 'rotate', tool: 'edit', label: `Rotate ${rot.join(' ')}`,
      clear: () => {
        while (e.quarter) rotateQuarter(e.quarter === 270 ? 1 : -1);
        e.straighten = 0;
      },
    });
  }
  if (e.flipH || e.flipV) {
    const label = e.flipH && e.flipV ? 'Flip both' : e.flipH ? 'Flip horizontal' : 'Flip vertical';
    out.push({ key: 'flip', tool: 'edit', label, clear: () => flipReset() });
  }
  if (file?.crop && !pp) {
    const c = file.crop;
    let label = 'Crop';
    if (c.kind === 'box') {
      const { w, h } = baseDims(file);
      const px = cropPx(c, w, h);
      label = `Crop ${px.w}×${px.h}`;
    } else if (c.kind === 'trim') label = 'Auto trim';
    else if (c.kind === 'aspect') label = `${c.anchor === 'face' ? 'Face' : 'Smart'} crop ${ratioLabel(c.ratio)}`;
    out.push({ key: 'crop', tool: 'crop', label, clear: () => { file.crop = null; } });
  }
  if (state.background.mode !== 'off' && state.features.segmentation) {
    const b = state.background;
    out.push({
      key: 'background', tool: 'background',
      label: b.mode === 'remove' ? 'Background removed' : colorName(b.color) ? `${colorName(b.color)} background` : `Background ${b.color}`,
      clear: () => resetBackground(),
    });
  }
  if (pp) {
    const pr = findPreset(state.passport.preset);
    const name = state.passport.preset === 'custom' ? 'custom' : (pr ? shortPresetName(pr) : state.passport.preset);
    out.push({ key: 'passport', tool: 'passport', label: `Passport · ${name}`, clear: () => { state.passport.on = false; } });
    if (state.passport.layout === 'sheet') {
      const paper = state.papers.find((p) => p.id === state.passport.paper);
      out.push({ key: 'sheet', tool: 'passport', label: `Print sheet ${paper?.name || state.passport.paper}`, clear: () => { state.passport.layout = 'single'; } });
    }
  } else {
    const rl = resizeLabel();
    if (rl) out.push({ key: 'resize', tool: 'resize', label: rl, clear: () => { state.resize = defaults.resize(); } });
  }
  const adj = [];
  if (e.brightness !== 1 || e.contrast !== 1 || e.saturation !== 1 || e.sharpness !== 1) adj.push('Adjust');
  if (e.autoContrast) adj.push('Auto contrast');
  if (e.grayscale) adj.push('Grayscale');
  if (adj.length) {
    out.push({
      key: 'adjust', tool: 'edit', label: adj.join(' · '),
      clear: () => Object.assign(e, { brightness: 1, contrast: 1, saturation: 1, sharpness: 1, grayscale: false, autoContrast: false }),
    });
  }
  const c = state.compress;
  if (c.on && (num(c.maxKb) > 0 || num(c.minKb) > 0)) {
    const max = num(c.maxKb);
    const min = num(c.minKb);
    const label = max > 0 && min > 0 ? `${fmtKb(min)}–${fmtKb(max)}` : max > 0 ? `≤ ${fmtKb(max)}` : `≥ ${fmtKb(min)}`;
    out.push({ key: 'compress', tool: 'compress', label, clear: () => { state.compress = defaults.compress(); } });
  }
  return out;
}

function flipReset() {
  if (state.edit.flipH) flip('h');
  if (state.edit.flipV) flip('v');
}

/** "4:5" stays, numbers become "1.5:1", "original" stays. */
export function ratioLabel(r) {
  return typeof r === 'number' ? `${fmtNum(r, 2)}:1` : String(r);
}

export function fmtKb(kb) {
  return kb >= 1024 && kb % 1024 === 0 ? `${kb / 1024} MB` : `${fmtNum(kb)} KB`;
}

/** "United States — passport & visa (2×2 in)" → "United States 2×2 in". */
export function shortPresetName(p) {
  const name = String(p.name || p.id);
  const head = name.split(/\s+[—–-]\s+/)[0].trim();
  const size = /\(([^)]+)\)\s*$/.exec(name)?.[1];
  const out = size ? `${head} ${size}` : head;
  return out.length > 32 ? `${out.slice(0, 31)}…` : out;
}
