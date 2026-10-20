// Editing layer over the displayed picture: movable / resizable / rotatable items (text, stickers, shapes,
// watermark, meme lines, blur boxes, the upscale loupe) and freehand brush strokes.
// Geometry is stored as fractions of the image (cx, cy = centre; w, h = size), so the layer only has to know
// the displayed image rect (stage px, uniform scale of the real image) to map pointer moves onto items.
// The pure helpers below are exported for the node tests.

import { h, clamp } from './dom.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
const CORNERS = ['nw', 'ne', 'se', 'sw'];
const SNAP_PX = 6; // centre snapping distance (displayed px)
const MIN_PX = 8; // smallest item side (displayed px)

const rad = (deg) => (deg * Math.PI) / 180;
const round = (v, d = 5) => Math.round(v * 10 ** d) / 10 ** d;

/** Normalise an angle to (−180, 180]. */
export function normAngle(deg) {
  let a = ((Number(deg) || 0) % 360 + 360) % 360;
  if (a > 180) a -= 360;
  return round(a, 3);
}

/** Handle name → sign of the x / y side it moves (w/n = −1, e/s = +1, middle = 0). */
export function handleSigns(hnd) {
  return { sx: hnd.includes('w') ? -1 : hnd.includes('e') ? 1 : 0, sy: hnd.includes('n') ? -1 : hnd.includes('s') ? 1 : 0 };
}

/**
 * Move an item by (dx, dy) fractions, keeping its centre on the picture. With `snap` (fractions per axis) the
 * centre sticks to the middle of the picture when it comes that close. Returns {item, snapped: {x, y}}.
 */
export function moveItem(item, dx, dy, { snap = null } = {}) {
  let cx = clamp(item.cx + dx, 0, 1);
  let cy = clamp(item.cy + dy, 0, 1);
  const snapped = { x: false, y: false };
  if (snap && Math.abs(cx - 0.5) <= snap.x) { cx = 0.5; snapped.x = true; }
  if (snap && Math.abs(cy - 0.5) <= snap.y) { cy = 0.5; snapped.y = true; }
  return { item: { ...item, cx: round(cx), cy: round(cy) }, snapped };
}

/**
 * Resize a (possibly rotated) item by dragging handle `hnd` by (dx, dy) displayed px on a W×H display.
 * The opposite side / corner stays put. `keepRatio` scales both sides together (corners and sides alike).
 * Returns {item, scale} — `scale` is the width factor applied (callers scale font sizes with it).
 */
export function resizeItem(item, hnd, dx, dy, { W, H, keepRatio = false, min = MIN_PX } = {}) {
  const { sx, sy } = handleSigns(hnd);
  const t = rad(item.rotation || 0);
  const cos = Math.cos(t);
  const sin = Math.sin(t);
  // pointer delta in the item's own (unrotated) frame
  const lx = dx * cos + dy * sin;
  const ly = -dx * sin + dy * cos;
  const w0 = Math.max(item.w * W, 1e-6);
  const h0 = Math.max(item.h * H, 1e-6);
  let w1 = sx ? w0 + sx * lx : w0;
  let h1 = sy ? h0 + sy * ly : h0;
  if (keepRatio) {
    let s;
    if (sx && sy) s = Math.max(w1 / w0, h1 / h0);
    else if (sx) s = w1 / w0;
    else s = h1 / h0;
    s = Math.max(s, min / Math.min(w0, h0));
    w1 = w0 * s;
    h1 = h0 * s;
  } else {
    w1 = Math.max(min, w1);
    h1 = Math.max(min, h1);
  }
  // the centre follows the moved side: half the growth along each moved axis, rotated back to the display
  const ox = keepRatio && !sx ? 0 : (sx * (w1 - w0)) / 2;
  const oy = keepRatio && !sy ? 0 : (sy * (h1 - h0)) / 2;
  const cx = item.cx * W + ox * cos - oy * sin;
  const cy = item.cy * H + ox * sin + oy * cos;
  return {
    item: { ...item, cx: round(clamp(cx / W, 0, 1)), cy: round(clamp(cy / H, 0, 1)), w: round(w1 / W), h: round(h1 / H) },
    scale: w1 / w0,
  };
}

/** Rotation (degrees) that points the item's top at display point (px, py); `snap` rounds to 15°. */
export function rotationTowards(item, px, py, { W, H, snap = false } = {}) {
  const a = (Math.atan2(py - item.cy * H, px - item.cx * W) * 180) / Math.PI + 90;
  return normAngle(snap ? Math.round(a / 15) * 15 : a);
}

/** Axis-aligned box (fractions) spanned by two display points, clipped to the picture. */
export function boxFromPoints(ax, ay, bx, by, { W, H }) {
  const x0 = clamp(Math.min(ax, bx), 0, W);
  const x1 = clamp(Math.max(ax, bx), 0, W);
  const y0 = clamp(Math.min(ay, by), 0, H);
  const y1 = clamp(Math.max(ay, by), 0, H);
  return { cx: round((x0 + x1) / 2 / W), cy: round((y0 + y1) / 2 / H), w: round((x1 - x0) / W), h: round((y1 - y0) / H) };
}

/** Item centre box → top-left box {x, y, w, h} in fractions (axis-aligned items only), clipped to 0…1. */
export function toCornerBox(it) {
  const x0 = clamp(it.cx - it.w / 2, 0, 1);
  const y0 = clamp(it.cy - it.h / 2, 0, 1);
  const x1 = clamp(it.cx + it.w / 2, 0, 1);
  const y1 = clamp(it.cy + it.h / 2, 0, 1);
  return { x: round(x0, 6), y: round(y0, 6), w: round(x1 - x0, 6), h: round(y1 - y0, 6) };
}

/** Top-left box → centre item fields. */
export function fromCornerBox(b) {
  return { cx: round(b.x + b.w / 2, 6), cy: round(b.y + b.h / 2, 6), w: round(b.w, 6), h: round(b.h, 6) };
}

/** Drop points closer than `minDist` (fractions, measured in display px via W/H) — keeps strokes small. */
export function simplifyStroke(points, { W, H, minDist = 1.5 } = {}) {
  const out = [];
  for (const p of points) {
    const last = out[out.length - 1];
    if (!last || Math.hypot((p[0] - last[0]) * W, (p[1] - last[1]) * H) >= minDist) out.push(p);
  }
  if (points.length > 1 && out[out.length - 1] !== points[points.length - 1]) out.push(points[points.length - 1]);
  return out.map(([x, y]) => [round(x, 4), round(y, 4)]);
}

/** Bounding box (centre form) of a stroke's points, padded by half the line width (fractions of W). */
export function strokeBounds(points, widthFrac = 0, { W = 1, H = 1 } = {}) {
  if (!points?.length) return { cx: 0.5, cy: 0.5, w: 0, h: 0 };
  let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
  for (const [x, y] of points) {
    x0 = Math.min(x0, x); x1 = Math.max(x1, x);
    y0 = Math.min(y0, y); y1 = Math.max(y1, y);
  }
  const px = widthFrac / 2;
  const py = (widthFrac * W) / H / 2;
  return { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, w: x1 - x0 + 2 * px, h: y1 - y0 + 2 * py };
}

/** Nearest of the nine watermark positions to a centre point (fractions). */
export function nearestZone(cx, cy) {
  const col = cx < 1 / 3 ? 'left' : cx > 2 / 3 ? 'right' : 'center';
  const row = cy < 1 / 3 ? 'top' : cy > 2 / 3 ? 'bottom' : 'middle';
  return { row, col };
}

// ── the layer ──────────────────────────────────────────────────────────────

/**
 * Item behaviour per kind, supplied by the app:
 *   { move: bool, resize: false | 'free' | 'ratio', rotate: bool, corners: bool (4 handles instead of 8),
 *     render(item, contentEl, ctx) → void, label(item) → string, fitContent: bool (size comes from the content) }
 */
export class OverlayLayer {
  constructor(host, { kinds = {}, onChange, onCommit, onSelect, onCreate, onDelete, onActivate } = {}) {
    this.kinds = kinds;
    this.cb = { onChange, onCommit, onSelect, onCreate, onDelete, onActivate };
    this.items = [];
    this.selected = null;
    this.mode = 'select'; // 'select' | 'box' (drag on empty draws a box) | 'brush'
    this.drag = null;
    this.rect = null;
    this.els = new Map();
    this.brushStyle = { color: '#e0262e', width: 0.01, opacity: 1 };
    this.strokes = document.createElementNS(SVG_NS, 'svg');
    this.strokes.setAttribute('class', 'ov-strokes');
    this.strokes.setAttribute('aria-hidden', 'true');
    this.strokes.setAttribute('preserveAspectRatio', 'none');
    this.live = document.createElementNS(SVG_NS, 'path');
    this.live.setAttribute('class', 'ov-live-stroke');
    this.guideX = h('div', { class: 'ov-guide ov-guide-x', hidden: true });
    this.guideY = h('div', { class: 'ov-guide ov-guide-y', hidden: true });
    this.zones = h('div', { class: 'ov-zones', hidden: true, 'aria-hidden': 'true' }, Array.from({ length: 9 }, () => h('i')));
    this.drawBox = h('div', { class: 'ov-drawbox', hidden: true });
    this.help = h('p', { id: 'ov-help', class: 'sr-only' }, 'Arrow keys move the item (Shift ×10), Alt with arrows resizes, [ and ] rotate, Delete removes it, Escape deselects.');
    this.layer = h('div', { class: 'ov-layer', hidden: true }, this.strokes, this.zones, this.guideX, this.guideY, this.drawBox, this.help);
    host.append(this.layer);
    this.layer.addEventListener('pointerdown', (e) => this.down(e));
    this.layer.addEventListener('pointermove', (e) => this.move(e));
    this.layer.addEventListener('pointerup', (e) => this.up(e));
    this.layer.addEventListener('pointercancel', (e) => this.up(e, true));
    this.layer.addEventListener('keydown', (e) => this.key(e));
    this.layer.addEventListener('dblclick', (e) => {
      const id = e.target.closest?.('.ov-item')?.dataset.id;
      if (id) this.cb.onActivate?.(id);
    });
  }

  get visible() {
    return !this.layer.hidden;
  }

  get dragging() {
    return Boolean(this.drag?.active);
  }

  show(on) {
    this.layer.hidden = !on;
    if (!on) this.cancelDrag();
  }

  /** Where the picture is drawn (stage px). */
  place(rect) {
    if (!rect) return;
    this.rect = rect;
    Object.assign(this.layer.style, { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px` });
    this.strokes.setAttribute('viewBox', `0 0 ${rect.width} ${rect.height}`);
    this.paintAll();
  }

  get W() { return this.rect?.width || 1; }
  get H() { return this.rect?.height || 1; }

  setMode(mode, brushStyle) {
    this.mode = mode || 'select';
    if (brushStyle) this.brushStyle = { ...this.brushStyle, ...brushStyle };
    this.layer.dataset.mode = this.mode;
  }

  /** The finished render is on screen: draw outlines only (items stay grabbable). */
  setSettled(on) {
    this.layer.classList.toggle('is-settled', Boolean(on));
  }

  setZones(on) {
    this.zones.hidden = !on;
  }

  /** Replace the items (keeps the element of an item being dragged). */
  setItems(items, selectedId = this.selected) {
    if (this.drag?.active) {
      // a re-render mid-gesture would drop the pointer: keep the dragged item's live geometry
      const d = this.drag;
      items = items.map((it) => (it.id === d.id ? { ...it, ...pickGeom(d.cur) } : it));
    }
    this.items = items.map((it) => ({ ...it }));
    if (selectedId && !this.items.some((it) => it.id === selectedId)) selectedId = null;
    this.selected = selectedId;
    const keep = new Set(this.items.map((it) => it.id));
    for (const [id, el] of this.els) if (!keep.has(id)) { el.remove(); this.els.delete(id); }
    this.paintAll();
  }

  select(id, { focus = false } = {}) {
    if (this.selected === id) {
      if (focus) this.els.get(id)?.focus({ preventScroll: true });
      return;
    }
    this.selected = id && this.items.some((it) => it.id === id) ? id : null;
    this.paintAll();
    if (focus && this.selected) this.els.get(this.selected)?.focus({ preventScroll: true });
    this.cb.onSelect?.(this.selected);
  }

  kindOf(it) {
    return this.kinds[it.kind] || {};
  }

  paintAll() {
    if (!this.rect) return;
    const paths = [];
    for (const it of this.items) {
      if (it.kind === 'brush') paths.push(this.strokePath(it));
      this.paintItem(it);
    }
    this.strokes.replaceChildren(...paths, this.live);
    // DOM order = paint order. Only re-order when it changed: moving a focused element would blur it.
    const want = this.items.map((it) => this.els.get(it.id)).filter(Boolean);
    const have = [...this.layer.children].filter((c) => c.classList.contains('ov-item'));
    if (want.length !== have.length || want.some((el, i) => el !== have[i])) {
      const focused = document.activeElement;
      for (const el of want) this.layer.append(el);
      if (focused && want.includes(focused) && document.activeElement !== focused) focused.focus({ preventScroll: true });
    }
  }

  strokePath(it) {
    const p = document.createElementNS(SVG_NS, 'path');
    const pts = it.points || [];
    p.setAttribute('d', pts.map(([x, y], i) => `${i ? 'L' : 'M'}${(x * this.W).toFixed(1)} ${(y * this.H).toFixed(1)}`).join(' ') + (pts.length === 1 ? ' l0.01 0' : ''));
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke', it.color || '#000');
    p.setAttribute('stroke-width', String(Math.max(0.5, (it.width || 0.01) * this.W)));
    p.setAttribute('stroke-linecap', 'round');
    p.setAttribute('stroke-linejoin', 'round');
    p.setAttribute('opacity', String(it.opacity ?? 1));
    p.dataset.id = it.id;
    return p;
  }

  paintItem(it) {
    const k = this.kindOf(it);
    let el = this.els.get(it.id);
    if (!el) {
      el = h('div', {
        class: 'ov-item', tabindex: '0', role: 'group', 'aria-roledescription': 'movable item', 'aria-describedby': 'ov-help',
        dataset: { id: it.id },
      }, h('div', { class: 'ov-content' }));
      el.addEventListener('focus', () => { if (this.selected !== it.id && !this.drag) this.select(it.id); });
      this.els.set(it.id, el);
      this.layer.append(el);
    }
    const sel = this.selected === it.id;
    el.className = `ov-item ov-${it.kind}${sel ? ' is-selected' : ''}${k.move === false ? ' is-fixed' : ''}${it.off ? ' is-off' : ''}`;
    el.dataset.id = it.id;
    el.setAttribute('aria-label', k.label ? k.label(it) : it.kind);
    const content = el.firstElementChild;
    k.render?.(it, content, { W: this.W, H: this.H, selected: sel, settled: this.layer.classList.contains('is-settled') });
    this.position(el, it);
    // handles
    for (const x of [...el.querySelectorAll('.ov-h, .ov-rot')]) x.remove();
    if (sel) {
      if (k.resize) for (const hn of (k.corners || k.resize === 'ratio' ? CORNERS : HANDLES)) el.append(h('span', { class: `ov-h ov-h-${hn}`, dataset: { h: hn }, 'aria-hidden': 'true' }));
      if (k.rotate) el.append(h('span', { class: 'ov-rot', dataset: { h: 'rot' }, 'aria-hidden': 'true' }));
    }
  }

  position(el, it) {
    const k = this.kindOf(it);
    const W = this.W;
    const H = this.H;
    const st = el.style;
    st.left = `${it.cx * W}px`;
    st.top = `${it.cy * H}px`;
    if (k.fitContent) {
      st.width = '';
      st.height = '';
    } else {
      st.width = `${Math.max(1, it.w * W)}px`;
      st.height = `${Math.max(1, it.h * H)}px`;
    }
    st.transform = `translate(-50%, -50%)${it.rotation ? ` rotate(${it.rotation}deg)` : ''}`;
    if (k.fitContent) {
      // the content decides the size: record it so handles and hit tests match what is drawn
      const w = el.offsetWidth;
      const hh = el.offsetHeight;
      if (w && hh) {
        it.w = round(w / W);
        it.h = round(hh / H);
      }
    }
  }

  // ── pointer ──────────────────────────────────────────────────────────────

  local(e) {
    const b = this.layer.getBoundingClientRect();
    // the layer is never rotated or scaled by CSS, so client px map 1:1 onto layer px
    return { x: e.clientX - b.left, y: e.clientY - b.top };
  }

  down(e) {
    if (e.button !== 0 || !this.rect) return;
    const p = this.local(e);
    const hEl = e.target.closest?.('[data-h]');
    const itemEl = e.target.closest?.('.ov-item');
    const id = itemEl?.dataset.id;
    const it = id ? this.items.find((x) => x.id === id) : null;
    e.preventDefault();
    if (it) {
      const k = this.kindOf(it);
      const hnd = hEl?.dataset.h;
      const mode = hnd === 'rot' ? 'rotate' : hnd ? 'resize' : k.move === false ? 'none' : 'move';
      if (this.selected !== id) this.select(id);
      this.els.get(id)?.focus({ preventScroll: true });
      this.layer.classList.add('pointer-focus');
      if (mode === 'none') return;
      this.drag = { id, mode, hnd, pid: e.pointerId, sx: p.x, sy: p.y, start: { ...it }, cur: { ...it }, active: false, scale: 1 };
    } else if (this.mode === 'box') {
      this.drag = { mode: 'draw', pid: e.pointerId, sx: p.x, sy: p.y, active: false };
    } else if (this.mode === 'brush') {
      this.drag = { mode: 'brush', pid: e.pointerId, points: [[clamp(p.x / this.W, 0, 1), clamp(p.y / this.H, 0, 1)]], active: true };
      this.paintLive();
    } else {
      if (this.selected) this.select(null);
      return;
    }
    try { this.layer.setPointerCapture(e.pointerId); } catch { /* pointer gone: the gesture still ends on up */ }
  }

  move(e) {
    const d = this.drag;
    if (!d || e.pointerId !== d.pid) return;
    const p = this.local(e);
    const W = this.W;
    const H = this.H;
    if (d.mode === 'brush') {
      d.points.push([clamp(p.x / W, 0, 1), clamp(p.y / H, 0, 1)]);
      this.paintLive();
      return;
    }
    const dx = p.x - d.sx;
    const dy = p.y - d.sy;
    if (!d.active && Math.hypot(dx, dy) < 3) return;
    if (!d.active) {
      d.active = true;
      this.layer.classList.add('is-dragging');
      if (d.mode !== 'draw' && this.kindOf(d.start).zones) this.setZones(true);
    }
    if (d.mode === 'draw') {
      const b = boxFromPoints(d.sx, d.sy, p.x, p.y, { W, H });
      Object.assign(this.drawBox.style, { left: `${(b.cx - b.w / 2) * W}px`, top: `${(b.cy - b.h / 2) * H}px`, width: `${b.w * W}px`, height: `${b.h * H}px` });
      this.drawBox.hidden = false;
      d.box = b;
      return;
    }
    const k = this.kindOf(d.start);
    let next = d.start;
    if (d.mode === 'move') {
      const r = moveItem(d.start, dx / W, dy / H, { snap: k.snap === false ? null : { x: SNAP_PX / W, y: SNAP_PX / H } });
      next = r.item;
      this.guideX.hidden = !r.snapped.x;
      this.guideY.hidden = !r.snapped.y;
      if (d.start.kind === 'brush') {
        // strokes are drawn in the shared SVG: slide this one along with its box
        const path = [...this.strokes.children].find((p) => p.dataset?.id === d.id);
        path?.setAttribute('transform', `translate(${((next.cx - d.start.cx) * W).toFixed(1)} ${((next.cy - d.start.cy) * H).toFixed(1)})`);
      }
    } else if (d.mode === 'resize') {
      const r = resizeItem(d.start, d.hnd, dx, dy, { W, H, keepRatio: k.resize === 'ratio' });
      next = r.item;
      d.scale = r.scale;
    } else if (d.mode === 'rotate') {
      next = { ...d.start, rotation: rotationTowards(d.start, p.x, p.y, { W, H, snap: e.shiftKey }) };
    }
    d.cur = next;
    const it = this.items.find((x) => x.id === d.id);
    if (it) Object.assign(it, pickGeom(next));
    const el = this.els.get(d.id);
    if (el) {
      if (d.mode === 'resize' && k.fitContent) {
        // text: the size handle scales the font; let the app re-render the content at the new size
        this.cb.onChange?.(d.id, { ...next }, { mode: d.mode, scale: d.scale, live: true });
        return;
      }
      this.position(el, next);
    }
    this.cb.onChange?.(d.id, { ...next }, { mode: d.mode, scale: d.scale, live: true });
  }

  up(e, cancelled = false) {
    const d = this.drag;
    if (!d || e.pointerId !== d.pid) return;
    this.drag = null;
    this.layer.classList.remove('is-dragging');
    this.guideX.hidden = true;
    this.guideY.hidden = true;
    this.setZones(false);
    try { this.layer.releasePointerCapture(e.pointerId); } catch { /* already released */ }
    if (d.mode === 'brush') {
      this.live.setAttribute('d', '');
      if (!cancelled && d.points.length) this.cb.onCreate?.({ kind: 'brush', points: simplifyStroke(d.points, { W: this.W, H: this.H }) });
      return;
    }
    if (d.mode === 'draw') {
      this.drawBox.hidden = true;
      const b = d.box;
      if (cancelled || !d.active || !b || b.w * this.W < MIN_PX || b.h * this.H < MIN_PX) {
        if (!d.active && this.selected) this.select(null);
        return;
      }
      this.cb.onCreate?.({ kind: 'box', ...b });
      return;
    }
    if (!d.active) return; // a click: selection only
    if (cancelled) {
      const it = this.items.find((x) => x.id === d.id);
      if (it) Object.assign(it, pickGeom(d.start));
      this.paintAll();
      return;
    }
    this.cb.onCommit?.(d.id, { ...d.cur }, { mode: d.mode, scale: d.scale });
  }

  cancelDrag() {
    if (!this.drag) return;
    this.drag = null;
    this.layer.classList.remove('is-dragging');
    this.drawBox.hidden = true;
    this.live.setAttribute('d', '');
    this.setZones(false);
  }

  paintLive() {
    const d = this.drag;
    const s = this.brushStyle;
    this.live.setAttribute('d', d.points.map(([x, y], i) => `${i ? 'L' : 'M'}${(x * this.W).toFixed(1)} ${(y * this.H).toFixed(1)}`).join(' ') + (d.points.length === 1 ? ' l0.01 0' : ''));
    this.live.setAttribute('fill', 'none');
    this.live.setAttribute('stroke', s.color);
    this.live.setAttribute('stroke-width', String(Math.max(0.5, s.width * this.W)));
    this.live.setAttribute('stroke-linecap', 'round');
    this.live.setAttribute('stroke-linejoin', 'round');
    this.live.setAttribute('opacity', String(s.opacity));
    if (!this.live.isConnected) this.strokes.append(this.live);
  }

  // ── keyboard ─────────────────────────────────────────────────────────────

  key(e) {
    const el = e.target.closest?.('.ov-item');
    if (!el || e.metaKey || e.ctrlKey) return;
    const it = this.items.find((x) => x.id === el.dataset.id);
    if (!it) return;
    const k = this.kindOf(it);
    const step = e.shiftKey ? 10 : 1;
    const arrows = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    let next = null;
    let mode = 'move';
    let scale = 1;
    if (arrows[e.key]) {
      const [dx, dy] = arrows[e.key];
      if (e.altKey && k.resize) {
        mode = 'resize';
        const hnd = dx ? 'e' : 's';
        const r = resizeItem({ ...it, rotation: 0 }, hnd, dx, dy, { W: this.W, H: this.H, keepRatio: k.resize === 'ratio' });
        next = { ...r.item, cx: it.cx, cy: it.cy, rotation: it.rotation };
        if (k.resize !== 'ratio') {
          // grow symmetrically about the centre for keyboard resizes
          next.cx = it.cx;
          next.cy = it.cy;
        }
        scale = r.scale;
      } else if (k.move !== false) {
        next = moveItem(it, dx / this.W, dy / this.H).item;
      }
    } else if ((e.key === '[' || e.key === ']') && k.rotate) {
      mode = 'rotate';
      const s = (e.shiftKey ? 15 : 1) * (e.key === ']' ? 1 : -1);
      next = { ...it, rotation: normAngle((it.rotation || 0) + s) };
    } else if ((e.key === 'Delete' || e.key === 'Backspace') && k.remove !== false) {
      e.preventDefault();
      e.stopPropagation();
      this.cb.onDelete?.(it.id);
      return;
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      this.select(null);
      el.blur();
      return;
    } else if (e.key === 'Enter') {
      e.preventDefault();
      e.stopPropagation();
      this.cb.onActivate?.(it.id);
      return;
    } else {
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    if (!next) return;
    this.layer.classList.remove('pointer-focus');
    Object.assign(it, pickGeom(next));
    this.cb.onCommit?.(it.id, { ...next }, { mode, scale, key: true });
  }
}

function pickGeom(it) {
  return { cx: it.cx, cy: it.cy, w: it.w, h: it.h, rotation: it.rotation };
}
