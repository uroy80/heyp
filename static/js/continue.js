// "Continue with": after an export or a job, offer the heyp sections that accept its output (the result becomes the
// photo you keep editing) and — when this deployment names one (`sibling_url` in /api/config) — a plain link to
// heypdf, the PDF toolbox. heypdf is a separate site with its own storage: no file ever moves across, so the link
// only opens it; the visitor downloads the result here and adds it there.

/**
 * Menu entries for `count` result files of `kind` ('image' | 'pdf' | …).
 * `heypSections`: [{id, title, accepts(kind, count)}]; `sibling`: heypdf's address or null.
 * → {heyp: [{id, title}], sibling: string | null}
 */
export function continueTargets({ kind, count = 1 }, { heypSections = [], exclude = null, sibling = null } = {}) {
  const heyp = heypSections
    .filter((s) => s.id !== exclude && (s.accepts ? s.accepts(kind, count) : kind === 'image'))
    .map((s) => ({ id: s.id, title: s.title }));
  return { heyp, sibling: sibling || null };
}

/** Kind + extension of an export: a combined PDF is a document, everything else an image. */
export function outputKind(format) {
  if (format === 'pdf') return { kind: 'pdf', ext: 'pdf' };
  const ext = { jpeg: 'jpg', tiff: 'tif' }[format] || format || 'jpg';
  return { kind: 'image', ext };
}

/** heyp sections a result of `kind` can continue in (images only). */
export const HEYP_CONTINUE = ['resize', 'crop', 'compress', 'convert', 'edit', 'background', 'upscale', 'watermark', 'blur', 'editor', 'meme', 'gif'];
