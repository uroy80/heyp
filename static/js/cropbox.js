// Interactive crop overlay. Works in image pixels of the pre-crop image (W×H) and
// stores fractions; the layer is positioned exactly over the displayed image rect
// (computed by Canvas with object-fit: contain math), so screen → image mapping is exact.

import { h, clamp } from './dom.js';

const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];

/** Resize box `s` (image px) by dragging `hnd` with delta (dx, dy); `r` = locked w/h ratio or null. */
export function resizeBox(s, hnd, dx, dy, r, W, H, min) {
  const hasW = hnd.includes('w');
  const hasE = hnd.includes('e');
  const hasN = hnd.includes('n');
  const hasS = hnd.includes('s');
  let x0 = s.x;
  let y0 = s.y;
  let x1 = s.x + s.w;
  let y1 = s.y + s.h;
  if (!r) {
    if (hasW) x0 = clamp(x0 + dx, 0, x1 - min);
    if (hasE) x1 = clamp(x1 + dx, x0 + min, W);
    if (hasN) y0 = clamp(y0 + dy, 0, y1 - min);
    if (hasS) y1 = clamp(y1 + dy, y0 + min, H);
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  }
  const minW = Math.max(min, min * r);
  if ((hasW || hasE) && (hasN || hasS)) {
    // corner: opposite corner stays put
    const ax = hasW ? x1 : x0;
    const ay = hasN ? y1 : y0;
    const w = hasW ? x1 - (x0 + dx) : x1 + dx - x0;
    const hh = hasN ? y1 - (y0 + dy) : y1 + dy - y0;
    const availX = hasW ? ax : W - ax;
    const availY = hasN ? ay : H - ay;
    const nw = clamp(Math.max(w, hh * r), minW, Math.min(availX, availY * r));
    const nh = nw / r;
    return { x: hasW ? ax - nw : ax, y: hasN ? ay - nh : ay, w: nw, h: nh };
  }
  if (hasW || hasE) {
    // side: keep the vertical centre
    const ax = hasW ? x1 : x0;
    const cy = s.y + s.h / 2;
    const w = hasW ? x1 - (x0 + dx) : x1 + dx - x0;
    const availX = hasW ? ax : W - ax;
    const nw = clamp(w, minW, Math.min(availX, 2 * Math.min(cy, H - cy) * r));
    const nh = nw / r;
    return { x: hasW ? ax - nw : ax, y: cy - nh / 2, w: nw, h: nh };
  }
  const ay = hasN ? y1 : y0;
  const cx = s.x + s.w / 2;
  const hh = hasN ? y1 - (y0 + dy) : y1 + dy - y0;
  const availY = hasN ? ay : H - ay;
  const nh = clamp(hh, minW / r, Math.min(availY, (2 * Math.min(cx, W - cx)) / r));
  const nw = nh * r;
  return { x: cx - nw / 2, y: hasN ? ay - nh : ay, w: nw, h: nh };
}

/** Largest box of ratio r inside W×H, centred on (cx, cy) as far as bounds allow. */
export function fitRatio(r, W, H, cx = W / 2, cy = H / 2) {
  let w = W;
  let hh = W / r;
  if (hh > H) {
    hh = H;
    w = H * r;
  }
  return { x: clamp(cx - w / 2, 0, W - w), y: clamp(cy - hh / 2, 0, H - hh), w, h: hh };
}

/**
 * Snap a px box to whole-pixel edges inside W×H (≥ 1 px). Stored fractions then map back onto exactly
 * these integers on the server (which rounds each edge), so every size label matches the output.
 */
export function snapBox(b, W, H) {
  const x0 = clamp(Math.round(b.x), 0, Math.max(0, W - 1));
  const y0 = clamp(Math.round(b.y), 0, Math.max(0, H - 1));
  const x1 = clamp(Math.round(b.x + b.w), x0 + 1, Math.max(x0 + 1, W));
  const y1 = clamp(Math.round(b.y + b.h), y0 + 1, Math.max(y0 + 1, H));
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

export class CropBox {
  /**
   * @param {HTMLElement} host positioned container (the canvas stage)
   * @param {{onChange?:Function, onCommit?:Function}} cb onChange during drag, onCommit when settled
   */
  constructor(host, { onChange, onCommit } = {}) {
    this.W = 1;
    this.H = 1;
    this.box = { x: 0, y: 0, w: 1, h: 1 }; // fractions
    this.ratio = null; // locked w/h in image px
    this.drag = null;
    this.onChange = onChange;
    this.onCommit = onCommit;

    this.shades = ['t', 'r', 'b', 'l'].map((k) => h('div', { class: `crop-shade crop-shade-${k}` }));
    this.sizeTag = h('span', { class: 'crop-size', 'aria-hidden': 'true' });
    this.frame = h('div', {
      class: 'crop-box', tabindex: '0', role: 'group', 'aria-roledescription': 'crop area',
      'aria-describedby': 'crop-help',
    },
    h('div', { class: 'crop-grid', 'aria-hidden': 'true' }, h('i'), h('i'), h('i'), h('i')),
    HANDLES.map((k) => h('div', { class: `crop-handle crop-handle-${k}`, dataset: { h: k }, 'aria-hidden': 'true' })),
    this.sizeTag);
    this.help = h('p', { id: 'crop-help', class: 'sr-only' }, 'Arrow keys move the crop area, Shift moves 10 pixels, Alt or Option with arrows resizes, Escape reverts.');
    this.layer = h('div', { class: 'crop-layer', hidden: true }, this.shades, this.frame, this.help);
    host.append(this.layer);

    this.layer.addEventListener('pointerdown', (e) => this.#down(e));
    this.layer.addEventListener('pointermove', (e) => this.#move(e));
    this.layer.addEventListener('pointerup', (e) => this.#up(e));
    this.layer.addEventListener('pointercancel', (e) => this.#up(e));
    this.frame.addEventListener('keydown', (e) => this.#key(e));
  }

  get visible() {
    return !this.layer.hidden;
  }

  show(on) {
    this.layer.hidden = !on;
    if (!on) this.drag = null;
  }

  /** Place the layer over the displayed image rect (stage-relative CSS px). */
  place(rect) {
    if (!rect) return;
    this.rect = rect;
    Object.assign(this.layer.style, { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px` });
  }

  /** Pre-crop image size in real pixels (fractions ↔ px). */
  setImageSize(W, H) {
    this.W = Math.max(1, W);
    this.H = Math.max(1, H);
    this.paint();
  }

  setRatio(r) {
    this.ratio = r || null;
  }

  setBox(frac) {
    this.box = frac ? { ...frac } : { x: 0, y: 0, w: 1, h: 1 };
    this.paint();
  }

  /** Snap the current box to ratio r (largest fit around the current centre). */
  applyRatio(r) {
    this.ratio = r || null;
    if (!r) return;
    const p = this.px();
    this.#setPx(fitRatio(r, this.W, this.H, p.x + p.w / 2, p.y + p.h / 2));
    this.onCommit?.(this.box);
  }

  px() {
    const { x, y, w, h: hh } = this.box;
    return { x: x * this.W, y: y * this.H, w: w * this.W, h: hh * this.H };
  }

  #setPx(b) {
    const W = this.W;
    const H = this.H;
    const w = clamp(b.w, 1, W);
    const hh = clamp(b.h, 1, H);
    const s = snapBox({ x: clamp(b.x, 0, W - w), y: clamp(b.y, 0, H - hh), w, h: hh }, W, H);
    this.box = { x: s.x / W, y: s.y / H, w: s.w / W, h: s.h / H };
    this.paint();
  }

  paint() {
    const { x, y, w, h: hh } = this.box;
    const pct = (v) => `${v * 100}%`;
    Object.assign(this.frame.style, { left: pct(x), top: pct(y), width: pct(w), height: pct(hh) });
    const [t, r, b, l] = this.shades;
    Object.assign(t.style, { left: '0', top: '0', width: '100%', height: pct(y) });
    Object.assign(b.style, { left: '0', top: pct(y + hh), width: '100%', height: pct(Math.max(0, 1 - y - hh)) });
    Object.assign(l.style, { left: '0', top: pct(y), width: pct(x), height: pct(hh) });
    Object.assign(r.style, { left: pct(x + w), top: pct(y), width: pct(Math.max(0, 1 - x - w)), height: pct(hh) });
    const p = this.px();
    this.sizeTag.textContent = `${Math.round(p.w)} × ${Math.round(p.h)}`;
    this.frame.setAttribute('aria-label', `Crop area ${Math.round(p.w)} by ${Math.round(p.h)} pixels, at ${Math.round(p.x)}, ${Math.round(p.y)}`);
  }

  #minPx() {
    return Math.min(16, this.W, this.H);
  }

  #down(e) {
    if (e.button !== 0 || !this.rect) return;
    const handle = e.target.closest('[data-h]')?.dataset.h;
    const onBox = e.target.closest('.crop-box');
    const mode = handle || (onBox ? 'move' : 'draw');
    e.preventDefault();
    this.frame.focus({ preventScroll: true });
    const lr = this.layer.getBoundingClientRect();
    this.drag = {
      mode, id: e.pointerId, sx: e.clientX, sy: e.clientY, start: this.px(), before: { ...this.box },
      ax: ((e.clientX - lr.left) / lr.width) * this.W, ay: ((e.clientY - lr.top) / lr.height) * this.H,
    };
    this.layer.setPointerCapture(e.pointerId);
    this.layer.classList.add('is-dragging');
  }

  #move(e) {
    const d = this.drag;
    if (!d || e.pointerId !== d.id) return;
    const lr = this.layer.getBoundingClientRect();
    const kx = this.W / lr.width;
    const ky = this.H / lr.height;
    const dx = (e.clientX - d.sx) * kx;
    const dy = (e.clientY - d.sy) * ky;
    const W = this.W;
    const H = this.H;
    let b;
    if (d.mode === 'move') {
      b = { ...d.start, x: clamp(d.start.x + dx, 0, W - d.start.w), y: clamp(d.start.y + dy, 0, H - d.start.h) };
    } else if (d.mode === 'draw') {
      if (Math.abs(dx) < 3 && Math.abs(dy) < 3) return;
      const ex = clamp(d.ax + dx, 0, W);
      const ey = clamp(d.ay + dy, 0, H);
      // a fresh box anchored at the press point, dragged towards (ex, ey)
      const hnd = `${ey < d.ay ? 'n' : 's'}${ex < d.ax ? 'w' : 'e'}`;
      const seed = { x: d.ax, y: d.ay, w: 0, h: 0 };
      b = resizeBox(seed, hnd, ex - d.ax, ey - d.ay, this.ratio, W, H, this.#minPx());
    } else {
      b = resizeBox(d.start, d.mode, dx, dy, this.ratio, W, H, this.#minPx());
    }
    this.#setPx(b);
    this.onChange?.(this.box);
  }

  #up(e) {
    const d = this.drag;
    if (!d || e.pointerId !== d.id) return;
    this.drag = null;
    this.layer.classList.remove('is-dragging');
    try {
      this.layer.releasePointerCapture(e.pointerId);
    } catch {
      /* already released */
    }
    if (d.mode === 'draw' && Math.abs(e.clientX - d.sx) < 3 && Math.abs(e.clientY - d.sy) < 3) {
      this.setBox(d.before); // a click outside the box is not a crop
      return;
    }
    this.onCommit?.(this.box);
  }

  #key(e) {
    const step = e.shiftKey ? 10 : 1;
    let dx = 0;
    let dy = 0;
    if (e.key === 'ArrowLeft') dx = -step;
    else if (e.key === 'ArrowRight') dx = step;
    else if (e.key === 'ArrowUp') dy = -step;
    else if (e.key === 'ArrowDown') dy = step;
    else return;
    e.preventDefault();
    e.stopPropagation();
    const b = this.px();
    if (e.altKey) {
      let nw = b.w + dx;
      let nh = b.h + dy;
      if (this.ratio) {
        if (dx) nh = nw / this.ratio;
        else nw = nh * this.ratio;
      }
      const min = this.#minPx();
      const s = Math.min(1, (this.W - b.x) / nw, (this.H - b.y) / nh);
      nw *= s;
      nh *= s;
      if (nw < min || nh < min) return;
      this.#setPx({ x: b.x, y: b.y, w: nw, h: nh });
    } else {
      this.#setPx({ ...b, x: b.x + dx, y: b.y + dy });
    }
    this.onChange?.(this.box);
    this.onCommit?.(this.box);
  }
}
