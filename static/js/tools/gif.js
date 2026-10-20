// Make GIF: the photos in strip order become the frames of an animated GIF (or WebP). Re-order them here or in
// the strip, set the frame time and looping, preview it as a flip book, then render it on the server as a job.

import { h, append, num, fmtNum } from '../dom.js';
import { state, defaults, toolEntry } from '../state.js';
import { toolHead, group, segmented, toggle, numberField, quickChips, note } from '../controls.js';
import { icon } from '../icons.js';
import { fitOptions, enumOf } from '../schema.js';
import { jobCard, isRunning } from '../jobui.js';

export const GIF_TOOL = 'img.make_gif';
const DELAYS = [100, 250, 500, 1000, 2000];
const SIZES = [0, 1080, 720, 480, 320];
const MIN_DELAY = 20; // ms — browsers clamp faster GIF frames to 100 ms anyway
const MAX_DELAY = 120_000;

let r = {};
let player = null; // {timer, i}

/** The photos that become frames, in strip order. */
export const gifFrames = () => state.files.filter((f) => !state.gif.skip.includes(f.id));

/** Options for the img.make_gif job, fitted to the server's schema. */
export function gifOptions(schema, g, frames) {
  const delay = Math.min(MAX_DELAY, Math.max(MIN_DELAY, Math.round(num(g.delay) || 500)));
  return fitOptions(schema, [
    { names: ['delay_ms', 'delay', 'duration_ms', 'frame_ms', 'duration'], value: delay, unit: 'ms' },
    { names: ['loop'], value: schemaLoop(schema, g.loop) },
    { names: ['size', 'max_size', 'max_side', 'width'], value: g.size > 0 ? g.size : sizeDefault(schema) },
    { names: ['format'], value: g.format },
    { names: ['order', 'frames'], value: frames.map((_, i) => i) },
  ]);
}

function schemaLoop(schema, loop) {
  const t = schema?.properties?.loop?.type;
  if (t === 'integer') return loop ? 0 : 1; // GIF loop count: 0 = forever, 1 = play once
  return Boolean(loop);
}

/** "Largest frame" → the schema's default (0 / null) when sizes are free numbers. */
function sizeDefault(schema) {
  const p = schema?.properties?.size || schema?.properties?.max_size || schema?.properties?.max_side;
  if (!p) return undefined;
  return p.default ?? 0;
}

function stopPlayer() {
  if (player) clearInterval(player.timer);
  player = null;
}

export default {
  id: 'gif',
  title: 'Make GIF',
  icon: 'gif',
  kicker: 'Moving pictures',
  jobTool: GIF_TOOL,

  isActive: () => false,
  reset() {
    state.gif = defaults.gif();
  },

  render(root, app) {
    stopPlayer();
    r = {};
    const g = state.gif;
    const tool = toolEntry(GIF_TOOL);
    append(root, toolHead('Make GIF', 'Turn your photos into an animated GIF or WebP — one frame per photo, in strip order.', { onReset: () => app.resetTool(this), active: false }));
    if (state.tools && !tool) {
      append(root, note('This server doesn’t offer Make GIF.', 'warn'));
      return;
    }
    if (tool && tool.available === false) {
      append(root, note(tool.reason || 'Make GIF is unavailable right now.', 'warn'));
      return;
    }
    const frames = gifFrames();
    const min = tool?.min_files || 2;
    const max = tool?.max_files || 0;

    // frames list
    r.list = h('ol', { class: 'frame-list', 'aria-label': 'Frames in order' });
    state.files.forEach((f, i) => {
      const on = !g.skip.includes(f.id);
      r.list.append(h('li', { class: `frame-row${on ? '' : ' is-off'}`, draggable: 'true', dataset: { id: f.id },
        ondragstart: (e) => { e.dataTransfer.setData('text/x-heyp-frame', f.id); e.dataTransfer.effectAllowed = 'move'; },
        ondragover: (e) => { if ([...e.dataTransfer.types].includes('text/x-heyp-frame')) { e.preventDefault(); e.currentTarget.classList.add('is-over'); } },
        ondragleave: (e) => e.currentTarget.classList.remove('is-over'),
        ondrop: (e) => {
          e.preventDefault();
          e.currentTarget.classList.remove('is-over');
          const id = e.dataTransfer.getData('text/x-heyp-frame');
          if (id && id !== f.id) app.moveFileTo(id, state.files.findIndex((x) => x.id === f.id));
        },
      },
      h('span', { class: 'frame-no', 'aria-hidden': 'true' }, String(i + 1).padStart(2, '0')),
      h('img', { class: 'frame-thumb', src: app.thumbUrl(f.id), alt: '', loading: 'lazy', decoding: 'async' }),
      h('label', { class: 'frame-on' },
        h('input', { type: 'checkbox', checked: on, onchange: (e) => { g.skip = e.target.checked ? g.skip.filter((x) => x !== f.id) : [...g.skip, f.id]; app.changed({ panel: true, preview: false }); } }),
        h('span', { class: 'frame-name', title: f.name }, f.name)),
      h('button', { type: 'button', class: 'icon-btn layer-btn', 'aria-label': `Move ${f.name} earlier`, title: 'Earlier', disabled: i === 0, onclick: () => app.moveFileTo(f.id, i - 1) }, icon('arrowUp', 14)),
      h('button', { type: 'button', class: 'icon-btn layer-btn', 'aria-label': `Move ${f.name} later`, title: 'Later', disabled: i === state.files.length - 1, onclick: () => app.moveFileTo(f.id, i + 1) }, icon('arrowDown', 14))));
    });
    append(root, group(`Frames · ${frames.length}`, state.files.length ? r.list : note('Add the photos you want to animate — at least two.', 'info'),
      state.files.length ? h('p', { class: 'hint' }, 'Drag rows or use the arrows to change the order. Untick a photo to leave it out.') : null));

    // flip-book preview
    if (frames.length >= 2) {
      r.player = h('img', { class: 'flipbook-img', alt: 'Animation preview', src: app.thumbUrl(frames[0].id, 480) });
      r.playBtn = h('button', { type: 'button', class: 'btn btn-icon-text', onclick: () => this.togglePlay(app) }, icon('play', 16), h('span', {}, 'Play preview'));
      append(root, group('Preview', h('div', { class: 'flipbook' }, r.player), r.playBtn));
    }

    // timing & output
    r.delay = numberField({ label: 'Each frame shows for', value: fmtNum(g.delay / 1000, 3), suffix: 's', step: 0.05, min: MIN_DELAY / 1000, max: MAX_DELAY / 1000, onChange: (v) => { g.delay = v > 0 ? Math.round(v * 1000) : g.delay; app.changed({ preview: false }); this.restartPlayer(app); } });
    const chips = quickChips(DELAYS.map((ms) => ({ value: ms, label: `${fmtNum(ms / 1000, 2)} s`, active: g.delay === ms })), (ms) => { g.delay = ms; r.delay.input.value = fmtNum(ms / 1000, 3); app.changed({ preview: false }); this.restartPlayer(app); }, { label: 'Common frame times' });
    const loop = toggle({ label: 'Loop forever', checked: g.loop, hint: 'Off plays the animation once.', onChange: (v) => { g.loop = v; app.changed({ preview: false }); } });
    const sizes = enumOf(tool?.options_schema, ['size', 'max_size', 'max_side']) || SIZES;
    const size = segmented({
      label: 'Size (longest side)', cols: Math.min(5, sizes.length), size: 'sm', value: String(g.size),
      options: sizes.map((s) => ({ value: String(s), label: s ? `${s}` : 'Original' })),
      onChange: (v) => { g.size = Number(v); app.changed({ preview: false }); },
    });
    const fmts = enumOf(tool?.options_schema, ['format']) || ['gif', 'webp'];
    const format = segmented({
      label: 'Format', cols: 2, value: g.format,
      options: fmts.map((f) => ({ value: f, label: f === 'webp' ? 'Animated WebP' : f.toUpperCase() })),
      onChange: (v) => { g.format = v; app.changed({ preview: false }); },
    });
    append(root, group('Timing', r.delay, chips, loop), group('Output', size, format,
      h('p', { class: 'hint' }, 'WebP is far smaller; GIF plays everywhere. Frames of different sizes are fitted onto the first frame’s canvas.')));

    if (app.hasEdits()) {
      // UIH-04: the frames get the edits (crops, captions, blur…) unless switched off; the note says which it is
      if (app.canApplyEdits?.()) {
        append(root, group('Edits', toggle({
          label: 'Apply my edits to the frames', checked: g.applyEdits !== false,
          hint: 'Crops, captions, blurred areas and the other sections’ edits are applied to each frame first. Off uses the photos as uploaded.',
          onChange: (v) => { g.applyEdits = v; app.changed({ panel: true, preview: false }); },
        })));
        if (g.applyEdits === false) append(root, note('The frames are the photos as uploaded: edits from the other sections — including blurred areas — are not in the GIF.', 'warn'));
      } else {
        append(root, note('This server can’t apply edits to GIF frames: the frames are the photos as uploaded. To animate edited photos, download them, add the downloaded files with “Add photos”, then make the GIF.', 'warn'));
      }
    }

    // run
    const why = () => {
      if (!tool && !state.tools) return 'Checking what this server offers…';
      if (frames.length < min) return `Pick at least ${min} photos.`;
      if (max && frames.length > max) return `At most ${max} frames — untick ${frames.length - max}.`;
      return null;
    };
    append(root, jobCard('gif', {
      startLabel: `Make ${g.format === 'webp' ? 'WebP' : 'GIF'} · ${frames.length} frame${frames.length === 1 ? '' : 's'}`,
      startIcon: 'gif',
      disabled: why,
      onStart: () => {
        stopPlayer();
        const ids = gifFrames().map((f) => f.id);
        const frames = gifFrames();
        if (app.makeGif) app.makeGif(frames, gifOptions(tool?.options_schema, state.gif, frames));
        else app.runTool('gif', GIF_TOOL, ids, gifOptions(tool?.options_schema, state.gif, frames), { kind: 'image', ext: state.gif.format });
      },
      renderResult: (res) => app.resultView(res, { kind: 'image', ext: state.gif.format, source: 'gif' }),
    }));
    if (isRunning('gif')) append(root, h('p', { class: 'hint' }, 'You can keep working while it renders.'));
  },

  togglePlay(app) {
    if (player) {
      stopPlayer();
      r.playBtn?.replaceChildren(icon('play', 16), h('span', {}, 'Play preview'));
      return;
    }
    const frames = gifFrames();
    if (frames.length < 2 || !r.player) return;
    player = { i: 0, timer: 0 };
    const tick = () => {
      if (!r.player?.isConnected) { stopPlayer(); return; }
      player.i = (player.i + 1) % frames.length;
      if (!state.gif.loop && player.i === 0) {
        this.togglePlay(app);
        return;
      }
      r.player.src = app.thumbUrl(frames[player.i].id, 480);
    };
    // browsers show GIF frames faster than 20 ms at 100 ms; mirror that so the preview matches
    const ms = state.gif.delay < MIN_DELAY ? 100 : Math.max(MIN_DELAY, state.gif.delay);
    player.timer = setInterval(tick, ms);
    r.playBtn?.replaceChildren(icon('pause', 16), h('span', {}, 'Pause preview'));
  },

  restartPlayer(app) {
    if (!player) return;
    stopPlayer();
    this.togglePlay(app);
  },

  leave() {
    stopPlayer();
  },
};
