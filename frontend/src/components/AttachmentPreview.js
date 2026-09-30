import { useEffect, useState } from 'react';
import { X, Download, Maximize2, Minimize2 } from 'lucide-react';
import { attachmentsApi } from '../api';
import useModalFocus from './useModalFocus';

// Reading a whole 50 MB spreadsheet into a preview is not worth it; above this
// the box shows what fits rather than stalling the page on the whole file.
const MAX_TEXT_PREVIEW = 512 * 1024;

export const isImage = (a) => a.kind === 'image';

// Mirror of the server's INLINE_EXTENSIONS, used only when a listing predates
// the `previewable` flag. The server decides; this just avoids a blank box.
const PREVIEWABLE = /\.(png|jpe?g|gif|webp|pdf|txt|csv|tsv|md)$/i;
export const canPreview = (a) => (typeof a.previewable === 'boolean'
  ? a.previewable : PREVIEWABLE.test(a.original_name));

const isPdf = (a) => (a.content_type || '') === 'application/pdf';
const isText = (a) => (a.content_type || '').startsWith('text/');

/**
 * Pop-up preview for one attachment.
 *
 * It is a modal rather than a box in the list because the point of the control
 * is size: an inline box can only ever be as wide as the panel it sits in, and
 * a broker statement or a full-screen chart is exactly what the user wants to
 * blow up. Two sizes — a comfortable default and near-full-screen — so
 * "bigger" is one click without leaving the trade.
 *
 * Focus, Tab cycling, Escape and focus restoration all come from
 * useModalFocus, the same hook the app's other dialogs use.
 */
export default function AttachmentPreview({ item, onClose }) {
  const [expanded, setExpanded] = useState(false);
  const [textBody, setTextBody] = useState(null);
  const [textError, setTextError] = useState(null);
  const dialogRef = useModalFocus(onClose);

  useEffect(() => {
    if (!isText(item)) return undefined;
    let live = true;
    setTextBody(null);
    setTextError(null);
    (async () => {
      try {
        // Range-capped so a 50 MB CSV still opens instantly.
        const res = await fetch(attachmentsApi.downloadUrl(item.id), {
          headers: { Range: `bytes=0-${MAX_TEXT_PREVIEW - 1}` },
        });
        if (!res.ok && res.status !== 206) throw new Error(`HTTP ${res.status}`);
        const body = await res.text();
        if (live) setTextBody(body);
      } catch (e) {
        if (live) setTextError(`Could not load this preview (${e.message}).`);
      }
    })();
    return () => { live = false; };
  }, [item]);

  // Cap with max-width/max-height rather than min() on width, so the window
  // never exceeds the viewport on a narrow screen, and the expanded state is a
  // plain "remove the caps". Explicit `max-*: none` matters: `.modal` in
  // index.css sets its own max-width/max-height, and those clamps are exactly
  // what "Expand" is meant to escape.
  const body = expanded
    ? {
      width: 'calc(100vw - 24px)', height: 'calc(100vh - 24px)',
      maxWidth: 'none', maxHeight: 'none',
    }
    : {
      width: 'calc(100vw - 32px)', height: 'calc(100vh - 48px)',
      maxWidth: '920px', maxHeight: '760px',
    };

  const scrim = (e) => { if (e.target === e.currentTarget) onClose(); };

  return (
    <div className="modal-overlay" onClick={scrim}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="attachment-preview-title"
        ref={dialogRef}
        tabIndex={-1}
        style={{ ...body, padding: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
      >
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px',
          borderBottom: '1px solid var(--divider)', flexShrink: 0,
        }}>
          <h2
            id="attachment-preview-title"
            className="section-title"
            style={{ margin: 0, fontSize: 15, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}
            title={item.original_name}
          >
            {item.original_name}
          </h2>
          <span className="num" style={{ fontSize: 12, color: 'var(--text-secondary)', flexShrink: 0 }}>
            {item.size_bytes >= 1024 * 1024
              ? `${(item.size_bytes / (1024 * 1024)).toFixed(1)} MB`
              : `${Math.max(1, Math.round(item.size_bytes / 1024))} KB`}
          </span>

          <div style={{ display: 'flex', gap: 6, marginLeft: 'auto', flexShrink: 0 }}>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setExpanded(v => !v)}
              aria-label={expanded ? 'Shrink preview' : 'Expand preview'}
              title={expanded ? 'Shrink' : 'Expand'}
            >
              {expanded ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
              {expanded ? 'Shrink' : 'Expand'}
            </button>
            <a
              href={attachmentsApi.downloadUrl(item.id)}
              className="btn btn-ghost btn-sm"
              style={{ display: 'inline-flex', alignItems: 'center' }}
              aria-label={`Download ${item.original_name}`}
              title="Download"
            >
              <Download size={15} />
            </a>
            <button type="button" className="btn btn-ghost btn-sm" onClick={onClose} aria-label="Close preview">
              <X size={15} />
            </button>
          </div>
        </div>

        {isText(item) ? (
          <div style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: '14px 16px' }}>
            {textError ? (
              <div style={{ fontSize: 13, color: 'var(--caution)' }}>{textError}</div>
            ) : textBody === null ? (
              <div style={{ fontSize: 13, color: 'var(--text-secondary)' }} role="status">Loading preview…</div>
            ) : (
              <pre style={{
                margin: 0, fontSize: 13, lineHeight: 1.55, whiteSpace: 'pre-wrap',
                wordBreak: 'break-word', color: 'var(--text-primary)',
              }}>{textBody}</pre>
            )}
          </div>
        ) : (
          <div style={{
            flex: 1, minHeight: 0, display: 'flex', alignItems: 'center',
            justifyContent: 'center', padding: 16, background: 'var(--surface-inset)',
            overflow: 'auto',
          }}>
            {isImage(item) && (
              <img
                src={attachmentsApi.previewUrl(item.id)}
                alt={`Preview of ${item.original_name}`}
                style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain', borderRadius: 'var(--radius-md)' }}
              />
            )}

            {isPdf(item) && (
              <iframe
                src={attachmentsApi.previewUrl(item.id)}
                title={`Preview of ${item.original_name}`}
                // `allow-scripts` but never `allow-same-origin`: the frame runs
                // in an opaque origin, so a PDF's embedded JavaScript can reach
                // neither the journal's DOM nor its storage. Scripts stay on
                // because Firefox's viewer is script-based.
                sandbox="allow-scripts"
                style={{
                  width: '100%', height: '100%', border: '1px solid var(--divider)',
                  borderRadius: 'var(--radius-md)', background: 'white',
                }}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
