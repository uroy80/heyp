// HTML → image: capture a web page (by URL) as a JPG or PNG, rendered by the isolated renderer service as a job.
// The capture is added to your photos, so every other section works on it.

import { h, append } from '../dom.js';
import { state, defaults, toolEntry } from '../state.js';
import { toolHead, group, segmented, slider, toggle, note } from '../controls.js';
import { PAGE_WIDTHS } from '../catalog.js';
import { fitOptions, enumOf } from '../schema.js';
import { jobCard } from '../jobui.js';

export const HTML_TOOL = 'img.html_to_image';
const MAX_URL = 2048;

let r = {};

const PRIVATE_V4 = [/^10\./, /^127\./, /^0\./, /^169\.254\./, /^192\.168\./, /^172\.(1[6-9]|2\d|3[01])\./, /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./];

/**
 * Check and normalise what was typed: adds https:// when the scheme is missing; only http(s), no user:password,
 * no obviously private hosts (the renderer's egress proxy enforces the real policy). → {ok, url} | {ok:false, error}
 */
export function normalizeUrl(input) {
  let s = String(input || '').trim();
  if (!s) return { ok: false, error: 'Enter the address of a web page.' };
  if (s.length > MAX_URL) return { ok: false, error: `That address is too long (max ${MAX_URL} characters).` };
  if (!/^[a-z][a-z0-9+.-]*:/i.test(s)) s = `https://${s.replace(/^\/+/, '')}`;
  let u;
  try {
    u = new URL(s);
  } catch {
    return { ok: false, error: 'That doesn’t look like a web address.' };
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return { ok: false, error: 'Only http:// and https:// pages can be captured.' };
  if (u.username || u.password) return { ok: false, error: 'Addresses with a user name or password can’t be captured.' };
  const host = u.hostname.toLowerCase().replace(/^\[|\]$/g, '');
  if (!host || (!host.includes('.') && !host.includes(':'))) return { ok: false, error: 'Use a full public address, like example.com.' };
  if (host === 'localhost' || /\.(localhost|local|internal|lan|home\.arpa)$/.test(host) || PRIVATE_V4.some((re) => re.test(host))
    || host === '::1' || /^f[cd][0-9a-f]{2}:/.test(host) || /^fe80:/.test(host)) {
    return { ok: false, error: 'Private and local network addresses can’t be captured.' };
  }
  return { ok: true, url: u.href };
}

/** Options for the img.html_to_image job, fitted to the server's schema. */
export function htmlOptions(schema, o, url) {
  const fmts = enumOf(schema, ['format']);
  const fmt = o.format === 'png' ? 'png' : fmts?.includes('jpeg') && !fmts.includes('jpg') ? 'jpeg' : 'jpg';
  return fitOptions(schema, [
    { names: ['url'], value: url },
    { names: ['width', 'viewport_width', 'screen_width'], value: o.width },
    { names: ['full_page', 'fullpage'], value: o.fullPage },
    { names: ['format'], value: fmt },
    { names: ['delay_ms', 'delay', 'wait_ms'], value: Math.round(o.delay * 1000), unit: 'ms' },
    { names: ['block_cookie_banners', 'hide_cookie_banners', 'cookie_banners'], value: o.blockBanners },
  ]);
}

export default {
  id: 'html',
  title: 'HTML → image',
  rail: 'Web page',
  icon: 'globe',
  kicker: 'Capture a web page',
  jobTool: HTML_TOOL,
  needsFiles: false,

  isActive: () => false,
  reset() {
    state.html = { ...defaults.html(), url: state.html.url };
  },

  render(root, app) {
    r = {};
    const o = state.html;
    const tool = toolEntry(HTML_TOOL);
    append(root, toolHead('HTML → image', 'Capture any public web page as a picture — the visible screen or the whole page.', { onReset: () => app.resetTool(this), active: false }));
    if (state.tools && !tool) {
      append(root, note('This server doesn’t offer web page captures.', 'warn'));
      return;
    }
    if (tool && tool.available === false) {
      // the server's reason is a whole sentence ("Web page capture is not set up on this server")
      const why = String(tool.reason || '').trim();
      append(root, note(why ? `${why}${/[.!?]$/.test(why) ? '' : '.'}` : 'Web page capture needs the renderer service, which isn’t running on this server.', 'warn'));
      return;
    }
    const id = 'html-url';
    r.err = h('p', { class: 'field-error', id: 'html-url-err', role: 'alert', hidden: true });
    r.url = h('input', {
      id, type: 'url', class: 'input', inputmode: 'url', autocomplete: 'url', spellcheck: 'false', maxLength: MAX_URL,
      placeholder: 'https://example.com', value: o.url, 'aria-describedby': 'html-url-err',
      oninput: () => { o.url = r.url.value; this.validate(false); },
      onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); this.start(app); } },
    });
    append(root, group(null, h('div', { class: 'field' }, h('label', { htmlFor: id, class: 'label' }, 'Web page address'), r.url, r.err)));

    const widths = enumOf(tool?.options_schema, ['width', 'viewport_width', 'screen_width']);
    const opts = (widths ? widths.map((w) => PAGE_WIDTHS.find((p) => p.value === w) || { value: w, label: String(w) }) : PAGE_WIDTHS);
    if (!opts.some((p) => p.value === o.width)) o.width = opts.find((p) => p.value === 1440)?.value ?? opts[0].value;
    const width = segmented({
      label: 'Screen width (px)', cols: Math.min(5, opts.length), size: 'sm', value: String(o.width),
      options: opts.map((p) => ({ value: String(p.value), label: p.label, title: p.title })),
      onChange: (v) => { o.width = Number(v); },
    });
    const full = toggle({ label: 'Whole page', checked: o.fullPage, hint: 'Off captures only the first screen.', onChange: (v) => { o.fullPage = v; } });
    const format = segmented({ label: 'Format', cols: 2, value: o.format, options: [{ value: 'jpg', label: 'JPG' }, { value: 'png', label: 'PNG' }], onChange: (v) => { o.format = v; } });
    const delay = slider({ label: 'Wait before capture', min: 0, max: 10, step: 0.5, value: o.delay, resetTo: 0, format: (v) => `${v} s`, onInput: (v) => { o.delay = v; } });
    const banners = toggle({ label: 'Hide cookie banners and pop-ups', checked: o.blockBanners, onChange: (v) => { o.blockBanners = v; } });
    append(root, group('Capture', width, full, format, delay, banners));

    append(root, jobCard('html', {
      startLabel: 'Capture page',
      startIcon: 'globe',
      disabled: () => (!tool && !state.tools ? 'Checking what this server offers…' : null),
      onStart: () => this.start(app),
      renderResult: (res) => app.resultView(res, { kind: 'image', ext: o.format, source: 'html' }),
    }));
    append(root, h('p', { class: 'hint' }, app.copy().renderNote));
  },

  validate(show = true) {
    const res = normalizeUrl(state.html.url);
    if (!r.url) return res;
    const bad = !res.ok && (show || r.url.getAttribute('aria-invalid') === 'true');
    r.url.setAttribute('aria-invalid', bad ? 'true' : 'false');
    r.err.hidden = !bad;
    r.err.textContent = bad ? res.error : '';
    return res;
  },

  start(app) {
    const res = this.validate(true);
    if (!res.ok) {
      r.url?.focus();
      return;
    }
    state.html.url = res.url;
    if (r.url) r.url.value = res.url;
    const tool = toolEntry(HTML_TOOL);
    app.runTool('html', HTML_TOOL, [], htmlOptions(tool?.options_schema, state.html, res.url), { kind: 'image', ext: state.html.format, addToStrip: true });
  },
};
