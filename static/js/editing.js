// The v2 editing layer on the canvas: which items each section shows over the picture (blur boxes, the
// watermark, meme captions, editor layers, the upscale detail square), how canvas gestures write back into
// the state, and which picture they are drawn on.
//
// Items are fractions of the image ENTERING the section's stage (state.js STAGES). When nothing after that
// stage changes the picture's frame (rotate, crop, passport, frame, padded resize, sheet …), the live preview
// maps 1:1 onto those fractions and is shown as usual. Otherwise the canvas shows the stage's input image
// (`stageBaseOps`) and the layer draws an instant approximation of the section's effect on it.

import { OverlayLayer, strokeBounds } from './overlay.js';
import { stageKinds, watermarkGeom, memeLayout, loupeRegion } from './stagekit.js';
import { state, buildOps, newId, activeRegions } from './state.js';
import { POSITIONS, fontCss } from './catalog.js';
import { clamp } from './dom.js';

/** Section → pipeline stage its canvas items are measured on. */
export const EDIT_STAGE = { blur: 'blur', watermark: 'watermark', meme: 'meme', editor: 'overlay', upscale: 'upscale' };

/** Run after the next paint (the layer re-creates its item elements on refresh). */
const later = (fn) => (typeof requestAnimationFrame === 'function' ? requestAnimationFrame(() => fn()) : setTimeout(fn, 0));

/** Does this op change the picture's frame (so fractions before it no longer line up with the result)? */
export function changesFrame(op) {
  if (!op) return false;
  if (['rotate', 'flip', 'crop', 'crop_aspect', 'trim', 'passport', 'frame', 'sheet'].includes(op.op)) return true;
  if (op.op === 'resize') return op.mode === 'fill' || op.mode === 'pad' || op.fit === 'fill' || op.fit === 'pad';
  return false;
}

/**
 * The ops whose render is the picture a section's items sit on, or null when the live preview is that picture
 * (nothing after the stage moves the frame). `ops`/`before` are injectable for the tests.
 */
export function stageBaseOps(file, stage, { ops = null, before = null } = {}) {
  const full = ops || buildOps(file, { preview: true });
  const pre = before || buildOps(file, { until: stage, preview: true });
  const after = full.slice(pre.length);
  return after.some(changesFrame) ? pre : null;
}

/**
 * A watermark dragged to centre (cx, cy): the nine-grid position whose placement is within `snap` of it
 * (fractions of the picture, per axis), else 'custom'. `geomAt(position)` → {cx, cy} of that position.
 */
export function snapWatermark(cx, cy, geomAt, snap = { x: 0.02, y: 0.02 }) {
  let best = null;
  for (const [pos] of POSITIONS) {
    const g = geomAt(pos);
    const dx = Math.abs(g.cx - cx);
    const dy = Math.abs(g.cy - cy);
    if (dx <= snap.x && dy <= snap.y && (!best || dx + dy < best.d)) best = { pos, d: dx + dy };
  }
  return best ? { position: best.pos } : { position: 'custom', x: clamp(cx, 0, 1), y: clamp(cy, 0, 1) };
}

/** Translate a brush stroke by (dx, dy) fractions, keeping every point on the picture. */
export function shiftStroke(points, dx, dy) {
  return (points || []).map(([x, y]) => [Math.round(clamp(x + dx, 0, 1) * 1e4) / 1e4, Math.round(clamp(y + dy, 0, 1) * 1e4) / 1e4]);
}

/** Ask the browser for a text font before measuring with it (a web font loads on first use). */
const asked = new Set();
export function loadFont(id) {
  const fam = fontCss(id).split(',')[0].trim();
  if (asked.has(fam) || typeof document === 'undefined' || !document.fonts?.load) return;
  asked.add(fam);
  document.fonts.load(`16px ${fam}`).catch(() => { /* the fallback stack draws it */ });
}

const MAX_REGIONS = 64;
const MAX_ITEMS = 100;
const MAX_POINTS = 20000;

export class Editing {
  /**
   * hooks: {changed(opts), selection(id), activate(item), logoSrc(), stickerSrc(id), loupeDims(), notify(msg)}
   */
  constructor(canvas, hooks) {
    this.canvas = canvas;
    this.hooks = hooks;
    this.tool = null;
    this.file = null;
    this.selected = null;
    this.live = null; // {id, size, scale…}: values at the start of a live resize
    const env = {
      blur: () => state.blur,
      wm: () => state.watermark,
      meme: () => state.meme,
      logoSrc: () => hooks.logoSrc?.() || '',
      stickerSrc: (id) => hooks.stickerSrc?.(id) || '',
    };
    const kinds = stageKinds(env);
    kinds.text = { ...kinds.text, fitContent: true };
    this.layer = new OverlayLayer(canvas.stage, {
      kinds,
      onChange: (id, geom, info) => this.onChange(id, geom, info),
      onCommit: (id, geom, info) => this.onCommit(id, geom, info),
      onSelect: (id) => { this.selected = id; this.hooks.selection?.(id); },
      onCreate: (spec) => this.onCreate(spec),
      onDelete: (id) => this.remove(id),
      onActivate: (id) => this.hooks.activate?.(this.itemById(id)),
    });
    canvas.layers.push(this.layer);
  }

  get active() {
    return Boolean(this.tool && this.layer.visible);
  }

  /** Show the layer for section `tool` on `file` (null hides it). `settled`: the shown render already has the effect. */
  update({ tool, file, settled = false }) {
    const stage = tool ? EDIT_STAGE[tool] : null;
    if (!stage || !file || !this.canvas.rect || this.canvas.img.hidden) {
      this.tool = null;
      this.file = null;
      this.layer.show(false);
      return;
    }
    if (this.tool !== tool || this.file !== file) this.selected = null;
    this.tool = tool;
    this.file = file;
    const mode = tool === 'blur' ? 'box' : tool === 'editor' && state.editor.tab === 'brush' && state.editor.drawing ? 'brush' : 'select';
    this.layer.setMode(mode, tool === 'editor' ? state.editor.brush : undefined);
    this.layer.setSettled(settled);
    this.layer.layer.dataset.tool = tool;
    this.layer.show(true);
    this.layer.place(this.canvas.rect);
    this.layer.setItems(this.items(), this.selected);
    this.selected = this.layer.selected;
  }

  /** Re-draw the items from the state (after panel edits) without moving the layer. */
  refresh() {
    if (!this.tool || !this.layer.visible) return;
    this.layer.setItems(this.items(), this.selected);
  }

  select(id, opts) {
    this.selected = id;
    this.layer.select(id, opts);
  }

  itemById(id) {
    const f = this.file;
    if (!f) return null;
    if (this.tool === 'blur') return (f.regions || []).find((r) => r.id === id) || null;
    if (this.tool === 'editor') return (f.items || []).find((it) => it.id === id) || null;
    return { id, kind: id };
  }

  /** Items for the current section, in display order. */
  items() {
    const f = this.file;
    const W = this.layer.W;
    const H = this.layer.H;
    switch (this.tool) {
      case 'blur':
        return (f.regions || []).map((g) => ({ ...g, kind: 'region', off: g.on === false, rotation: 0 }));
      case 'watermark': {
        const wm = state.watermark;
        if (wm.kind === 'text') loadFont(wm.font);
        if (!(wm.kind === 'image' ? wm.imageId : wm.text.trim())) return [];
        if (wm.mosaic) return [{ id: 'wm', kind: 'wm-mosaic', cx: 0.5, cy: 0.5, w: 1, h: 1, rotation: 0 }];
        const aspect = wm.imageW > 0 && wm.imageH > 0 ? wm.imageW / wm.imageH : 1;
        const g = this.wmGeom(wm, W, H, aspect);
        return [{ id: 'wm', kind: 'watermark', ...g, rotation: wm.rotation }];
      }
      case 'meme': {
        loadFont(state.meme.font);
        const lay = memeLayout(state.meme, W, H);
        return ['top', 'bottom'].filter((k) => lay[k]).map((k) => ({ id: `meme-${k}`, kind: `meme-${k}`, rotation: 0, ...lay[k] }));
      }
      case 'editor':
        return (f.items || []).map((it) => {
          if (it.kind === 'text') loadFont(it.font);
          if (it.kind === 'sticker') return { ...it, h: (it.w * W * (it.ratio || 1)) / H };
          if (it.kind === 'brush') return { ...it, ...strokeBounds(it.points, it.width, { W, H }), rotation: 0 };
          return { ...it };
        });
      case 'upscale': {
        if (!(state.upscale.factor > 1)) return [];
        const dims = this.hooks.loupeDims?.();
        if (!dims) return [];
        const r = loupeRegion(state.upscale.loupe, dims.w, dims.h);
        return [{ id: 'loupe', kind: 'loupe', cx: r.x + r.w / 2, cy: r.y + r.h / 2, w: r.w, h: r.h, rotation: 0 }];
      }
      default:
        return [];
    }
  }

  wmGeom(wm, W, H, aspect) {
    if (!wm.mosaic && wm.position === 'custom') {
      const g = watermarkGeom({ ...wm, position: 'center' }, W, H, { logoAspect: aspect });
      return { ...g, cx: wm.x, cy: wm.y };
    }
    return watermarkGeom(wm, W, H, { logoAspect: aspect });
  }

  // ── gestures ─────────────────────────────────────────────────────────────

  onChange(id, geom, { mode, scale }) {
    if (mode !== 'resize') return;
    const s = Number.isFinite(scale) && scale > 0 ? scale : 1;
    if (this.tool === 'editor') {
      const it = this.itemById(id);
      if (it?.kind !== 'text') return;
      if (this.live?.id !== id) this.live = { id, size: it.size };
      it.size = clamp(this.live.size * s, 0.01, 0.6);
      this.layer.setItems(this.items(), this.selected);
    } else if (this.tool === 'watermark') {
      const wm = state.watermark;
      if (this.live?.id !== id) this.live = { id, size: wm.size, scale: wm.imageScale };
      if (wm.kind === 'image') wm.imageScale = clamp(this.live.scale * s, 0.01, 1);
      else wm.size = clamp(this.live.size * s, 0.005, 0.5);
    }
  }

  onCommit(id, geom, { mode }) {
    this.live = null;
    const f = this.file;
    if (!f) return;
    switch (this.tool) {
      case 'blur': {
        const g = this.itemById(id);
        if (!g) return;
        Object.assign(g, { cx: geom.cx, cy: geom.cy, w: geom.w, h: geom.h });
        if (g.source && g.source !== 'manual') g.edited = true;
        break;
      }
      case 'watermark': {
        const wm = state.watermark;
        if (mode === 'rotate') wm.rotation = Math.round(geom.rotation);
        else if (mode === 'move') {
          const W = this.layer.W;
          const H = this.layer.H;
          const aspect = wm.imageW > 0 && wm.imageH > 0 ? wm.imageW / wm.imageH : 1;
          const snapped = snapWatermark(geom.cx, geom.cy, (pos) => watermarkGeom({ ...wm, position: pos }, W, H, { logoAspect: aspect }), { x: 10 / W, y: 10 / H });
          Object.assign(wm, snapped);
        }
        break;
      }
      case 'editor': {
        const it = this.itemById(id);
        if (!it) return;
        if (it.kind === 'brush') {
          const b = strokeBounds(it.points, it.width, { W: this.layer.W, H: this.layer.H });
          it.points = shiftStroke(it.points, geom.cx - b.cx, geom.cy - b.cy);
        } else {
          it.cx = geom.cx;
          it.cy = geom.cy;
          it.rotation = Math.round((geom.rotation || 0) * 10) / 10;
          if (it.kind !== 'text') {
            it.w = Math.max(0.005, geom.w);
            if (it.kind === 'shape') it.h = Math.max(0.005, geom.h);
          }
        }
        break;
      }
      case 'upscale':
        state.upscale.loupe = { cx: geom.cx, cy: geom.cy };
        break;
      default:
        return;
    }
    this.hooks.changed?.({ panel: this.tool === 'editor' || this.tool === 'watermark' });
  }

  onCreate(spec) {
    const f = this.file;
    if (!f) return;
    if (spec.kind === 'box' && this.tool === 'blur') {
      f.regions ||= [];
      if (f.regions.length >= MAX_REGIONS) {
        this.hooks.notify?.(`At most ${MAX_REGIONS} areas per photo.`);
        return;
      }
      const id = newId('r');
      f.regions.push({ id, source: 'manual', on: true, shape: 'rect', cx: spec.cx, cy: spec.cy, w: spec.w, h: spec.h, score: null });
      this.selected = id;
      this.hooks.changed?.({ panel: true });
      // focus the new box (drawing kept focus on the page): Delete / arrows now act on it (UIH-03). After the
      // re-render the changed() hook schedules, which re-creates the item elements.
      later(() => { if (this.file === f && f.regions.some((r) => r.id === id)) this.select(id, { focus: true }); });
      return;
    }
    if (spec.kind === 'brush' && this.tool === 'editor') {
      const b = state.editor.brush;
      const used = (f.items || []).reduce((n, it) => n + (it.points?.length || 0), 0);
      if (used + spec.points.length > MAX_POINTS) {
        this.hooks.notify?.('That’s a lot of drawing — undo some strokes before adding more.');
        return;
      }
      if (!this.add({ kind: 'brush', points: spec.points, color: b.color, width: b.width, opacity: b.opacity }, { select: false })) return;
      this.hooks.changed?.({ panel: true });
    }
  }

  /** Add an editor item to the active photo (centred unless placed). Returns its id, or null when full. */
  add(item, { select = true } = {}) {
    const f = this.file;
    if (!f) return null;
    f.items ||= [];
    if (f.items.length >= MAX_ITEMS) {
      this.hooks.notify?.(`At most ${MAX_ITEMS} layers per photo.`);
      return null;
    }
    const id = newId('o');
    f.items.push({ cx: 0.5, cy: 0.5, w: 0.2, h: 0.1, rotation: 0, opacity: 1, ...item, id });
    if (select) this.selected = id;
    return id;
  }

  remove(id) {
    const f = this.file;
    if (!f) return;
    if (this.tool === 'blur') f.regions = (f.regions || []).filter((r) => r.id !== id);
    else if (this.tool === 'editor') f.items = (f.items || []).filter((it) => it.id !== id);
    else return;
    if (this.selected === id) this.selected = null;
    this.hooks.changed?.({ panel: true });
  }
}

/** Regions switched on, for counting in the panel (re-exported for the app). */
export { activeRegions };
