// Keyboard decisions that must stay predictable, kept pure so the node tests can pin them down.

/**
 * What Delete / Backspace does outside text fields:
 * - 'layer' — a layer (blur box, sticker, text…) is selected in a section that edits on the photo: delete it;
 * - 'photo' — remove the current photo, but only from "neutral" focus (page, canvas, strip) and never while a
 *   section that edits on the photo is open: there the key must not reach the photo (UIH-03 — a box just drawn
 *   leaves focus on the page, and Delete used to drop the whole photo);
 * - null — nothing.
 * `target`: {body, inStrip} where the key was pressed; `editing`: {active, selected, removable}.
 */
export function deleteKeyAction({ hasPhoto, target = {}, editing = {} }) {
  if (target.inStrip) return hasPhoto ? 'photo' : null; // a focused thumbnail: the photo, in every section
  if (editing.active) return editing.selected && editing.removable !== false ? 'layer' : null;
  return hasPhoto && target.body ? 'photo' : null;
}
