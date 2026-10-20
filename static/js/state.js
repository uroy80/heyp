// Single source of truth + the op builder. Fixed op order (PLAN.md §7, extended for v2):
// blur_regions → rotate/flip/straighten → crop → background → passport → resize → adjust → filter → upscale →
// frame → meme → overlay → watermark → sheet.
// - blur_regions comes first, so its boxes are fractions of the EXIF-oriented original — the same image
//   /api/detect measures and the Blur tool draws on.
// - upscale is heavy: live previews leave it out (`preview: true`); everything after it is resolution-relative.

import { posInt, num, fmtNum } from './dom.js';
import { toCornerBox } from './overlay.js';
import { fontId, catalogLabel, FILTERS, FRAMES } from './catalog.js';

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
  /**
   * quarter/flipH/flipV: canonical "rotate, then flip" form for photos the quarter turn applies to
   * (`only` filter); plainFlipH/V: the same flips for photos it skips (no rotation to swap them through).
   */
  edit: () => ({
    quarter: 0, flipH: false, flipV: false, plainFlipH: false, plainFlipV: false, only: 'all', straighten: 0, cropEdges: true,
    brightness: 1, contrast: 1, saturation: 1, sharpness: 1, grayscale: false, autoContrast: false,
  }),
  /** factor 0 = off. loupe = centre of the detail check (fractions of the image entering upscale). */
  upscale: () => ({ factor: 0, model: 'balanced', loupe: { cx: 0.5, cy: 0.5 } }),
  watermark: () => ({
    kind: 'text', text: '', font: 'libre_franklin', size: 0.05, color: '#ffffff', opacity: 0.6, rotation: 0,
    /** position: a 9-grid name, or 'custom' = dragged on the photo to centre (x, y) fractions */
    position: 'bottom-right', x: 0.5, y: 0.5, mosaic: false, margin: 0.03, imageId: null, imageName: '', imageW: 0, imageH: 0, imageScale: 0.25,
  }),
  meme: () => ({ top: '', bottom: '', font: 'anton', autoSize: true, size: 0.1, color: '#ffffff', outline: '#000000', uppercase: true }),
  /** strength 0…1; regions live on each file (`file.regions`). */
  blur: () => ({ mode: 'blur', strength: 0.6, color: '#141210' }),
  /** filter/frame apply to every photo; overlay items live on each file (`file.items`). */
  editor: () => ({ filter: 'none', intensity: 1, frame: 'none', frameColor: null, frameWidth: null, tab: 'filters', brush: { color: '#e0262e', width: 0.012, opacity: 1 }, drawing: false }),
  /** applyEdits: frames get the other sections' edits first (rendered by the img.process job). */
  gif: () => ({ delay: 500, loop: true, format: 'gif', size: 0, skip: [], applyEdits: true }),
  html: () => ({ url: '', width: 1440, fullPage: true, format: 'jpg', delay: 0, blockBanners: true }),
  resize: () => ({
    on: false, mode: 'dimensions', width: null, height: null, driver: 'width', lock: true, fit: 'fill',
    percent: 50, longEdge: null, printW: null, printH: null, printDriver: 'width', unit: 'in', dpi: 300,
    noEnlarge: false,
  }),
  /** autoFrom: the export format Remove replaced with PNG (restored when leaving Remove). engine: v2 matting. */
  background: () => ({ mode: 'off', color: '#ffffff', autoFrom: null, engine: 'auto' }),
  passport: () => ({
    on: false, preset: null, background: null, replace: true, level: true, guides: true,
    zoom: 1, offsetX: 0, offsetY: 0,
    layout: 'single', paper: '4x6', copies: 0, cutLines: true,
    custom: { width_mm: 35, height_mm: 45, dpi: 300 },
  }),
  /** mode 'keep': img.compress job (same format); 'target': the export hits maxKb/minKb (on = a limit is set). */
  compress: () => ({ on: false, maxKb: null, minKb: null, mode: 'keep', level: 'recommended', pngQuantize: true }),
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
  /** GET /api/config (v2), null on older servers. */
  config: null,
  /** GET /api/tools: the job-tool registry entries, null until loaded. */
  tools: null,
  edit: defaults.edit(),
  resize: defaults.resize(),
  background: defaults.background(),
  passport: defaults.passport(),
  compress: defaults.compress(),
  output: defaults.output(),
  upscale: defaults.upscale(),
  watermark: defaults.watermark(),
  meme: defaults.meme(),
  blur: defaults.blur(),
  editor: defaults.editor(),
  gif: defaults.gif(),
  html: defaults.html(),
  cropRatio: 'free',
};

/** The registry entry of a job tool (`img.make_gif` …) or null. */
export const toolEntry = (id) => (state.tools || []).find((t) => t.id === id) || null;

let idN = 0;
/** Short unique id for overlay items and regions. */
export const newId = (p = 'i') => `${p}${Date.now().toString(36)}${(++idN).toString(36)}`;

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

/**
 * Whether the batch quarter turn applies to `file` (Edit › Rotate "Apply to"): all photos, or only those that
 * are landscape / portrait as shot (after EXIF orientation). Squares count as neither.
 */
export function quarterApplies(file, only = state.edit.only) {
  if (!only || only === 'all') return true;
  const w = file?.info?.width;
  const h = file?.info?.height;
  if (!(w > 0 && h > 0)) return false;
  return only === 'landscape' ? w > h : h > w;
}

/** Image size after EXIF orientation + quarter turns. */
export function orientedDims(file) {
  const { width: w, height: h } = file.info;
  return state.edit.quarter % 180 && quarterApplies(file) ? { w: h, h: w } : { w, h };
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
  const turns = quarterApplies(file);
  if (e.quarter && turns) ops.push({ op: 'rotate', angle: e.quarter });
  if (turns ? e.flipH : e.plainFlipH) ops.push({ op: 'flip', direction: 'horizontal' });
  if (turns ? e.flipV : e.plainFlipV) ops.push({ op: 'flip', direction: 'vertical' });
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
  const ops = buildOps(file, { preview: true });
  const i = ops.findIndex((o) => o.op === 'passport');
  return i < 0 ? null : ops.slice(0, i);
}

/** Pipeline stages in op order; `buildOps(file, {until})` stops before the named stage. */
export const STAGES = ['blur', 'orient', 'crop', 'background', 'passport', 'resize', 'adjust', 'filter', 'upscale', 'frame', 'meme', 'overlay', 'watermark', 'sheet'];

/** Ops whose cost makes a live preview impractical: left out of previews, applied on export. */
export const HEAVY_OPS = new Set(['upscale']);

/**
 * Ops too slow for one HTTP request behind a proxy (Cloudflare cuts at 100 s, Apache at 60 s): an export with any
 * of them — or with several photos — runs as the `img.process` job (progress, no proxy timeout).
 */
export const JOB_OPS = new Set(['upscale', 'background']);

/**
 * The op list for `file`. `preview` leaves out heavy ops (upscale); `until` = a STAGES name: only the ops
 * before that stage (the image a tool edits on — e.g. 'overlay' for the photo editor's canvas).
 */
export function buildOps(file, { preview = false, until = null } = {}) {
  const ops = [];
  const stop = until ? STAGES.indexOf(until) : STAGES.length;
  const at = (stage) => STAGES.indexOf(stage) < stop;
  const pp = passportActive();
  if (at('blur')) {
    const b = blurOp(file);
    if (b) ops.push(b);
  }
  if (at('orient')) ops.push(...preCropOps(file));
  if (at('crop') && !pp && file?.crop) {
    const c = cropOp(file.crop, file);
    if (c) ops.push(c);
  }
  if (at('background') && state.background.mode !== 'off' && state.features.segmentation) {
    const b = state.background.mode === 'replace'
      ? { op: 'background', mode: 'replace', color: state.background.color }
      : { op: 'background', mode: 'remove' };
    // v2 matting engines; older servers reject unknown fields, so only send it when the server has them
    if (state.features.matting && state.background.engine && state.background.engine !== 'auto') b.engine = state.background.engine;
    ops.push(b);
  }
  if (at('passport') && pp) ops.push(passportOp());
  if (at('resize') && !pp) {
    const r = resizeOp();
    if (r) ops.push(r);
  }
  if (at('adjust')) {
    const a = adjustOp();
    if (a) ops.push(a);
  }
  if (at('filter')) {
    const f = filterOp();
    if (f) ops.push(f);
  }
  if (at('upscale') && !preview) {
    const u = upscaleOp();
    if (u) ops.push(u);
  }
  if (at('frame')) {
    const f = frameOp();
    if (f) ops.push(f);
  }
  if (at('meme')) {
    const m = memeOp();
    if (m) ops.push(m);
  }
  if (at('overlay')) {
    const o = overlayOp(file);
    if (o) ops.push(o);
  }
  if (at('watermark')) {
    const w = watermarkOp();
    if (w) ops.push(w);
  }
  if (at('sheet') && pp && state.passport.layout === 'sheet') {
    ops.push({ op: 'sheet', paper: state.passport.paper, copies: state.passport.copies, cut_lines: state.passport.cutLines });
  }
  return ops;
}

// ── v2 ops ──────────────────────────────────────────────────────────────────

/** Blur regions that are switched on (a detected suggestion can be unticked). */
export const activeRegions = (file) => (file?.regions || []).filter((r) => r.on !== false && r.w > 0 && r.h > 0);

/** Pixelate below this strength leaves faces recognisable (≈ 14+ blocks across a face): never sent lower (img-06). */
export const PIXELATE_MIN = 0.4;

export function blurOp(file) {
  const regions = activeRegions(file);
  if (!regions.length) return null;
  const b = state.blur;
  const op = {
    op: 'blur_regions', regions: regions.map((r) => ({ ...toCornerBox(r), shape: r.shape === 'ellipse' ? 'ellipse' : 'rect' })),
    mode: b.mode, strength: round3(b.mode === 'pixelate' ? Math.max(PIXELATE_MIN, b.strength) : b.strength),
  };
  if (b.mode === 'solid') op.color = b.color;
  return op;
}

/** Upscale applies (a factor chosen, the server has the model, and Passport isn't fixing the output size). */
export const upscaleActive = () => state.upscale.factor > 1 && state.features.upscale !== false && !passportActive();

export function upscaleOp() {
  if (!upscaleActive()) return null;
  return { op: 'upscale', factor: state.upscale.factor, model: state.upscale.model };
}

/** Output size of `dims` after the chosen upscale, and whether it stays within the server's pixel limit. */
/** Upscale's output size against the server's pixel limit, and its input against Upscale's own input limit. */
export function upscalePlan(dims, factor = state.upscale.factor, limits = state.limits) {
  if (!dims || !(factor > 1)) return null;
  const w = Math.round(dims.w * factor);
  const h = Math.round(dims.h * factor);
  const max = limits?.max_pixels > 0 ? limits.max_pixels : Infinity;
  const maxIn = limits?.upscale_max_input_pixels > 0 ? limits.upscale_max_input_pixels : Infinity;
  const inOk = dims.w * dims.h <= maxIn;
  return {
    w, h, mp: (w * h) / 1e6, ok: w * h <= max && inOk, maxMp: Number.isFinite(max) ? max / 1e6 : null,
    inOk, inMp: (dims.w * dims.h) / 1e6, maxInMp: Number.isFinite(maxIn) ? maxIn / 1e6 : null,
  };
}

export function filterOp() {
  const e = state.editor;
  if (!e.filter || e.filter === 'none' || !(e.intensity > 0)) return null;
  return { op: 'filter', name: e.filter, intensity: round3(e.intensity) };
}

export function frameOp() {
  const e = state.editor;
  if (!e.frame || e.frame === 'none') return null;
  // colour / width left out = the style's own (film strip dark, instant photo white …), see editor.py FRAMES
  const op = { op: 'frame', style: e.frame };
  if (e.frameColor) op.color = e.frameColor;
  if (e.frameWidth != null && Number.isFinite(e.frameWidth)) op.width = round3(e.frameWidth);
  return op;
}

export const memeActive = () => Boolean(state.meme.top.trim() || state.meme.bottom.trim());

export function memeOp() {
  if (!memeActive()) return null;
  const m = state.meme;
  return {
    op: 'meme', top: m.top.trim(), bottom: m.bottom.trim(), font: fontId(m.font),
    size: m.autoSize ? 'auto' : round3(m.size), color: m.color, outline: m.outline === 'none' ? 'transparent' : m.outline, uppercase: m.uppercase,
  };
}

/** Overlay items in the server's JSON (fractions of the image at the overlay step, centre-anchored). */
export function overlayItemJSON(it) {
  const base = { x: round4(it.cx), y: round4(it.cy), rotation: round3(it.rotation || 0), opacity: round3(it.opacity ?? 1) };
  if (it.kind === 'text') {
    return { type: 'text', text: String(it.text).slice(0, 500), ...base, size: round4(it.size), font: fontId(it.font), color: it.color, align: it.align || 'center' };
  }
  // the server sizes a sticker by its width (w) and keeps the artwork's aspect (app/imaging/ops_ext/edit.py StickerItem)
  if (it.kind === 'sticker') return { type: 'sticker', sticker: it.sticker, ...base, w: round4(it.w) };
  if (it.kind === 'shape') {
    return {
      type: 'shape', shape: it.shape, ...base, w: round4(it.w), h: round4(it.h),
      // ShapeItem: `color` is the outline, `fill` null = none
      color: it.color || '#e0262e', fill: it.fill || null, stroke_width: round4(it.strokeWidth ?? 0.008),
    };
  }
  if (it.kind === 'brush') {
    return { type: 'brush', points: it.points, color: it.color, width: round4(it.width), opacity: round3(it.opacity ?? 1) };
  }
  return null;
}

/** Overlay items worth sending (text needs words, brush needs points). */
export const liveItems = (file) => (file?.items || []).filter((it) => (it.kind === 'text' ? Boolean(String(it.text || '').trim()) : it.kind === 'brush' ? it.points?.length > 0 : true));

export function overlayOp(file) {
  const items = liveItems(file).map(overlayItemJSON).filter(Boolean);
  return items.length ? { op: 'overlay', items } : null;
}

export const watermarkActive = () => {
  const w = state.watermark;
  return w.kind === 'image' ? Boolean(w.imageId) : Boolean(w.text.trim());
};

export function watermarkOp() {
  if (!watermarkActive()) return null;
  const w = state.watermark;
  const op = {
    op: 'watermark', kind: w.kind, opacity: round3(w.opacity), rotation: round3(w.rotation),
    position: w.mosaic ? 'mosaic' : w.position, margin: round3(w.margin),
  };
  if (!w.mosaic && w.position === 'custom') Object.assign(op, { x: round4(w.x), y: round4(w.y) });
  if (w.kind === 'image') Object.assign(op, { image_id: w.imageId, image_scale: round3(w.imageScale) });
  else Object.assign(op, { text: w.text.trim().slice(0, 200), font: fontId(w.font), size: round3(w.size), color: w.color });
  return op;
}

const round3 = (v) => Math.round(Number(v) * 1000) / 1000;
const round4 = (v) => Math.round(Number(v) * 10000) / 10000;

/** Any op in this request can't be previewed live (export applies it). */
export const hasHeavyOps = (files = state.files) => files.some((f) => buildOps(f).some((o) => HEAVY_OPS.has(o.op)));

/** Any photo gets an op that is too slow for one request (upscale, background removal). */
export const hasJobOps = (files = state.files) => files.some((f) => buildOps(f).some((o) => JOB_OPS.has(o.op)));

/** Export through the img.process job (when the server has it): several photos, or a slow op. */
export const exportNeedsJob = (files = state.files) => files.length > 1 || hasJobOps(files);

/** Ops that change a photo's size before upscale, so its original size says nothing about upscale's input. */
const SIZE_OPS = new Set(['crop', 'crop_aspect', 'resize', 'pad', 'trim', 'passport', 'sheet']);

/**
 * Photos of a batch an upscale by `factor` can't take (input or output over the server's limits), as
 * [{file, plan}]. `dimsOf(file)` → the size entering upscale, or null when unknown (such photos are left to the
 * server to check rather than refused on a guess).
 */
export function upscaleProblems(files, dimsOf, factor = state.upscale.factor, limits = state.limits) {
  if (!(factor > 1)) return [];
  return files.map((file) => ({ file, plan: upscalePlan(dimsOf(file), factor, limits) })).filter((x) => x.plan && !x.plan.ok);
}

/** Best-known size entering upscale: the live preview's, else the original's when nothing before resizes it. */
export function upscaleInputDims(file) {
  const p = file?.preview;
  if (p?.width > 0 && p?.height > 0) return { w: p.width, h: p.height };
  if (!(file?.info?.width > 0 && file?.info?.height > 0)) return null;
  if (buildOps(file, { preview: true }).some((o) => SIZE_OPS.has(o.op))) return null;
  return { w: file.info.width, h: file.info.height };
}

/** Formats that keep every frame of an animated source ('png' only for an APNG source). */
function keepsAnimation(fmt, file) {
  if (fmt === 'original' || fmt === 'gif' || fmt === 'webp') return true;
  return fmt === 'png' && file?.info?.format === 'png';
}

/** Animated photos the chosen export format reduces to their first frame (export meta + status line). */
export function stillAnimations(files = state.files, fmt = state.output.format) {
  return files.filter((f) => Number(f?.info?.frames) > 1 && !keepsAnimation(fmt, f));
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

const MiB = 1024 * 1024;
/**
 * Largest request body the UI sends when the server doesn't publish `limits.max_request_mb`: below Cloudflare's
 * 100 MB request-body cap (its edge answers bigger uploads with an HTML 413 before they reach the server).
 */
export const DEFAULT_REQUEST_MB = 90;

/** Bytes of files per upload request: the server's (or the default) request cap minus 1 MiB of multipart framing. */
export function requestChunkBytes(limits) {
  const mb = limits?.max_request_mb > 0 ? limits.max_request_mb : DEFAULT_REQUEST_MB;
  return Math.max(1, mb - 1) * MiB;
}

/**
 * Split picked files into upload requests within the server's limits; oversize files are rejected up front.
 * A request carries at most `chunkFiles` files and `chunkBytes` bytes; a single larger file goes alone.
 */
export function planUploads(files, limits, { chunkFiles = 20, chunkBytes = requestChunkBytes(limits) } = {}) {
  const maxBytes = limits?.max_upload_mb > 0 ? limits.max_upload_mb * MiB : Infinity;
  const maxRequest = limits?.max_request_mb > 0 ? limits.max_request_mb * MiB : Infinity;
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
    if (f.size > maxRequest - MiB) {
      rejected.push({ name: f.name, detail: `larger than the ${fmtNum(limits.max_request_mb)} MB this site accepts in one upload` });
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

/** Rotate the batch (or the photos the "Apply to" filter picks) by ±90°, keeping flips and crop boxes consistent. */
export function rotateQuarter(dir) {
  const e = state.edit;
  e.quarter = (((e.quarter + (dir > 0 ? 90 : 270)) % 360) + 360) % 360;
  // flip-after-rotate is canonical: R90 ∘ Fh = Fv ∘ R90, so swap the flips (photos the turn skips keep plain flips).
  [e.flipH, e.flipV] = [e.flipV, e.flipH];
  for (const f of state.files) {
    const c = f.crop;
    if (c?.kind !== 'box' || !quarterApplies(f)) continue;
    const nb = dir > 0
      ? { x: 1 - c.y - c.h, y: c.x, w: c.h, h: c.w }
      : { x: c.y, y: 1 - c.x - c.w, w: c.h, h: c.w };
    f.crop = { ...c, ...nb, ratio: swapRatio(c.ratio) };
  }
  // any a:b, so 4:5 → 5:4 and 35:45 → 45:35 stay locked to the box's shape
  if (quarterApplies(activeFile())) state.cropRatio = swapRatio(state.cropRatio);
}

export function flip(axis) {
  const e = state.edit;
  if (axis === 'h') {
    e.flipH = !e.flipH;
    e.plainFlipH = !e.plainFlipH;
  } else {
    e.flipV = !e.flipV;
    e.plainFlipV = !e.plainFlipV;
  }
  for (const f of state.files) {
    const c = f.crop;
    if (c?.kind !== 'box') continue;
    f.crop = axis === 'h' ? { ...c, x: 1 - c.x - c.w } : { ...c, y: 1 - c.y - c.h };
  }
}

/**
 * Change Edit › "Apply to" (all | landscape | portrait). Photos whose rotation changes get their crop box
 * reset (it was drawn on the other orientation). Returns how many crops were reset.
 */
export function setRotateOnly(only) {
  const e = state.edit;
  const next = ['all', 'landscape', 'portrait'].includes(only) ? only : 'all';
  let reset = 0;
  if (e.quarter || e.flipH !== e.plainFlipH || e.flipV !== e.plainFlipV) {
    for (const f of state.files) {
      if (quarterApplies(f, e.only) !== quarterApplies(f, next) && f.crop?.kind === 'box') {
        f.crop = null;
        reset++;
      }
    }
  }
  e.only = next;
  if (activeFile() && !activeFile().crop) state.cropRatio = 'free';
  return reset;
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
  if (e.quarter) rot.push({ 90: '90° right', 180: '180°', 270: '90° left' }[e.quarter] + (e.only !== 'all' ? ` (${e.only} only)` : ''));
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
  if (e.plainFlipH || e.plainFlipV) {
    // named by what was pressed (the canonical flags swap with every quarter turn)
    const label = e.plainFlipH && e.plainFlipV ? 'Flip both' : e.plainFlipH ? 'Flip horizontal' : 'Flip vertical';
    out.push({ key: 'flip', tool: 'edit', label, clear: () => flipReset() });
  }
  const regions = activeRegions(file);
  if (regions.length) {
    out.push({
      key: 'blur', tool: 'blur', label: `${{ blur: 'Blur', pixelate: 'Pixelate', solid: 'Cover' }[state.blur.mode] || 'Blur'} ${regions.length} area${regions.length > 1 ? 's' : ''}`,
      clear: () => { file.regions = []; },
    });
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
  const ed = state.editor;
  if (filterOp()) {
    out.push({ key: 'filter', tool: 'editor', label: `Filter ${catalogLabel(FILTERS, ed.filter)}${ed.intensity < 1 ? ` ${Math.round(ed.intensity * 100)}%` : ''}`, clear: () => { ed.filter = 'none'; ed.intensity = 1; } });
  }
  if (upscaleActive()) {
    out.push({ key: 'upscale', tool: 'upscale', label: `Upscale ${state.upscale.factor}×`, clear: () => { state.upscale.factor = 0; } });
  }
  if (frameOp()) out.push({ key: 'frame', tool: 'editor', label: `Frame ${catalogLabel(FRAMES, ed.frame)}`, clear: () => { ed.frame = 'none'; } });
  if (memeActive()) {
    const m = state.meme;
    out.push({ key: 'meme', tool: 'meme', label: `Meme “${clip(m.top.trim() || m.bottom.trim(), 18)}”`, clear: () => { state.meme = { ...defaults.meme(), font: m.font, color: m.color, outline: m.outline }; } });
  }
  const items = liveItems(file);
  if (items.length) {
    const kinds = new Set(items.map((it) => it.kind));
    const what = kinds.size > 1 ? `${items.length} layers` : `${items.length} ${{ text: 'text', sticker: 'sticker', shape: 'shape', brush: 'drawing' }[[...kinds][0]]}${items.length > 1 && !kinds.has('text') ? 's' : ''}`;
    out.push({ key: 'overlay', tool: 'editor', label: what.charAt(0).toUpperCase() + what.slice(1), clear: () => { file.items = []; } });
  }
  if (watermarkActive()) {
    const w = state.watermark;
    out.push({
      key: 'watermark', tool: 'watermark', label: w.kind === 'image' ? 'Logo watermark' : `Watermark “${clip(w.text.trim(), 16)}”`,
      clear: () => {
        if (w.kind === 'image') Object.assign(w, { imageId: null, imageName: '', imageW: 0, imageH: 0 });
        else w.text = '';
      },
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

const clip = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/** Clear every flip, mirroring each photo's crop box by the flips its own op list had. */
export function flipReset() {
  const e = state.edit;
  for (const f of state.files) {
    const c = f.crop;
    if (c?.kind !== 'box') continue;
    const turns = quarterApplies(f);
    let b = c;
    if (turns ? e.flipH : e.plainFlipH) b = { ...b, x: 1 - b.x - b.w };
    if (turns ? e.flipV : e.plainFlipV) b = { ...b, y: 1 - b.y - b.h };
    f.crop = b;
  }
  Object.assign(e, { flipH: false, flipV: false, plainFlipH: false, plainFlipV: false });
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
