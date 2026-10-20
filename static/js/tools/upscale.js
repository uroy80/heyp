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
  kicker: 'Bigger & sharper',

  isActive: () => upscaleActive(),
  reset() {
    state.upscale = { ...defaults.upscale(), loupe: state.upscale.loupe };
  },

  render(root, app) {
    r = {};
    const u = state.upscale;
    append(root, toolHead('Upscale', 'Enlarge photos 2× or 4× with an AI model that adds detail instead of blur. It runs when you download.', { onReset: () => app.resetTool(this), active: this.isActive() }));
    if (state.features.upscale === false) {
      append(root, note('The upscale model isn’t installed on this server.', 'warn'));
      return;
    }
    if (passportActive()) {
      append(root, note('Passport photos have a fixed size, so Upscale is off while Passport framing is on.', 'warn'));
      return;
    }
    r.factor = segmented({
      label: 'Enlarge', cols: 3, value: String(u.factor), size: 'lg',
      options: [{ value: '0', label: 'Off' }, { value: '2', label: '2×' }, { value: '4', label: '4×' }],
      onChange: (v) => { u.factor = Number(v); app.changed({ panel: true }); },
    });
    r.model = segmented({
      label: 'Model', cols: 2, value: u.model,
      options: [{ value: 'balanced', label: 'Balanced' }, { value: 'strong', label: 'Strong' }],
      onChange: (v) => { u.model = v; app.changed(); },
    });
    r.modelHint = h('p', { class: 'hint' });
    r.plan = h('div', { class: 'readout', 'aria-live': 'polite' });
    r.limit = note('', 'warn');
    r.before = h('div', { class: 'loupe-pane loupe-before', role: 'img', 'aria-label': 'Before: the square, enlarged by the browser' });
    r.afterImg = h('img', { alt: 'After: the square, upscaled by the model', decoding: 'async' });
    r.after = h('div', { class: 'loupe-pane loupe-after' }, r.afterImg, h('span', { class: 'loupe-wait', 'aria-hidden': 'true' }));
    r.loupeState = h('p', { class: 'hint loupe-state', 'aria-live': 'polite' });
    r.detail = group('Detail check',
      h('div', { class: 'loupe' },
        h('figure', {}, r.before, h('figcaption', {}, 'Before')),
        h('figure', {}, r.after, h('figcaption', {}, 'After'))),
      r.loupeState,
      h('p', { class: 'hint' }, 'Drag the square on the photo to check another spot.'));
    append(root,
      group(null, r.factor),
      group(null, r.model, r.modelHint),
      r.plan, r.limit, r.detail,
      h('p', { class: 'hint' }, `Upscaling is heavy work: a large photo can take several minutes, depending on the server. ${app.canApplyEdits?.() ? 'Download runs it on the server and shows the progress on the button.' : 'Download waits until the file is ready.'}`));
    this.sync(app);
  },

  sync(app) {
    if (!r.plan) return;
    const u = state.upscale;
    r.factor.set(String(u.factor));
    r.model.set(u.model);
    r.modelHint.textContent = u.model === 'strong'
      ? 'Strong: the cleanest edges; removes JPEG blocks and noise, smooths fine texture.'
      : 'Balanced: keeps more natural texture (hair, fabric, foliage). Best for most photos.';
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
        ? `This photo is ${fmtNum(plan.inMp, 1)} MP — Upscale takes photos up to ${fmtNum(plan.maxInMp, 1)} MP on this server. Crop or resize it first.`
        : `${u.factor}× would make ${fmtNum(plan.mp, 1)} MP — over this server’s ${fmtNum(plan.maxMp, 0)} MP limit. Choose 2×, crop, or resize the photo first.`;
    }
    r.detail.hidden = !(u.factor > 1);
    const view = app.loupeView();
    if (!view) {
      r.loupeState.textContent = app.hasFiles() ? '' : 'Add a photo to check the detail.';
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
    r.loupeState.textContent = view.error ? `Detail check failed: ${view.error}` : view.pending ? 'Rendering the detail…' : '';
  },
};
