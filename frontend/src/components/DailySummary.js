import { useState, useEffect, useRef, useCallback } from 'react';
import {
  ChevronLeft, ChevronRight, RotateCcw, Calendar,
  AlertTriangle
} from 'lucide-react';
import { tradesApi, kpisApi, diaryApi, dailySummaryApi, weeklySummaryApi } from '../api';
import { PageHeader, PanelHead } from './ui';
import { usageLine } from './aiUsage';
import { DayCurve, DayMeasures, Coaching, DayTrades, WeeklySummary } from '../v3/ReviewParts';
import {
  BarChart, Bar, XAxis, YAxis, ReferenceLine,
  Tooltip, ResponsiveContainer, Cell
} from 'recharts';

// ── date helpers ──────────────────────────────────────────────────────────────
function prevTradingDay(iso) {
  const d = new Date(iso + 'T12:00:00');
  d.setDate(d.getDate() - 1);
  while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() - 1);
  return d.toISOString().split('T')[0];
}
function nextTradingDay(iso) {
  const d = new Date(iso + 'T12:00:00');
  d.setDate(d.getDate() + 1);
  while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() + 1);
  return d.toISOString().split('T')[0];
}
function formatDateLabel(iso) {
  const d = new Date(iso + 'T12:00:00');
  return d.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
}

// ── R-Multiple chart ──────────────────────────────────────────────────────────
function RMultipleChart({ trades }) {
  const data = trades.filter(t => t.r_multiple != null).map(t => ({
    name: t.ticker,
    r: Number(t.r_multiple),
  }));
  if (!data.length) return null;
  return (
    <section className="card">
      <PanelHead title="R-Multiple by Trade" />
      <ResponsiveContainer width="100%" height={140}>
        <BarChart data={data} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
          <XAxis dataKey="name" tick={{ fontSize: 12, fill: 'var(--text-secondary)' }} axisLine={false} tickLine={false} />
          <YAxis tick={{ fontSize: 11, fill: 'var(--text-secondary)' }} axisLine={false} tickLine={false} />
          <ReferenceLine y={0} stroke="var(--divider)" />
          <Tooltip
            cursor={{ fill: 'var(--accent-soft)' }}
            contentStyle={{ background: 'var(--surface-panel)', border: '1px solid var(--divider)', borderRadius: 6, fontSize: 13, color: 'var(--text-primary)' }}
            formatter={(v) => [`${v.toFixed(2)}R`, 'R-Multiple']}
          />
          <Bar dataKey="r" radius={[4, 4, 0, 0]}>
            {data.map((entry, i) => (
              <Cell key={i} fill={entry.r >= 0 ? 'var(--green)' : 'var(--red)'} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </section>
  );
}

// ── Main component ────────────────────────────────────────────────────────────
export default function DailySummary({ accountId, date, onDateChange, onOpenDetail }) {
  const [trades, setTrades] = useState([]);
  const [kpis, setKpis] = useState(null);
  const [, setDiary] = useState(null);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState(null);
  const [regenerating, setRegenerating] = useState(false);
  const [cbDismissed, setCbDismissed] = useState(false);
  // The weekly synthesis belongs to the week this day is in, so it is dropped
  // when the day moves rather than left showing the previous week's answer.
  const [weeklyOpen, setWeeklyOpen] = useState(false);
  const [weekly, setWeekly] = useState(null);
  const [weeklyLoading, setWeeklyLoading] = useState(false);
  const [weeklyError, setWeeklyError] = useState(null);
  const weeklySeqRef = useRef(0);
  const weeklyPendingRef = useRef(null);
  const allTimeKpisRef = useRef(null);

  const today = new Date().toISOString().split('T')[0];

  const fetchAllTimeKpis = useCallback(async () => {
    if (allTimeKpisRef.current) return;
    try {
      const params = {};
      if (accountId != null) params.account_id = accountId;
      const res = await kpisApi.get(params);
      allTimeKpisRef.current = res.data;
    } catch (e) {
      console.error('Failed to load all-time kpis', e);
    }
  }, [accountId]);

  const fetchDay = useCallback(async (d) => {
    setLoading(true);
    setSummaryLoading(true);
    setSummary(null);
    setCbDismissed(false);
    // Discard an answer still in flight for the day we just left, so the card
    // can never show last week's synthesis under this week's header.
    weeklySeqRef.current += 1;
    weeklyPendingRef.current = null;
    setWeekly(null);
    setWeeklyError(null);
    setWeeklyLoading(false);
    try {
      const params = { date_from: d, date_to: d };
      if (accountId != null) params.account_id = accountId;

      const [tradesRes, kpisRes, diaryRes] = await Promise.all([
        tradesApi.list(params),
        kpisApi.get(params),
        diaryApi.list(accountId != null ? { account_id: accountId } : {}),
      ]);

      // Merge trade_analysis fields if present
      const rawTrades = tradesRes.data || [];
      setTrades(rawTrades);
      setKpis(kpisRes.data || null);

      const diaryEntries = diaryRes.data || [];
      const dayDiary = diaryEntries.find(e => e.entry_date === d) || null;
      setDiary(dayDiary);
    } catch (e) {
      console.error('Failed to load day data', e);
    } finally {
      setLoading(false);
    }

    // Fetch AI summary separately (can be slow)
    try {
      const sumParams = { date: d };
      if (accountId != null) sumParams.account_id = accountId;
      const sumRes = await dailySummaryApi.get(sumParams);
      setSummary(sumRes.data);
      setSummaryError(null);
    } catch (e) {
      // A refusal (feature turned off) and a failed call both have to be readable
      // in the panel — the console is where this used to disappear.
      console.error('Failed to load summary', e);
      setSummaryError(e.response?.data?.detail || 'Could not load the AI report.');
    } finally {
      setSummaryLoading(false);
    }
  }, [accountId]);

  useEffect(() => {
    fetchAllTimeKpis();
  }, [fetchAllTimeKpis]);

  useEffect(() => {
    fetchDay(date);
  }, [date, fetchDay]);

  const handleRegenerate = async () => {
    setRegenerating(true);
    setSummaryLoading(true);
    setSummary(null);
    try {
      const params = { date, force: true };
      if (accountId != null) params.account_id = accountId;
      const res = await dailySummaryApi.get(params);
      setSummary(res.data);
      setSummaryError(null);
    } catch (e) {
      console.error('Regenerate failed', e);
      setSummaryError(e.response?.data?.detail || 'Could not regenerate the AI report.');
    } finally {
      setRegenerating(false);
      setSummaryLoading(false);
    }
  };

  // Reading the week is on demand: the first open asks for it, re-opening a
  // card that still holds its answer does not ask again, and moving to another
  // day re-asks because that answer is about a different week. `force` is only
  // ever the Regenerate button.
  const fetchWeekly = useCallback(async (force = false) => {
    // React StrictMode runs every effect twice in development, and a plain
    // "am I open and empty" guard still asks twice — so the in-flight key is
    // checked first, before anything is mutated. Ordering matters: bumping the
    // sequence number before this return would leave the first request holding
    // a stale seq and it would throw its own answer away.
    const key = `${date}|${accountId ?? ''}`;
    if (!force && weeklyPendingRef.current === key) return;
    // Two Previous clicks in a row can leave an older week's answer in flight.
    const seq = ++weeklySeqRef.current;
    weeklyPendingRef.current = key;
    setWeeklyLoading(true);
    setWeeklyError(null);
    try {
      const params = { date, force };
      if (accountId != null) params.account_id = accountId;
      const res = await weeklySummaryApi.get(params);
      if (seq !== weeklySeqRef.current) return;
      // A body of `null` must still count as an answer: the effect decides
      // "has it asked yet?" by `weekly == null`, so storing null straight from
      // the response would ask again on every render, forever.
      setWeekly(res.data || {});
    } catch (e) {
      if (seq !== weeklySeqRef.current) return;
      // A refused feature (403) and a failed call both have to be readable in
      // the card — the console is where this used to disappear.
      console.error('Weekly summary failed', e);
      setWeeklyError(e.response?.data?.detail || 'Could not load the weekly summary.');
    } finally {
      if (weeklyPendingRef.current === key) weeklyPendingRef.current = null;
      if (seq === weeklySeqRef.current) setWeeklyLoading(false);
    }
  }, [date, accountId]);

  // One effect handles the on-open request and a dropped answer (a new day).
  // `weeklyError` is in the deps but guarded by `!weeklyError`, so a failure
  // clears nothing: the retry path is the card's Try again button, and an
  // unguarded retry against a 403 would spin forever.
  useEffect(() => {
    if (weeklyOpen && weekly == null && !weeklyError) fetchWeekly(false);
  }, [weeklyOpen, weekly, weeklyError, fetchWeekly]);

  // Consecutive losing trades from end of today's list
  const consecutiveLosses = (() => {
    let count = 0;
    for (let i = trades.length - 1; i >= 0; i--) {
      if ((trades[i].net_pnl || 0) < 0) count++;
      else break;
    }
    return count;
  })();

  // Build grade map for trades table
  const tradeGradeMap = {};
  if (summary?.trade_grades) {
    summary.trade_grades.forEach(g => { tradeGradeMap[g.trade_group] = g; });
  }

  return (
    <div>
      {/* ── Header ── */}
      <PageHeader
        title={formatDateLabel(date)}
        subtitle="Day Review. Read the session while the decisions are fresh."
        actions={<>
          <button type="button" className="btn btn-secondary" onClick={() => onDateChange(prevTradingDay(date))} aria-label="Previous trading day">
            <ChevronLeft size={16} /> Previous
          </button>
          <button type="button" className="btn btn-secondary" onClick={() => onDateChange(nextTradingDay(date))} aria-label="Next trading day">
            Next <ChevronRight size={16} />
          </button>
          {date !== today && (
            <button type="button" className="btn btn-ghost" onClick={() => onDateChange(today)}>
              <Calendar size={15} aria-hidden="true" /> Today
            </button>
          )}
          <button
            type="button"
            className="btn btn-secondary"
            onClick={handleRegenerate}
            disabled={regenerating || loading}
          >
            <RotateCcw size={14} style={{ animation: regenerating ? 'spin 1s linear infinite' : 'none' }} aria-hidden="true" />
            {regenerating ? 'Regenerating...' : 'Regenerate AI'}
          </button>
        </>}
      />

      {/* ── KPI Strip ── */}
      {loading ? (
        <div className="v3-band"><div className="v3-empty">Loading…</div></div>
      ) : (
        <>
          <div className="v3-hero" style={{ paddingTop: 8 }}>
            <div className="v3-sec-head" style={{ marginBottom: 6 }}>
              <div>
                <h2 className="v3-h">The session</h2>
                <p className="v3-h-sub">
                  Running P&amp;L from the open to the close, with every trade marked where you entered it
                </p>
              </div>
            </div>
            <DayCurve trades={trades} onPick={(t) => onOpenDetail && onOpenDetail(t, trades)} />
          </div>
          <DayMeasures kpis={kpis} trades={trades} summary={summary} allTime={allTimeKpisRef.current} />
        </>
      )}

      {/* ── Circuit Breaker Banner ── */}
      {!loading && !cbDismissed && consecutiveLosses >= 3 && (
        <div className="notice caution" role="alert" style={{ alignItems: 'center', marginBottom: 20 }}>
          <AlertTriangle size={16} style={{ flexShrink: 0 }} aria-hidden="true" />
          <span style={{ fontSize: 14, fontWeight: 600, flex: 1 }}>
            {consecutiveLosses} losses in a row. Consider stepping back and reviewing before the next trade.
          </span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setCbDismissed(true)} aria-label="Dismiss loss-streak alert" style={{ color: 'inherit' }}>
            Dismiss
          </button>
        </div>
      )}

      {/* ── Main 2-column grid ── */}
      <div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
          {/* Coaching first: you read the review, then the trades it is about */}
          <section className="card">
            {summaryError && (
              <div className="notice neg" role="alert" style={{ marginBottom: 14 }}>
                {summaryError}
              </div>
            )}
            <Coaching summary={summary} loading={summaryLoading} onRegenerate={handleRegenerate} />
            {usageLine(summary?.ai_usage) && (
              <div style={{
                marginTop: 12,
                paddingTop: 10,
                borderTop: '1px solid var(--divider-soft)',
                color: 'var(--text-secondary)',
                fontSize: 12,
              }} role="status" aria-label="AI token usage">
                {usageLine(summary.ai_usage)}
              </div>
            )}
          </section>

          {/* The week this day sits in: same voice, one level up */}
          <WeeklySummary
            open={weeklyOpen}
            loading={weeklyLoading}
            data={weekly}
            error={weeklyError}
            usage={weekly?.ai_usage}
            onToggle={() => setWeeklyOpen(o => !o)}
            onRegenerate={() => fetchWeekly(true)}
            onRetry={() => fetchWeekly(false)}
          />

          {/* The trades */}
          <section className="card panel-flush">
            <div style={{ padding: '18px 0 12px' }}>
              <PanelHead title="Trade by trade" sub="Hover a grade for the reason. Click a row to open the trade." />
            </div>
            <DayTrades
              trades={trades}
              gradeMap={tradeGradeMap}
              loading={loading || summaryLoading}
              onOpen={onOpenDetail}
            />
          </section>

          {/* Trade Timeline */}

          {/* R-Multiple Chart */}
          {!loading && <RMultipleChart trades={trades} />}

        </div>

        {/* ── Right column ── */}
      </div>

    </div>
  );
}
