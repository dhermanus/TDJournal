import { useState, useEffect } from 'react';
import { ChevronDown, ChevronUp, BookOpen, Trash2 } from 'lucide-react';
import { diaryApi, tradesApi, API_BASE } from '../api';
import { PageHeader } from './ui';
import { numOr, detailOf, patchAnalysis as updateAnalysis } from './diaryReview';

const BACKEND = API_BASE;

function ConfidenceDot({ level }) {
  return (
    <span
      className={`confidence-dot confidence-${level || 'unmatched'}`}
      style={{ marginRight: 6 }}
      title={`Match: ${level}`}
    />
  );
}

function DeleteButton({ onDelete, small, label = 'Delete' }) {
  const [confirm, setConfirm] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const handleClick = async (e) => {
    e.stopPropagation();
    if (!confirm) { setConfirm(true); return; }
    setDeleting(true);
    await onDelete().catch(() => { setDeleting(false); setConfirm(false); });
  };

  if (confirm) {
    return (
      <span style={{ display: 'inline-flex', gap: 6 }} onClick={e => e.stopPropagation()}>
        <button
          type="button"
          onClick={handleClick}
          disabled={deleting}
          className="btn btn-danger btn-sm"
        >
          {deleting ? '…' : 'Confirm'}
        </button>
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); setConfirm(false); }}
          className="btn btn-ghost btn-sm"
        >
          Cancel
        </button>
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      className="btn btn-ghost btn-icon"
      title={label}
      aria-label={label}
    >
      <Trash2 size={small ? 14 : 15} />
    </button>
  );
}

const fieldLabel = { color: 'var(--text-secondary)', fontSize: 12.5, marginBottom: 3 };

// A diary match the model guessed at is worth a second look; one a human
// confirmed shows 'manual' and leaves the queue for good.
function MatchReview({ ta, tradeGroup, confidence, onChange }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    r_multiple: ta?.r_multiple ?? '',
    stop_loss: ta?.stop_loss ?? '',
    emotional_state: ta?.emotional_state ?? '',
    mistakes: ta?.mistakes ?? '',
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const confirmed = confidence === 'manual';

  const openEditor = () => {
    if (!open) {
      setForm({
        r_multiple: ta?.r_multiple ?? '',
        stop_loss: ta?.stop_loss ?? '',
        emotional_state: ta?.emotional_state ?? '',
        mistakes: ta?.mistakes ?? '',
      });
    }
    setOpen(o => !o);
    setError(null);
  };

  const confirm = async () => {
    if (!tradeGroup) { setError('This diary line could not be matched to a trade.'); return; }
    setBusy(true);
    setError(null);
    try {
      await tradesApi.updateAnalysis(tradeGroup, { match_confidence: 'manual', match_notes: 'confirmed by you' });
      onChange({ match_confidence: 'manual' });
    } catch (e) {
      setError(detailOf(e, 'Could not confirm this match.'));
    } finally {
      setBusy(false);
    }
  };

  const saveFields = async () => {
    if (!tradeGroup) return;
    setSaving(true);
    setError(null);
    try {
      const patch = {
        r_multiple: numOr(form.r_multiple),
        stop_loss: numOr(form.stop_loss),
        emotional_state: form.emotional_state || null,
        mistakes: form.mistakes || null,
      };
      if (!confirmed) {
        patch.match_confidence = 'manual';
        patch.match_notes = 'confirmed by you';
      }
      const res = await tradesApi.updateAnalysis(tradeGroup, patch);
      onChange(res.data);
      setOpen(false);
    } catch (e) {
      setError(detailOf(e, 'Could not save these fields.'));
    } finally {
      setSaving(false);
    }
  };

  if (!tradeGroup) {
    return (
      <div className="notice" role="status" style={{ marginTop: 12, fontSize: 13.5 }}>
        Not matched to any trade on this date — nothing to confirm.
      </div>
    );
  }

  return (
    <div style={{ marginTop: 12, borderTop: '1px solid var(--divider-soft)', paddingTop: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <button type="button" className="btn btn-sm" onClick={confirm} disabled={busy || confirmed}
          aria-label={confirmed ? 'Match confirmed' : 'Confirm this diary match'}>
          {confirmed ? 'Match confirmed' : busy ? 'Confirming…' : 'Confirm match'}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={openEditor}
          aria-expanded={open} aria-label="Edit extracted fields">
          {open ? 'Close' : 'Edit fields'}
        </button>
        {error && <span className="neg" role="alert" style={{ fontSize: 13 }}>{error}</span>}
      </div>

      {open && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10, marginTop: 10 }}>
          <label style={fieldLabel}>
            R Multiple
            <input type="number" step="0.01" className="input" value={form.r_multiple}
              onChange={e => setForm(f => ({ ...f, r_multiple: e.target.value }))} />
          </label>
          <label style={fieldLabel}>
            Stop Loss ($)
            <input type="number" step="0.01" className="input" value={form.stop_loss}
              onChange={e => setForm(f => ({ ...f, stop_loss: e.target.value }))} />
          </label>
          <label style={fieldLabel}>
            Emotional State
            <input type="text" className="input" value={form.emotional_state}
              onChange={e => setForm(f => ({ ...f, emotional_state: e.target.value }))} />
          </label>
          <label style={fieldLabel}>
            Mistakes
            <input type="text" className="input" value={form.mistakes}
              onChange={e => setForm(f => ({ ...f, mistakes: e.target.value }))} />
          </label>
          <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 8 }}>
            <button type="button" className="btn btn-sm" onClick={saveFields} disabled={saving}>
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function DiaryCard({ entry, onDeleted }) {
  const [expanded, setExpanded] = useState(false);
  // A .txt or .csv upload also stores a path, but only a picture can be shown as one.
  const hasImage = !!entry.image_path && /\.(png|jpe?g|webp|gif|heic|heif)$/i.test(entry.image_path);
  const [analysis, setAnalysis] = useState(entry.ai_analysis);
  const panelId = `diary-entry-${entry.id}`;

  // A field save or a confirmation changes one extraction: patch it in place so
  // the card stays open instead of re-fetching and collapsing.
  const patchAnalysis = (index, fields) => {
    setAnalysis(prev => updateAnalysis(prev, index, fields));
  };

  return (
    <div className="card" style={{ marginBottom: 'var(--space-3)' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
        <button
          type="button"
          className="diary-toggle"
          aria-expanded={expanded}
          aria-controls={panelId}
          onClick={() => setExpanded(v => !v)}
        >
          {/* Thumbnail */}
          {hasImage ? (
            <img
              src={`${BACKEND}/uploads/${entry.image_path}`}
              alt=""
              style={{ width: 76, height: 56, objectFit: 'cover', borderRadius: 'var(--radius-sm)', border: '1px solid var(--divider)', flexShrink: 0 }}
              onError={e => { e.target.style.display = 'none'; }}
            />
          ) : (
            <div style={{ width: 76, height: 56, background: 'var(--surface-inset)', borderRadius: 'var(--radius-sm)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
              <BookOpen size={18} color="var(--text-tertiary)" />
            </div>
          )}

          {/* Summary */}
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <span className="num" style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Entry #{entry.id}</span>
              {analysis && (
                <span className="chip">
                  <span className="num">{analysis.trade_analyses?.length || 0}</span>&nbsp;trades
                </span>
              )}
            </div>
            {analysis?.overall_summary ? (
              <div style={{ color: 'var(--text-primary)', fontSize: 14.5, lineHeight: 1.55, marginTop: 6, display: '-webkit-box', WebkitLineClamp: expanded ? 'none' : 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                {analysis.overall_summary}
              </div>
            ) : (
              <div style={{ color: 'var(--text-secondary)', fontSize: 14, marginTop: 6 }}>No AI analysis available.</div>
            )}
          </div>
          <span style={{ color: 'var(--text-secondary)', paddingTop: 2 }} aria-hidden="true">
            {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </span>
        </button>
        <DeleteButton label={`Delete diary entry ${entry.id}`} onDelete={() => diaryApi.delete(entry.id).then(() => onDeleted(entry.id))} />
      </div>

      {expanded && analysis && (
        <div id={panelId} style={{ marginTop: 16, borderTop: '1px solid var(--divider)', paddingTop: 16 }}>

          {hasImage && (
            <div style={{ marginBottom: 16, textAlign: 'center' }}>
              <img
                src={`${BACKEND}/uploads/${entry.image_path}`}
                alt={`Diary entry ${entry.id}`}
                style={{ maxWidth: '100%', borderRadius: 'var(--radius-md)', border: '1px solid var(--divider)' }}
              />
            </div>
          )}

          <div className="grid-2" style={{ marginBottom: 16 }}>
            {analysis.patterns_identified?.length > 0 && (
              <div>
                <h3 style={{ fontWeight: 600, marginBottom: 8, fontSize: 15 }}>Patterns Identified</h3>
                <ul style={{ paddingLeft: 20, color: 'var(--text-secondary)', fontSize: 14, lineHeight: 1.75 }}>
                  {analysis.patterns_identified.map((p, i) => <li key={i}>{p}</li>)}
                </ul>
              </div>
            )}
            {analysis.improvement_areas?.length > 0 && (
              <div>
                <h3 style={{ fontWeight: 600, marginBottom: 8, fontSize: 15 }}>Improvement Areas</h3>
                <ul style={{ paddingLeft: 20, color: 'var(--text-secondary)', fontSize: 14, lineHeight: 1.75 }}>
                  {analysis.improvement_areas.map((a, i) => <li key={i}>{a}</li>)}
                </ul>
              </div>
            )}
          </div>

          {analysis.trade_analyses?.length > 0 && (
            <div>
              <h3 style={{ fontWeight: 600, marginBottom: 12, fontSize: 16 }}>Trade Analyses</h3>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {analysis.trade_analyses.map((ta, i) => (
                  <div key={i} style={{ background: 'var(--surface-inset)', border: '1px solid var(--divider-soft)', borderRadius: 'var(--radius-md)', padding: '14px 16px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10, flexWrap: 'wrap' }}>
                      <span style={{ fontWeight: 600, fontSize: 15 }}>{ta.ticker}</span>
                      {ta.strategy && <span className="chip accent">{ta.strategy}</span>}
                      <div style={{ display: 'flex', alignItems: 'center', marginLeft: 'auto', fontSize: 13, color: 'var(--text-secondary)', gap: 6 }}>
                        <ConfidenceDot level={ta.match_confidence} />
                        {ta.match_confidence === 'manual' ? 'you confirmed' : ta.match_confidence}
                      </div>
                    </div>

                    {/* The model's own verdict, kept when it differs from the
                        check against the real trades — a disagreement is the
                        thing worth seeing, not something to hide. */}
                    {ta.model_match_confidence
                      && ta.model_match_confidence !== ta.match_confidence
                      && ta.match_confidence !== 'manual' && (
                      <div className="notice" style={{ marginBottom: 12, fontSize: 13 }}>
                        The model called this <strong>{ta.model_match_confidence}</strong>
                        {ta.model_match_notes ? `: ${ta.model_match_notes}` : ''}.
                        Checked against the trades on this date, it is{' '}
                        <strong>{ta.match_confidence}</strong>{ta.match_notes ? ` — ${ta.match_notes}` : ''}.
                      </div>
                    )}

                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 12, fontSize: 14, lineHeight: 1.5 }}>
                      {ta.entry_reason && (
                        <div>
                          <div style={fieldLabel}>Entry Reason</div>
                          <div>{ta.entry_reason}</div>
                        </div>
                      )}
                      {ta.exit_reason && (
                        <div>
                          <div style={fieldLabel}>Exit Reason</div>
                          <div>{ta.exit_reason}</div>
                        </div>
                      )}
                      {ta.emotional_state && (
                        <div>
                          <div style={fieldLabel}>Emotional State</div>
                          <div>{ta.emotional_state}</div>
                        </div>
                      )}
                      {ta.r_multiple != null && (
                        <div>
                          <div style={fieldLabel}>R Multiple</div>
                          <div className={`num ${ta.r_multiple >= 0 ? 'pos' : 'neg'}`} style={{ fontWeight: 600 }}>
                            {ta.r_multiple > 0 ? '+' : ''}{Number(ta.r_multiple).toFixed(2)}R
                          </div>
                        </div>
                      )}
                      {ta.mistakes && (
                        <div>
                          <div style={fieldLabel}>Mistakes</div>
                          <div className="neg">{ta.mistakes}</div>
                        </div>
                      )}
                    </div>

                    {ta.ai_feedback && (
                      <div className="notice accent" style={{ marginTop: 12, fontSize: 14 }}>
                        {ta.ai_feedback}
                      </div>
                    )}

                    <MatchReview
                      ta={ta}
                      tradeGroup={ta.trade_group}
                      confidence={ta.match_confidence}
                      onChange={fields => patchAnalysis(i, fields)}
                    />
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function Diary({ accountId }) {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    setLoading(true);
    const params = {};
    if (accountId != null) params.account_id = accountId;
    diaryApi.list(params)
      .then(r => { setEntries(r.data); setLoading(false); })
      .catch(e => { setError(e.message); setLoading(false); });
  }, [accountId]);

  const removeEntry = (id) => setEntries(prev => prev.filter(e => e.id !== id));

  const removeDate = (date) => setEntries(prev => prev.filter(e => e.entry_date !== date));

  // Group by date descending
  const byDate = entries.reduce((acc, e) => {
    (acc[e.entry_date] = acc[e.entry_date] || []).push(e);
    return acc;
  }, {});
  const dates = Object.keys(byDate).sort((a, b) => b.localeCompare(a));

  return (
    <div>
      <PageHeader
        title="Diary"
        subtitle={!loading && entries.length > 0
          ? <><span className="num">{entries.length}</span> {entries.length === 1 ? 'entry' : 'entries'} across <span className="num">{dates.length}</span> {dates.length === 1 ? 'day' : 'days'}</>
          : 'AI readings of your trading notes, newest first'}
      />

      {loading && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {[...Array(3)].map((_, i) => <div key={i} className="skeleton" style={{ height: 100 }} />)}
        </div>
      )}

      {error && (
        <div className="notice neg" role="alert">{error}</div>
      )}

      {!loading && entries.length === 0 && (
        <div className="card" style={{ textAlign: 'center', padding: 48, color: 'var(--text-secondary)' }}>
          <BookOpen size={40} style={{ marginBottom: 16, opacity: 0.6 }} />
          <div style={{ fontSize: 17, fontWeight: 600, marginBottom: 8, color: 'var(--text-primary)' }}>No diary entries yet</div>
          <div style={{ fontSize: 14 }}>Upload a diary screenshot on the Import page to get started.</div>
        </div>
      )}

      {dates.map(date => (
        <section key={date} style={{ marginBottom: 'var(--space-6)' }} aria-label={`Diary entries for ${date}`}>
          {/* Date group header */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10, gap: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <h2 className="section-title num">{date}</h2>
              <span className="chip">
                <span className="num">{byDate[date].length}</span>&nbsp;{byDate[date].length === 1 ? 'entry' : 'entries'}
              </span>
            </div>
            {byDate[date].length > 1 && (
              <DeleteButton
                label={`Delete all diary entries for ${date}`}
                onDelete={() => diaryApi.deleteByDate(date, accountId).then(() => removeDate(date))}
                small
              />
            )}
          </div>

          {/* Individual entry cards */}
          {byDate[date].map(entry => (
            <DiaryCard
              key={entry.id}
              entry={entry}
              onDeleted={removeEntry}
            />
          ))}
        </section>
      ))}
    </div>
  );
}
