// Job progress + result UI shared by the job tools (Make GIF, HTML → image) and export: a run registry that
// survives panel re-renders, the progress card (queued / running with SSE progress / cancel), result cards
// with downloads, and the "Continue with" menu.

import { h, fmtBytes } from './dom.js';
import { icon } from './icons.js';
import { jobStatusLine } from './jobs.js';

const runs = new Map(); // key → run state
const listeners = new Map(); // key → Set<fn>

/** Current run for `key`: {phase: idle|queued|running|done|error, progress, message, position, result, error}. */
export function getRun(key) {
  return runs.get(key) || { phase: 'idle' };
}

function emit(key) {
  for (const fn of [...(listeners.get(key) || [])]) fn(getRun(key));
}

export function subscribe(key, fn) {
  if (!listeners.has(key)) listeners.set(key, new Set());
  listeners.get(key).add(fn);
  return () => listeners.get(key)?.delete(fn);
}

export function isRunning(key) {
  const p = getRun(key).phase;
  return p === 'queued' || p === 'running';
}

/** Forget a finished run (e.g. its result was used). */
export function clearRun(key) {
  if (isRunning(key)) return;
  runs.delete(key);
  emit(key);
}

/**
 * Start a run. `exec({signal, onQueued, onProgress})` performs the work and resolves the result.
 * Resolves the result (or null when it failed / was cancelled — the error is kept on the run).
 */
export async function startRun(key, exec) {
  if (isRunning(key)) return null;
  const ctrl = new AbortController();
  const run = { phase: 'queued', progress: 0, message: '', position: null, ctrl, startedAt: Date.now() };
  runs.set(key, run);
  emit(key);
  const update = (patch) => {
    if (runs.get(key) !== run) return;
    Object.assign(run, patch);
    emit(key);
  };
  try {
    const result = await exec({
      signal: ctrl.signal,
      onQueued: (position) => update({ phase: 'queued', position }),
      onProgress: (progress, message) => update({ phase: 'running', progress, message: message || run.message }),
    });
    update({ phase: 'done', result, ctrl: null });
    return result;
  } catch (e) {
    const cancelled = e?.name === 'AbortError' || e?.cancelled;
    update({ phase: cancelled ? 'idle' : 'error', error: cancelled ? null : e, ctrl: null });
    return null;
  }
}

export function cancelRun(key) {
  getRun(key).ctrl?.abort();
}

/** Progress block for a running job: bar (determinate once progress > 0), status line, Cancel. */
export function progressBlock(run, { onCancel } = {}) {
  const pct = Math.round(Math.min(1, run.progress || 0) * 100);
  const bar = h('div', { class: `job-bar${pct > 0 ? '' : ' is-indeterminate'}`, role: 'progressbar', 'aria-label': 'Progress', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': pct > 0 ? String(pct) : null },
    h('span', { class: 'job-bar-fill', style: pct > 0 ? { transform: `scaleX(${pct / 100})` } : null }));
  return h('div', { class: 'job-progress' },
    h('div', { class: 'job-line' },
      h('span', { class: 'spinner', 'aria-hidden': 'true' }),
      h('span', { class: 'job-status', role: 'status', 'aria-live': 'polite' }, jobStatusLine({ phase: run.phase, position: run.position, progress: run.progress, message: run.message })),
      onCancel ? h('button', { type: 'button', class: 'link-btn job-cancel', onclick: onCancel }, 'Cancel') : null),
    bar);
}

/** Error line for a failed run (with "Try again in N s" for rate limits). */
export function errorBlock(err) {
  const wait = err?.retryAfter && (err.status === 429 || err.status === 503) ? ` Try again in ${Math.ceil(err.retryAfter)} s.` : '';
  return h('p', { class: 'field-error job-error', role: 'alert' }, icon('alert', 14), h('span', {}, `${err?.detail || err?.message || 'Something went wrong.'}${wait}`));
}

/** Download URL of a stored result file. */
export const downloadUrl = (id) => `/api/files/${encodeURIComponent(id)}/download`;

/** One result file: picture (animated results play), name, size, Download. */
export function resultFile(info, { previewSrc } = {}) {
  const isImage = !info.kind || info.kind === 'image';
  const animated = isImage && (info.frames > 1 || /gif|webp/.test(info.format || ''));
  const src = isImage ? (animated ? downloadUrl(info.id) : previewSrc || `/api/files/${encodeURIComponent(info.id)}/preview?max=480`) : null;
  const dims = info.width && info.height ? `${info.width}×${info.height}` : '';
  return h('figure', { class: 'result-file' },
    src ? h('div', { class: 'result-pic' }, h('img', {
      src, alt: info.name || 'Result', loading: 'lazy', decoding: 'async',
      // an animation that can't be shown as is (or a busy server) falls back to the still preview once
      onerror: (e) => {
        const img = e.currentTarget;
        const still = `/api/files/${encodeURIComponent(info.id)}/preview?max=480`;
        if (!img.src.endsWith(still)) img.src = still;
      },
    })) : null,
    h('figcaption', { class: 'result-cap' },
      h('span', { class: 'result-name', title: info.name }, info.name || 'result'),
      h('span', { class: 'result-facts' }, [dims, fmtBytes(info.size_bytes)].filter((x) => x && x !== '—').join(' · '))),
    h('a', { class: 'btn btn-icon-text result-dl', href: downloadUrl(info.id), download: info.name || '' }, icon('download', 16), h('span', {}, 'Download')));
}

/**
 * "Continue with" menu. `targets` → {heyp: [{id, title}], sibling: url | null} (continueTargets output);
 * `onPick(entry)` for a heyp section. heypdf is a plain link: it opens the other site, and no file goes with it.
 * A <details> disclosure, so it works with keyboard and screen readers without extra scripting.
 */
export function continueMenu({ targets, onPick, label = 'Continue with', up = false }) {
  if (!targets.heyp.length && !targets.sibling) return null;
  const item = (t) => h('li', {}, h('button', {
    type: 'button', class: 'cw-item',
    onclick: (e) => {
      e.currentTarget.closest('details').open = false;
      onPick(t);
    },
  }, h('span', { class: 'cw-title' }, t.title)));
  const groups = [];
  if (targets.heyp.length) groups.push(h('div', { class: 'cw-group' }, h('p', { class: 'cw-head' }, 'In heyp'), h('ul', {}, targets.heyp.map(item))));
  if (targets.sibling) {
    groups.push(h('div', { class: 'cw-group' }, h('p', { class: 'cw-head' }, 'Elsewhere'),
      h('ul', {}, h('li', {}, h('a', { class: 'cw-item', href: targets.sibling, target: '_blank', rel: 'noopener' },
        h('span', { class: 'cw-title' }, 'Open heypdf'), h('span', { class: 'cw-cat' }, 'PDF tools')))),
      h('p', { class: 'cw-note' }, 'Download first, then add the file there.')));
  }
  const d = h('details', { class: `cw${up ? ' cw-up' : ''}` },
    h('summary', { class: 'btn btn-icon-text cw-btn' }, h('span', {}, label), icon('chevron', 14)),
    h('div', { class: 'cw-pop' }, groups));
  // close when focus or a click goes elsewhere
  d.addEventListener('toggle', () => {
    if (!d.open) return;
    const away = (e) => {
      if (!d.contains(e.target)) {
        d.open = false;
        document.removeEventListener('pointerdown', away, true);
        document.removeEventListener('focusin', away, true);
      }
    };
    document.addEventListener('pointerdown', away, true);
    document.addEventListener('focusin', away, true);
  });
  d.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && d.open) {
      e.stopPropagation();
      d.open = false;
      d.querySelector('summary').focus();
    }
  });
  return d;
}

/**
 * A job card bound to run `key`: renders idle (the start button), progress, error or result, and re-renders
 * itself on run updates while it is in the document.
 */
export function jobCard(key, { startLabel, startIcon = 'sparkle', onStart, disabled = () => null, renderResult, onCancel }) {
  const root = h('div', { class: 'job-card' });
  const paint = () => {
    const run = getRun(key);
    const why = disabled();
    const busy = run.phase === 'queued' || run.phase === 'running';
    const prog = root.querySelector('.job-progress');
    if (busy && prog && root.dataset.phase === 'busy') {
      // progress tick: patch the line and bar in place (no re-announcement, focus stays on Cancel)
      const fresh = progressBlock(run);
      const st = prog.querySelector('.job-status');
      const text = fresh.querySelector('.job-status').textContent;
      if (st.textContent !== text) st.textContent = text;
      prog.querySelector('.job-bar').replaceWith(fresh.querySelector('.job-bar'));
      return;
    }
    const finished = root.dataset.phase === 'busy' && (run.phase === 'done' || run.phase === 'error');
    root.dataset.phase = busy ? 'busy' : run.phase;
    const start = h('button', {
      type: 'button', class: `btn btn-primary btn-block${busy ? ' is-busy' : ''}`, disabled: Boolean(why) || busy, 'aria-busy': busy ? 'true' : 'false',
      onclick: () => onStart(),
    }, h('span', { class: 'btn-ico' }, busy ? h('span', { class: 'spinner' }) : icon(startIcon, 18)), h('span', {}, busy ? 'Working…' : startLabel));
    const parts = [start];
    if (why && !busy) parts.push(h('p', { class: 'hint job-why' }, why));
    if (busy) parts.push(progressBlock(run, { onCancel: onCancel || (() => cancelRun(key)) }));
    if (run.phase === 'error') parts.push(errorBlock(run.error));
    if (run.phase === 'done' && run.result) {
      const res = renderResult(run.result);
      if (res) parts.push(res);
    }
    root.replaceChildren(...parts);
    // the outcome appears below the button: bring it into view (the panel may have been scrolled meanwhile)
    if (finished && root.isConnected) parts.at(-1)?.scrollIntoView?.({ block: 'nearest' });
  };
  const off = subscribe(key, () => {
    if (!root.isConnected && root.dataset.mounted) {
      off();
      return;
    }
    paint();
  });
  paint();
  queueMicrotask(() => { root.dataset.mounted = '1'; });
  root.repaint = paint;
  return root;
}
