// Tiny DOM + formatting helpers shared by every module.

const PROPS = new Set(['value', 'checked', 'disabled', 'selected', 'hidden', 'htmlFor', 'textContent', 'indeterminate', 'open']);

/**
 * Create an element. `props`: class, style (object), dataset, on<Event> listeners,
 * DOM properties (value, checked, …) or attributes. Children: nodes, strings, arrays, null.
 */
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (PROPS.has(k)) el[k] = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, children);
  return el;
}

/** Null-safe append: skips null/false (Element.append would print "null"). */
export function append(el, ...children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);

const liveSig = new WeakMap();

/**
 * Replace `target`'s children only when the new markup differs from what it shows, so an aria-live
 * region isn't rewritten (and re-announced) on every input event. Returns true when it changed.
 */
export function replaceIfChanged(target, nodes) {
  const list = nodes.flat(Infinity).filter((n) => n != null && n !== false);
  const sig = list.map((n) => (typeof n === 'object' ? n.outerHTML ?? n.textContent ?? '' : String(n))).join('');
  if (liveSig.get(target) === sig) return false;
  liveSig.set(target, sig);
  target.replaceChildren(...list);
  return true;
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

export const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

export function debounce(fn, ms) {
  let t = 0;
  const d = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  d.cancel = () => clearTimeout(t);
  return d;
}

let uidN = 0;
export const uid = (prefix = 'u') => `${prefix}-${++uidN}`;

/** 1024-based sizes ("214 KB", "1.8 MB"). */
export function fmtBytes(n) {
  if (n == null || !Number.isFinite(n)) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  const mb = n / (1024 * 1024);
  return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
}

/** Trim trailing zeros: 2.50 → "2.5", 3.00 → "3". */
export function fmtNum(n, digits = 2) {
  if (n == null || !Number.isFinite(n)) return '—';
  return String(Number(n.toFixed(digits)));
}

export const fmtDims = (w, h) => `${Math.round(w)}×${Math.round(h)}`;

/** Parse a user-typed number; empty/invalid → null. */
export function num(v) {
  if (v === '' || v == null) return null;
  const n = typeof v === 'number' ? v : Number(String(v).replace(',', '.'));
  return Number.isFinite(n) ? n : null;
}

export function posInt(v) {
  const n = num(v);
  return n != null && n >= 1 ? Math.round(n) : null;
}

/** True when keyboard input should go to the focused control rather than to shortcuts. */
export function isTyping(el) {
  if (!el || el === document.body) return false;
  if (el.isContentEditable) return true;
  if (el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true;
  if (el.tagName === 'INPUT') {
    return !['range', 'checkbox', 'radio', 'button', 'submit', 'reset', 'color', 'file'].includes(el.type);
  }
  return false;
}
