import { useCallback, useEffect, useRef, useState } from 'react';
import { Paperclip, Trash2, Download, FileText, ClipboardPaste, Eye } from 'lucide-react';
import { attachmentsApi } from '../api';
import AttachmentPreview, { canPreview, isImage } from './AttachmentPreview';

// Server authority is the backend allowlist; this mirror gives instant feedback
// for an obvious unsupported suffix before starting a 50 MB upload.
const ALLOWED_EXTENSIONS = [
  '.png', '.jpg', '.jpeg', '.webp', '.gif', '.heic', '.heif',
  '.pdf', '.txt', '.csv', '.tsv', '.md', '.xlsx', '.xlsm', '.doc', '.docx',
];
const ALLOWED = new RegExp(
  `\\.(${ALLOWED_EXTENSIONS.map(ext => ext.slice(1)).join('|')})$`, 'i',
);
const MAX_BYTES = 50 * 1024 * 1024;

const fmtSize = (n) => (n >= 1024 * 1024
  ? `${(n / (1024 * 1024)).toFixed(1)} MB`
  : `${Math.max(1, Math.round(n / 1024))} KB`);

// The backend answers refusals with {"error": ...} and routing failures with
// FastAPI's {"detail": ...}; an axios network failure has neither. Reading only
// one key turned every real reason into the same vague sentence, which made an
// un-diagnosable message the thing users were shown.
const serverMessage = (e) => {
  const d = e?.response?.data;
  const msg = (typeof d?.error === 'string' && d.error) ? d.error
    : (typeof d?.detail === 'string' && d.detail) ? d.detail
    : null;
  if (msg && e.response.status !== 404) return msg;
  if (msg) {
    // A 404 on this route means the running backend does not have it at all.
    // In practice that is a backend that was never restarted after this
    // feature was added — worth naming, because the message would otherwise
    // just be "Not Found".
    return `${msg} (HTTP 404. If the backend was started before attachments were added, restart it.)`;
  }
  if (e?.response) return `Upload failed (HTTP ${e.response.status}).`;
  return 'The journal backend did not answer. Check that it is running.';
};

/** Files attached to one trade: screenshots, broker statements, notes. */
export default function AttachmentsPanel({ tradeGroup, pendingFiles, onFilesConsumed }) {
  const [items, setItems] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [previewItem, setPreviewItem] = useState(null);
  const inputRef = useRef();
  const consumed = useRef();   // the pendingFiles array last handed to addAll

  const refresh = useCallback(() => {
    if (!tradeGroup) return;
    attachmentsApi.list(tradeGroup)
      .then(r => setItems(r.data.attachments || []))
      .catch(() => setError('Could not load this trade’s attachments.'));
  }, [tradeGroup]);

  useEffect(() => { refresh(); }, [refresh]);

  const add = useCallback(async (file) => {
    if (!file) return;
    if (file.size > MAX_BYTES) {
      setError(`“${file.name || 'That file'}” is over the 50 MB attachment limit.`);
      return;
    }
    if (file.name && !ALLOWED.test(file.name)) {
      setError(`“${file.name}” can’t be attached. Images, PDF, CSV, TXT, MD, XLSX and DOCX files can.`);
      return;
    }
    setError(null);
    setBusy(true);
    const form = new FormData();
    // Pasted images commonly arrive as a Blob with no name. Give it a safe
    // default extension so the backend allowlist can validate it.
    form.append('file', file, file.name || 'pasted-image.png');
    try {
      const r = await attachmentsApi.upload(tradeGroup, form);
      setItems(prev => [...prev, r.data]);
      setError(null);
    } catch (e) {
      setError(serverMessage(e));
    } finally {
      setBusy(false);
    }
  }, [tradeGroup]);

  const addAll = useCallback(async (fileList) => {
    for (const file of Array.from(fileList || [])) {
      // Keep order stable; a refused file should not cancel the remaining files.
      // eslint-disable-next-line no-await-in-loop
      await add(file);
    }
  }, [add]);

  useEffect(() => {
    if (!pendingFiles?.length) return;
    // Identity guard: a parent render recreates `onFilesConsumed`, which
    // re-runs this effect while an upload from the same batch is still in
    // flight. Without this the same screenshot would be sent twice.
    if (consumed.current === pendingFiles) return;
    consumed.current = pendingFiles;
    addAll(pendingFiles);
    onFilesConsumed?.();
  }, [pendingFiles, addAll, onFilesConsumed]);

  const remove = useCallback(async (id) => {
    if (previewItem?.id === id) setPreviewItem(null);
    try {
      await attachmentsApi.remove(id);
      setItems(prev => prev.filter(a => a.id !== id));
    } catch {
      setError('That attachment could not be removed.');
    }
  }, [previewItem]);

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    addAll(e.dataTransfer?.files);
  };

  return (
    <div>
      <div
        className={`dropzone ${dragging ? 'active' : ''}`}
        style={{ padding: '14px 16px' }}
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
        onKeyDown={e => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        role="button"
        tabIndex={0}
        aria-label="Attach files to this trade. Press Enter to browse"
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ALLOWED_EXTENSIONS.join(',')}
          style={{ display: 'none' }}
          tabIndex={-1}
          onChange={e => { addAll(e.target.files); e.target.value = ''; }}
        />
        <Paperclip size={24} style={{ marginBottom: 8 }} />
        <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
          {busy ? 'Attaching…' : 'Attach files'}
        </div>
        <div style={{ fontSize: 13, marginTop: 6 }}>
          Drag and drop, click to browse, or press Ctrl/Cmd + V to paste
        </div>
        <div style={{ fontSize: 12, marginTop: 4, color: 'var(--text-secondary)' }}>
          Images, PDF, CSV, TXT, MD, XLSX, DOCX — up to 50 MB each
        </div>
      </div>

      <div aria-live="polite">
        {error && <div role="alert" style={{
          fontSize: 13, color: 'var(--caution)', marginTop: 10,
          background: 'var(--surface-inset)', borderLeft: '2px solid var(--caution)',
          borderRadius: 'var(--radius-md)', padding: '10px 12px',
        }}>{error}</div>}
      </div>

      {items.length ? (
        <ul style={{ listStyle: 'none', margin: '14px 0 0', padding: 0, display: 'grid', gap: 8 }}>
          {items.map(a => {
            const shown = canPreview(a);
            return (
              <li key={a.id} style={{
                display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px',
                background: 'var(--surface-inset)', border: '1px solid var(--divider)',
                borderRadius: 'var(--radius-md)',
              }}>
                {isImage(a) && shown ? (
                  <img src={attachmentsApi.previewUrl(a.id)} alt="" loading="lazy"
                    style={{ width: 40, height: 40, objectFit: 'cover', borderRadius: 4, background: 'var(--surface-panel)' }} />
                ) : <FileText size={20} style={{ color: 'var(--text-secondary)', flexShrink: 0 }} />}
                <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 13.5 }} title={a.original_name}>
                  {a.original_name}
                </span>
                <span className="num" style={{ fontSize: 12, color: 'var(--text-secondary)', flexShrink: 0 }}>{fmtSize(a.size_bytes)}</span>
                {shown && (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}
                    aria-label={`Preview ${a.original_name}`}
                    onClick={() => setPreviewItem(a)}
                  >
                    <Eye size={14} /> Preview
                  </button>
                )}
                <a href={attachmentsApi.downloadUrl(a.id)} className="btn btn-ghost btn-sm"
                  style={{ display: 'inline-flex', alignItems: 'center' }} aria-label={`Download ${a.original_name}`} title="Download">
                  <Download size={14} />
                </a>
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => remove(a.id)}
                  aria-label={`Delete attachment ${a.original_name}`} title="Delete">
                  <Trash2 size={14} />
                </button>
              </li>
            );
          })}
        </ul>
      ) : (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginTop: 14, display: 'flex', gap: 7, alignItems: 'center' }}>
          <ClipboardPaste size={14} />
          No attachments yet. Copy a chart, statement or screenshot and paste it here.
        </p>
      )}

      {previewItem && (
        <AttachmentPreview item={previewItem} onClose={() => setPreviewItem(null)} />
      )}
    </div>
  );
}
