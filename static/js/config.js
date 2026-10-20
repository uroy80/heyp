// Deployment-dependent copy. Every line must be checkable against /api/config or the code: where the work runs,
// where the source is, which sibling site exists. Nothing about "local", "this machine", "your container" or
// "editions" — the same page is served by self-hosted copies and by the public site. How long files are kept is
// told only on the Privacy page (/privacy): the page never prints the configured TTLs.
// Pure functions — the app applies the result to the page.

export const DEFAULT_SOURCE_URL = 'https://github.com/uroy80/heyp';

/** Only http(s) URLs (or a site path) are used as links; anything else falls back. */
export function safeUrl(u, fallback) {
  const s = String(u || '').trim();
  if (/^https?:\/\/[^\s"'<>]+$/i.test(s) || /^\/[^\s"'<>]*$/.test(s)) return s;
  return fallback;
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

export const OFFLINE = 'Can’t reach heyp — check your connection and try again.';

/**
 * Page copy for a /api/config response (null = unknown). Returns
 * {mode, consent, bgHint, renderNote, footer[], source, version}. `consent` (hosted only) is the one line under the
 * upload button; the hints are shown behind "i" buttons.
 */
export function copyFor(cfg) {
  const hosted = cfg?.mode === 'hosted';
  const source = sourceLink(cfg);
  const version = typeof cfg?.version === 'string' && /^[\w.+-]{1,40}$/.test(cfg.version) ? cfg.version : null;
  const footer = [
    { label: 'Privacy', href: '/privacy' },
    { label: 'Terms', href: '/terms' },
    { label: 'Credits', href: '/credits' },
    { label: 'Source (AGPL-3.0)', href: source, external: true },
  ];
  return {
    mode: hosted ? 'hosted' : 'local',
    consent: hosted ? 'By uploading you agree to the' : null,
    bgHint: `Runs on ${hosted ? 'our' : 'the heyp'} server; the photo isn’t sent to an AI service.`,
    renderNote: `${hosted ? 'Our' : 'The heyp'} server loads the page; private network addresses are blocked.`,
    footer,
    source,
    version,
  };
}
