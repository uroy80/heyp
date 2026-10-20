// heyp v2 UI: job client (SSE + polling), hosted-mode copy, "Continue with" targets and the heypdf link.
import test from 'node:test';
import assert from 'node:assert/strict';

import { parseSSE, createJobClient, jobStatusLine, JobError } from '../js/jobs.js';
import { copyFor, safeUrl, siblingUrl, DEFAULT_SOURCE_URL } from '../js/config.js';
import { continueTargets, outputKind } from '../js/continue.js';

const ID = 'a'.repeat(32);

// ── SSE parsing ─────────────────────────────────────────────────────────────

test('SSE: complete frames parsed, comments skipped, the unfinished tail kept for the next chunk', () => {
  const [events, rest] = parseSSE(': keep-alive\n\nevent: progress\ndata: {"progress":0.5,"message":"Half"}\n\nevent: text\ndata: plain\ndata: two\n\nevent: done\ndata: {"res');
  assert.deepEqual(events, [{ event: 'progress', data: { progress: 0.5, message: 'Half' } }, { event: 'text', data: 'plain\ntwo' }]);
  assert.equal(rest, 'event: done\ndata: {"res');
  const [again] = parseSSE(`${rest}ult":{"files":[]}}\r\n\r\n`);
  assert.deepEqual(again, [{ event: 'done', data: { result: { files: [] } } }]);
  assert.deepEqual(parseSSE('')[0], []);
});

// ── job client ──────────────────────────────────────────────────────────────

const json = (status, body, headers = {}) => ({
  ok: status >= 200 && status < 300, status, statusText: '', headers: { get: (k) => headers[k.toLowerCase()] ?? headers[k] ?? null },
  json: async () => body,
});

function sseResponse(chunks) {
  const enc = new TextEncoder();
  let i = 0;
  return {
    ok: true, status: 200, headers: { get: (k) => (k.toLowerCase() === 'content-type' ? 'text/event-stream; charset=utf-8' : null) },
    body: {
      getReader: () => ({
        read: async () => (i < chunks.length ? { value: enc.encode(chunks[i++]), done: false } : { value: undefined, done: true }),
        releaseLock() {},
      }),
    },
  };
}

test('jobs: POST, follow the event stream to done, then read the record for warnings', async () => {
  const calls = [];
  const fetchImpl = async (url, opts = {}) => {
    calls.push(`${opts.method || 'GET'} ${url}`);
    if (url === '/api/jobs' && opts.method === 'POST') {
      assert.deepEqual(JSON.parse(opts.body), { tool: 'img.make_gif', file_ids: [ID], options: { delay_ms: 100 } });
      return json(202, { job_id: 'j1', status: 'queued', position: 2 });
    }
    if (url.endsWith('/events')) return sseResponse(['event: progress\ndata: {"progress":0.25,"message":"Frame 1"}\n\n', 'event: done\ndata: {"result":{"files":[{"id":"x"}]}}\n\n']);
    if (url === '/api/jobs/j1') return json(200, { id: 'j1', status: 'done', warnings: ['w1'], result: { files: [{ id: 'x' }], zip: null, data: {} } });
    throw new Error(`unexpected ${url}`);
  };
  const jobs = createJobClient({ fetchImpl, sleep: async () => {} });
  const seen = [];
  const res = await jobs.run({ tool: 'img.make_gif', file_ids: [ID], options: { delay_ms: 100 } }, {
    onQueued: (p) => seen.push(`queued ${p}`), onProgress: (p, m) => seen.push(`progress ${p} ${m}`), onJob: (id) => seen.push(`job ${id}`),
  });
  assert.deepEqual(res.files, [{ id: 'x' }]);
  assert.deepEqual(res.warnings, ['w1']);
  assert.deepEqual(seen, ['job j1', 'queued 2', 'progress 0.25 Frame 1']);
  assert.deepEqual(calls, ['POST /api/jobs', 'GET /api/jobs/j1/events', 'GET /api/jobs/j1']);
});

test('jobs: without a usable stream the client polls until the job ends; failures keep their status', async () => {
  const states = [{ status: 'queued', position: 1 }, { status: 'running', progress: 0.5, message: 'Half' }, { status: 'failed', error: { status: 422, detail: 'Too many frames' } }];
  const fetchImpl = async (url, opts = {}) => {
    if (opts.method === 'POST') return json(202, { job_id: 'j2' });
    if (url.endsWith('/events')) return json(404, { detail: 'no stream' });
    return json(200, { id: 'j2', ...states.shift() });
  };
  const jobs = createJobClient({ fetchImpl, sleep: async () => {} });
  const seen = [];
  await assert.rejects(jobs.run({ tool: 't', file_ids: [], options: {} }, { onProgress: (p, m) => seen.push(`${p} ${m}`) }), (e) => {
    assert.ok(e instanceof JobError);
    assert.equal(e.status, 422);
    assert.equal(e.detail, 'Too many frames');
    return true;
  });
  assert.deepEqual(seen, ['0.5 Half']);
});

test('jobs: refused requests carry the server detail, Retry-After and the encrypted-file hint', async () => {
  const fetchImpl = async () => json(429, { detail: 'Slow down' }, { 'retry-after': '7' });
  await assert.rejects(createJobClient({ fetchImpl }).run({ tool: 't', file_ids: [], options: {} }), (e) => e.status === 429 && e.retryAfter === 7 && e.detail === 'Slow down');
  const pw = async () => json(422, { detail: 'This PDF is encrypted', needs_password: true });
  await assert.rejects(createJobClient({ fetchImpl: pw }).run({ tool: 't', file_ids: [], options: {} }), (e) => e.needsPassword === true);
  const offline = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(createJobClient({ fetchImpl: offline }).run({ tool: 't', file_ids: [], options: {} }), (e) => e.status === 0);
  const noId = async () => json(202, {});
  await assert.rejects(createJobClient({ fetchImpl: noId }).run({ tool: 't', file_ids: [], options: {} }), (e) => e.status === 500);
});

test('jobs: aborting cancels the job on the server', async () => {
  const ctrl = new AbortController();
  const calls = [];
  const fetchImpl = async (url, opts = {}) => {
    calls.push(`${opts.method || 'GET'} ${url}`);
    if (opts.method === 'POST') return json(202, { job_id: 'j3' });
    if (url.endsWith('/events')) return json(404, {});
    if (opts.method === 'DELETE') return json(200, {});
    ctrl.abort();
    return json(200, { status: 'running', progress: 0.1 });
  };
  await assert.rejects(createJobClient({ fetchImpl, sleep: async () => {} }).run({ tool: 't', file_ids: [], options: {} }, { signal: ctrl.signal }), (e) => e.name === 'AbortError');
  assert.ok(calls.includes('DELETE /api/jobs/j3'));
});

test('job status line', () => {
  assert.equal(jobStatusLine({ phase: 'queued', position: 3 }), 'Waiting — 3 ahead of you');
  assert.equal(jobStatusLine({ phase: 'queued', position: 0 }), 'Starting…');
  assert.equal(jobStatusLine({ phase: 'running', progress: 0.426, message: 'Rendering' }), 'Rendering 43%');
  assert.equal(jobStatusLine({ phase: 'running', progress: 0 }), 'Working…');
});

// ── hosted-mode copy ────────────────────────────────────────────────────────

test('copy: no mode claims "local", and no mode prints a deletion time', () => {
  const local = copyFor(null);
  assert.equal(local.mode, 'local');
  assert.ok(!/local only|this machine|container|never|third part/i.test(JSON.stringify(local)), JSON.stringify(local));
  const hosted = copyFor({ mode: 'hosted', ttl_minutes: 60, hard_ttl_minutes: 240, source_url: 'https://github.com/uroy80/heyp', version: '2.0.0' });
  assert.equal(hosted.mode, 'hosted');
  const text = JSON.stringify(hosted);
  assert.ok(!/local only|this machine|never the internet|nothing is uploaded/i.test(text), text);
  assert.ok(!/hour|minute|delet|auto-delete/i.test(text), text);
  assert.deepEqual(hosted.footer.map((l) => l.label), ['Privacy', 'Terms', 'Credits', 'Source (AGPL-3.0)']);
  assert.equal(hosted.version, '2.0.0');
});

test('copy: hostile config values never become links or markup', () => {
  const c = copyFor({ mode: 'hosted', source_url: 'javascript:alert(1)', version: '<img src=x onerror=1>', ttl_minutes: -5 });
  assert.equal(c.source, DEFAULT_SOURCE_URL);
  assert.equal(c.version, null);
  assert.equal(c.footer.at(-1).href, DEFAULT_SOURCE_URL);
  assert.equal(safeUrl('/privacy', 'x'), '/privacy');
  assert.equal(safeUrl('https://ok.example/a b', 'x'), 'x');
});

// ── "Continue with" ──────────────────────────────────────────────────────────

test('continue targets: heyp sections that take the output kind and count, and heypdf as a plain link', () => {
  const sections = [{ id: 'compress', title: 'Compress' }, { id: 'gif', title: 'Make GIF', accepts: (k, n) => k === 'image' && n >= 2 }];
  const one = continueTargets({ kind: 'image', count: 1 }, { heypSections: sections, exclude: 'compress' });
  assert.deepEqual(one, { heyp: [], sibling: null });
  const two = continueTargets({ kind: 'image', count: 2 }, { heypSections: sections, sibling: 'https://heypdf.example.org' });
  assert.deepEqual(two, { heyp: [{ id: 'compress', title: 'Compress' }, { id: 'gif', title: 'Make GIF' }], sibling: 'https://heypdf.example.org' });
  const pdf = continueTargets({ kind: 'pdf', count: 1 }, { heypSections: sections, sibling: 'https://heypdf.example.org' });
  assert.deepEqual(pdf, { heyp: [], sibling: 'https://heypdf.example.org' }, 'a combined PDF can only go to heypdf, by hand');
  assert.deepEqual(outputKind('pdf'), { kind: 'pdf', ext: 'pdf' });
  assert.deepEqual(outputKind('jpeg'), { kind: 'image', ext: 'jpg' });
});

test('the sibling link: only an absolute http(s) address from the config, else no link at all', () => {
  assert.equal(siblingUrl({ sibling_url: 'https://heypdf.edgr.in' }), 'https://heypdf.edgr.in');
  assert.equal(siblingUrl({ sibling_url: 'http://localhost:8081' }), 'http://localhost:8081');
  for (const bad of [null, '', '/pdf/', 'javascript:alert(1)', 'https://x.example/"onmouseover=1']) {
    assert.equal(siblingUrl({ sibling_url: bad }), null, String(bad));
  }
  assert.equal(siblingUrl(null), null);
});
