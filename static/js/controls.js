// Form building blocks used by the tool panels. Each returns an element; some expose .set().

import { h, uid, num } from './dom.js';
import { icon } from './icons.js';

export function toolHead(title, desc, { onReset, active = false } = {}) {
  return h('header', { class: 'tool-head' },
    h('div', { class: 'tool-head-row' },
      h('h2', { class: 'tool-title', id: 'tool-title' }, title),
      onReset ? h('button', { type: 'button', class: 'link-btn', hidden: !active, onclick: onReset, dataset: { role: 'reset' } }, 'Reset') : null),
    desc ? h('p', { class: 'tool-desc' }, desc) : null);
}

export function group(title, ...children) {
  return h('section', { class: 'group' }, title ? h('h3', { class: 'group-title' }, title) : null, ...children);
}

export function note(text, kind = '') {
  return h('p', { class: `note ${kind ? `note-${kind}` : ''}` },
    kind === 'warn' ? icon('alert', 15) : kind === 'info' ? icon('info', 15) : null, h('span', {}, text));
}

/** Number input with optional suffix. onChange(value|null) fires on every keystroke. */
export function numberField({ label, value, min, max, step = 'any', placeholder, suffix, onChange, id = uid('n'), hideLabel = false, inputmode = 'decimal' }) {
  const input = h('input', {
    type: 'number', id, min, max, step, placeholder, inputmode,
    value: value ?? '', class: 'input',
    oninput: () => onChange?.(num(input.value)),
  });
  const wrap = h('div', { class: 'field' },
    h('label', { htmlFor: id, class: hideLabel ? 'sr-only' : 'label' }, label),
    h('div', { class: 'input-wrap' }, input, suffix ? h('span', { class: 'suffix', 'aria-hidden': 'true' }, suffix) : null));
  wrap.input = input;
  wrap.set = (v) => { if (document.activeElement !== input) input.value = v ?? ''; };
  return wrap;
}

export function textField({ label, value, placeholder, onChange, id = uid('t'), type = 'text' }) {
  const input = h('input', { type, id, class: 'input', value: value ?? '', placeholder, spellcheck: 'false', autocomplete: 'off', oninput: () => onChange?.(input.value) });
  const wrap = h('div', { class: 'field' }, h('label', { htmlFor: id, class: 'label' }, label), input);
  wrap.input = input;
  return wrap;
}

/** Range slider with live readout; double-click (or the ↺ button) resets. */
export function slider({ label, min, max, step, value, format = String, onInput, resetTo, id = uid('r') }) {
  const out = h('output', { class: 'slider-val', htmlFor: id });
  const input = h('input', { type: 'range', id, min, max, step, value, class: 'range' });
  const paint = () => {
    const v = Number(input.value);
    out.textContent = format(v);
    input.style.setProperty('--p', `${((v - min) / (max - min)) * 100}%`);
    if (resetBtn) resetBtn.hidden = resetTo == null || v === resetTo;
  };
  const resetBtn = resetTo != null
    ? h('button', { type: 'button', class: 'mini-reset', 'aria-label': `Reset ${label}`, title: 'Reset', onclick: () => { input.value = resetTo; paint(); onInput?.(resetTo); } }, icon('reset', 13))
    : null;
  input.addEventListener('input', () => { paint(); onInput?.(Number(input.value)); });
  if (resetTo != null) input.addEventListener('dblclick', () => { input.value = resetTo; paint(); onInput?.(resetTo); });
  const wrap = h('div', { class: 'field slider' },
    h('div', { class: 'field-row' }, h('label', { htmlFor: id, class: 'label' }, label), h('span', { class: 'slider-meta' }, resetBtn, out)),
    input);
  paint();
  wrap.input = input;
  wrap.set = (v) => { input.value = v; paint(); };
  return wrap;
}

/** Radio-based segmented control. options: [{value,label,disabled,title}]. */
export function segmented({ label, options, value, onChange, name = uid('seg'), cols, hideLabel = false, size = '' }) {
  const fs = h('fieldset', { class: `seg ${size ? `seg-${size}` : ''}` },
    h('legend', { class: hideLabel ? 'sr-only' : 'label' }, label));
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

/** Switch (checkbox with role=switch). */
export function toggle({ label, checked, onChange, hint, disabled = false, id = uid('s') }) {
  const hintId = hint ? `${id}-hint` : null;
  const input = h('input', { type: 'checkbox', role: 'switch', id, checked, disabled, 'aria-describedby': hintId, onchange: () => onChange?.(input.checked) });
  const wrap = h('div', { class: `switch-field ${disabled ? 'is-disabled' : ''}` },
    h('label', { class: 'switch', htmlFor: id }, input, h('span', { class: 'track', 'aria-hidden': 'true' }), h('span', { class: 'switch-label' }, label)),
    hint ? h('p', { class: 'hint', id: hintId }, hint) : null);
  wrap.input = input;
  wrap.set = (v) => { input.checked = Boolean(v); };
  return wrap;
}

/** <select>; options: [{value,label}] or groups: [{label, options:[…]}]. */
export function selectField({ label, options = [], groups, value, onChange, id = uid('sel'), hideLabel = false, placeholder }) {
  const sel = h('select', { id, class: 'input select', onchange: () => onChange?.(sel.value) });
  fillSelect(sel, { options, groups, value, placeholder });
  const wrap = h('div', { class: 'field' }, h('label', { htmlFor: id, class: hideLabel ? 'sr-only' : 'label' }, label), h('div', { class: 'select-wrap' }, sel, icon('chevron', 16)));
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
