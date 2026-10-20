// Preview canvas: fits the current image into the stage (object-fit: contain math,
// computed explicitly so overlays line up to the pixel), plus loading/compare/badge chrome.

import { h, clamp } from './dom.js';
import { CropBox } from './cropbox.js';
import { imageRetryDelay } from './api.js';

const IMAGE_TRIES = 4;
const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

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

export class Canvas {
  constructor(root, { onCropChange, onCropCommit } = {}) {
    this.root = root;
    this.stage = root.querySelector('.stage');
    this.frame = h('div', { class: 'img-frame', hidden: true });
    this.img = h('img', { class: 'view', alt: '', decoding: 'async', draggable: 'false', hidden: true });
    this.err = h('p', { class: 'canvas-error', hidden: true });
    this.guides = h('div', { class: 'guides', hidden: true, 'aria-hidden': 'true' });
    this.guideKey = '';
    this.stage.append(this.frame, this.img, this.guides, this.err);
    this.crop = new CropBox(this.stage, { onChange: onCropChange, onCommit: onCropCommit });
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

  /** Show `src`; `full` = {w,h} of the real image it represents (caps upscaling of tiny images). */
  async show(src, full, alt = '') {
    const token = ++this.token;
    this.full = full || null;
    this.img.alt = alt;
    if (!src) {
      this.src = null;
      this.nat = null;
      this.img.hidden = true;
      this.frame.hidden = true;
      this.guides.hidden = true;
      this.img.removeAttribute('src');
      return;
    }
    if (src !== this.src) {
      let pre = null;
      // A server preview may be refused while the server is busy (503): wait as asked, then retry.
      for (let attempt = 0; attempt < IMAGE_TRIES && !pre; attempt++) {
        const url = attempt ? `${src}${src.includes('?') ? '&' : '?'}retry=${attempt}` : src;
        const img = new Image();
        img.decoding = 'async';
        img.src = url;
        try {
          await img.decode();
          pre = img;
        } catch {
          if (token !== this.token) return;
          const wait = src.startsWith('/api/') && attempt + 1 < IMAGE_TRIES ? await imageRetryDelay(url) : null;
          if (token !== this.token) return;
          if (wait == null) break;
          await sleep(wait);
          if (token !== this.token) return;
        }
      }
      if (token !== this.token) return;
      if (!pre) {
        this.error('Preview unavailable for this file.');
        return;
      }
      this.nat = { w: pre.naturalWidth, h: pre.naturalHeight };
      this.img.src = pre.src;
      this.src = src;
    }
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
    this.crop.show(false);
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
    const left = Math.round((cw - width) / 2);
    const top = Math.round((ch - height) / 2);
    this.rect = { left, top, width, height };
    const st = { left: `${left}px`, top: `${top}px`, width: `${width}px`, height: `${height}px` };
    Object.assign(this.img.style, st);
    Object.assign(this.frame.style, st);
    Object.assign(this.guides.style, st);
    this.crop.place(this.rect);
  }

  /** Draw passport guides (null hides them). Positions are percentages, so they follow every re-layout. */
  setGuides(pp) {
    const items = guideModel(pp);
    const key = JSON.stringify(items);
    this.guides.hidden = !items.length || this.img.hidden;
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
