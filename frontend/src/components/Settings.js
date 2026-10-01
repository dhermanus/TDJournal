import { useState, useEffect, useCallback, useMemo } from 'react';
import { Plus, Pencil, GitMerge, Trash2, Search } from 'lucide-react';
import { libraryApi, mt5TimezoneApi, backupApi, aiSettingsApi } from '../api';
import { PageHeader } from './ui';

const SECTIONS = [
  { id: 'strategy', label: 'Strategies' },
  { id: 'source', label: 'Sources' },
  { id: 'tag', label: 'Tags' },
  { id: 'ai', label: 'AI' },
  { id: 'import', label: 'Import' },
  { id: 'backup', label: 'Backup' },
];

// Sections that read their count from the loaded library list. Anything else
// (Import, Backup) has no count; falling through to `lib.sources` for an
// unknown id would print a meaningless number on a tab that has no list.
const COUNTED_SECTIONS = new Set(['strategy', 'source', 'tag']);

const TAG_TYPE_LABEL = {
  mistake: 'Mistakes', execution: 'Execution', setup: 'Setup', emotion: 'Emotion', outcome: 'Outcome',
};
const TAG_TYPE_ORDER = ['mistake', 'execution', 'setup', 'emotion', 'outcome'];

const SECTION_COPY = {
  strategy: {
    title: 'Strategies',
    sub: 'Why you took the trade, the same idea as a setup.',
    noun: 'strategy',
  },
  source: {
    title: 'Sources',
    sub: 'Where the idea or alert came from.',
    noun: 'source',
  },
};

const errText = (e) => e?.response?.data?.detail || e?.response?.data?.error || e?.message || 'Something went wrong';
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

const fmtBytes = (n) => (n >= 1024 * 1024
  ? `${(n / (1024 * 1024)).toFixed(1)} MB`
  : `${Math.max(1, Math.round(n / 1024))} KB`);

/** One editable list: strategies, sources, or a single tag type. */
function ItemList({ kind, tagType = '', title, sub, noun, items, onChanged }) {
  const [query, setQuery] = useState('');
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState({ name: '', description: '' });
  const [mode, setMode] = useState(null); // { type: 'edit'|'merge'|'delete', name }
  const [form, setForm] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const base = { kind, tag_type: tagType };
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? items.filter(i => i.name.toLowerCase().includes(q) || (i.description || '').toLowerCase().includes(q)) : items;
  }, [items, query]);

  const close = () => { setMode(null); setForm({}); setError(null); };
  const run = async (fn, message) => {
    setBusy(true); setError(null);
    try {
      const res = await fn();
      setNotice(message(res?.data || {}));
      close();
      await onChanged();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const startAdd = () => { close(); setAdding(true); setDraft({ name: '', description: '' }); };
  const saveAdd = () => run(
    () => libraryApi.create({ ...base, name: draft.name, description: draft.description }),
    () => { setAdding(false); return `Added "${draft.name.trim()}".`; },
  );

  const open = (type, item) => {
    setAdding(false); setError(null); setNotice(null);
    setMode({ type, name: item.name });
    if (type === 'edit') setForm({ name: item.name, description: item.description || '' });
    if (type === 'merge') setForm({ target: '' });
    if (type === 'delete') setForm({ reassign: '' });
  };

  const others = (name) => items.filter(i => i.name !== name);

  return (
    <section className="card panel-flush" aria-label={title}>
      <div className="settings-head">
        <div style={{ minWidth: 0 }}>
          <h2 className="section-title">{title} <span className="text-muted num" style={{ fontWeight: 500, fontSize: 14 }}>{items.length}</span></h2>
          {sub && <div className="section-sub">{sub}</div>}
        </div>
        <div className="settings-tools">
          <label className="settings-search">
            <Search size={14} aria-hidden="true" />
            <input
              type="search"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder={`Search ${title.toLowerCase()}`}
              aria-label={`Search ${title.toLowerCase()}`}
            />
          </label>
          <button type="button" className="btn btn-primary btn-sm" onClick={startAdd}>
            <Plus size={14} /> Add {noun}
          </button>
        </div>
      </div>

      {notice && <div className="notice pos settings-notice" role="status">{notice}</div>}

      {adding && (
        <div className="settings-form">
          <label>
            <span className="field-label">Name</span>
            <input autoFocus value={draft.name} onChange={e => setDraft(d => ({ ...d, name: e.target.value }))}
              onKeyDown={e => { if (e.key === 'Enter' && draft.name.trim()) saveAdd(); if (e.key === 'Escape') setAdding(false); }} />
          </label>
          <label style={{ flex: 2 }}>
            <span className="field-label">Description (optional)</span>
            <input value={draft.description} onChange={e => setDraft(d => ({ ...d, description: e.target.value }))} />
          </label>
          <div className="settings-form-actions">
            <button type="button" className="btn btn-primary btn-sm" disabled={busy || !draft.name.trim()} onClick={saveAdd}>Save</button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setAdding(false); setError(null); }}>Cancel</button>
          </div>
          {error && <div className="notice neg" role="alert" style={{ flexBasis: '100%' }}>{error}</div>}
        </div>
      )}

      <div className="table-container">
        <table style={{ minWidth: 640 }}>
          <thead>
            <tr>
              <th style={{ paddingLeft: 20 }}>Name</th>
              <th>Description</th>
              <th className="num">Trades</th>
              <th className="num" style={{ paddingRight: 20 }}><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {shown.map(item => {
              const active = mode && mode.name === item.name ? mode.type : null;
              const fixed = false;
              return [
                <tr key={item.name} className={active ? 'row-selected' : undefined}>
                  <td style={{ paddingLeft: 20, fontWeight: 600 }}>
                    {item.name}
                    {item.aliases?.length > 0 && (
                      <div className="text-muted" style={{ fontSize: 12.5, fontWeight: 400, marginTop: 2 }}>
                        Also saved as: {item.aliases.join(', ')}
                      </div>
                    )}
                  </td>
                  <td className="text-muted" style={{ fontSize: 14 }}>{item.description || ''}</td>
                  <td className="num">{item.trades}</td>
                  <td className="num" style={{ paddingRight: 20, whiteSpace: 'nowrap' }}>
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => open('edit', item)} aria-label={`Edit ${item.name}`}>
                      <Pencil size={13} /> Edit
                    </button>
                    {!fixed && (
                      <>
                        <button type="button" className="btn btn-ghost btn-sm" onClick={() => open('merge', item)} aria-label={`Merge ${item.name} into another ${noun}`} disabled={items.length < 2}>
                          <GitMerge size={13} /> Merge
                        </button>
                        <button type="button" className="btn btn-ghost btn-sm" style={{ color: 'var(--result-neg)' }} onClick={() => open('delete', item)} aria-label={`Delete ${item.name}`}>
                          <Trash2 size={13} /> Delete
                        </button>
                      </>
                    )}
                  </td>
                </tr>,
                active && (
                  <tr key={`${item.name}-panel`} className="settings-panel-row">
                    <td colSpan={4}>
                      {active === 'edit' && (
                        <div className="settings-form">
                          <label>
                            <span className="field-label">Name</span>
                            <input value={form.name} disabled={fixed} autoFocus={!fixed}
                              onChange={e => setForm(f => ({ ...f, name: e.target.value }))} />
                          </label>
                          <label style={{ flex: 2 }}>
                            <span className="field-label">Description</span>
                            <input value={form.description} autoFocus={fixed}
                              onChange={e => setForm(f => ({ ...f, description: e.target.value }))} />
                          </label>
                          <div className="settings-form-actions">
                            <button type="button" className="btn btn-primary btn-sm" disabled={busy || !String(form.name || '').trim()}
                              onClick={() => run(
                                () => libraryApi.update({ ...base, name: item.name, new_name: form.name, description: form.description }),
                                (r) => (r.name && r.name !== item.name
                                  ? `Renamed "${item.name}" to "${r.name}" on ${plural(r.trades ?? item.trades, 'trade')}.`
                                  : `Saved "${item.name}".`),
                              )}>
                              Save
                            </button>
                            <button type="button" className="btn btn-ghost btn-sm" onClick={close}>Cancel</button>
                          </div>
                          {!fixed && item.trades > 0 && (
                            <div className="text-muted" style={{ flexBasis: '100%', fontSize: 13 }}>
                              Renaming updates {plural(item.trades, 'trade')}.
                            </div>
                          )}
                        </div>
                      )}

                      {active === 'merge' && (
                        <div className="settings-form">
                          <label style={{ flex: 2 }}>
                            <span className="field-label">Merge "{item.name}" into</span>
                            <select value={form.target} autoFocus onChange={e => setForm({ target: e.target.value })}>
                              <option value="">Choose a {noun}…</option>
                              {others(item.name).map(o => <option key={o.name} value={o.name}>{o.name} ({o.trades})</option>)}
                            </select>
                          </label>
                          <div className="settings-form-actions">
                            <button type="button" className="btn btn-primary btn-sm" disabled={busy || !form.target}
                              onClick={() => run(
                                () => libraryApi.merge({ ...base, source_name: item.name, target_name: form.target }),
                                (r) => `Merged "${item.name}" into "${form.target}": ${plural(r.moved ?? item.trades, 'trade')} moved.`,
                              )}>
                              Merge
                            </button>
                            <button type="button" className="btn btn-ghost btn-sm" onClick={close}>Cancel</button>
                          </div>
                          <div className="text-muted" style={{ flexBasis: '100%', fontSize: 13 }}>
                            {form.target
                              ? `Moves ${plural(item.trades, 'trade')} to "${form.target}" and removes "${item.name}". If the AI writes "${item.name}" again, it is saved as "${form.target}".`
                              : 'Pick the name to keep.'}
                          </div>
                        </div>
                      )}

                      {active === 'delete' && (
                        <div className="settings-form">
                          {item.trades > 0 ? (
                            <label style={{ flex: 2 }}>
                              <span className="field-label">{plural(item.trades, 'trade')} use "{item.name}". Reassign them to</span>
                              <select value={form.reassign} autoFocus onChange={e => setForm({ reassign: e.target.value })}>
                                <option value="">Leave blank</option>
                                {others(item.name).map(o => <option key={o.name} value={o.name}>{o.name}</option>)}
                              </select>
                            </label>
                          ) : (
                            <div style={{ flex: 2, alignSelf: 'center' }}>Delete "{item.name}"? No trades use it.</div>
                          )}
                          <div className="settings-form-actions">
                            <button type="button" className="btn btn-danger btn-sm" disabled={busy}
                              onClick={() => run(
                                () => libraryApi.remove({ ...base, name: item.name, reassign_to: form.reassign || null }),
                                (r) => (r.reassigned_to
                                  ? `Deleted "${item.name}". ${plural(r.affected, 'trade')} now use "${r.reassigned_to}".`
                                  : `Deleted "${item.name}".${r.affected ? ` ${plural(r.affected, 'trade')} left blank.` : ''}`),
                              )}>
                              Delete
                            </button>
                            <button type="button" className="btn btn-ghost btn-sm" onClick={close}>Cancel</button>
                          </div>
                        </div>
                      )}
                      {error && <div className="notice neg" role="alert" style={{ marginTop: 10 }}>{error}</div>}
                    </td>
                  </tr>
                ),
              ];
            })}
            {!shown.length && (
              <tr><td colSpan={4}><div className="empty">{query ? 'Nothing matches that search.' : `No ${title.toLowerCase()} yet.`}</div></td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** The endpoint always sends these keys; normalising keeps every read below
 *  safe when a test or a proxy answers with an empty object. */
const normaliseMt5 = (data) => ({
  timezone: '',
  effective: '',
  source: 'unset',
  valid: false,
  error: null,
  current_offset_hours: null,
  candidates: [],
  transitions: [],
  ...(data || {}),
});

/** MT5 imports read broker-server wall time with no offset attached, so the
 *  zone has to be named here before a deal file means anything. */
// ── AI settings: the model, and what is allowed to be sent ───────────────────
//
// Each row states what actually crosses the network, because the app's "no
// telemetry" claim is true of the journal and not of an AI feature once it runs.
// The switch means *nothing is sent*: the server refuses the endpoint before it
// builds a prompt, so off is a refusal rather than a hidden button.
function AiSettings() {
  const [state, setState] = useState(null);
  const [draftModel, setDraftModel] = useState('');
  const [busy, setBusy] = useState(null);   // 'model' or a feature key
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await aiSettingsApi.get();
      setState(res.data);
      setDraftModel(res.data?.model || '');
      setError(null);
    } catch (e) {
      setError(errText(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const save = async (patch, who) => {
    setBusy(who);
    setError(null); setNotice(null);
    try {
      const res = await aiSettingsApi.put(patch);
      setState(res.data);
      if (patch.model != null) setNotice(`Saved ${res.data.model}.`);
      else {
        const name = Object.keys(patch.features)[0];
        setNotice(`${res.data.feature_info[name].label} ${patch.features[name] ? 'on' : 'off'}.`);
      }
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(null);
    }
  };

  if (!state && !error) return <div className="skeleton" style={{ height: 260 }} />;

  const features = Object.keys(state.feature_info);
  return (
    <section className="card" aria-label="AI settings">
      <div className="settings-head">
        <div style={{ minWidth: 0 }}>
          <h2 className="section-title">AI features</h2>
          <div className="section-sub">
            {state.notice} The journal itself never sends anything: only the
            switches you leave on.
          </div>
        </div>
      </div>

      {!state.api_key_configured && (
        <div className="notice" role="status" style={{ marginBottom: 14 }}>
          No API key is set in <span className="num">backend/.env</span>. The features below
          will report that when called.
        </div>
      )}

      {notice && <div className="notice pos settings-notice" role="status">{notice}</div>}

      <div className="settings-form" style={{ marginBottom: 4 }}>
        <label>
          <span className="field-label">Model</span>
          <select
            aria-label="AI model"
            value={draftModel}
            onChange={e => setDraftModel(e.target.value)}
          >
            {state.models.map(m => <option key={m} value={m}>{m}</option>)}
            {draftModel && !state.models.includes(draftModel) && <option value={draftModel}>{draftModel}</option>}
          </select>
        </label>
        <div className="settings-form-actions">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={busy !== null || !draftModel.trim() || draftModel === state.model}
            onClick={() => save({ model: draftModel.trim() }, 'model')}
          >
            {busy === 'model' ? 'Saving…' : 'Save model'}
          </button>
        </div>
      </div>
      <div className="section-sub" style={{ marginBottom: 18 }}>
        Used by every feature below. A cheaper model trades some reasoning depth
        for lower cost per call.
      </div>

      <table className="table">
        <caption className="text-muted" style={{ captionSide: 'top', textAlign: 'left', fontSize: 13, paddingBottom: 8 }}>
          Each row says what is sent when it is on. Turning a row off means that
          feature refuses to run and sends nothing.
        </caption>
        <thead>
          <tr>
            <th scope="col">Feature</th>
            <th scope="col">Sent when on</th>
            <th scope="col" style={{ width: 90 }}>On</th>
          </tr>
        </thead>
        <tbody>
          {features.map(name => {
            const info = state.feature_info[name];
            const on = !!state.features[name];
            const working = busy === name;
            return (
              <tr key={name}>
                <th scope="row" style={{ fontWeight: 600 }}>{info.label}</th>
                <td className="text-muted" style={{ fontSize: 13, fontWeight: 400 }}>
                  {info.sends}
                </td>
                <td>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={on}
                    aria-label={`${info.label} — ${on ? 'on' : 'off'}`}
                    disabled={working}
                    onClick={() => save({ features: { [name]: !on } }, name)}
                    style={{
                      width: 46, height: 24, borderRadius: 12, cursor: working ? 'wait' : 'pointer',
                      border: `1px solid ${on ? 'var(--accent-line)' : 'var(--divider)'}`,
                      background: on ? 'var(--accent-line)' : 'var(--surface-control)',
                      color: on ? 'var(--surface-page)' : 'var(--text-secondary)',
                      fontSize: 12, fontWeight: 700, lineHeight: '20px', padding: 0,
                    }}
                  >
                    {on ? 'On' : 'Off'}
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {error && <div className="notice neg" role="alert" style={{ marginTop: 10 }}>{error}</div>}
    </section>
  );
}


function ImportSettings() {
  const [state, setState] = useState(null);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await mt5TimezoneApi.get();
      setState(normaliseMt5(res.data));
      setDraft(res.data?.timezone || res.data?.effective || '');
      setError(null);
    } catch (e) {
      setError(errText(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setBusy(true); setError(null); setNotice(null);
    try {
      const res = await mt5TimezoneApi.put(draft.trim());
      setState(normaliseMt5(res.data));
      setNotice(`Saved ${res.data?.effective || draft.trim()}. Imports will convert with this zone.`);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  if (!state && !error) return <div className="skeleton" style={{ height: 220 }} />;

  const candidates = state.candidates || [];
  const transitions = state.transitions || [];

  return (
    <section className="card" aria-label="MT5 import settings">
      <div className="settings-head">
        <div style={{ minWidth: 0 }}>
          <h2 className="section-title">MT5 timestamps</h2>
          <div className="section-sub">
            MT5 exports the broker server's own clock with no timezone attached.
            Name the zone the server runs on and every deal and bar is converted
            to UTC before it is stored, using the daylight-saving rule that was
            in force on that date.
          </div>
        </div>
      </div>

      {notice && <div className="notice pos settings-notice" role="status">{notice}</div>}

      <div className="settings-form">
        <label>
          <span className="field-label">Broker server timezone</span>
          <select value={draft} onChange={e => { setDraft(e.target.value); setNotice(null); }}>
            <option value="">Choose a timezone…</option>
            {draft && !candidates.includes(draft) && <option value={draft}>{draft}</option>}
            {candidates.map(z => <option key={z} value={z}>{z}</option>)}
          </select>
        </label>
        <div className="settings-form-actions">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={busy || !draft.trim() || draft.trim() === state.effective}
            onClick={save}
          >
            Save timezone
          </button>
        </div>
      </div>

      {error && <div className="notice neg" role="alert" style={{ marginTop: 10 }}>{error}</div>}

      <div className="section-sub" style={{ marginTop: 12 }}>
        {state.valid ? (
          <>
            {state.source === 'setting' ? 'Saved setting.' : 'Not saved yet — using the environment value.'}
            {' '}Current offset on this zone: {state.current_offset_hours > 0 ? '+' : ''}
            {state.current_offset_hours} h.
            {' '}That is today's offset; imports use each date's own offset.
          </>
        ) : (
          <span role="alert">No usable timezone yet. Imports will refuse to convert timestamps until this is set.</span>
        )}
      </div>

      {transitions.length > 0 && (
        <table className="table" style={{ marginTop: 14 }}>
          <caption className="text-muted" style={{ captionSide: 'top', textAlign: 'left', fontSize: 13, paddingBottom: 8 }}>
            Clock changes on this zone — check they match what your broker says.
          </caption>
          <thead>
            <tr><th scope="col">At (UTC)</th><th scope="col">Offset before</th><th scope="col">Offset after</th></tr>
          </thead>
          <tbody>
              {transitions.map(t => (
              <tr key={t.at_utc}>
                <td className="num">{t.at_utc}</td>
                <td className="num">+{t.offset_hours_before} h</td>
                <td className="num">+{t.offset_hours_after} h</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** Backup, restore, and export. All local: an archive written to a folder on
 *  this machine, never to a network destination. */
function BackupSettings() {
  const [dest, setDest] = useState(null);
  const [draft, setDraft] = useState('');
  const [archives, setArchives] = useState([]);
  const [busy, setBusy] = useState(null);   // 'save' | 'backup' | restore name
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [restored, setRestored] = useState(null);

  const load = useCallback(async () => {
    try {
      const [d, list] = await Promise.all([backupApi.getDestination(), backupApi.list()]);
      setDest(d.data);
      setDraft(d.data?.folder || '');
      setArchives(list.data?.archives || []);
      setError(null);
    } catch (e) {
      setError(errText(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const saveFolder = async () => {
    setBusy('save'); setError(null); setNotice(null);
    try {
      const res = await backupApi.setDestination(draft.trim());
      setDest(res.data);
      setNotice('Backup folder saved.');
      await load();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(null);
    }
  };

  const runBackup = async () => {
    setBusy('backup'); setError(null); setNotice(null); setRestored(null);
    try {
      const res = await backupApi.create();
      setNotice(`${res.data.name} written — ${fmtBytes(res.data.size_bytes)}.`);
      await load();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(null);
    }
  };

  // Restoring writes to a *new* folder; the running journal is untouched, so
  // there is nothing destructive to confirm here.
  const runRestore = async (name) => {
    setBusy(name); setError(null); setNotice(null); setRestored(null);
    try {
      const res = await backupApi.restore(name);
      setRestored(res.data);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(null);
    }
  };

  const ready = dest?.exists;

  return (
    <section className="card" aria-label="Backup and export settings">
      <div className="settings-head">
        <div style={{ minWidth: 0 }}>
          <h2 className="section-title">Backup &amp; export</h2>
          <div className="section-sub">
            A backup is a single archive containing the database <em>and</em> your
            attached files, written through SQLite&apos;s own snapshot API so it is
            consistent even while the app is running.
          </div>
        </div>
      </div>

      {notice && <div className="notice pos settings-notice" role="status">{notice}</div>}
      {error && <div className="notice neg" role="alert">{error}</div>}

      <div className="settings-form">
        <label>
          <span className="field-label">Backup folder (full path)</span>
          <input
            type="text"
            value={draft}
            placeholder="C:\Users\you\Documents\TDJournal-backups"
            onChange={e => { setDraft(e.target.value); setNotice(null); }}
            style={{ width: '100%' }}
          />
        </label>
        <div className="settings-form-actions">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={busy === 'save' || !draft.trim() || draft.trim() === dest?.folder}
            onClick={saveFolder}
          >
            {busy === 'save' ? 'Saving…' : 'Save folder'}
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={busy !== null || !ready}
            onClick={runBackup}
          >
            {busy === 'backup' ? 'Backing up…' : 'Create backup'}
          </button>
          <a
            className="btn btn-ghost btn-sm"
            href={backupApi.exportUrl('json')}
            download="tdjournal-export.json"
          >
            Export JSON
          </a>
          <a
            className="btn btn-ghost btn-sm"
            href={backupApi.exportUrl('csv')}
            download="tdjournal-export.csv.zip"
          >
            Export CSV
          </a>
        </div>
      </div>

      {!ready && (
        <div className="section-sub" style={{ marginTop: 12 }}>
          {dest?.set
            ? 'The saved folder no longer exists. Choose another one to enable backups.'
            : 'Choose a folder first — backups are written there, and the app remembers it.'}
        </div>
      )}

      <h3 className="section-sub" style={{ marginTop: 20, fontWeight: 600 }}>
        Backups {archives.length > 0 && <span className="text-muted num">({archives.length})</span>}
      </h3>

      {archives.length ? (
        <table className="table" style={{ marginTop: 8 }}>
          <thead>
            <tr>
              <th scope="col">Archive</th>
              <th scope="col">Created</th>
              <th scope="col">Contents</th>
              <th scope="col" style={{ textAlign: 'right' }}>Restore</th>
            </tr>
          </thead>
          <tbody>
            {archives.map(a => (
              <tr key={a.name}>
                <td>
                  <span title={a.name}>{a.name}</span>
                  <div className="text-muted num" style={{ fontSize: 12 }}>
                    {fmtBytes(a.size_bytes)}
                    {a.alembic_revision ? ` · schema ${a.alembic_revision}` : ''}
                  </div>
                  {a.error && <div role="alert" style={{ color: 'var(--caution)', fontSize: 12 }}>{a.error}</div>}
                </td>
                <td className="num" style={{ whiteSpace: 'nowrap' }}>
                  {(a.created_at || a.modified || '').replace('T', ' ').slice(0, 16)}
                </td>
                <td className="num" style={{ fontSize: 12 }}>
                  {a.tables
                    ? `${Object.keys(a.tables).length} tables · ${Object.values(a.tables).reduce((n, v) => n + v, 0)} rows`
                    : '—'}
                  <div className="text-muted">{a.attachment_files ?? 0} attached file(s)</div>
                </td>
                <td style={{ textAlign: 'right' }}>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    disabled={busy !== null || !!a.error}
                    onClick={() => runRestore(a.name)}
                  >
                    {busy === a.name ? 'Restoring…' : 'Restore'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="text-muted" style={{ fontSize: 13 }}>
          No backups yet. Restore writes into a new folder beside the archive so you can
          inspect it — it never replaces the journal you are using.
        </p>
      )}

      {restored && (
        <div className="notice pos settings-notice" role="status" style={{ marginTop: 14 }}>
          Restored {restored.restored_members} file(s) to <strong>{restored.folder}</strong>.
          {restored.manifest?.created_at && ` From a backup made ${restored.manifest.created_at}.`}
          The running journal was not changed.
        </div>
      )}
    </section>
  );
}

export default function Settings() {
  const [lib, setLib] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [section, setSection] = useState('strategy');

  const load = useCallback(async () => {
    try {
      const res = await libraryApi.list();
      setLib(res.data);
      setLoadError(null);
    } catch (e) {
      setLoadError(errText(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const onTabKey = (e) => {
    const i = SECTIONS.findIndex(s => s.id === section);
    if (e.key === 'ArrowRight') setSection(SECTIONS[(i + 1) % SECTIONS.length].id);
    if (e.key === 'ArrowLeft') setSection(SECTIONS[(i - 1 + SECTIONS.length) % SECTIONS.length].id);
  };

  return (
    <div>
      <PageHeader
        title="Settings"
        subtitle="Clean up the names the journal uses. Renames and merges update every trade that uses the name."
      />

      <div className="tabs" role="tablist" aria-label="Settings sections" style={{ marginBottom: 'var(--space-5)' }}>
        {SECTIONS.map(s => (
          <button
            type="button"
            key={s.id}
            role="tab"
            id={`settings-tab-${s.id}`}
            aria-selected={section === s.id}
            aria-controls="settings-panel"
            tabIndex={section === s.id ? 0 : -1}
            className="tab"
            onClick={() => setSection(s.id)}
            onKeyDown={onTabKey}
          >
            {s.label}
            {lib && COUNTED_SECTIONS.has(s.id) && (
              <span className="text-muted num" style={{ marginLeft: 6, fontWeight: 500 }}>
                {s.id === 'tag'
                  ? TAG_TYPE_ORDER.reduce((n, t) => n + (lib.tags?.[t]?.length || 0), 0)
                  : (s.id === 'strategy' ? lib.strategies : lib.sources).length}
              </span>
            )}
          </button>
        ))}
      </div>

      <div role="tabpanel" id="settings-panel" aria-labelledby={`settings-tab-${section}`}>
        {/* Import and Backup do not read the library, so a library load failure
            (or its loading skeleton) must not cover them up. */}
        {loadError && !COUNTED_SECTIONS.has(section) && <div className="notice neg" role="alert">{loadError}</div>}
        {!lib && !loadError && COUNTED_SECTIONS.has(section) && <div className="skeleton" style={{ height: 320 }} />}

        {lib && section === 'strategy' && (
          <ItemList kind="strategy" {...SECTION_COPY.strategy} items={lib.strategies} onChanged={load} />
        )}
        {lib && section === 'source' && (
          <ItemList kind="source" {...SECTION_COPY.source} items={lib.sources} onChanged={load} />
        )}
        {lib && section === 'tag' && (
          <div className="stack">
            {TAG_TYPE_ORDER.map(t => (
              <ItemList
                key={t}
                kind="tag"
                tagType={t}
                title={`${TAG_TYPE_LABEL[t]} tags`}
                noun="tag"
                items={lib.tags?.[t] || []}
                onChanged={load}
              />
            ))}
          </div>
        )}
        {section === 'ai' && <AiSettings />}
        {section === 'import' && <ImportSettings />}
        {section === 'backup' && <BackupSettings />}
      </div>
    </div>
  );
}
