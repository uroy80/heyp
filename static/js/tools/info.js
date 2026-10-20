// Info: what's inside the file — size, DPI, colour profile, camera, GPS and full EXIF.

import { h, fmtBytes, fmtNum, append } from '../dom.js';
import { state, activeFile, FORMAT_LABEL } from '../state.js';
import { toolHead, note } from '../controls.js';
import { icon } from '../icons.js';
import { printSize } from './resize.js';

const ORIENTATION = {
  1: 'Normal', 2: 'Mirrored', 3: 'Rotated 180°', 4: 'Flipped vertically',
  5: 'Mirrored, rotated 90° left', 6: 'Rotated 90° right', 7: 'Mirrored, rotated 90° right', 8: 'Rotated 90° left',
};

let token = 0;
let view = null; // {body, info, complete} of the rendered panel

function row(label, ...value) {
  const v = value.flat().filter((x) => x != null && x !== '');
  if (!v.length) return null;
  return h('div', { class: 'kv' }, h('dt', {}, label), h('dd', {}, v));
}

function exifValue(v) {
  if (v == null) return '';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : fmtNum(v, 4);
  if (typeof v === 'string') return v.length > 200 ? `${v.slice(0, 200)}…` : v;
  try {
    const s = JSON.stringify(v);
    return s.length > 200 ? `${s.slice(0, 200)}…` : s;
  } catch {
    return String(v);
  }
}

export default {
  id: 'info',
  title: 'Info',
  icon: 'info',
  isActive: () => false,
  reset() {},

  render(root, app) {
    append(root, toolHead('Info', null));
    const f = activeFile();
    if (!f) {
      append(root, note('Add a photo first.', 'info'));
      return;
    }
    const body = h('div', { class: 'info-body' });
    append(root, body);
    view = { body, info: f.full || f.info, complete: Boolean(f.full) };
    renderInfo(body, view.info, view.complete);
    if (!f.full) {
      const my = ++token;
      app.fileFull(f).then((full) => {
        if (my !== token || !body.isConnected || !full) return;
        view = { body, info: full, complete: true };
        renderInfo(body, full, true);
      }).catch(() => { /* app reports errors */ });
    }
  },

  /** Export settings changed (e.g. metadata stripping) → refresh, keeping the EXIF table open. */
  sync() {
    if (!view?.body.isConnected) return;
    const stripping = state.output.stripMetadata || state.output.stripGps;
    if (stripping === view.stripping) return;
    const open = view.body.querySelector('details')?.open;
    renderInfo(view.body, view.info, view.complete);
    const d = view.body.querySelector('details');
    if (d && open) d.open = true;
  },
};

function renderInfo(body, info, complete) {
  if (view) view.stripping = state.output.stripMetadata || state.output.stripGps;
  const mp = (info.width * info.height) / 1e6;
  const fmt = FORMAT_LABEL[info.format] || String(info.format || '').toUpperCase();
  const dpi = Array.isArray(info.dpi) ? info.dpi : null;
  const cam = info.camera;
  const gps = info.gps;
  const stripping = state.output.stripMetadata || state.output.stripGps;

  const general = h('dl', { class: 'kv-list' },
    row('Name', h('span', { class: 'break' }, info.name)),
    row('Dimensions', `${info.width} × ${info.height} px`, h('span', { class: 'muted' }, ` · ${fmtNum(mp, 1)} MP`)),
    row('Format', fmt, info.mime ? h('span', { class: 'muted' }, ` · ${info.mime}`) : null),
    row('File size', fmtBytes(info.size_bytes)),
    row('Colour', [info.icc || 'No profile (sRGB assumed)', info.mode ? ` · ${info.mode}` : '', info.bit_depth ? ` · ${info.bit_depth}-\u2060bit` : ''].join('')),
    row('Transparency', info.has_alpha ? 'Yes' : 'No'),
    row('DPI', dpi ? `${fmtNum(dpi[0], 1)} × ${fmtNum(dpi[1], 1)}` : 'Not set'),
    row('Print size', dpi ? printSize(info.width, info.height, dpi[0]) : h('span', { class: 'muted' }, 'Not set')),
    row('Orientation', info.orientation && info.orientation !== 1 ? `${ORIENTATION[info.orientation] || info.orientation} (auto-corrected)` : 'Normal'),
    info.frames > 1 ? row('Frames', `${info.frames} (animated)`) : null,
  );
  const parts = [general];

  if (gps) {
    parts.push(h('div', { class: `gps-chip ${stripping ? '' : 'gps-chip-danger'}` }, icon('pin', 16),
      h('span', {}, stripping ? 'Location · removed on export' : 'Location · kept on export'),
      h('span', { class: 'muted gps-coords' }, `${fmtNum(gps.lat, 5)}, ${fmtNum(gps.lon, 5)}`)));
  }

  if (cam && Object.values(cam).some((v) => v != null && v !== '')) {
    const shot = [
      cam.exposure ? `${cam.exposure} s` : null,
      cam.f_number ? `ƒ/${fmtNum(cam.f_number, 1)}` : null,
      cam.iso ? `ISO ${cam.iso}` : null,
      cam.focal_length_mm ? `${fmtNum(cam.focal_length_mm, 1)} mm` : null,
    ].filter(Boolean).join(' · ');
    parts.push(h('h3', { class: 'group-title' }, 'Camera'), h('dl', { class: 'kv-list' },
      row('Camera', [cam.make, cam.model].filter(Boolean).join(' ')),
      row('Lens', cam.lens),
      row('Taken', cam.datetime ? String(cam.datetime).replace(/^(\d{4}):(\d{2}):(\d{2})/, '$1-$2-$3') : null),
      row('Exposure', shot)));
  }

  const exif = info.exif && typeof info.exif === 'object' ? Object.entries(info.exif) : [];
  if (complete && exif.length) {
    parts.push(h('details', { class: 'disclosure' },
      h('summary', {}, `All EXIF tags (${exif.length})`),
      h('div', { class: 'table-wrap' }, h('table', { class: 'exif-table' },
        h('caption', { class: 'sr-only' }, 'All EXIF tags'),
        h('tbody', {}, exif.sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => h('tr', {}, h('th', { scope: 'row' }, k), h('td', {}, exifValue(v)))))))));
  } else if (complete) {
    parts.push(h('p', { class: 'muted' }, 'No EXIF data.'));
  } else {
    parts.push(h('p', { class: 'muted' }, 'Loading…'));
  }
  body.replaceChildren(...parts);
}
