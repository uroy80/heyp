// HTTP client for the heyp API (PLAN.md §3). All errors surface as ApiError {status, detail}.

export class ApiError extends Error {
  constructor(status, detail, body = null, retryAfter = null) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.body = body;
    /** Seconds the server asked us to wait before retrying (503 busy), else null. */
    this.retryAfter = retryAfter;
  }
}

const BUSY_WAIT_S = 5;
const BUSY_WAIT_MAX_S = 30;

/** Retry-After in seconds, clamped to 1–30; the default wait when absent or unparsable. */
export function retryAfterSeconds(value) {
  const s = Number(value);
  return Number.isFinite(s) && s > 0 ? Math.min(BUSY_WAIT_MAX_S, Math.max(1, s)) : BUSY_WAIT_S;
}

/**
 * After an image URL failed to load: ms to wait before retrying when the server was only busy
 * (503 + Retry-After) or has recovered meanwhile (0); null when the failure is permanent.
 */
export async function imageRetryDelay(url) {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (res.ok) return 0;
    return res.status === 503 ? retryAfterSeconds(res.headers.get('Retry-After')) * 1000 : null;
  } catch {
    return null;
  }
}

const OFFLINE = 'Can’t reach heyp. Is the container still running?';

/** Turn FastAPI's `detail` (string, or a 422 list of {loc,msg}) into one readable line. */
export function formatDetail(detail, fallback = 'Something went wrong') {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => {
      if (typeof d === 'string') return d;
      const loc = Array.isArray(d?.loc) ? d.loc.filter((p) => p !== 'body').join('.') : '';
      return [loc, d?.msg].filter(Boolean).join(': ');
    }).filter(Boolean);
    if (msgs.length) return msgs.join('; ');
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail);
  return fallback;
}

async function errorFrom(res) {
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* not JSON */
  }
  const fallback = res.status === 404 ? 'File not found — it may have expired' : `${res.status} ${res.statusText || 'error'}`;
  const retry = res.status === 503 ? retryAfterSeconds(res.headers.get('Retry-After')) : null;
  return new ApiError(res.status, formatDetail(body?.detail, fallback), body, retry);
}

async function request(method, url, { json, signal } = {}) {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: json === undefined ? { Accept: 'application/json' } : { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: json === undefined ? undefined : JSON.stringify(json),
      signal,
      cache: 'no-store',
    });
  } catch (e) {
    if (e?.name === 'AbortError') throw e;
    throw new ApiError(0, OFFLINE);
  }
  if (!res.ok) throw await errorFrom(res);
  return res;
}

const getJSON = (url, opts) => request('GET', url, opts).then((r) => r.json());

/** RFC 6266 / 5987: prefer filename*, fall back to filename. */
export function filenameFromDisposition(cd) {
  if (!cd) return null;
  let m = /filename\*\s*=\s*([^;]+)/i.exec(cd);
  if (m) {
    let v = m[1].trim().replace(/^"(.*)"$/, '$1');
    const i = v.indexOf("''");
    if (i >= 0) v = v.slice(i + 2);
    try {
      return safeName(decodeURIComponent(v));
    } catch {
      /* malformed escape → fall through */
    }
  }
  m = /(?:^|;)\s*filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]+))/i.exec(cd);
  if (m) return safeName((m[1] ?? m[2]).replace(/\\(.)/g, '$1').trim());
  return null;
}

function safeName(name) {
  const n = String(name).split(/[\\/]/).pop().replace(/[\u0000-\u001f]/g, '').trim();
  return n || null;
}

function parseWarnings(header) {
  if (!header) return [];
  try {
    const list = JSON.parse(decodeURIComponent(header));
    return Array.isArray(list) ? list.map(String) : [];
  } catch {
    return [];
  }
}

export const api = {
  health: () => getJSON('/api/health'),
  formats: () => getJSON('/api/formats'),
  presets: () => getJSON('/api/presets/passport'),
  fileInfo: (id, opts) => getJSON(`/api/files/${encodeURIComponent(id)}`, opts),
  previewUrl: (id, max = 1600) => `/api/files/${encodeURIComponent(id)}/preview?max=${max}`,

  async deleteFile(id, { keepalive = false } = {}) {
    try {
      await fetch(`/api/files/${encodeURIComponent(id)}`, { method: 'DELETE', keepalive });
    } catch {
      /* best effort: the server TTL cleans up anyway */
    }
  },

  /** POST /api/preview → {width,height,format,size_bytes,dpi,warnings,image}. */
  preview: (req, signal, max = 1600) =>
    request('POST', `/api/preview?max=${max}`, { json: req, signal }).then((r) => r.json()),

  /** POST /api/process → {blob, filename, width, height, warnings}. */
  async process(req, signal) {
    const res = await request('POST', '/api/process', { json: req, signal });
    const blob = await res.blob();
    return {
      blob,
      filename: filenameFromDisposition(res.headers.get('Content-Disposition')),
      width: Number(res.headers.get('X-Heyp-Width')) || null,
      height: Number(res.headers.get('X-Heyp-Height')) || null,
      warnings: parseWarnings(res.headers.get('X-Heyp-Warnings')),
    };
  },

  /**
   * Multipart upload with progress (XHR — fetch has no upload progress).
   * Resolves {files, errors}; rejects ApiError (body may still carry `errors`).
   */
  upload(files, onProgress) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      for (const f of files) fd.append('files', f, f.name || 'pasted-image.png');
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/files');
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) onProgress?.(e.loaded / e.total);
      });
      xhr.upload.addEventListener('load', () => onProgress?.(1));
      xhr.addEventListener('load', () => {
        let body = null;
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          /* not JSON */
        }
        if (xhr.status >= 200 && xhr.status < 300 && body) {
          resolve({ files: body.files || [], errors: body.errors || [] });
        } else {
          const fallback = xhr.status === 413 ? 'File too large' : `Upload failed (${xhr.status || 'network'})`;
          reject(new ApiError(xhr.status, formatDetail(body?.detail, fallback), body));
        }
      });
      xhr.addEventListener('error', () => reject(new ApiError(0, OFFLINE)));
      xhr.addEventListener('abort', () => reject(new ApiError(0, 'Upload cancelled')));
      xhr.send(fd);
    });
  },
};
