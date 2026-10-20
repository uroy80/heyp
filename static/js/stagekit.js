// What the editing layer draws for each tool, and the geometry behind it: detector boxes → blur regions,
// watermark placement on the 3×3 grid, meme caption layout, text boxes, the upscale loupe square.
// The live drawing is an instant approximation; the server's render replaces it once it arrives.

import { h, clamp } from './dom.js';
import { fontCss } from './catalog.js';
import { zoneOf } from './catalog.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const round = (v, d = 5) => Math.round(v * 10 ** d) / 10 ** d;

// ── text measurement ───────────────────────────────────────────────────────

let ctx2d = null;
/**
 * Width/height (px) of `text` (newlines = lines) set in font `fontId` at `px`. Uses a canvas when there is one,
 * else a 0.55 em per character estimate (node tests).
 */
export function measureText(text, fontId, px, { uppercase = false, weight = 400, lineHeight = 1.15 } = {}) {
  const lines = String(text ?? '').split('\n').map((l) => (uppercase ? l.toUpperCase() : l));
  let w = 0;
  if (typeof document !== 'undefined') {
    ctx2d ||= document.createElement('canvas').getContext('2d');
    ctx2d.font = `${weight} ${px}px ${fontCss(fontId)}`;
    for (const l of lines) w = Math.max(w, ctx2d.measureText(l).width);
  } else {
    for (const l of lines) w = Math.max(w, l.length * px * 0.55);
  }
  return { w, h: lines.length * px * lineHeight, lines };
}

// ── meme ───────────────────────────────────────────────────────────────────

/** Meme caption metrics (app/imaging/meme.py): width share, edge, auto-size range, wrapping, outline ratio. */
export const MEME = { width: 0.94, edge: 0.025, max: 0.14, min: 0.035, wrapBelow: 0.06, maxShare: 0.3, maxLines: 3, lineSpacing: 1.08, stroke: 0.08 };

/** Greedy word wrap of `text` (explicit newlines kept) at `px` into lines no wider than `maxW`. */
export function wrapWords(text, font, px, maxW, measure = measureText, { uppercase = false } = {}) {
  const out = [];
  for (const para of String(text).split('\n')) {
    let line = '';
    for (const word of para.split(/\s+/).filter(Boolean)) {
      const next = line ? `${line} ${word}` : word;
      if (!line || measure(next, font, px, { uppercase }).w <= maxW) line = next;
      else {
        out.push(line);
        line = word;
      }
    }
    out.push(line);
  }
  return out.map((l) => (uppercase ? l.toUpperCase() : l));
}

/**
 * Caption blocks for the meme op on a W×H picture, mirroring the server's sizing (app/imaging/meme.py): auto
 * size fits the widest line into 94 % of the width (outline included) and the block into 30 % of the height,
 * between 3.5 % and 14 % of the height; long captions that would drop below 6 % wrap instead (≤ 3 lines while
 * above the 3.5 % floor). A caption that can't fit even at the floor is set smaller — never wider than the
 * width or taller than 30 % of the height.
 * → {top, bottom}: {cx, cy, w, h, px, lines, outline} (fractions + px).
 */
export function memeLayout(m, W, H, measure = measureText) {
  const out = {};
  const M = MEME;
  const margin = M.edge * H;
  const width = M.width * W;
  const strokeK = m.outline && m.outline !== 'none' ? M.stroke : 0;
  for (const key of ['top', 'bottom']) {
    const text = String(m[key] || '').trim();
    if (!text) continue;
    const opts = { uppercase: m.uppercase, lineHeight: M.lineSpacing };
    let px;
    let lines;
    if (m.autoSize) {
      const hi = M.max * H;
      const lo = Math.max(4, M.min * H);
      const maxH = M.maxShare * H;
      const ref = measure(text, m.font, 100, opts);
      const fitW = ref.w > 0 ? (100 * width) / ref.w / (1 + 2 * strokeK) : hi;
      const fitH = maxH / (ref.lines.length * M.lineSpacing);
      const one = Math.min(hi, fitW, fitH); // every explicit line on one line
      px = Math.max(4, one);
      lines = ref.lines;
      if (text.length >= 12 && (one < M.wrapBelow * H || one < lo)) {
        const wrapAt = (s) => wrapWords(text, m.font, s, width / (1 + 2 * strokeK), measure, { uppercase: m.uppercase });
        let s = hi;
        let ls = wrapAt(s);
        for (; s > 4; s = Math.max(4, s * 0.92)) {
          ls = wrapAt(s);
          const widest = Math.max(...ls.map((l) => measure(l, m.font, s, opts).w)) * (1 + 2 * strokeK);
          if (widest <= width && ls.length * s * M.lineSpacing <= maxH && (ls.length <= M.maxLines || s <= lo)) break;
        }
        if (!(one >= lo && one >= s)) {
          px = s;
          lines = ls;
        }
      }
    } else {
      px = Math.max(4, (m.size || 0.1) * H);
      lines = wrapWords(text, m.font, px, width / (1 + 2 * strokeK), measure, { uppercase: m.uppercase });
    }
    const wPx = Math.max(...lines.map((l) => measure(l, m.font, px, opts).w)) * (1 + 2 * strokeK);
    const hh = Math.min(lines.length * px * M.lineSpacing, H / 2 - margin);
    const cy = key === 'top' ? margin + hh / 2 : H - margin - hh / 2;
    out[key] = { cx: 0.5, cy: round(cy / H), w: round(Math.min(1, wPx / W)), h: round(hh / H), px, lines, outline: Math.max(1, px * strokeK) };
  }
  return out;
}

// ── watermark ──────────────────────────────────────────────────────────────

/** Text watermarks are set at this weight (app/imaging/watermark.py TEXT_WEIGHT). */
export const WM_TEXT_WEIGHT = 600;

/**
 * Where the watermark sits on a W×H picture (app/imaging/watermark.py): text size is a share of the SHORTER
 * side (set at weight 600), the margin a share of the shorter side, a logo's width a share of the width.
 * → {cx, cy, w, h, px} (fractions, px = font px for text).
 */
export function watermarkGeom(wm, W, H, { measure = measureText, logoAspect = 1 } = {}) {
  const short = Math.min(W, H);
  let w;
  let hh;
  let px = 0;
  if (wm.kind === 'image') {
    w = clamp(wm.imageScale, 0.01, 1) * W;
    hh = w / (logoAspect > 0 ? logoAspect : 1);
  } else {
    px = Math.max(4, wm.size * short);
    const box = measure(wm.text || ' ', wm.font, px, { weight: WM_TEXT_WEIGHT });
    w = box.w + px * 0.2;
    hh = box.h;
  }
  const m = wm.margin * short;
  const { row, col } = zoneOf(wm.position);
  const cx = col === 'left' ? m + w / 2 : col === 'right' ? W - m - w / 2 : W / 2;
  const cy = row === 'top' ? m + hh / 2 : row === 'bottom' ? H - m - hh / 2 : H / 2;
  return { cx: round(cx / W), cy: round(cy / H), w: round(w / W), h: round(hh / H), px };
}

// ── detections → blur regions ──────────────────────────────────────────────

/** Intersection over union of two centre boxes. */
export function iou(a, b) {
  const ax0 = a.cx - a.w / 2; const ax1 = a.cx + a.w / 2; const ay0 = a.cy - a.h / 2; const ay1 = a.cy + a.h / 2;
  const bx0 = b.cx - b.w / 2; const bx1 = b.cx + b.w / 2; const by0 = b.cy - b.h / 2; const by1 = b.cy + b.h / 2;
  const iw = Math.max(0, Math.min(ax1, bx1) - Math.max(ax0, bx0));
  const ih = Math.max(0, Math.min(ay1, by1) - Math.max(ay0, by0));
  const inter = iw * ih;
  const uni = a.w * a.h + b.w * b.h - inter;
  return uni > 0 ? inter / uni : 0;
}

const KIND_OF = { face: 'face', faces: 'face', plate: 'plate', plates: 'plate', license_plate: 'plate', licence_plate: 'plate' };
const PAD = { face: 0.12, plate: 0.08 };

/**
 * GET /api/detect/{id} → regions {source, cx, cy, w, h, score} in fractions of the oriented image.
 * Accepts {faces: [...], plates: [...]}, {boxes|detections|regions: [...]} or a bare list; each box as
 * {box: [x, y, w, h]}, {x, y, w, h} or {x1, y1, x2, y2}, in pixels (of `width`×`height`, or the file's size)
 * or fractions (every value ≤ 1). Faces get 12 % and plates 8 % extra on each side so edges are covered.
 */
export function normalizeDetections(res, dims) {
  // heyp's /api/detect answers {file_id, width, height, regions: [{kind, x, y, w, h, shape, score, selected}]}
  // with boxes already grown to cover the whole face / plate: no extra padding then
  const grown = Boolean(res && !Array.isArray(res) && (res.file_id || res.level));
  const W = Number(res?.width) || dims?.w || 0;
  const H = Number(res?.height) || dims?.h || 0;
  const raw = [];
  const take = (list, kind) => { for (const d of Array.isArray(list) ? list : []) raw.push({ d, kind }); };
  if (Array.isArray(res)) take(res, null);
  else if (res && typeof res === 'object') {
    take(res.faces, 'face');
    take(res.plates, 'plate');
    take(res.boxes, null);
    take(res.detections, null);
    take(res.regions, null);
  }
  const out = [];
  for (const { d, kind } of raw) {
    if (!d || typeof d !== 'object') continue;
    let x; let y; let w; let hh;
    if (Array.isArray(d.box) && d.box.length >= 4) [x, y, w, hh] = d.box.map(Number);
    else if (Array.isArray(d.bbox) && d.bbox.length >= 4) [x, y, w, hh] = d.bbox.map(Number);
    else if ([d.x1, d.y1, d.x2, d.y2].every((v) => v != null)) { x = +d.x1; y = +d.y1; w = d.x2 - d.x1; hh = d.y2 - d.y1; }
    else { x = +d.x; y = +d.y; w = +(d.w ?? d.width); hh = +(d.h ?? d.height); }
    if (![x, y, w, hh].every(Number.isFinite) || w <= 0 || hh <= 0) continue;
    const frac = d.unit === 'fraction' || (d.unit !== 'px' && [x, y, w, hh].every((v) => v >= 0 && v <= 1));
    if (!frac) {
      if (!(W > 0 && H > 0)) continue;
      x /= W; w /= W; y /= H; hh /= H;
    }
    const source = KIND_OF[String(kind || d.kind || d.label || d.type || d.class || '').toLowerCase()] || 'face';
    const pad = grown ? 0 : PAD[source] || 0;
    const x0 = clamp(x - w * pad, 0, 1);
    const y0 = clamp(y - hh * pad, 0, 1);
    const x1 = clamp(x + w * (1 + pad), 0, 1);
    const y1 = clamp(y + hh * (1 + pad), 0, 1);
    if (x1 - x0 <= 0 || y1 - y0 <= 0) continue;
    const score = Number(d.score ?? d.confidence ?? d.conf);
    out.push({
      source, cx: round((x0 + x1) / 2), cy: round((y0 + y1) / 2), w: round(x1 - x0), h: round(y1 - y0),
      score: Number.isFinite(score) ? score : null, on: d.selected !== false, shape: d.shape === 'ellipse' ? 'ellipse' : 'rect',
    });
  }
  return out;
}

/** Add `found` regions to `existing`, skipping ones that overlap a box already there (IoU > 0.5). */
export function mergeRegions(existing, found, makeId, max = 64) {
  const list = [...(existing || [])];
  let added = 0;
  for (const r of found) {
    if (list.length >= max) break;
    if (list.some((e) => iou(e, r) > 0.5)) continue;
    list.push({ id: makeId(), on: true, ...r });
    added++;
  }
  return { list, added };
}

// ── upscale loupe ──────────────────────────────────────────────────────────

/** The detail square (fractions) around `center` on a W×H image: 18 % of the short side, 48–200 px, inside. */
export function loupeRegion(center, W, H) {
  const side = clamp(Math.round(Math.min(W, H) * 0.18), Math.min(48, W, H), Math.min(200, W, H));
  const w = side / W;
  const hh = side / H;
  const x = clamp((center?.cx ?? 0.5) - w / 2, 0, 1 - w);
  const y = clamp((center?.cy ?? 0.5) - hh / 2, 0, 1 - hh);
  return { x: round(x, 6), y: round(y, 6), w: round(w, 6), h: round(hh, 6), side };
}

// ── DOM renderers (one per item kind) ──────────────────────────────────────

/** Text lines into `el` (kept as text nodes — user words are never HTML). */
function setLines(el, lines) {
  const want = lines.join('\n');
  if (el.dataset.text === want) return;
  el.dataset.text = want;
  el.replaceChildren(...lines.flatMap((l, i) => (i ? [h('br'), l] : [l])));
}

function outlineShadow(px, color) {
  if (!color || color === 'none') return 'none';
  const o = Math.max(1, px).toFixed(1);
  const d = (Math.max(1, px) * 0.71).toFixed(1);
  return [`${o}px 0`, `-${o}px 0`, `0 ${o}px`, `0 -${o}px`, `${d}px ${d}px`, `-${d}px ${d}px`, `${d}px -${d}px`, `-${d}px -${d}px`].map((s) => `${s} 0 ${color}`).join(', ');
}

/** Shape outline as an SVG sized to the box in display px (no distortion of arrow heads, hearts, stars). */
export function shapeSvg(shape, w, hh, { color = '#e0262e', fill = null, stroke = 2 } = {}) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${Math.max(1, w)} ${Math.max(1, hh)}`);
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', '100%');
  svg.setAttribute('aria-hidden', 'true');
  const s = Math.max(0.5, stroke);
  const i = s / 2;
  let el;
  const mk = (tag, attrs) => {
    const n = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
    return n;
  };
  if (shape === 'ellipse') el = mk('ellipse', { cx: w / 2, cy: hh / 2, rx: Math.max(0, w / 2 - i), ry: Math.max(0, hh / 2 - i) });
  else if (shape === 'line') el = mk('line', { x1: i, y1: hh / 2, x2: w - i, y2: hh / 2 });
  else if (shape === 'arrow') {
    const head = Math.min(w * 0.35, Math.max(hh, s * 4));
    el = mk('path', { d: `M${i} ${hh / 2}H${w - i}M${w - head} ${hh / 2 - head * 0.55}L${w - i} ${hh / 2}L${w - head} ${hh / 2 + head * 0.55}` });
  } else if (shape === 'star') {
    const pts = [];
    for (let k = 0; k < 10; k++) {
      const a = -Math.PI / 2 + (k * Math.PI) / 5;
      const r = k % 2 ? 0.4 : 1;
      pts.push(`${(w / 2 + Math.cos(a) * r * (w / 2 - i)).toFixed(1)},${(hh / 2 + Math.sin(a) * r * (hh / 2 - i) + hh * 0.04).toFixed(1)}`);
    }
    el = mk('polygon', { points: pts.join(' ') });
  } else if (shape === 'heart') {
    const x = (u) => (i + u * (w - s)).toFixed(1);
    const y = (v) => (i + v * (hh - s)).toFixed(1);
    el = mk('path', { d: `M${x(0.5)} ${y(0.98)}C${x(0.12)} ${y(0.7)} ${x(0)} ${y(0.45)} ${x(0)} ${y(0.28)}C${x(0)} ${y(0.1)} ${x(0.14)} ${y(0)} ${x(0.28)} ${y(0)}C${x(0.4)} ${y(0)} ${x(0.47)} ${y(0.08)} ${x(0.5)} ${y(0.18)}C${x(0.53)} ${y(0.08)} ${x(0.6)} ${y(0)} ${x(0.72)} ${y(0)}C${x(0.86)} ${y(0)} ${x(1)} ${y(0.1)} ${x(1)} ${y(0.28)}C${x(1)} ${y(0.45)} ${x(0.88)} ${y(0.7)} ${x(0.5)} ${y(0.98)}Z` });
  } else el = mk('rect', { x: i, y: i, width: Math.max(0, w - s), height: Math.max(0, hh - s) });
  el.setAttribute('fill', fill && shape !== 'line' && shape !== 'arrow' ? fill : 'none');
  el.setAttribute('stroke', color);
  el.setAttribute('stroke-width', String(s));
  el.setAttribute('stroke-linecap', 'round');
  el.setAttribute('stroke-linejoin', 'round');
  svg.append(el);
  return svg;
}

/**
 * Kind behaviour + renderers for OverlayLayer. `env` gives live settings: {blur(), wm(), meme(), logoSrc(),
 * stickerSrc(id)}.
 */
export function stageKinds(env) {
  return {
    region: {
      move: true, resize: 'free', rotate: false, snap: false,
      label: (it) => `${{ face: 'Face', plate: 'Plate' }[it.source] || 'Area'} to hide${it.off ? ' (kept visible)' : ''}`,
      render(it, el, { W, H }) {
        const b = env.blur();
        const side = Math.min(it.w * W, it.h * H);
        el.className = `ov-content ov-fx ov-fx-${b.mode}${it.shape === 'ellipse' ? ' is-ellipse' : ''}`;
        el.style.setProperty('--fx-blur', `${Math.max(3, (side / 5) * b.strength).toFixed(1)}px`);
        el.style.setProperty('--fx-cell', `${Math.max(3, (side / 7) * b.strength).toFixed(1)}px`);
        el.style.setProperty('--fx-color', b.color);
      },
    },
    text: {
      move: true, resize: 'ratio', rotate: true,
      label: (it) => `Text “${String(it.text || '').slice(0, 40)}”`,
      render(it, el, { H }) {
        el.className = 'ov-content ov-text';
        const px = it.size * H;
        Object.assign(el.style, { fontFamily: fontCss(it.font), fontSize: `${px.toFixed(2)}px`, color: it.color, opacity: String(it.opacity ?? 1), textAlign: it.align || 'center', lineHeight: '1.15' });
        setLines(el, String(it.text || '').split('\n'));
      },
    },
    sticker: {
      move: true, resize: 'ratio', rotate: true,
      label: (it) => `Sticker ${it.stickerName || it.sticker}`,
      render(it, el) {
        el.className = 'ov-content ov-sticker';
        el.style.opacity = String(it.opacity ?? 1);
        const src = it.stickerSrc || env.stickerSrc(it.sticker);
        let img = el.firstElementChild;
        if (!img) { img = h('img', { alt: '', draggable: 'false' }); el.append(img); }
        if (src && img.getAttribute('src') !== src) img.src = src;
      },
    },
    shape: {
      move: true, resize: 'free', rotate: true,
      label: (it) => `Shape: ${it.shape}`,
      render(it, el, { W, H }) {
        el.className = 'ov-content ov-shape';
        el.style.opacity = String(it.opacity ?? 1);
        el.replaceChildren(shapeSvg(it.shape, it.w * W, it.h * H, { color: it.color, fill: it.fill, stroke: (it.strokeWidth ?? 0.008) * W }));
      },
    },
    brush: {
      move: true, resize: false, rotate: false,
      label: () => 'Drawing',
      render(it, el) {
        el.className = 'ov-content ov-brushbox';
      },
    },
    watermark: {
      move: true, resize: 'ratio', rotate: true, zones: true, snap: false, remove: false,
      label: () => 'Watermark — drag to one of nine positions',
      render(it, el, { H, W }) {
        const wm = env.wm();
        el.className = 'ov-content ov-wm';
        el.style.opacity = String(wm.opacity);
        if (wm.kind === 'image') {
          let img = el.querySelector('img');
          if (!img) { el.replaceChildren(img = h('img', { alt: '', draggable: 'false' })); }
          const src = env.logoSrc();
          if (src && img.getAttribute('src') !== src) img.src = src;
          return;
        }
        el.querySelector('img')?.remove();
        Object.assign(el.style, { fontFamily: fontCss(wm.font), fontSize: `${(wm.size * Math.min(W, H)).toFixed(2)}px`, color: wm.color, fontWeight: String(WM_TEXT_WEIGHT), lineHeight: '1.15', whiteSpace: 'pre' });
        setLines(el, [wm.text || ' ']);
      },
    },
    'wm-mosaic': {
      move: false, resize: false, rotate: false, remove: false,
      label: () => 'Watermark tiled across the photo',
      render(it, el, { W, H }) {
        const wm = env.wm();
        el.className = 'ov-content ov-wm-mosaic';
        el.style.opacity = String(wm.opacity);
        const textPx = wm.size * Math.min(W, H); // same basis as watermarkGeom and the server
        const cell = wm.kind === 'image' ? Math.max(24, wm.imageScale * W) : Math.max(24, (wm.text.length * 0.6 + 2) * textPx);
        const n = Math.min(400, Math.ceil((W * 1.6) / cell) * Math.ceil((H * 1.6) / (cell * 0.6)));
        const key = `${wm.kind}|${wm.text}|${wm.font}|${wm.color}|${n}|${env.logoSrc()}|${Math.round(cell)}|${wm.size}`;
        if (el.dataset.key !== key) {
          el.dataset.key = key;
          const tile = () => (wm.kind === 'image'
            ? h('img', { alt: '', draggable: 'false', src: env.logoSrc() || '', style: { width: `${cell}px` } })
            : h('span', { style: { fontFamily: fontCss(wm.font), fontSize: `${textPx.toFixed(1)}px`, fontWeight: String(WM_TEXT_WEIGHT), color: wm.color } }, wm.text));
          el.replaceChildren(h('div', { class: 'ov-mosaic-grid', style: { transform: `rotate(${wm.rotation}deg)`, gap: `${(cell * 0.35).toFixed(0)}px ${(cell * 0.5).toFixed(0)}px` } }, Array.from({ length: n }, tile)));
        } else {
          el.firstElementChild.style.transform = `rotate(${wm.rotation}deg)`;
        }
      },
    },
    'meme-top': memeKind(env, 'top'),
    'meme-bottom': memeKind(env, 'bottom'),
    loupe: {
      move: true, resize: false, rotate: false, remove: false, snap: false,
      label: () => 'Detail check square — drag to another spot',
      render(it, el) {
        el.className = 'ov-content ov-loupe';
      },
    },
  };
}

function memeKind(env, key) {
  return {
    move: false, resize: false, rotate: false, remove: false,
    label: () => `${key === 'top' ? 'Top' : 'Bottom'} caption`,
    render(it, el) {
      const m = env.meme();
      el.className = 'ov-content ov-meme';
      Object.assign(el.style, {
        fontFamily: fontCss(m.font), fontSize: `${it.px.toFixed(2)}px`, color: m.color, lineHeight: '1.08',
        textShadow: outlineShadow(it.outline * (it.scale || 1), m.outline),
      });
      setLines(el, it.lines);
    },
  };
}
