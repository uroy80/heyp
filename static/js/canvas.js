// Preview canvas: fits the current image into the stage (object-fit: contain math,
// computed explicitly so overlays line up to the pixel), plus loading/compare/badge chrome.
// Passport "context view": a fixed frame with the whole pre-passport photo around it (dimmed),
// which the user drags / zooms under the frame.

import { h, clamp } from './dom.js';
import { CropBox } from './cropbox.js';
import { imageRetryDelay } from './api.js';

const IMAGE_TRIES = 4;
const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

/**
 * Decode `src` off-screen, retrying while the server only refused it as busy (503 + Retry-After).
 * `alive()` false = the caller moved on. Returns the decoded Image, or null.
 */
async function decodeImage(src, alive) {
  for (let attempt = 0; attempt < IMAGE_TRIES; attempt++) {
    const url = attempt ? `${src}${src.includes('?') ? '&' : '?'}retry=${attempt}` : src;
    const img = new Image();
    img.decoding = 'async';
    img.src = url;
    try {
      await img.decode();
      return img;
    } catch {
      if (!alive()) return null;
      const wait = src.startsWith('/api/') && attempt + 1 < IMAGE_TRIES ? await imageRetryDelay(url) : null;
      if (!alive() || wait == null) return null;
      await sleep(wait);
      if (!alive()) return null;
    }
  }
  return null;
}

/**
 * Passport guides (contract v1.1 C2) as fractions of the passport frame: allowed bands (crown range,
 * chin range from the measured crown + head_min/max, eye range) and the measured crown/eye/chin lines,
 * each line flagged ok when it sits inside its band. `pp` is the preview's `passport` block.
 */
export function guideModel(pp) {
  if (!pp?.height) return [];
  const H = pp.height;
  const b = pp.bands || {};
  const L = pp.face_found === false ? null : pp.lines;
  const f = (y) => clamp(y / H, 0, 1);
  const inside = (v, lo, hi) => v >= lo - 0.002 && v <= hi + 0.002;
  const out = [];
  const top = b.top_min != null && b.top_max != null ? [b.top_min, b.top_max] : null;
  const eye = b.eye_min != null && b.eye_max != null ? [1 - b.eye_max, 1 - b.eye_min] : null;
  const chin = L?.crown != null && b.head_min != null && b.head_max != null
    ? [f(L.crown + b.head_min * H), f(L.crown + b.head_max * H)] : null;
  if (top) out.push({ kind: 'band', name: 'top', from: top[0], to: top[1], label: 'Crown range' });
  if (eye) out.push({ kind: 'band', name: 'eye', from: eye[0], to: eye[1], label: 'Eye range' });
  if (chin) out.push({ kind: 'band', name: 'chin', from: chin[0], to: chin[1], label: 'Chin range' });
  out.push({ kind: 'center' });
  if (L?.crown != null) out.push({ kind: 'line', name: 'crown', at: f(L.crown), label: 'Crown', ok: top ? inside(f(L.crown), top[0], top[1]) : null });
  if (L?.eye != null) out.push({ kind: 'line', name: 'eye', at: f(L.eye), label: 'Eyes', ok: eye ? inside(f(L.eye), eye[0], eye[1]) : null });
  if (L?.chin != null) out.push({ kind: 'line', name: 'chin', at: f(L.chin), label: 'Chin', ok: chin ? inside(f(L.chin), chin[0], chin[1]) : null });
  return out;
}

// ── passport context view: pure geometry ────────────────────────────────────

/** Share of the stage (each direction) the passport frame takes, leaving room for the surroundings while moving. */
export const CONTEXT_FILL = 0.82;
/** How long the surroundings stay up after a move has settled (ms), so they fade rather than blink. */
export const SURROUND_LINGER_MS = 600;

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

/** A usable `passport.source` block: where the frame sits on the image entering the passport op. */
export function isSourceFrame(s) {
  return Boolean(s && finite(s.width) && s.width > 0 && finite(s.height) && s.height > 0
    && Array.isArray(s.center) && finite(s.center[0]) && finite(s.center[1])
    && Array.isArray(s.size) && finite(s.size[0]) && s.size[0] > 0 && finite(s.size[1]) && s.size[1] > 0
    && (s.angle == null || finite(s.angle)));
}

/**
 * The fixed frame rectangle (stage px) of the context view: the output aspect fitted into
 * CONTEXT_FILL of the stage in both directions, centred, never wider than `maxW` (the plain view's size).
 */
export function contextFrame(cw, ch, aspect, maxW = Infinity) {
  const a = aspect > 0 ? aspect : 1;
  const width = Math.max(1, Math.round(Math.min(cw * CONTEXT_FILL, ch * CONTEXT_FILL * a, maxW)));
  const height = Math.max(1, Math.round(width / a));
  return { left: Math.round((cw - width) / 2), top: Math.round((ch - height) / 2), width, height };
}

/**
 * Where the pre-passport photo goes so the frame region of `source` lands exactly on `frame` (stage px).
 * The element is sized width × height (the whole photo at scale k = frame.width / fw) with
 * transform-origin 0 0; `transform` = translate(D) rotate(−angle) translate(−c·k), D = frame centre.
 * `move` is the not-yet-rendered change shown live: the photo shifted by (dx, dy) stage px and scaled by r
 * about the stage point (px, py), i.e. P' = (dx, dy) + p + r·(P − p).
 * Returns {width, height, scale: k, matrix: [a, b, c, d, e, f], transform} (CSS matrix order).
 */
export function contextPlacement(source, frame, move = null) {
  const [cx, cy] = source.center;
  const k = frame.width / source.size[0];
  const rad = (-(source.angle || 0) * Math.PI) / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  const Dx = frame.left + frame.width / 2;
  const Dy = frame.top + frame.height / 2;
  // P = D + R(−angle)·(e − c·k), e = element px; R(θ) = [[cos θ, −sin θ], [sin θ, cos θ]] (CSS rotate)
  let m = [cos, sin, -sin, cos, Dx - (cos * cx - sin * cy) * k, Dy - (sin * cx + cos * cy) * k];
  const r = move?.r > 0 ? move.r : 1;
  const px = move?.px ?? Dx;
  const py = move?.py ?? Dy;
  const tx = (move?.dx || 0) + px * (1 - r);
  const ty = (move?.dy || 0) + py * (1 - r);
  if (r !== 1 || tx || ty) m = [m[0] * r, m[1] * r, m[2] * r, m[3] * r, m[4] * r + tx, m[5] * r + ty];
  const n = (v, d) => String(Number(v.toFixed(d)));
  return {
    width: source.width * k,
    height: source.height * k,
    scale: k,
    matrix: m,
    transform: `matrix(${n(m[0], 6)}, ${n(m[1], 6)}, ${n(m[2], 6)}, ${n(m[3], 6)}, ${n(m[4], 3)}, ${n(m[5], 3)})`,
  };
}

/** passport.py MIN_TOP_MARGIN: share of the height kept above the hair when the eye line is lowered for it. */
const MIN_TOP_MARGIN = 0.04;
/** passport.py _mid: mean of the bounds that are set, or null. */
const mid = (a, b) => {
  const v = [a, b].filter((x) => x != null);
  return v.length ? v.reduce((s, x) => s + x, 0) / v.length : null;
};

/**
 * How the server re-frames when only the head size changes by a ratio r (PLAN §5.3 steps 2–4), so live
 * zoom feedback lands where the next preview will: the photo scales by r about the frame point (fx, fy)
 * (fractions), then moves down by zoomShift(pivot, r) frame heights.
 * - Horizontally the eye midpoint stays at centre + offset_x.
 * - Vertically, with an eye range the eye line stays, but it is lowered (at most to eye_min) when the hair
 *   would come closer than the top margin (`eye` model); with a crown range the crown stays; otherwise the
 *   free space is shared 45/55 above the hair and below the chin (`hair` model), which moves the hair top.
 * - Without a face the crop is centred on the photo: its centre stays at frame centre + offsets.
 * `offsetX`/`offsetY` = the offsets the preview was made with. The hair top is `pp.hair_top` (the crown line
 * when the server doesn't report it).
 */
export function zoomPivot(pp, offsetX = 0, offsetY = 0) {
  const ox = offsetX || 0;
  const H = pp?.height;
  const L = pp?.lines;
  if (!H || !pp) return { fx: 0.5, fy: 0.5 };
  if (pp.face_found === false) return { fx: 0.5 + ox, fy: 0.5 + (offsetY || 0) };
  if (!L) return { fx: 0.5 + ox, fy: 0.5 };
  const b = pp.bands || {};
  const hair = finite(pp.hair_top) ? pp.hair_top : L.crown;
  const fx = 0.5 + ox;
  const eyeMid = mid(b.eye_min, b.eye_max);
  if (eyeMid != null && finite(L.eye)) {
    const pivot = { fx, fy: L.eye / H };
    if (finite(hair) && b.eye_min != null) {
      pivot.eye = { cae: (L.eye - hair) / H, mid: 1 - eyeMid, minTop: b.top_min ?? MIN_TOP_MARGIN, lowest: 1 - b.eye_min };
    }
    return pivot;
  }
  if (mid(b.top_min, b.top_max) != null && finite(L.crown)) return { fx, fy: L.crown / H };
  if (finite(hair) && finite(L.chin) && finite(L.crown)) {
    return { fx, fy: hair / H, hair: { vh: (L.chin - hair) / H, th: (L.chin - L.crown) / H } };
  }
  return { fx, fy: 0.5 };
}

/** The vertical move (frame heights, + = down) that comes with scaling the head by `r` about `pivot` (zoomPivot). */
export function zoomShift(pivot, r) {
  if (!pivot || !(r > 0) || r === 1) return 0;
  const e = pivot.eye;
  if (e) {
    // passport.py: eye_out − crown_above_eye < min_top → eye_out = min(1 − eye_min, min_top + crown_above_eye)
    const eyeAt = (c) => (e.mid - c < e.minTop ? Math.min(e.lowest, e.minTop + c) : e.mid);
    return eyeAt(e.cae * r) - eyeAt(e.cae);
  }
  const hh = pivot.hair;
  if (hh) {
    // passport.py: margin above the hair = min((1 − vh)·0.45, 1 − vh − 0.275·max(0, 1 − head))
    const top = (q) => Math.min(0.45 * (1 - hh.vh * q), 1 - hh.vh * q - 0.275 * Math.max(0, 1 - hh.th * q));
    return top(r) - top(1);
  }
  return 0;
}

/**
 * The measured crown/eye/chin lines of `pp` after the photo moved by `dy` output px and scaled by `r`
 * about output row `py` (live guides while dragging or zooming; the bands stay with the frame).
 */
export function movedGuides(pp, { dy = 0, r = 1, py = 0 } = {}) {
  const L = pp?.lines;
  if (!L || (!dy && r === 1)) return pp;
  const f = (y) => (y == null ? y : py + (y - py) * r + dy);
  return { ...pp, lines: { ...L, crown: f(L.crown), eye: f(L.eye), chin: f(L.chin) } };
}

const isMoved = (m) => Math.abs(m.dx) > 0.25 || Math.abs(m.dy) > 0.25 || Math.abs(m.r - 1) > 5e-4;

export class Canvas {
  constructor(root, { onCropChange, onCropCommit } = {}) {
    this.root = root;
    this.stage = root.querySelector('.stage');
    // context view (passport): the photo entering the passport op, laid out under the fixed frame
    this.ctxImg = h('img', { class: 'pp-context', alt: '', decoding: 'async', draggable: 'false', hidden: true, 'aria-hidden': 'true' });
    this.frame = h('div', { class: 'img-frame', hidden: true });
    this.img = h('img', { class: 'view', alt: '', decoding: 'async', draggable: 'false', hidden: true });
    this.err = h('p', { class: 'canvas-error', hidden: true });
    this.guides = h('div', { class: 'guides', hidden: true, 'aria-hidden': 'true' });
    this.guideKey = '';
    this.guidePp = null;
    this.stage.append(this.ctxImg, this.frame, this.img, this.guides, this.err);
    this.crop = new CropBox(this.stage, { onChange: onCropChange, onCommit: onCropCommit });
    this.pan = null;
    this.panDrag = null;
    this.panShown = { dx: 0, dy: 0 };
    this.panTimer = 0;
    this.pointers = new Map(); // pointerId → {x, y} while down on the pannable stage
    this.pinch = null;
    this.ctx = null; // {src, source, out:{w,h}, pending:{fx,fy,r}, pivot: zoomPivot(…)} from the app
    this.ctxLoaded = null; // src decoded into ctxImg
    this.ctxWant = null;
    this.ctxFailed = null;
    this.ctxToken = 0;
    this.stage.addEventListener('pointerdown', (e) => this.panDown(e));
    this.stage.addEventListener('pointermove', (e) => this.panMove(e));
    this.stage.addEventListener('pointerup', (e) => this.panUp(e));
    this.stage.addEventListener('pointercancel', (e) => this.panUp(e, true));
    this.stage.addEventListener('wheel', (e) => this.panWheel(e), { passive: false });
    this.stage.addEventListener('dblclick', (e) => { if (this.pan && this.inImage(e)) this.pan.onReset(); });
    this.stage.addEventListener('keydown', (e) => this.panKey(e));
    this.stage.addEventListener('blur', () => this.stage.classList.remove('pointer-focus'));
    this.loadbar = root.querySelector('.loadbar');
    this.badge = root.querySelector('.canvas-badge');
    this.src = null;
    this.nat = null;
    this.full = null;
    this.token = 0;
    this.rect = null;
    this.loadTimer = 0;
    new ResizeObserver(() => this.layout()).observe(this.stage);
  }

  /**
   * Show `src`; `full` = {w,h} of the real image it represents (caps upscaling of tiny images).
   * `context` (passport) lays the pre-passport photo around a fixed frame; it applies together with the
   * image it belongs to, so a re-framed preview and its new placement appear in the same frame.
   */
  async show(src, full, alt = '', { context = null } = {}) {
    const token = ++this.token;
    this.full = full || null;
    this.img.alt = alt;
    if (context?.src) this.loadContext(context.src);
    if (!src) {
      this.src = null;
      this.nat = null;
      this.ctx = null;
      this.img.hidden = true;
      this.frame.hidden = true;
      this.guides.hidden = true;
      this.img.removeAttribute('src');
      this.hideContext();
      return;
    }
    if (src !== this.src) {
      const pre = await decodeImage(src, () => token === this.token);
      if (token !== this.token) return;
      if (!pre) {
        this.error('Preview unavailable for this file.');
        return;
      }
      this.nat = { w: pre.naturalWidth, h: pre.naturalHeight };
      this.img.src = pre.src;
      this.src = src;
      if (!this.panDrag) this.resetPan(); // the re-framed preview replaces the nudged one
    }
    this.ctx = context;
    // the context's pending move now includes any committed drag: drop the drag offset
    if (this.ctxOn && !this.panDrag && !this.pinch) this.resetPan();
    this.err.hidden = true;
    this.img.hidden = false;
    this.frame.hidden = false;
    this.layout();
  }

  error(msg) {
    this.err.textContent = msg;
    this.err.hidden = false;
    this.img.hidden = true;
    this.frame.hidden = true;
    this.guides.hidden = true;
    this.ctx = null;
    this.hideContext();
    this.crop.show(false);
  }

  /** The context view is on: the app asked for it and its photo is decoded. */
  get ctxOn() {
    return Boolean(this.ctx?.src && this.ctx.src === this.ctxLoaded && isSourceFrame(this.ctx.source));
  }

  /** Decode the context photo off-screen; the view switches on once it is ready (never half-loaded). */
  loadContext(src) {
    if (src === this.ctxLoaded && this.ctxWant) {
      // back to the photo already shown: drop the load that would replace it
      this.ctxWant = null;
      this.ctxToken++;
    }
    if (src === this.ctxLoaded || src === this.ctxWant || src === this.ctxFailed) return;
    this.ctxWant = src;
    const token = ++this.ctxToken;
    decodeImage(src, () => token === this.ctxToken).then((pre) => {
      if (token !== this.ctxToken) return;
      this.ctxWant = null;
      if (!pre) {
        this.ctxFailed = src; // the plain view stays
        return;
      }
      this.ctxImg.src = pre.src;
      this.ctxLoaded = src;
      // switching from the plain view: the context's pending move already holds a committed nudge
      if (this.ctxOn && !this.panDrag && !this.pinch) this.resetPan();
      this.layout();
    });
  }

  hideContext() {
    this.ctxImg.hidden = true;
    this.setSurround(false, true);
    this.stage.classList.remove('is-context', 'is-moved');
  }

  layout() {
    if (!this.nat) return;
    const { w: nw, h: nh } = this.nat;
    const cw = this.stage.clientWidth;
    const ch = this.stage.clientHeight;
    if (!nw || !nh || !cw || !ch) return;
    const long = Math.max(nw, nh);
    const fullLong = this.full ? Math.max(this.full.w, this.full.h) : long;
    const cap = Math.max(fullLong / long, 320 / long); // don't blow tiny images up to the whole canvas
    const s = Math.min(cw / nw, ch / nh, cap);
    const width = Math.round(nw * s);
    const height = Math.round(nh * s);
    if (this.ctxOn) {
      const o = this.ctx.out;
      this.rect = contextFrame(cw, ch, o?.w > 0 && o?.h > 0 ? o.w / o.h : nw / nh, width);
    } else {
      this.rect = { left: Math.round((cw - width) / 2), top: Math.round((ch - height) / 2), width, height };
    }
    const { left, top } = this.rect;
    const st = { left: `${left}px`, top: `${top}px`, width: `${this.rect.width}px`, height: `${this.rect.height}px` };
    Object.assign(this.img.style, st);
    Object.assign(this.frame.style, st);
    Object.assign(this.guides.style, st);
    this.crop.place(this.rect);
    this.applyPan();
  }

  /** Draw passport guides (null hides them). Positions are percentages, so they follow every re-layout. */
  setGuides(pp) {
    this.guidePp = pp || null;
    this.drawGuides();
  }

  drawGuides() {
    let pp = this.guidePp;
    if (pp && this.ctxOn && this.rect && pp.height) {
      // context view: the bands stay with the frame, the measured lines ride along with the photo
      const m = this.liveMove();
      const H = this.rect.height;
      pp = movedGuides(pp, { dy: (m.dy / H) * pp.height, r: m.r, py: ((m.py - this.rect.top) / H) * pp.height });
    }
    const items = guideModel(pp);
    const key = JSON.stringify(items);
    this.guides.hidden = !items.length || this.img.hidden || (!this.ctxOn && Boolean(this.panShown.dx || this.panShown.dy));
    if (key === this.guideKey) return;
    this.guideKey = key;
    const pct = (v) => `${(v * 100).toFixed(3)}%`;
    this.guides.replaceChildren(...items.map((g) => {
      if (g.kind === 'center') return h('div', { class: 'guide-center' });
      if (g.kind === 'band') {
        return h('div', { class: `guide-band guide-band-${g.name}`, style: { top: pct(g.from), height: pct(Math.max(0, g.to - g.from)) } },
          h('span', { class: 'guide-label' }, g.label));
      }
      const state = g.ok == null ? '' : g.ok ? ' is-ok' : ' is-off';
      return h('div', { class: `guide-line guide-line-${g.name}${state}`, style: { top: pct(g.at) } }, h('span', { class: 'guide-label' }, g.label));
    }));
  }

  /**
   * Drag-to-align (passport framing). `opts` = {onCommit(fx, fy), onZoom(step), onReset()} or null to turn
   * it off, plus optional clampMove(fx, fy) → [fx, fy], onZoomBy(ratio) and clampScale(ratio) → ratio (pinch).
   * fx/fy are movements as fractions of the shown frame's width/height (positive = right/down).
   */
  setPan(opts) {
    this.pan = opts || null;
    this.stage.classList.toggle('is-pannable', Boolean(opts));
    if (opts) {
      this.stage.tabIndex = 0;
      this.stage.setAttribute('aria-label', 'Photo position. Drag or use the arrow keys to move the photo under the frame; scroll, pinch or press + and − to resize the head; 0 resets.');
      return;
    }
    this.stage.removeAttribute('tabindex');
    this.stage.removeAttribute('aria-label');
    this.panDrag = null;
    this.pinch = null;
    this.pointers.clear();
    this.stage.classList.remove('is-panning');
    this.setPanOffset(0, 0);
  }

  inImage(e) {
    if (!this.rect || this.img.hidden) return false;
    if (this.ctxOn) return true; // the whole photo around the frame can be grabbed
    const b = this.stage.getBoundingClientRect();
    const x = e.clientX - b.left - this.rect.left;
    const y = e.clientY - b.top - this.rect.top;
    return x >= 0 && y >= 0 && x <= this.rect.width && y <= this.rect.height;
  }

  resetPan() {
    clearTimeout(this.panTimer);
    this.panShown = { dx: 0, dy: 0 };
  }

  /** Surroundings on/off: on at once, off after a short linger (`now` skips it). */
  setSurround(on, now = false) {
    clearTimeout(this.surroundTimer);
    if (on) {
      this.stage.classList.add('is-surround');
    } else if (now) {
      this.stage.classList.remove('is-surround');
    } else if (this.stage.classList.contains('is-surround')) {
      this.surroundTimer = setTimeout(() => this.stage.classList.remove('is-surround'), SURROUND_LINGER_MS);
    }
  }

  /** Move the shown photo by (dx, dy) stage px until the re-framed preview arrives (CSS only). */
  setPanOffset(dx, dy) {
    clearTimeout(this.panTimer);
    this.panShown = { dx, dy };
    this.applyPan();
  }

  /** The move the context view shows on top of the rendered framing: pending edits + the live gesture. */
  liveMove() {
    const f = this.rect;
    const pend = this.ctx?.pending || {};
    const pivot = this.ctx?.pivot || {};
    const r = (pend.r > 0 ? pend.r : 1) * (this.pinch?.ratio || 1);
    return {
      dx: (pend.fx || 0) * f.width + this.panShown.dx,
      // a head-size change may also re-place the head vertically (eye line lowered, hair margin re-shared)
      dy: ((pend.fy || 0) + zoomShift(pivot, r)) * f.height + this.panShown.dy,
      r,
      px: f.left + (pivot.fx ?? 0.5) * f.width,
      py: f.top + (pivot.fy ?? 0.5) * f.height,
    };
  }

  applyPan() {
    if (this.ctxOn && this.rect) {
      // context view: the frame stays, the photo under it moves; the finished preview fades out while
      // it no longer matches (CSS), so the raw photo shows through the frame
      this.img.style.transform = '';
      this.img.style.clipPath = '';
      const m = this.liveMove();
      const pl = contextPlacement(this.ctx.source, this.rect, m);
      Object.assign(this.ctxImg.style, { width: `${pl.width}px`, height: `${pl.height}px`, transform: pl.transform });
      this.ctxImg.hidden = false;
      this.stage.classList.add('is-context');
      this.stage.classList.toggle('is-moved', isMoved(m));
      // the surroundings show only while the photo is being moved (and until the re-framed preview lands)
      this.setSurround(Boolean(this.panDrag || this.pinch) || isMoved(m));
      this.drawGuides();
      return;
    }
    this.hideContext();
    // plain view: slide the shown preview inside its frame
    const { dx, dy } = this.panShown;
    const moved = Boolean(dx || dy);
    this.img.style.transform = moved ? `translate(${dx}px, ${dy}px)` : '';
    this.img.style.clipPath = moved
      ? `inset(${Math.max(0, -dy)}px ${Math.max(0, dx)}px ${Math.max(0, dy)}px ${Math.max(0, -dx)}px)` : '';
    if (moved) this.guides.hidden = true; // guides describe the old framing
  }

  panDown(e) {
    if (!this.pan || e.button !== 0 || !this.inImage(e)) return;
    e.preventDefault();
    this.stage.classList.add('pointer-focus'); // focus for the arrow keys, but no keyboard ring after a mouse drag
    this.stage.focus({ preventScroll: true });
    try { this.stage.setPointerCapture(e.pointerId); } catch { /* pointer already gone: drag still works inside the stage */ }
    this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (this.ctxOn) this.setSurround(true);
    if (this.pointers.size === 2 && e.pointerType === 'touch' && !this.pinch) {
      // a second finger turns the gesture into a pinch (head size); the one-finger move is dropped
      const d = this.panDrag;
      this.panDrag = null;
      if (d) this.setPanOffset(d.base.dx, d.base.dy);
      const ids = [...this.pointers.keys()];
      const [a, b] = ids.map((id) => this.pointers.get(id));
      this.pinch = { ids, d0: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y)), ratio: 1 };
      this.stage.classList.add('is-panning');
      return;
    }
    if (this.pinch || this.pointers.size > 1) return;
    const base = this.panShown; // a previous drag may still be waiting for its preview
    this.panDrag = { id: e.pointerId, x: e.clientX - base.dx, y: e.clientY - base.dy, base, dx: base.dx, dy: base.dy };
    this.stage.classList.add('is-panning');
  }

  panMove(e) {
    if (this.pointers.has(e.pointerId)) this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const p = this.pinch;
    if (p) {
      if (!p.ids.includes(e.pointerId)) return;
      const [a, b] = p.ids.map((id) => this.pointers.get(id));
      if (!a || !b) return;
      const raw = Math.hypot(a.x - b.x, a.y - b.y) / p.d0;
      p.ratio = this.pan?.clampScale ? this.pan.clampScale(raw) : raw;
      this.applyPan();
      return;
    }
    const d = this.panDrag;
    if (!d || e.pointerId !== d.id) return;
    let dx = e.clientX - d.x;
    let dy = e.clientY - d.y;
    if (this.pan?.clampMove && this.rect) {
      // stop at the position limits instead of snapping back on release
      const fx0 = (dx - d.base.dx) / this.rect.width;
      const fy0 = (dy - d.base.dy) / this.rect.height;
      const [fx, fy] = this.pan.clampMove(fx0, fy0);
      if (Math.abs(fx - fx0) > 1e-9) dx = d.base.dx + fx * this.rect.width;
      if (Math.abs(fy - fy0) > 1e-9) dy = d.base.dy + fy * this.rect.height;
    }
    d.dx = dx;
    d.dy = dy;
    this.setPanOffset(d.dx, d.dy);
  }

  panUp(e, cancelled = false) {
    this.pointers.delete(e.pointerId);
    const p = this.pinch;
    if (p) {
      if (!p.ids.includes(e.pointerId)) return;
      this.pinch = null;
      this.stage.classList.remove('is-panning');
      if (!cancelled && Math.abs(p.ratio - 1) > 0.004) this.pan?.onZoomBy?.(p.ratio);
      this.applyPan();
      return;
    }
    const d = this.panDrag;
    if (!d || e.pointerId !== d.id) return;
    this.panDrag = null;
    this.stage.classList.remove('is-panning');
    const mx = d.dx - d.base.dx;
    const my = d.dy - d.base.dy;
    if (cancelled || !this.rect || (Math.abs(mx) < 3 && Math.abs(my) < 3)) {
      this.setPanOffset(d.base.dx, d.base.dy);
      return;
    }
    // keep the moved photo until show() brings the committed framing (context view: synchronously, as a
    // pending move; plain view: the re-framed preview); give up if none comes
    this.panTimer = setTimeout(() => this.setPanOffset(0, 0), 8000);
    this.pan?.onCommit(mx / this.rect.width, my / this.rect.height);
  }

  panWheel(e) {
    if (!this.pan || !this.inImage(e)) return;
    e.preventDefault();
    const step = clamp(-e.deltaY * (e.ctrlKey ? 0.005 : 0.0002), -0.03, 0.03); // ctrlKey = trackpad pinch
    if (step) this.pan.onZoom(step);
  }

  panKey(e) {
    if (!this.pan || e.target !== this.stage || e.metaKey || e.ctrlKey || e.altKey) return;
    this.stage.classList.remove('pointer-focus');
    const s = e.shiftKey ? 0.02 : 0.005;
    const moves = { ArrowLeft: [-s, 0], ArrowRight: [s, 0], ArrowUp: [0, -s], ArrowDown: [0, s] };
    if (moves[e.key]) this.pan.onCommit(...moves[e.key]);
    else if (e.key === '+' || e.key === '=') this.pan.onZoom(0.01);
    else if (e.key === '-' || e.key === '_') this.pan.onZoom(-0.01);
    else if (e.key === '0') this.pan.onReset();
    else return;
    e.preventDefault();
  }

  /** Thin progress bar; delayed so fast responses don't flicker. */
  loading(on) {
    clearTimeout(this.loadTimer);
    if (on) this.loadTimer = setTimeout(() => this.loadbar.classList.add('is-on'), 150);
    else this.loadbar.classList.remove('is-on');
    this.root.setAttribute('aria-busy', String(Boolean(on)));
  }

  setBadge(text) {
    this.badge.textContent = text || '';
    this.badge.hidden = !text;
  }
}
