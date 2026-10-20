// Upscale 2× / 4× (AI super-resolution). Too slow for live previews, so it runs on export; the panel shows the
// predicted size and a "detail check" of one small square, rendered by the server at the chosen settings.

import { h, append, fmtNum } from '../dom.js';
import { state, defaults, upscaleActive, upscalePlan, passportActive } from '../state.js';
import { toolHead, group, segmented, note } from '../controls.js';

let r = {};

export default {
  id: 'upscale',
  title: 'Upscale',
  icon: 'upscale',

  isActive: () => upscaleActive(),
  reset() {
    state.upscale = { ...defaults.upscale(), loupe: state.upscale.loupe };
  },

  render(root, app) {
    r = {};
    const u = state.upscale;
    append(root, toolHead('Upscale', 'Runs when you download.', { onReset: () => app.resetTool(this), active: this.isActive() }));
    if (state.features.upscale === false) {
      append(root, note('Not available on this server.', 'warn'));
      return;
    }
    if (passportActive()) {
      append(root, note('Off while Passport is on.', 'warn'));
      return;
    }
    r.factor = segmented({
      label: 'Enlarge', cols: 3, value: String(u.factor), size: 'lg',
      options: [{ value: '0', label: 'Off' }, { value: '2', label: '2×' }, { value: '4', label: '4×' }],
      onChange: (v) => { u.factor = Number(v); app.changed({ panel: true }); },
    });
    r.model = segmented({
      label: 'Model', cols: 2, value: u.model, tip: 'Balanced keeps natural texture (hair, fabric, foliage) — best for most photos. Strong gives the cleanest edges and removes JPEG blocks and noise.',
      options: [{ value: 'balanced', label: 'Balanced' }, { value: 'strong', label: 'Strong' }],
      onChange: (v) => { u.model = v; app.changed(); },
    });
    r.plan = h('div', { class: 'readout', 'aria-live': 'polite' });
    r.limit = note('', 'warn');
    r.before = h('div', { class: 'loupe-pane loupe-before', role: 'img', 'aria-label': 'Before: the square, enlarged by the browser' });
    r.afterImg = h('img', { alt: 'After: the square, upscaled by the model', decoding: 'async' });
    r.after = h('div', { class: 'loupe-pane loupe-after' }, r.afterImg, h('span', { class: 'loupe-wait', 'aria-hidden': 'true' }));
    r.loupeState = h('p', { class: 'hint loupe-state', 'aria-live': 'polite' });
    r.detail = group({ title: 'Detail check', tip: 'Drag the square on the photo to check another spot.' },
      h('div', { class: 'loupe' },
        h('figure', {}, r.before, h('figcaption', {}, 'Before')),
        h('figure', {}, r.after, h('figcaption', {}, 'After'))),
      r.loupeState);
    append(root,
      group(null, r.factor),
      group(null, r.model),
      r.plan, r.limit, r.detail);
    this.sync(app);
  },

  sync(app) {
    if (!r.plan) return;
    const u = state.upscale;
    r.factor.set(String(u.factor));
    r.model.set(u.model);
    const dims = app.upscaleInput();
    const plan = upscalePlan(dims, u.factor);
    r.plan.replaceChildren();
    if (dims && plan) {
      append(r.plan,
        h('div', { class: 'readout-row' }, h('span', { class: 'muted' }, 'Now'), h('span', {}, `${dims.w} × ${dims.h} px`)),
        h('div', { class: 'readout-row' }, h('span', { class: 'muted' }, `After ${u.factor}×`), h('strong', { class: plan.ok ? '' : 'warn' }, `${plan.w} × ${plan.h} px · ${fmtNum(plan.mp, 1)} MP`)));
    }
    r.limit.hidden = !plan || plan.ok;
    if (plan && !plan.ok) {
      r.limit.querySelector('span').textContent = !plan.inOk
        ? `Too large (${fmtNum(plan.inMp, 1)} MP, max ${fmtNum(plan.maxInMp, 1)} MP) — crop or resize first.`
        : `${u.factor}× makes ${fmtNum(plan.mp, 1)} MP (max ${fmtNum(plan.maxMp, 0)}) — choose 2× or make it smaller.`;
    }
    r.detail.hidden = !(u.factor > 1);
    const view = app.loupeView();
    if (!view) {
      r.loupeState.textContent = app.hasFiles() ? '' : 'Add a photo first.';
      return;
    }
    // before: the same square of the photo, enlarged by the browser
    const k = 100 / (view.region.w * 100);
    Object.assign(r.before.style, {
      backgroundImage: `url("${view.beforeSrc}")`,
      backgroundSize: `${k * 100}% ${(100 / (view.region.h * 100)) * 100}%`,
      backgroundPosition: `${view.region.w >= 1 ? 0 : (view.region.x / (1 - view.region.w)) * 100}% ${view.region.h >= 1 ? 0 : (view.region.y / (1 - view.region.h)) * 100}%`,
    });
    if (view.afterSrc && r.afterImg.getAttribute('src') !== view.afterSrc) r.afterImg.src = view.afterSrc;
    r.afterImg.hidden = !view.afterSrc;
    r.after.classList.toggle('is-pending', Boolean(view.pending));
    r.loupeState.textContent = view.error ? `Failed: ${view.error}` : view.pending ? 'Rendering…' : '';
  },
};
