// node --test static/tests/*.test.mjs — HTTP client helpers (no DOM needed).
import test from 'node:test';
import assert from 'node:assert/strict';
import { retryAfterSeconds, imageRetryDelay, filenameFromDisposition } from '../js/api.js';

test('API-5: Retry-After is honoured within 1–30 s, with a 5 s default', () => {
  assert.equal(retryAfterSeconds('5'), 5);
  assert.equal(retryAfterSeconds('0.2'), 1);
  assert.equal(retryAfterSeconds('600'), 30);
  assert.equal(retryAfterSeconds(null), 5);
  assert.equal(retryAfterSeconds('soon'), 5);
});

test('API-5: an image that failed because the server was busy is retried; other failures are final', async () => {
  const real = globalThis.fetch;
  try {
    const reply = (status, headers = {}) => async () => ({ ok: status < 400, status, headers: new Map(Object.entries(headers)) });
    globalThis.fetch = reply(503, { 'Retry-After': '3' });
    assert.equal(await imageRetryDelay('/api/files/x/preview?max=240'), 3000);
    globalThis.fetch = reply(200);
    assert.equal(await imageRetryDelay('/api/files/x/preview?max=240'), 0);
    globalThis.fetch = reply(404);
    assert.equal(await imageRetryDelay('/api/files/x/preview?max=240'), null);
    globalThis.fetch = async () => { throw new TypeError('offline'); };
    assert.equal(await imageRetryDelay('/api/files/x/preview?max=240'), null);
  } finally {
    globalThis.fetch = real;
  }
});

test('Content-Disposition: RFC 5987 filename* wins over the ASCII fallback', () => {
  assert.equal(filenameFromDisposition("attachment; filename=\"a.jpg\"; filename*=UTF-8''%C3%A9t%C3%A9.jpg"), 'été.jpg');
  assert.equal(filenameFromDisposition('attachment; filename="heyp-2-files.zip"'), 'heyp-2-files.zip');
});
