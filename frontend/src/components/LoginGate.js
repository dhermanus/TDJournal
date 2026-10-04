import { useState, useRef, useEffect } from 'react';
import { Lock, ArrowRight, AlertCircle } from 'lucide-react';

/**
 * Shown instead of the app when the deployment asks for a password.
 *
 * `status` decides what this renders: while it is `checking` we show nothing
 * rather than flashing the login form at a user who is already signed in on
 * every normal (auth-off) start, and an unconfigured server never asks for a
 * password at all. The gate is therefore a no-op for local development.
 */
export default function LoginGate({ status, onAuthenticated }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef(null);

  useEffect(() => {
    if (status === 'required') inputRef.current?.focus();
  }, [status]);

  if (status !== 'required') return null;

  const submit = async (e) => {
    e.preventDefault();
    if (!password) return;
    setBusy(true);
    setError(null);
    try {
      await onAuthenticated(password);
      setPassword('');
    } catch (err) {
      setError(err?.response?.data?.detail || err?.response?.data?.error || 'Sign in failed.');
      inputRef.current?.focus();
      inputRef.current?.select?.();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="app-shell" style={{ placeItems: 'center' }}>
      <main
        id="main"
        style={{ maxWidth: 380, width: '100%', display: 'grid', gap: 16, justifyItems: 'stretch' }}
      >
        <div style={{ display: 'grid', gap: 6, justifyItems: 'center', textAlign: 'center' }}>
          <Lock size={26} color="var(--accent-line)" aria-hidden="true" />
          <h1 className="section-title" style={{ margin: 0 }}>TDJournal</h1>
          <div className="section-sub">This journal is password protected.</div>
        </div>

        <form onSubmit={submit} style={{ display: 'grid', gap: 12 }} aria-label="Sign in">
          <div>
            <label className="field-label" htmlFor="tdj-password">Password</label>
            <input
              id="tdj-password"
              ref={inputRef}
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              style={{ width: '100%' }}
              disabled={busy}
              required
            />
          </div>

          {error && (
            <div className="notice neg" role="alert" style={{ display: 'block' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <AlertCircle size={14} aria-hidden="true" />{error}
              </div>
            </div>
          )}

          <button type="submit" className="btn btn-primary" disabled={busy || !password}>
            {busy ? 'Signing in…' : <>Sign in <ArrowRight size={15} /></>}
          </button>
        </form>
      </main>
    </div>
  );
}
