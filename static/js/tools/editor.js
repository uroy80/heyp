// Photo editor: named filters and frames (whole batch), and layers placed on the photo — text, stickers, shapes
// and brush strokes — that you drag, resize and rotate on the canvas (per photo).

import { h, append, uid } from '../dom.js';
import { state, defaults, activeFile, filterOp, frameOp, liveItems } from '../state.js';
import { toolHead, group, segmented, slider, toggle, selectField, swatches, note } from '../controls.js';
import { icon } from '../icons.js';
import { FONTS, FILTERS, FRAMES, SHAPES, filterCss } from '../catalog.js';

const TABS = [
  { value: 'filters', label: 'Filters', icon: 'filter' },
  { value: 'frames', label: 'Frames', icon: 'frame' },
  { value: 'text', label: 'Text', icon: 'text' },
  { value: 'stickers', label: 'Stickers', icon: 'sticker' },
  { value: 'shapes', label: 'Shapes', icon: 'shape' },
  { value: 'brush', label: 'Brush', icon: 'brush' },
];
const INK = [
  { value: '#ffffff', label: 'White' },
  { value: '#141210', label: 'Ink' },
  { value: '#e0262e', label: 'Red' },
  { value: '#ffd60a', label: 'Yellow' },
  { value: '#1f6fd1', label: 'Blue' },
];
const KIND_ICON = { text: 'text', sticker: 'sticker', shape: 'shape', brush: 'brush' };

let r = {};

/** Readable label of a layer. */
export function itemLabel(it) {
  if (it.kind === 'text') return `“${String(it.text || '').trim().slice(0, 24) || 'Empty text'}”`;
  if (it.kind === 'sticker') return `Sticker ${it.stickerName || it.sticker}`;
  if (it.kind === 'shape') return (SHAPES.find((s) => s.id === it.shape)?.label || 'Shape');
  if (it.kind === 'brush') return 'Drawing';
  return it.kind;
}

/** A filter swatch button: the photo's thumbnail with the filter's CSS approximation. */
function filterSwatch(f, thumb, active, onPick) {
  return h('button', {
    type: 'button', class: 'fx-swatch', 'aria-pressed': String(active), title: f.label,
    onclick: () => onPick(f.id),
  }, h('span', { class: 'fx-pic' }, thumb ? h('img', { src: thumb, alt: '', style: { filter: filterCss(f.id, 1) }, loading: 'lazy', decoding: 'async' }) : null),
  h('span', { class: 'fx-name' }, f.label));
}

export default {
  id: 'editor',
  title: 'Photo editor',
  icon: 'editor',
  kicker: 'Filters, frames & stickers',

  isActive: () => Boolean(filterOp() || frameOp() || state.files.some((f) => liveItems(f).length)),
  reset() {
    const tab = state.editor.tab;
    state.editor = { ...defaults.editor(), tab, brush: state.editor.brush };
    for (const f of state.files) f.items = [];
  },

  render(root, app) {
    r = {};
    const e = state.editor;
    const f = activeFile();
    append(root, toolHead('Photo editor', 'Filters and frames for every photo; text, stickers, shapes and drawings placed on this one.', { onReset: () => app.resetTool(this), active: this.isActive() }));
    const tabs = segmented({
      label: 'Editor tools', hideLabel: true, cols: 3, size: 'sm', value: e.tab,
      options: TABS.map((t) => ({ value: t.value, label: t.label, icon: t.icon })),
      onChange: (v) => { e.tab = v; if (v !== 'brush') e.drawing = false; app.changed({ panel: true, preview: false }); },
    });
    tabs.classList.add('ed-tabs');
    append(root, tabs);

    if (e.tab === 'filters') {
      const thumb = f ? app.thumbUrl(f.id) : null;
      r.fx = h('div', { class: 'fx-grid', role: 'group', 'aria-label': 'Filters' },
        app.catalog().filters.map((x) => filterSwatch(x, thumb, x.id === e.filter, (id) => { e.filter = id; if (!(e.intensity > 0)) e.intensity = 1; app.changed({ panel: true }); })));
      const intensity = e.filter === 'none' ? null : slider({ label: 'Intensity', min: 0.05, max: 1, step: 0.05, value: e.intensity, resetTo: 1, format: (v) => `${Math.round(v * 100)}%`, onInput: (v) => { e.intensity = v; app.changed(); } });
      append(root, group(null, r.fx, intensity, h('p', { class: 'hint' }, 'Swatches are a quick impression; the picture shows the real result. Filters apply to every photo.')));
    } else if (e.tab === 'frames') {
      const frames = h('div', { class: 'frame-grid', role: 'group', 'aria-label': 'Frames' },
        app.catalog().frames.map((x) => h('button', {
          type: 'button', class: 'frame-swatch', 'aria-pressed': String(x.id === e.frame),
          // each style starts with its own colour and width (a film strip is dark, an instant photo white)
          onclick: () => { e.frame = x.id; e.frameColor = null; e.frameWidth = null; app.changed({ panel: true }); },
        }, h('span', { class: `frame-pic frame-pic-${x.id}`, 'aria-hidden': 'true' }), h('span', { class: 'fx-name' }, x.label))));
      const parts = [frames];
      if (e.frame !== 'none') {
        const def = app.catalog().frames.find((x) => x.id === e.frame) || {};
        const defColor = /^#[0-9a-f]{6}$/i.test(def.color || '') ? def.color : null; // not "transparent" or with alpha
        const defWidth = Number.isFinite(def.width) ? def.width : 0.04;
        // rounded corners are cut out (transparent, or the export background): there is no frame colour to pick
        if (def.color !== 'transparent') {
          parts.push(swatches({ label: 'Frame colour', colors: [{ value: '#ffffff', label: 'White' }, { value: '#141210', label: 'Ink' }, { value: '#f4efe4', label: 'Paper' }, { value: '#b4141b', label: 'Red' }], value: e.frameColor || defColor, defaultValue: defColor, onChange: (v) => { e.frameColor = v; app.changed(); } }));
        }
        parts.push(slider({ label: 'Width', min: 0, max: 0.15, step: 0.005, value: e.frameWidth ?? defWidth, resetTo: defWidth, format: (v) => `${Math.round(v * 1000) / 10}%`, onInput: (v) => { e.frameWidth = v; app.changed(); } }));
      }
      append(root, group(null, ...parts, h('p', { class: 'hint' }, 'Frames apply to every photo.')));
    } else if (!f) {
      append(root, note('Add a photo to place text, stickers, shapes or drawings on it.', 'info'));
    } else if (e.tab === 'text') {
      append(root, group(null, h('button', { type: 'button', class: 'btn btn-icon-text', onclick: () => app.addItem({ kind: 'text', text: 'Your text', font: 'libre_franklin', color: '#ffffff', size: 0.08, opacity: 1, rotation: 0, align: 'center' }, { focusText: true }) }, icon('plus', 16), h('span', {}, 'Add text'))));
      append(root, this.selectedPanel(app, 'text'));
    } else if (e.tab === 'stickers') {
      const list = app.catalog().stickers;
      if (!list.length) {
        append(root, note(app.catalog().stickersLoading ? 'Loading stickers…' : 'Stickers aren’t available on this server.', 'info'));
      } else {
        const cats = [...new Set(list.map((s) => s.category || 'Stickers'))];
        for (const c of cats) {
          append(root, group(cats.length > 1 ? c : null, h('div', { class: 'sticker-grid', role: 'group', 'aria-label': c },
            list.filter((s) => (s.category || 'Stickers') === c).map((s) => h('button', {
              type: 'button', class: 'sticker-btn', title: s.name, 'aria-label': `Add sticker: ${s.name}`,
              onclick: () => app.addItem({ kind: 'sticker', sticker: s.id, stickerName: s.name, stickerSrc: s.url, opacity: 1, rotation: 0, w: 0.2, ratio: s.ratio > 0 ? s.ratio : 1 }),
            }, h('img', { src: s.url, alt: '', loading: 'lazy', decoding: 'async', draggable: 'false' }))))));
        }
        if (app.catalog().stickerCredit) append(root, h('p', { class: 'hint' }, app.catalog().stickerCredit));
      }
      append(root, this.selectedPanel(app, 'sticker'));
    } else if (e.tab === 'shapes') {
      append(root, group(null, h('div', { class: 'btn-row' }, SHAPES.map((s) => h('button', {
        type: 'button', class: 'btn btn-icon-text',
        onclick: () => app.addItem({ kind: 'shape', shape: s.id, color: '#e0262e', fill: null, strokeWidth: 0.008, opacity: 1, rotation: 0, w: s.id === 'line' || s.id === 'arrow' ? 0.4 : 0.3, h: s.id === 'line' || s.id === 'arrow' ? 0.05 : 0.22 }),
      }, h('span', { class: `shape-ico shape-ico-${s.id}`, 'aria-hidden': 'true' }), h('span', {}, s.label))))));
      append(root, this.selectedPanel(app, 'shape'));
    } else if (e.tab === 'brush') {
      const bs = e.brush;
      append(root, group(null,
        toggle({ label: 'Draw on the photo', checked: e.drawing, hint: 'While this is on, dragging on the photo paints a stroke.', onChange: (v) => { e.drawing = v; app.changed({ preview: false }); } }),
        swatches({ label: 'Colour', colors: INK, value: bs.color, defaultValue: '#e0262e', onChange: (v) => { bs.color = v; app.changed({ preview: false }); } }),
        slider({ label: 'Width', min: 0.002, max: 0.06, step: 0.001, value: bs.width, resetTo: 0.012, format: (v) => `${Math.round(v * 1000) / 10}%`, onInput: (v) => { bs.width = v; app.changed({ preview: false }); } }),
        slider({ label: 'Opacity', min: 0.1, max: 1, step: 0.05, value: bs.opacity, resetTo: 1, format: (v) => `${Math.round(v * 100)}%`, onInput: (v) => { bs.opacity = v; app.changed({ preview: false }); } }),
        h('button', {
          type: 'button', class: 'btn btn-icon-text', disabled: !(f.items || []).some((x) => x.kind === 'brush'),
          onclick: () => {
            const i = (f.items || []).map((x) => x.kind).lastIndexOf('brush');
            if (i >= 0) f.items.splice(i, 1);
            app.changed({ panel: true });
          },
        }, icon('rotateLeft', 16), h('span', {}, 'Undo last stroke'))));
    }
    if (f && ['text', 'stickers', 'shapes', 'brush'].includes(e.tab)) append(root, this.layers(app, f));
  },

  /** Properties of the selected layer when it is of `kind`. */
  selectedPanel(app, kind) {
    const f = activeFile();
    const it = (f?.items || []).find((x) => x.id === app.selectedItem());
    if (!it || it.kind !== kind) return h('p', { class: 'hint' }, kind === 'text' ? 'Select a text on the photo to edit it.' : 'Select one on the photo to change it.');
    const upd = (k, opts) => (v) => { it[k] = v; app.changed(opts); };
    const parts = [];
    if (kind === 'text') {
      const id = uid('txt');
      const ta = h('textarea', { id, class: 'input textarea', rows: 2, maxLength: 500, value: it.text, oninput: () => { it.text = ta.value.slice(0, 500); app.changed(); } });
      r.text = ta;
      parts.push(h('div', { class: 'field' }, h('label', { htmlFor: id, class: 'label' }, 'Text'), ta));
      parts.push(selectField({ label: 'Font', value: it.font, options: FONTS.map((x) => ({ value: x.id, label: x.label })), onChange: upd('font') }));
      parts.push(swatches({ label: 'Colour', colors: INK, value: it.color, defaultValue: '#ffffff', onChange: upd('color') }));
      parts.push(slider({ label: 'Size', min: 0.02, max: 0.4, step: 0.005, value: it.size, resetTo: 0.08, format: (v) => `${Math.round(v * 1000) / 10}%`, onInput: upd('size') }));
      parts.push(segmented({ label: 'Align', cols: 3, size: 'sm', value: it.align || 'center', options: [{ value: 'left', label: 'Left' }, { value: 'center', label: 'Centre' }, { value: 'right', label: 'Right' }], onChange: upd('align') }));
    }
    if (kind === 'shape') {
      parts.push(swatches({ label: 'Line colour', colors: INK, value: it.color, defaultValue: '#e0262e', onChange: upd('color') }));
      if (it.shape !== 'line' && it.shape !== 'arrow') {
        parts.push(toggle({ label: 'Fill', checked: Boolean(it.fill), onChange: (v) => { it.fill = v ? (it.color || '#e0262e') : null; app.changed({ panel: true }); } }));
        if (it.fill) parts.push(swatches({ label: 'Fill colour', colors: INK, value: it.fill, onChange: upd('fill') }));
      }
      parts.push(slider({ label: 'Line width', min: 0.002, max: 0.05, step: 0.001, value: it.strokeWidth ?? 0.008, resetTo: 0.008, format: (v) => `${Math.round(v * 1000) / 10}%`, onInput: upd('strokeWidth') }));
    }
    parts.push(slider({ label: 'Opacity', min: 0.05, max: 1, step: 0.05, value: it.opacity ?? 1, resetTo: 1, format: (v) => `${Math.round(v * 100)}%`, onInput: upd('opacity') }));
    parts.push(slider({ label: 'Rotation', min: -180, max: 180, step: 1, value: Math.round(it.rotation || 0), resetTo: 0, format: (v) => `${v}°`, onInput: upd('rotation') }));
    parts.push(h('button', { type: 'button', class: 'btn btn-icon-text', onclick: () => app.removeItem(it.id) }, icon('trash', 16), h('span', {}, 'Delete')));
    return group(kind === 'text' ? 'Selected text' : kind === 'sticker' ? 'Selected sticker' : 'Selected shape', ...parts);
  },

  /** The layers on this photo, top first: select, re-order, delete. */
  layers(app, f) {
    const items = f.items || [];
    if (!items.length) return null;
    const sel = app.selectedItem();
    const list = h('ol', { class: 'layer-list', 'aria-label': 'Layers on this photo, top first' });
    [...items].reverse().forEach((it) => {
      const i = items.indexOf(it);
      list.append(h('li', { class: `layer-row${it.id === sel ? ' is-selected' : ''}` },
        h('button', { type: 'button', class: 'layer-pick', 'aria-pressed': String(it.id === sel), onclick: () => app.selectItem(it.id, { tab: true }) },
          icon(KIND_ICON[it.kind] || 'layers', 15), h('span', {}, itemLabel(it))),
        h('button', { type: 'button', class: 'icon-btn layer-btn', 'aria-label': `Bring ${itemLabel(it)} forward`, title: 'Forward', disabled: i === items.length - 1, onclick: () => app.moveItem(it.id, 1) }, icon('arrowUp', 14)),
        h('button', { type: 'button', class: 'icon-btn layer-btn', 'aria-label': `Send ${itemLabel(it)} backward`, title: 'Backward', disabled: i === 0, onclick: () => app.moveItem(it.id, -1) }, icon('arrowDown', 14)),
        h('button', { type: 'button', class: 'chip-x', 'aria-label': `Delete ${itemLabel(it)}`, title: 'Delete', onclick: () => app.removeItem(it.id) }, icon('x', 12))));
    });
    return group('Layers', list, h('button', { type: 'button', class: 'link-btn', onclick: () => { f.items = []; app.changed({ panel: true }); } }, 'Remove all layers on this photo'));
  },

  sync(app) {
    // keep the text box in step with canvas edits without stealing the caret
    const f = activeFile();
    const it = (f?.items || []).find((x) => x.id === app.selectedItem());
    if (r.text && it?.kind === 'text' && document.activeElement !== r.text && r.text.value !== it.text) r.text.value = it.text;
  },

  focusText() {
    r.text?.focus();
    r.text?.select();
  },
};
