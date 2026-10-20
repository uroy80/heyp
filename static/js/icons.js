// Inline SVG icon set: 24×24 viewBox, 1.5px stroke, currentColor. Static, trusted markup only.

const PATHS = {
  convert: '<path d="M4 7.5h13"/><path d="m14 4.5 3 3-3 3"/><path d="M20 16.5H7"/><path d="m10 13.5-3 3 3 3"/>',
  resize: '<path d="M14.5 3.5h6v6"/><path d="M9.5 20.5h-6v-6"/><path d="m20.5 3.5-7 7"/><path d="m3.5 20.5 7-7"/>',
  crop: '<path d="M6.5 2.5v13a2 2 0 0 0 2 2h13"/><path d="M17.5 21.5v-13a2 2 0 0 0-2-2h-13"/>',
  passport: '<rect x="4.5" y="2.5" width="15" height="19" rx="2.5"/><circle cx="12" cy="10" r="3"/><path d="M7.5 18c.9-2.1 2.5-3.2 4.5-3.2s3.6 1.1 4.5 3.2"/>',
  compress: '<path d="M4 14h6v6"/><path d="M20 10h-6V4"/><path d="m14 10 6.5-6.5"/><path d="M3.5 20.5 10 14"/>',
  edit: '<path d="M4 6h8.5"/><path d="M17.5 6H20"/><circle cx="15" cy="6" r="2.25"/><path d="M4 12h2.5"/><path d="M11.5 12H20"/><circle cx="9" cy="12" r="2.25"/><path d="M4 18h10.5"/><path d="M19.5 18h.5"/><circle cx="17" cy="18" r="2.25"/>',
  background: '<path d="M12 3.5 3.5 8 12 12.5 20.5 8 12 3.5Z"/><path d="m3.5 12 8.5 4.5 8.5-4.5"/><path d="m3.5 16 8.5 4.5 8.5-4.5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><path d="M12 7.75h.01"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  x: '<path d="M6.5 6.5l11 11"/><path d="M17.5 6.5l-11 11"/>',
  download: '<path d="M12 3.5v12"/><path d="m7 10.5 5 5 5-5"/><path d="M4.5 20.5h15"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2"/><path d="M12 19.5v2"/><path d="m5.3 5.3 1.4 1.4"/><path d="m17.3 17.3 1.4 1.4"/><path d="M2.5 12h2"/><path d="M19.5 12h2"/><path d="m5.3 18.7 1.4-1.4"/><path d="m17.3 6.7 1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"/>',
  rotateLeft: '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1L3.5 8.5"/><path d="M3.5 3.5v5h5"/>',
  rotateRight: '<path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1l2.6 2.6"/><path d="M20.5 3.5v5h-5"/>',
  flipH: '<path d="M12 3v18"/><path d="M8.5 7 3.5 17h5Z"/><path d="M15.5 7l5 10h-5Z"/>',
  flipV: '<path d="M3 12h18"/><path d="M7 8.5 17 3.5v5Z"/><path d="M7 15.5l10 5v-5Z"/>',
  lock: '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8.5 10.5V7.5a3.5 3.5 0 0 1 7 0v3"/>',
  unlock: '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8.5 10.5V7.5a3.5 3.5 0 0 1 6.8-1.2"/>',
  compare: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M12 2.5v19"/><path d="M12 4.5h6.5a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H12Z" fill="currentColor" fill-opacity=".14" stroke="none"/>',
  image: '<rect x="3.5" y="3.5" width="17" height="17" rx="3"/><circle cx="15" cy="9" r="1.75"/><path d="m4 17 4.6-5.2a1.2 1.2 0 0 1 1.8 0l4.1 4.7"/><path d="m13.5 15.5 1.6-1.7a1.2 1.2 0 0 1 1.8 0l3.1 3.4"/>',
  upload: '<path d="M12 15.5v-11"/><path d="m7.5 9 4.5-4.5L16.5 9"/><path d="M4.5 15v3a2.5 2.5 0 0 0 2.5 2.5h10a2.5 2.5 0 0 0 2.5-2.5v-3"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  alert: '<path d="M10.3 4.2 2.9 17.5A2 2 0 0 0 4.6 20.5h14.8a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0Z"/><path d="M12 9.5v4"/><path d="M12 16.75h.01"/>',
  shield: '<path d="M12 3 5 6v5.5c0 4.2 2.9 7.9 7 9.5 4.1-1.6 7-5.3 7-9.5V6l-7-3Z"/><path d="m9 12 2 2 4-4"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20.5 20.5-4.9-4.9"/>',
  external: '<path d="M14 4.5h5.5V10"/><path d="M19.5 4.5 11 13"/><path d="M17.5 14v4a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h4"/>',
  keyboard: '<rect x="2.5" y="5.5" width="19" height="13" rx="2"/><path d="M6.5 9.5h.01M10 9.5h.01M13.5 9.5h.01M17.5 9.5h.01M6.5 12.5h.01M17.5 12.5h.01M10 12.5h4M8 15.5h8"/>',
  pin: '<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11Z"/><circle cx="12" cy="10" r="2.25"/>',
  sparkle: '<path d="M12 3.5c.6 3.9 2.6 5.9 6.5 6.5-3.9.6-5.9 2.6-6.5 6.5-.6-3.9-2.6-5.9-6.5-6.5 3.9-.6 5.9-2.6 6.5-6.5Z"/><path d="M19 15.5c.3 1.6 1 2.3 2.5 2.5-1.5.2-2.2.9-2.5 2.5-.3-1.6-1-2.3-2.5-2.5 1.5-.2 2.2-.9 2.5-2.5Z"/>',
  face: '<path d="M3.5 8V6a2.5 2.5 0 0 1 2.5-2.5h2"/><path d="M16 3.5h2A2.5 2.5 0 0 1 20.5 6v2"/><path d="M20.5 16v2a2.5 2.5 0 0 1-2.5 2.5h-2"/><path d="M8 20.5H6A2.5 2.5 0 0 1 3.5 18v-2"/><path d="M9 14.5c.8.8 1.8 1.2 3 1.2s2.2-.4 3-1.2"/><path d="M9.5 9.5h.01M14.5 9.5h.01"/>',
  trim: '<path d="M7.5 3.5v3M3.5 7.5h3M16.5 20.5v-3M20.5 16.5h-3"/><rect x="7.5" y="7.5" width="9" height="9" rx="1"/><path d="M3.5 3.5l2.5 2.5M20.5 20.5 18 18"/>',
  reset: '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3L4.5 9"/><path d="M4.5 4.5V9H9"/>',
  chevron: '<path d="m6.5 9.5 5.5 5.5 5.5-5.5"/>',
  // v2 sections and editor tools
  upscale: '<rect x="3.5" y="11.5" width="9" height="9" rx="1"/><path d="M12.5 3.5h8v8"/><path d="m20.5 3.5-7 7"/><path d="M3.5 7.5v-4h4"/>',
  watermark: '<path d="M12 3.5c3.6 4.2 6 7.4 6 10.3a6 6 0 0 1-12 0c0-2.9 2.4-6.1 6-10.3Z"/><path d="M9.5 14.5a2.6 2.6 0 0 0 2.5 2.5"/>',
  blur: '<rect x="3.5" y="3.5" width="17" height="17" rx="2"/><path d="M7.5 8h.01M12 8h.01M16.5 8h.01M7.5 12h.01M12 12h.01M16.5 12h.01M7.5 16h.01M12 16h.01M16.5 16h.01" stroke-width="2.6"/>',
  meme: '<rect x="3.5" y="3.5" width="17" height="17" rx="2"/><path d="M7 7.5h10"/><path d="M7 16.5h10"/><path d="M12 7.5v1.5M12 15v1.5"/>',
  editor: '<path d="M4 20c1.5-.2 3-1 3.6-2.6l.6-1.4 2-2-2.2-2.2-2 2-1.4.6C3 15 3.6 18.5 4 20Z"/><path d="m10.2 14 9.3-9.3a1.6 1.6 0 0 0-2.2-2.2L8 11.8"/>',
  gif: '<rect x="3.5" y="5.5" width="17" height="13" rx="2"/><path d="M7.5 5.5v13M16.5 5.5v13M3.5 9h4M3.5 15h4M16.5 9h4M16.5 15h4"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.4 2.6 3.5 5.4 3.5 8.5s-1.1 5.9-3.5 8.5c-2.4-2.6-3.5-5.4-3.5-8.5s1.1-5.9 3.5-8.5Z"/>',
  text: '<path d="M5 6.5V5h14v1.5"/><path d="M12 5v14"/><path d="M9.5 19h5"/>',
  sticker: '<path d="M20.5 12.5a8 8 0 1 1-9-9h1a8 8 0 0 1 8 8Z"/><path d="M20.5 12.5c-4.4 0-8 3.6-8 8"/><path d="M8.5 10h.01M14.5 10h.01"/><path d="M8.5 14.5c.8.8 1.8 1.2 3 1.2"/>',
  shape: '<rect x="3.5" y="10.5" width="10" height="10" rx="1"/><circle cx="15.5" cy="8.5" r="5"/>',
  brush: '<path d="M4 20c1.5-.2 3-1 3.6-2.6l.6-1.4 2-2-2.2-2.2-2 2-1.4.6C3 15 3.6 18.5 4 20Z"/><path d="m10.2 14 9.3-9.3a1.6 1.6 0 0 0-2.2-2.2L8 11.8"/>',
  filter: '<circle cx="9" cy="9" r="5.5"/><circle cx="15" cy="9" r="5.5"/><circle cx="12" cy="15" r="5.5"/>',
  frame: '<rect x="3.5" y="3.5" width="17" height="17" rx="1"/><rect x="7.5" y="7.5" width="9" height="9"/>',
  arrowUp: '<path d="M12 19V5"/><path d="m6 11 6-6 6 6"/>',
  arrowDown: '<path d="M12 5v14"/><path d="m6 13 6 6 6-6"/>',
  play: '<path d="M7.5 5.5v13l11-6.5Z"/>',
  pause: '<path d="M8 5.5v13M16 5.5v13"/>',
  trash: '<path d="M4.5 7h15"/><path d="M9.5 7V4.5h5V7"/><path d="M6.5 7l1 13h9l1-13"/>',
  layers: '<path d="M12 3.5 3.5 8 12 12.5 20.5 8 12 3.5Z"/><path d="m3.5 12 8.5 4.5 8.5-4.5"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/>',
  doc: '<path d="M6.5 2.5h7l5 5v14h-12Z"/><path d="M13.5 2.5v5h5"/><path d="M9 13h6M9 16.5h6"/>',
};

const NS = 'http://www.w3.org/2000/svg';

/** Build an icon <svg>. Decorative by default (aria-hidden). */
export function icon(name, size = 18) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.5');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.classList.add('icon');
  svg.innerHTML = PATHS[name] || '';
  return svg;
}
