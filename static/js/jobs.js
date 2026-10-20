// Job client for registry tools (contract v2 §2.3): POST /api/jobs, then follow GET /api/jobs/{id}/events
// (Server-Sent Events read with fetch, so errors keep their status) and fall back to polling GET /api/jobs/{id}
// when the stream can't be used (old proxy buffering, a dropped connection). Cancelling DELETEs the job.
// No DOM needed: `fetchImpl`, timers and the poll interval are injectable for the node tests.

import { formatDetail, retryAfterSeconds } from './api.js';

export class JobError extends Error {
  constructor(status, detail, { retryAfter = null, body = null } = {}) {
    super(detail);
    this.name = 'JobError';
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
    this.body = body;
    /** An encrypted input: the UI should ask for its password once (contract §4). */
    this.needsPassword = Boolean(body?.needs_password);
  }
}

const OFFLINE = 'Can’t reach heyp. Check your connection and try again.';
const FINAL = new Set(['done', 'failed', 'cancelled']);

/**
 * Parse an SSE buffer. Returns [events, rest]: complete frames as {event, data} (data JSON-decoded when it
 * parses, else the raw string), and the unfinished tail to prepend to the next chunk. Comments (": ping") are skipped.
 */
export function parseSSE(buffer) {
  const events = [];
  const frames = String(buffer).split(/\r?\n\r?\n/);
  const rest = frames.pop();
  for (const frame of frames) {
    let name = 'message';
    const data = [];
    for (const line of frame.split(/\r?\n/)) {
      if (!line || line.startsWith(':')) continue;
      const i = line.indexOf(':');
      const field = i < 0 ? line : line.slice(0, i);
      const value = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '');
      if (field === 'event') name = value.trim() || 'message';
      else if (field === 'data') data.push(value);
    }
    if (!data.length) continue;
    const raw = data.join('\n');
    let parsed = raw;
    try {
      parsed = JSON.parse(raw);
    } catch {
      /* plain text event */
    }
    events.push({ event: name, data: parsed });
  }
  return [events, rest];
}

async function errorOf(res) {
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* not JSON */
  }
  const fallback = {
    404: 'This tool or job is no longer available.',
    413: 'Too large for this tool.',
    422: 'These files or settings can’t be used with this tool.',
    429: 'Too many requests — wait a moment and try again.',
    503: 'The server is busy or this tool is unavailable right now.',
  }[res.status] || `${res.status} ${res.statusText || 'error'}`;
  const retry = res.status === 429 || res.status === 503 ? retryAfterSeconds(res.headers?.get?.('Retry-After')) : null;
  return new JobError(res.status, formatDetail(body?.detail, fallback), { retryAfter: retry, body });
}

/** A job's terminal record → result, or a thrown JobError. */
function settle(job) {
  if (job.status === 'done') return { ...(job.result || {}), warnings: job.warnings || [] };
  if (job.status === 'cancelled') {
    const e = new JobError(499, 'Cancelled.');
    e.cancelled = true;
    throw e;
  }
  const err = job.error || {};
  throw new JobError(err.status || 500, formatDetail(err.detail, 'The job failed.'), { body: err });
}

export function createJobClient({ fetchImpl = (...a) => globalThis.fetch(...a), sleep = (ms) => new Promise((r) => { setTimeout(r, ms); }), pollMs = [400, 700, 1000, 1500, 2000] } = {}) {
  async function call(method, url, { json, signal } = {}) {
    let res;
    try {
      res = await fetchImpl(url, {
        method,
        headers: json === undefined ? { Accept: 'application/json' } : { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: json === undefined ? undefined : JSON.stringify(json),
        signal,
        cache: 'no-store',
      });
    } catch (e) {
      if (e?.name === 'AbortError') throw e;
      throw new JobError(0, OFFLINE);
    }
    if (!res.ok) throw await errorOf(res);
    return res;
  }

  const get = (id, signal) => call('GET', `/api/jobs/${encodeURIComponent(id)}`, { signal }).then((r) => r.json());

  /** DELETE the job; best effort (it may have finished meanwhile). */
  async function cancel(id) {
    try {
      await fetchImpl(`/api/jobs/${encodeURIComponent(id)}`, { method: 'DELETE', keepalive: true });
    } catch {
      /* the job record expires with its files */
    }
  }

  /**
   * Follow the event stream. Resolves the terminal job record ({status, result | error}) or null when the
   * stream ended / failed before a terminal event (the caller then polls).
   */
  async function stream(id, { signal, onEvent }) {
    let res;
    try {
      res = await fetchImpl(`/api/jobs/${encodeURIComponent(id)}/events`, { headers: { Accept: 'text/event-stream' }, signal, cache: 'no-store' });
    } catch (e) {
      if (e?.name === 'AbortError') throw e;
      return null;
    }
    if (!res.ok || !res.body || !String(res.headers?.get?.('content-type') || '').includes('text/event-stream')) return null;
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const [events, rest] = parseSSE(buf + dec.decode(value, { stream: true }));
        buf = rest;
        for (const ev of events) {
          if (ev.event === 'done') return { status: 'done', result: ev.data?.result ?? ev.data };
          if (ev.event === 'error') return { status: 'failed', error: ev.data };
          if (ev.event === 'cancelled') return { status: 'cancelled' };
          onEvent(ev);
        }
      }
    } catch (e) {
      if (e?.name === 'AbortError') throw e;
      return null;
    } finally {
      try { reader.releaseLock(); } catch { /* stream already closed */ }
    }
    return null;
  }

  /**
   * Run a registry tool: `req` = {tool, file_ids, options}. Callbacks: onQueued(position), onProgress(frac, msg),
   * onText(delta). Resolves {files, zip, data, warnings}; rejects JobError (or AbortError after `signal` aborts,
   * which also cancels the job on the server).
   */
  async function run(req, { signal, onQueued, onProgress, onText, onJob } = {}) {
    const res = await call('POST', '/api/jobs', { json: req, signal });
    const created = await res.json();
    const id = created.job_id || created.id;
    if (!id) throw new JobError(500, 'The server did not start the job.');
    onJob?.(id);
    if (created.position != null) onQueued?.(created.position);
    const onAbort = () => cancel(id);
    signal?.addEventListener?.('abort', onAbort, { once: true });
    try {
      const onEvent = (ev) => {
        const d = ev.data || {};
        if (ev.event === 'progress') onProgress?.(Number(d.progress) || 0, d.message || '');
        else if (ev.event === 'text') onText?.(d.delta ?? '');
        else if (ev.event === 'queued') onQueued?.(d.position ?? null);
      };
      const fin = await stream(id, { signal, onEvent });
      if (fin) {
        // the stream's done event carries the result; the record adds warnings (cheap, optional)
        if (fin.status === 'done') {
          try {
            const full = await get(id, signal);
            if (full?.status === 'done') return settle(full);
          } catch (e) {
            if (e?.name === 'AbortError') throw e;
          }
        }
        return settle(fin);
      }
      // polling fallback
      for (let i = 0; ; i++) {
        if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
        let job;
        try {
          job = await get(id, signal);
        } catch (e) {
          if (e?.name === 'AbortError') throw e;
          if (e?.status === 404) throw new JobError(404, 'The job expired before it finished. Please run it again.');
          if (e?.status === 0 && i < 30) { await sleep(2000); continue; }
          throw e;
        }
        if (FINAL.has(job.status)) return settle(job);
        if (job.status === 'queued' && job.position != null) onQueued?.(job.position);
        if (job.status === 'running') onProgress?.(Number(job.progress) || 0, job.message || '');
        await sleep(pollMs[Math.min(i, pollMs.length - 1)]);
      }
    } finally {
      signal?.removeEventListener?.('abort', onAbort);
    }
  }

  return { run, get, cancel, stream };
}

export const jobs = createJobClient();

/** "Queued — 2 ahead", "Working… 40%", "Rendering page" — one status line for a running job. */
export function jobStatusLine({ phase, position, progress, message }) {
  if (phase === 'queued') return position > 0 ? `Waiting — ${position} ahead of you` : 'Starting…';
  const pct = progress > 0 ? ` ${Math.round(Math.min(1, progress) * 100)}%` : '';
  return `${message || 'Working…'}${pct}`.trim();
}
