import { useEffect, useRef } from 'react';

/**
 * Copy/paste plumbing shared by the trade detail view.
 *
 * Fires `onFiles` with file data from the clipboard when the user hits Ctrl/Cmd+V.
 * Browsers and screenshot utilities are inconsistent: some expose images in
 * `clipboardData.files`, while others expose only `clipboardData.items` with
 * `kind === 'file'` and `getAsFile()`. Read both representations, deduplicating
 * the same File object when the browser exposes it in both places.
 * Two deliberate refusals:
 *
 *   * an event that carries no files is left entirely alone, so an ordinary
 *     Ctrl+V of text keeps working everywhere else in the app; and
 *   * nothing is taken while focus sits in a text field or editable region, so
 *     pasting into a notes box is never hijacked.
 *
 * The listener sits on the window rather than on the dropzone because the caret
 * is almost never inside that panel when a screenshot is pasted: the user has
 * just copied something from a chart or their broker and presses Ctrl+V
 * wherever they happen to be looking.
 *
 * The callback is held in a ref so callers do not have to memoize it for the
 * listener to survive a render.
 */
const editableTarget = (target) => {
  const tag = target?.tagName || '';
  return Boolean(target?.isContentEditable) || /^(INPUT|TEXTAREA|SELECT)$/.test(tag);
};

/**
 * Everything the event is willing to hand over as a file, in order.
 *
 * `clipboardData.files` is empty in several real cases — notably a screenshot
 * copied in some editors — while the same clipboard exposes the image through
 * `clipboardData.items`. Reading only one of the two is what makes an image
 * paste look like "nothing happened".
 */
export function clipboardFiles(clipboardData) {
  if (!clipboardData) return [];
  const out = [];
  const seen = new Set();

  const push = (file) => {
    if (!file || seen.has(file)) return;
    seen.add(file);
    out.push(file);
  };

  Array.from(clipboardData.files || []).forEach(push);
  Array.from(clipboardData.items || [])
    .filter(item => item?.kind === 'file')
    .forEach((item) => {
      try { push(item.getAsFile()); } catch {
        // Some producers answer getAsFile() with null for non-image types;
        // that is not a paste failure, just nothing to add.
      }
    });
  return out;
}

export default function useFilePaste(onFiles) {
  const cb = useRef(onFiles);
  useEffect(() => { cb.current = onFiles; });

  useEffect(() => {
    const handler = (e) => {
      const files = clipboardFiles(e.clipboardData);
      // No files: leave the event alone so ordinary text paste keeps working.
      if (!files.length) return;
      // ...and never take files while the caret is in an editable region.
      if (editableTarget(e.target)) return;
      e.preventDefault();
      cb.current?.(files);
    };
    window.addEventListener('paste', handler);
    return () => window.removeEventListener('paste', handler);
  }, []);
}
