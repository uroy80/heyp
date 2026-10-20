// Form building blocks used by the tool panels. Each returns an element; some expose .set().
// Explanations live behind a small "i" button (infoTip), never as paragraphs under the controls.

import { h, uid, num } from './dom.js';
import { icon } from './icons.js';

export function toolHead(title, desc, { onReset, active = false } = {}) {
  return h('header', { class: 'tool-head' },
    h('div', { class: 'tool-head-row' },
      h('h2', { class: 'tool-title', id: 'tool-title' }, title),
      onReset ? h('button', { type: 'button', class: 'link-btn', hidden: !active, onclick: onReset, dataset: { role: 'reset' } }, 'Reset') : null),
    desc ? h('p', { class: 'tool-desc' }, desc) : null);
}

let closeOpenTip = null;
let bubble = null; // the one visible tip on the page (outside the panels, so no scroller or mask clips it)

/**
 * A small "i" button that shows `text` on hover, keyboard focus or tap, and hides on Esc, blur or scroll. The text
 * stays in place as the button's description (aria-describedby, visually hidden), so screen readers read it with
 * the button; the bubble you see is an aria-hidden copy at the end of <body>. Returns the wrapper; its `tipId` is
 * the text's id (controls point their own aria-describedby at it).
 */
export function infoTip(text, { label = 'More info' } = {}) {
  const id = uid('tip');
  const btn = h('button', { type: 'button', class: 'tip-btn', 'aria-label': label, 'aria-describedby': id }, 'i');
  const desc = h('span', { class: 'sr-only', id, role: 'tooltip' }, text);
  const wrap = h('span', { class: 'tip' }, btn, desc);
  let pinned = false;
  const place = () => {
    const vw = document.documentElement.clientWidth;
    bubble.style.maxWidth = `${Math.min(260, vw - 16)}px`;
    const b = btn.getBoundingClientRect();
    const p = bubble.getBoundingClientRect();
    const above = b.top - p.height - 8;
    bubble.style.left = `${Math.max(8, Math.min(vw - p.width - 8, b.left + b.width / 2 - p.width / 2))}px`;
    bubble.style.top = `${above >= 8 ? above : b.bottom + 8}px`;
  };
  const onScroll = () => hide();
  function hide() {
    pinned = false;
    wrap.classList.remove('is-open');
    window.removeEventListener('scroll', onScroll, true);
    if (closeOpenTip !== hide) return;
    closeOpenTip = null;
    if (bubble) bubble.hidden = true;
  }
  const show = () => {
    if (closeOpenTip && closeOpenTip !== hide) closeOpenTip();
    closeOpenTip = hide;
    if (!bubble) {
      bubble = h('div', { class: 'tip-pop', 'aria-hidden': 'true', hidden: true });
      document.body.append(bubble);
    }
    bubble.textContent = desc.textContent; // a control may have changed its note since
    bubble.hidden = false;
    wrap.classList.add('is-open');
    place();
    window.addEventListener('scroll', onScroll, true);
  };
  btn.addEventListener('mouseenter', show);
  btn.addEventListener('mouseleave', () => { if (!pinned) hide(); });
  btn.addEventListener('focus', show);
  btn.addEventListener('blur', hide);
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation(); // e.g. the drop zone opens the file picker on a click
    if (pinned) hide();
    else {
      show();
      pinned = true;
    }
  });
  btn.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !wrap.classList.contains('is-open')) return;
    e.preventDefault();
    e.stopPropagation(); // Esc closes the tip, not the crop or a dialog
    hide();
  });
  wrap.tipId = id;
  return wrap;
}

/** A label (or legend text) followed by its info tip; `tip` null → just the label. */
function withTip(labelEl, tip, name) {
  if (!tip) return { head: labelEl, tipId: null };
  const t = infoTip(tip, { label: `About ${name}` });
  return { head: h('span', { class: 'label-tip' }, labelEl, t), tipId: t.tipId };
}

/** Section of a panel. `title` is a string, or {title, tip} for a heading with an info tip. */
export function group(title, ...children) {
  let head = null;
  if (title && typeof title === 'object') {
    head = h('div', { class: 'group-head' }, h('h3', { class: 'group-title' }, title.title),
      title.tip ? infoTip(title.tip, { label: `About ${title.title}` }) : null);
  } else if (title) {
    head = h('h3', { class: 'group-title' }, title);
  }
  return h('section', { class: 'group' }, head, ...children);
}

export function note(text, kind = '') {
  return h('p', { class: `note ${kind ? `note-${kind}` : ''}` },
    kind === 'warn' ? icon('alert', 15) : kind === 'info' ? icon('info', 15) : null, h('span', {}, text));
}

/** Number input with optional suffix. onChange(value|null) fires on every keystroke. */
export function numberField({ label, value, min, max, step = 'any', placeholder, suffix, onChange, id = uid('n'), hideLabel = false, inputmode = 'decimal', tip }) {
  const input = h('input', {
    type: 'number', id, min, max, step, placeholder, inputmode,
    value: value ?? '', class: 'input',
    oninput: () => onChange?.(num(input.value)),
  });
  const { head, tipId } = withTip(h('label', { htmlFor: id, class: hideLabel ? 'sr-only' : 'label' }, label), hideLabel ? null : tip, label);
  if (tipId) input.setAttribute('aria-describedby', tipId);
  const wrap = h('div', { class: 'field' },
    head,
    h('div', { class: 'input-wrap' }, input, suffix ? h('span', { class: 'suffix', 'aria-hidden': 'true' }, suffix) : null));
  wrap.input = input;
  wrap.set = (v) => { if (document.activeElement !== input) input.value = v ?? ''; };
  return wrap;
}

export function textField({ label, value, placeholder, onChange, id = uid('t'), type = 'text', tip }) {
  const input = h('input', { type, id, class: 'input', value: value ?? '', placeholder, spellcheck: 'false', autocomplete: 'off', oninput: () => onChange?.(input.value) });
  const { head, tipId } = withTip(h('label', { htmlFor: id, class: 'label' }, label), tip, label);
  if (tipId) input.setAttribute('aria-describedby', tipId);
  const wrap = h('div', { class: 'field' }, head, input);
  wrap.input = input;
  return wrap;
}

/** Range slider with live readout; double-click (or the ↺ button) resets. */
export function slider({ label, min, max, step, value, format = String, onInput, resetTo, id = uid('r'), tip }) {
  const out = h('output', { class: 'slider-val', htmlFor: id });
  const input = h('input', { type: 'range', id, min, max, step, value, class: 'range' });
  const paint = () => {
    const v = Number(input.value);
    out.textContent = format(v);
    input.setAttribute('aria-valuetext', out.textContent); // "+50", "60%", "↓ 7%" — not the raw 1.5 / 0.6 / 0.07
    input.style.setProperty('--p', `${((v - min) / (max - min)) * 100}%`);
    if (resetBtn) resetBtn.hidden = resetTo == null || v === resetTo;
  };
  const resetBtn = resetTo != null
    ? h('button', { type: 'button', class: 'mini-reset', 'aria-label': `Reset ${label}`, title: 'Reset', onclick: () => { input.value = resetTo; paint(); onInput?.(resetTo); } }, icon('reset', 13))
    : null;
  input.addEventListener('input', () => { paint(); onInput?.(Number(input.value)); });
  if (resetTo != null) input.addEventListener('dblclick', () => { input.value = resetTo; paint(); onInput?.(resetTo); });
  const { head, tipId } = withTip(h('label', { htmlFor: id, class: 'label' }, label), tip, label);
  if (tipId) input.setAttribute('aria-describedby', tipId);
  const wrap = h('div', { class: 'field slider' },
    h('div', { class: 'field-row' }, head, h('span', { class: 'slider-meta' }, resetBtn, out)),
    input);
  paint();
  wrap.input = input;
  wrap.set = (v) => { input.value = v; paint(); };
  return wrap;
}

/** Radio-based segmented control. options: [{value,label,disabled,title}]. */
export function segmented({ label, options, value, onChange, name = uid('seg'), cols, hideLabel = false, size = '', tip }) {
  const fs = h('fieldset', { class: `seg ${size ? `seg-${size}` : ''}` });
  if (tip && !hideLabel) {
    // the group is named by the label text alone (not by the "i" button inside the legend)
    const lid = uid('lg');
    const t = infoTip(tip, { label: `About ${label}` });
    fs.setAttribute('aria-labelledby', lid);
    fs.setAttribute('aria-describedby', t.tipId);
    fs.append(h('legend', { class: 'label label-tip' }, h('span', { id: lid }, label), t));
  } else {
    fs.append(h('legend', { class: hideLabel ? 'sr-only' : 'label' }, label));
  }
  const row = h('div', { class: 'seg-row' });
  if (cols) row.style.setProperty('--cols', String(cols));
  const inputs = [];
  for (const o of options) {
    const input = h('input', {
      type: 'radio', name, value: o.value, checked: o.value === value, disabled: o.disabled,
      onchange: () => input.checked && onChange?.(o.value),
    });
    inputs.push(input);
    row.append(h('label', { class: 'seg-opt', title: o.title || null }, input, h('span', {}, o.icon ? icon(o.icon, 16) : null, o.label)));
  }
  fs.append(row);
  fs.set = (v) => inputs.forEach((i) => { i.checked = i.value === String(v); });
  fs.inputs = inputs;
  return fs;
}

/** Switch (checkbox with role=switch). `hint` is shown behind an "i" button next to the label. */
export function toggle({ label, checked, onChange, hint, disabled = false, id = uid('s') }) {
  const t = hint ? infoTip(hint, { label: `About ${label}` }) : null;
  const input = h('input', { type: 'checkbox', role: 'switch', id, checked, disabled, 'aria-describedby': t?.tipId, onchange: () => onChange?.(input.checked) });
  const wrap = h('div', { class: `switch-field ${disabled ? 'is-disabled' : ''}` },
    h('div', { class: 'switch-row' },
      h('label', { class: 'switch', htmlFor: id }, input, h('span', { class: 'track', 'aria-hidden': 'true' }), h('span', { class: 'switch-label' }, label)),
      t));
  wrap.input = input;
  wrap.tip = t;
  wrap.set = (v) => { input.checked = Boolean(v); };
  return wrap;
}

/** <select>; options: [{value,label}] or groups: [{label, options:[…]}]. */
export function selectField({ label, options = [], groups, value, onChange, id = uid('sel'), hideLabel = false, placeholder, tip }) {
  const sel = h('select', { id, class: 'input select', onchange: () => onChange?.(sel.value) });
  fillSelect(sel, { options, groups, value, placeholder });
  const { head, tipId } = withTip(h('label', { htmlFor: id, class: hideLabel ? 'sr-only' : 'label' }, label), hideLabel ? null : tip, label);
  if (tipId) sel.setAttribute('aria-describedby', tipId);
  const wrap = h('div', { class: 'field' }, head, h('div', { class: 'select-wrap' }, sel, icon('chevron', 16)));
  wrap.select = sel;
  return wrap;
}

export function fillSelect(sel, { options = [], groups, value, placeholder }) {
  sel.replaceChildren();
  if (placeholder) sel.append(h('option', { value: '', disabled: true, selected: value == null || value === '' }, placeholder));
  const opt = (o) => h('option', { value: o.value, selected: String(o.value) === String(value), disabled: o.disabled }, o.label);
  if (groups) for (const g of groups) sel.append(h('optgroup', { label: g.label }, g.options.map(opt)));
  else sel.append(...options.map(opt));
}

/** Small pill buttons for quick values; the picked one shows as pressed. */
export function quickChips(items, onPick, { label } = {}) {
  const btns = items.map((it) => h('button', {
    type: 'button', class: 'qchip', 'aria-pressed': it.active ? 'true' : 'false',
    onclick: (e) => {
      for (const b of btns) b.setAttribute('aria-pressed', String(b === e.currentTarget));
      onPick(it.value);
    },
  }, it.label));
  return h('div', { class: 'qchips', role: 'group', 'aria-label': label || null }, btns);
}

/** Colour swatches with optional custom picker. colors: [{value,label}]. */
export function swatches({ label, colors, value, onChange, allowCustom = true, defaultValue }) {
  const name = uid('sw');
  const wrap = h('fieldset', { class: 'swatches' }, h('legend', { class: 'label' }, label));
  const row = h('div', { class: 'swatch-row' });
  const inputs = [];
  const norm = (c) => String(c || '').toLowerCase();
  const isPreset = colors.some((c) => norm(c.value) === norm(value));
  for (const c of colors) {
    const input = h('input', { type: 'radio', name, value: c.value, checked: norm(c.value) === norm(value), onchange: () => input.checked && onChange(c.value) });
    inputs.push(input);
    const dot = h('span', { class: 'swatch', style: { background: c.value } });
    row.append(h('label', { class: 'swatch-opt', title: c.label }, input, dot,
      h('span', { class: 'swatch-name' }, c.label), norm(c.value) === norm(defaultValue) ? h('span', { class: 'swatch-default', 'aria-hidden': 'true' }) : null));
  }
  if (allowCustom) {
    const picker = h('input', {
      type: 'color', class: 'swatch-picker', value: /^#[0-9a-f]{6}$/i.test(value) ? value : '#ffffff', 'aria-label': `${label}: custom colour`,
      oninput: () => { inputs.forEach((i) => { i.checked = false; }); customLbl.classList.add('is-checked'); onChange(picker.value); },
    });
    const customLbl = h('label', { class: `swatch-opt swatch-custom ${!isPreset && value ? 'is-checked' : ''}`, title: 'Custom colour' }, picker,
      h('span', { class: 'swatch swatch-rainbow', style: !isPreset && value ? { background: value } : null }),
      h('span', { class: 'swatch-name', 'aria-hidden': 'true' }, 'Custom'));
    picker.addEventListener('input', () => { customLbl.querySelector('.swatch').style.background = picker.value; });
    inputs.forEach((i) => i.addEventListener('change', () => customLbl.classList.remove('is-checked')));
    row.append(customLbl);
  }
  wrap.append(row);
  return wrap;
}

export function iconButton({ label, iconName, onClick, text, pressed, cls = '' }) {
  return h('button', {
    type: 'button', class: `btn ${text ? 'btn-icon-text' : 'btn-square'} ${cls}`,
    'aria-label': text ? null : label, title: label, onclick: onClick,
    'aria-pressed': pressed == null ? null : String(Boolean(pressed)),
  }, icon(iconName, 18), text ? h('span', {}, text) : null);
}

/** Two numeric inputs joined by an aspect-lock button. */
export function dimsPair({ wLabel = 'Width', hLabel = 'Height', w, h: hv, wPlaceholder, hPlaceholder, unit, locked, onW, onH, onLock, step = 'any' }) {
  const wf = numberField({ label: wLabel, value: w, placeholder: wPlaceholder, suffix: unit, onChange: onW, step, min: 0 });
  const hf = numberField({ label: hLabel, value: hv, placeholder: hPlaceholder, suffix: unit, onChange: onH, step, min: 0 });
  const lock = h('button', {
    type: 'button', class: 'lock-btn', 'aria-pressed': String(locked), title: locked ? 'Aspect ratio locked' : 'Aspect ratio unlocked',
    'aria-label': 'Lock aspect ratio',
    onclick: () => {
      const v = lock.getAttribute('aria-pressed') !== 'true';
      lock.setAttribute('aria-pressed', String(v));
      lock.replaceChildren(icon(v ? 'lock' : 'unlock', 16));
      lock.title = v ? 'Aspect ratio locked' : 'Aspect ratio unlocked';
      onLock?.(v);
    },
  }, icon(locked ? 'lock' : 'unlock', 16));
  const row = h('div', { class: 'dims-row' }, wf, lock, hf);
  row.w = wf;
  row.h = hf;
  return row;
}
