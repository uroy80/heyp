// Deployment-dependent copy. Every line must be checkable against /api/config or the code:
// how long files are kept (the configured TTLs), where the work runs, where the source is. Nothing about "local",
// "this machine", "your container" or "editions" — the same page is served by self-hosted copies and by the public
// site. When /api/config hasn't answered (older server, network error) no deletion claim is printed at all.
// Pure functions — the app applies the result to the page.

export const DEFAULT_SOURCE_URL = 'https://github.com/uroy80/heyp';

/** 60 → "1 hour", 90 → "90 minutes", 240 → "4 hours", 1 → "1 minute". */
export function fmtMinutes(min) {
  const m = Math.round(Number(min));
  if (!(m > 0)) return null;
  if (m % 60 === 0) {
    const hrs = m / 60;
    return `${hrs} hour${hrs === 1 ? '' : 's'}`;
  }
  return `${m} minute${m === 1 ? '' : 's'}`;
}

/** Only http(s) URLs (or a site path) are used as links; anything else falls back. */
export function safeUrl(u, fallback) {
  const s = String(u || '').trim();
  if (/^https?:\/\/[^\s"'<>]+$/i.test(s) || /^\/[^\s"'<>]*$/.test(s)) return s;
  return fallback;
}

/** {ttl, hard} as words from the configured TTLs, or null when the config doesn't say (no claim then). */
export function retention(cfg) {
  const ttl = cfg && typeof cfg === 'object' ? fmtMinutes(cfg.ttl_minutes) : null;
  if (!ttl) return null;
  return { ttl, hard: fmtMinutes(cfg.hard_ttl_minutes) };
}

/**
 * The AGPL §13 source offer: the exact running commit when the server reports one (`revision`) and the
 * repository is on GitHub, else the repository itself.
 */
export function sourceLink(cfg) {
  const repo = safeUrl(cfg?.source_url, DEFAULT_SOURCE_URL).replace(/\/+$/, '');
  const rev = typeof cfg?.revision === 'string' && /^[0-9a-f]{7,40}$/.test(cfg.revision) ? cfg.revision : null;
  return rev && /^https:\/\/github\.com\/[^/]+\/[^/]+$/.test(repo) ? `${repo}/tree/${rev}` : repo;
}

/** heypdf, the PDF toolbox, for this deployment (`sibling_url` from /api/config): an absolute http(s) URL, else null
 * (no link at all — the switch and the "Continue with" entry are hidden). */
export function siblingUrl(cfg) {
  const u = safeUrl(cfg?.sibling_url, null);
  return u && /^https?:\/\//i.test(u) ? u : null;
}

/** Why files a page held are gone, in words true for this deployment (expiry toast). */
export function expiryReason(cfg) {
  const r = retention(cfg);
  if (!r) return 'removed after a while without use, or by a server restart';
  const when = cfg?.mode === 'hosted' && r.hard
    ? `${r.ttl} after last use and ${r.hard} after upload at the latest`
    : `${r.ttl} after last use${r.hard ? ` (${r.hard} at most)` : ''}`;
  return `files are deleted ${when}; a server restart can also clear them`;
}

export const OFFLINE = 'Can’t reach the heyp server. Check your connection and try again.';

/**
 * Page copy for a /api/config response (null = unknown). Returns
 * {mode, known, pill, pillTitle, dateline, lede, privacy, privacyNote, offline, bgHint, renderNote, footer[], source, version}.
 * `pill` and `dateline` are '' when the deployment is unknown: nothing is printed rather than a guess.
 */
export function copyFor(cfg) {
  const hosted = cfg?.mode === 'hosted';
  const r = retention(cfg);
  const source = sourceLink(cfg);
  const version = typeof cfg?.version === 'string' && /^[\w.+-]{1,40}$/.test(cfg.version) ? cfg.version : null;
  const footer = [
    { label: 'Privacy', href: '/privacy' },
    { label: 'Terms', href: '/terms' },
    { label: 'Credits', href: '/credits' },
    { label: 'Source code (AGPL-3.0)', href: source, external: true },
  ];
  const deleted = r ? `Files are deleted ${r.ttl} after you last use them${r.hard ? ` (${r.hard} at most)` : ''}` : '';
  return {
    mode: hosted ? 'hosted' : 'local',
    known: Boolean(r),
    pill: r ? 'Auto-delete' : '',
    // the page also deletes what it uploaded when the tab closes — "asks", because a browser may not run that code
    pillTitle: r ? `${deleted}. Closing the tab also asks the server to delete the photos and results of this page.${hosted ? ' No account needed.' : ''}` : '',
    dateline: r ? `Files deleted ${r.ttl} after last use${r.hard ? ` · ${r.hard} at most` : ''}` : '',
    lede: hosted
      ? 'Convert HEIC to JPG, resize, crop, compress, upscale, watermark, blur faces and make passport photos. No sign-up.'
      : 'Convert HEIC to JPG, resize, crop, compress, upscale, watermark, blur faces and make passport photos.',
    privacy: deleted || 'Open source (AGPL-3.0)',
    privacyNote: hosted ? 'By uploading you agree to the' : null,
    offline: OFFLINE,
    bgHint: `Runs a segmentation model on ${hosted ? 'our' : 'the heyp'} server — the photo isn’t sent to an AI service. The first preview can take a few seconds.`,
    renderNote: hosted
      ? 'Our server loads the page from its own network (addresses on private networks are blocked). The capture is deleted with your files; the address of the site may remain in our server logs.'
      : 'The page is loaded by the heyp server’s renderer; addresses on private networks are blocked.',
    footer,
    source,
    version,
  };
}
