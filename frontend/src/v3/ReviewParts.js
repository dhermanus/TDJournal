/* Review page pieces in the V3 language. Presentation only. */
import { useState, useMemo } from 'react';
import { Brain, ChevronUp, ChevronDown, RotateCcw } from 'lucide-react';
import { Measures, Tabs, Grade, money, money2, tone } from './parts';
import { readResult, rangeLabel } from '../components/weeklySummary';
import { usageLine } from '../components/aiUsage';

/* the clock every trading session runs on */
const OPEN = 9.5;
const CLOSE = 16;

export function tradeTime(t) {
  const execs = t.executions || [];
  const raw = execs.length ? (execs[0].time || execs[0].datetime || '') : (t.open_time || t.time || '');
  const m = String(raw).match(/(\d{1,2}):(\d{2})/);
  if (!m) return null;
  return Number(m[1]) + Number(m[2]) / 60;
}

/* ── the day, as one picture ────────────────────────────────────────────────
   The session's running P&L from the open to the close, with every trade
   marked on the curve at the time it was entered. Replaces having a separate
   equity line and a separate timeline showing the same session twice.      */
export function DayCurve({ trades, onPick }) {
  const [hover, setHover] = useState(null);

  const marks = useMemo(() => {
    const withTime = (trades || [])
      .map((t) => ({ t, at: tradeTime(t) }))
      .filter((x) => x.at != null)
      .sort((a, b) => a.at - b.at);
    let cum = 0;
    return withTime.map(({ t, at }) => {
      cum += Number(t.net_pnl) || 0;
      return { trade: t, at, pnl: Number(t.net_pnl) || 0, cum };
    });
  }, [trades]);

  const W = 1000;
  const H = 210;
  const PAD = 26;

  const geom = useMemo(() => {
    if (!marks.length) return null;
    const vals = marks.map((m) => m.cum).concat([0]);
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const span = (hi - lo) || 1;
    const X = (hourFrac) => ((hourFrac - OPEN) / (CLOSE - OPEN)) * W;
    const Y = (v) => H - PAD - ((v - lo) / span) * (H - PAD * 2);
    // a step line: the balance holds until the next trade closes it
    let d = `M0 ${Y(0).toFixed(1)}`;
    let prev = 0;
    marks.forEach((m) => {
      d += ` L${X(m.at).toFixed(1)} ${Y(prev).toFixed(1)} L${X(m.at).toFixed(1)} ${Y(m.cum).toFixed(1)}`;
      prev = m.cum;
    });
    d += ` L${W} ${Y(prev).toFixed(1)}`;
    const area = `${d} L${W} ${H} L0 ${H} Z`;
    return { X, Y, d, area, zero: Y(0), final: prev };
  }, [marks]);

  if (!geom) return <div className="v3-empty">No timed executions on this day, so the session cannot be drawn.</div>;

  const peak = Math.max(1, ...marks.map((m) => Math.abs(m.pnl)));
  const up = geom.final >= 0;

  return (
    <div className="v3-session" onPointerLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        <defs>
          <linearGradient id="v3day" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={up ? 'var(--result-pos)' : 'var(--result-neg)'} stopOpacity="0.2" />
            <stop offset="100%" stopColor={up ? 'var(--result-pos)' : 'var(--result-neg)'} stopOpacity="0" />
          </linearGradient>
        </defs>
        {/* the hours */}
        {[10, 11, 12, 13, 14, 15].map((h) => (
          <g key={h}>
            <line x1={geom.X(h)} y1="0" x2={geom.X(h)} y2={H} stroke="var(--divider-soft)" strokeWidth="1"
              vectorEffect="non-scaling-stroke" />
            <text className="v3-tl-lab" x={geom.X(h) + 5} y="12">{h}:00</text>
          </g>
        ))}
        <line x1="0" y1={geom.zero} x2={W} y2={geom.zero} stroke="var(--divider)" strokeWidth="1"
          vectorEffect="non-scaling-stroke" />
        <path d={geom.area} fill="url(#v3day)" />
        <path d={geom.d} fill="none" stroke={up ? 'var(--result-pos)' : 'var(--result-neg)'}
          strokeWidth="1.8" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
      </svg>

      {/* one mark per trade, on the curve at its entry time */}
      {marks.map((m, i) => {
        const leftPct = (geom.X(m.at) / W) * 100;
        const topPct = (geom.Y(m.cum) / H) * 100;
        const size = 5 + (Math.abs(m.pnl) / peak) * 7;
        return (
          <button
            key={i}
            type="button"
            className="v3-daymark"
            style={{
              left: `${leftPct}%`, top: `${topPct}%`, width: size, height: size,
              background: m.pnl >= 0 ? 'var(--result-pos)' : 'var(--result-neg)',
            }}
            aria-label={`${m.trade.ticker} at ${m.at ? `${Math.floor(m.at)}:${String(Math.round((m.at % 1) * 60)).padStart(2, '0')}` : ''}, ${money2(m.pnl)}`}
            onPointerEnter={() => setHover(i)}
            onFocus={() => setHover(i)}
            onClick={() => onPick && onPick(m.trade)}
          />
        );
      })}

      {hover != null && (
        <div
          className="v3-tipbox"
          style={{
            left: (geom.X(marks[hover].at) / W) * 100 > 62 ? undefined : `calc(${(geom.X(marks[hover].at) / W) * 100}% + 14px)`,
            right: (geom.X(marks[hover].at) / W) * 100 > 62 ? `calc(${100 - (geom.X(marks[hover].at) / W) * 100}% + 14px)` : undefined,
            top: 8,
          }}
        >
          <div className="d">{marks[hover].trade.ticker}</div>
          <div className={`v ${tone(marks[hover].pnl)}`}>{money2(marks[hover].pnl)}</div>
          <div className="r">running {money(marks[hover].cum)}</div>
        </div>
      )}
    </div>
  );
}

/* ── the day's measures ───────────────────────────────────────────────────
   Each cell reads the day's own KPIs and sets them against all time in the
   same cell, instead of a separate comparison row. Values that cannot be
   measured on a day (a profit factor with no losers) say so in words, and are
   never compared as if they were zero. */
function Delta({ day, all, digits = 1, unit = '', prefix = '' }) {
  if (day == null || all == null || !Number.isFinite(Number(day)) || !Number.isFinite(Number(all))) return null;
  const d = Number(day) - Number(all);
  if (Math.abs(d) < 10 ** -digits / 2) return <span className="v3-flat"> level</span>;
  const cls = d > 0 ? 'v3-pos' : 'v3-neg';
  const abs = Math.abs(d).toFixed(digits);
  return <span className={cls}> {d > 0 ? '+' : '−'}{prefix}{abs}{unit}</span>;
}

export function DayMeasures({ kpis, trades, summary, allTime }) {
  const k = kpis || {};
  const a = allTime || null;
  const net = Number(k.total_net_pnl || 0);
  const n = k.total_trades ?? (trades || []).length;

  // how far the day came off its own high water mark
  const { peak, given } = useMemo(() => {
    const withTime = (trades || [])
      .map((t) => ({ at: tradeTime(t), p: Number(t.net_pnl) || 0 }))
      .filter((x) => x.at != null)
      .sort((x, y) => x.at - y.at);
    let cum = 0; let hi = 0;
    withTime.forEach((x) => { cum += x.p; hi = Math.max(hi, cum); });
    return { peak: hi, given: Math.max(0, hi - cum) };
  }, [trades]);

  const breaks = (summary?.mistakes || []).length;
  const wins = (trades || []).filter((t) => (t.net_pnl || 0) > 0).length;
  const losses = (trades || []).filter((t) => (t.net_pnl || 0) < 0).length;

  const avgDay = a && a.trading_days ? Number(a.total_net_pnl || 0) / a.trading_days : null;
  const pf = k.profit_factor == null ? null : Number(k.profit_factor);
  const noLosers = pf == null && wins > 0;
  const eff = k.exit_efficiency == null ? null : Number(k.exit_efficiency);
  const allEff = a && a.exit_efficiency != null ? Number(a.exit_efficiency) : null;
  const perTrade = n ? net / n : null;
  const allExp = a && a.expectancy != null ? Number(a.expectancy) : null;

  const allLine = (label, value) => (a && value != null ? <>All-time {label}{value}</> : null);

  return (
    <Measures
      className="v3-measures-4"
      items={[
        {
          label: 'Net for the day',
          value: money2(net),
          met: net >= 0,
          tone: net < 0 ? 'neg' : undefined,
          read: avgDay == null
            ? `${wins} green, ${losses} red`
            : <>Your average day {money(avgDay)}<Delta day={net} all={avgDay} digits={0} prefix="$" /></>,
        },
        {
          label: 'Win rate',
          value: n ? `${Number(k.win_rate || 0).toFixed(0)}%` : '—',
          read: <>
            {wins} of {n} won.{' '}
            {allLine('', a ? `${Number(a.win_rate || 0).toFixed(1)}%` : null)}
            {a && n ? <Delta day={k.win_rate} all={a.win_rate} unit="pp" /> : null}
          </>,
        },
        {
          label: 'Profit factor',
          value: noLosers ? 'No losers' : pf == null ? '—' : pf.toFixed(2),
          met: noLosers || (pf != null && pf >= 1),
          tone: pf != null && pf < 1 ? 'neg' : undefined,
          read: <>
            {noLosers ? 'Nothing to divide by today. ' : ''}
            {allLine('', a && a.profit_factor != null ? Number(a.profit_factor).toFixed(2) : null)}
            {pf != null && a && a.profit_factor != null ? <Delta day={pf} all={a.profit_factor} digits={2} unit="x" /> : null}
          </>,
        },
        {
          label: 'Avg win',
          value: k.avg_win ? money(k.avg_win) : '—',
          read: <>
            {allLine('', a && a.avg_win ? money(a.avg_win) : null)}
            {k.avg_win && a && a.avg_win ? <Delta day={k.avg_win} all={a.avg_win} digits={0} prefix="$" /> : null}
          </>,
        },
        {
          label: 'Avg per trade',
          value: perTrade == null ? '—' : money2(perTrade),
          tone: perTrade != null && perTrade < 0 ? 'neg' : undefined,
          read: <>
            {allExp != null ? <>All-time expectancy {money2(allExp)}</> : 'Net divided by trades'}
            {perTrade != null && allExp != null ? <Delta day={perTrade} all={allExp} digits={0} prefix="$" /> : null}
          </>,
        },
        {
          label: 'Exit efficiency',
          value: eff == null ? '—' : `${eff.toFixed(0)}%`,
          amber: eff != null && allEff != null && eff < allEff,
          read: eff == null
            ? 'No excursion data for these trades'
            : <>
              Share of the move you kept.{' '}
              {allEff != null ? <>All-time {allEff.toFixed(0)}%</> : null}
              {allEff != null ? <Delta day={eff} all={allEff} digits={0} unit="pp" /> : null}
            </>,
        },
        {
          label: 'Given back',
          value: given > 0 ? money(-given) : '$0',
          amber: given > 0,
          read: peak > 0
            ? `From a session high of ${money(peak)}`
            : 'The session never went green',
        },
        {
          label: 'Rule breaks',
          value: String(breaks),
          amber: breaks > 0,
          read: breaks ? 'Flagged in the review below' : 'Nothing flagged',
        },
      ]}
    />
  );
}

/* ── coaching: the report, with the lists behind tabs ───────────────────── */
export function Coaching({ summary, loading, onRegenerate }) {
  const [tab, setTab] = useState('strengths');
  if (loading) return <div className="v3-empty">Reading the session…</div>;
  if (!summary) return <div className="v3-empty">No review for this day yet.</div>;

  const lists = {
    strengths: summary.strengths || [],
    mistakes: summary.mistakes || [],
    focus: summary.coaching || [],
    patterns: summary.patterns || [],
  };
  const tabs = [
    { id: 'strengths', label: 'Strengths' },
    { id: 'mistakes', label: 'Mistakes' },
    { id: 'focus', label: 'Tomorrow’s focus' },
    { id: 'patterns', label: 'Patterns' },
  ];
  const rows = lists[tab];

  return (
    <>
      <div className="v3-sec-head">
        <div>
          <h2 className="v3-h">Coaching</h2>
          <p className="v3-h-sub">Written against your trades and your diary together, and graded on process</p>
        </div>
        <div className="v3-acts">
          {onRegenerate && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={onRegenerate}>Regenerate</button>
          )}
        </div>
      </div>

      <div className="v3-cols">
        {summary.narrative && <p className="v3-narr">{summary.narrative}</p>}
        {summary.mental_game && <p className="v3-narr v3-narr-quiet">{summary.mental_game}</p>}
      </div>

      <div style={{ marginTop: 22 }}>
        <Tabs tabs={tabs} active={tab} onChange={setTab} label="Review detail" />
        {!rows.length ? (
          <div className="v3-empty">
            {tab === 'mistakes' ? 'Nothing flagged on this day.' : 'Nothing recorded here.'}
          </div>
        ) : tab === 'patterns' ? (
          <div style={{ display: 'flex', gap: 7, flexWrap: 'wrap' }}>
            {rows.map((p, i) => <span className="v3-chip" key={i} style={{ whiteSpace: 'normal' }}>{p}</span>)}
          </div>
        ) : (
          <ul className="v3-list v3-cols">
            {rows.map((r, i) => (
              <li key={i} className={tab === 'mistakes' ? 'bad' : tab === 'focus' ? 'next' : 'good'}>{r}</li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}

/* ── the week behind that day ──────────────────────────────────────────────
   Collapsed until asked for: opening it is the request, and a second open
   re-reads what is already held rather than paying for it again.        */
export function WeeklySummary({ open, loading, data, error, usage, onToggle, onRegenerate, onRetry }) {
  const panelId = 'weekly-summary-panel';
  const result = readResult(data);
  const range = rangeLabel(result);

  return (
    <section className="card">
      <div className="panel-head">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={open ? panelId : undefined}
          style={{
            display: 'flex', alignItems: 'center', gap: 8, width: '100%',
            background: 'none', border: 0, padding: 0, cursor: 'pointer',
            color: 'var(--text-primary)', textAlign: 'left',
          }}
        >
          <Brain size={16} color="var(--accent-line)" aria-hidden="true" />
          {/* h2 + div, not spans: `.section-title` and `.section-sub` are block
              styles, and as inline spans they collapse onto one line. `flex: 1`
              puts the chevron at the card edge, as the other panels do. */}
          <span style={{ flex: 1, minWidth: 0 }}>
            <span className="section-title" style={{ display: 'block' }}>Weekly Summary</span>
            <span className="section-sub" style={{ display: 'block' }}>
              {range || 'The trading week this day belongs to'}
            </span>
          </span>
          {open
            ? <ChevronUp size={16} color="var(--text-tertiary)" aria-hidden="true" />
            : <ChevronDown size={16} color="var(--text-tertiary)" aria-hidden="true" />}
        </button>
      </div>

      {open && (
        <div id={panelId} style={{ marginTop: 4 }}>
          {error && (
            <div className="notice neg" role="alert" style={{ marginBottom: 14 }}>
              {error}
            </div>
          )}

          {loading ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div className="skeleton" style={{ height: 12 }} />
              <div className="skeleton" style={{ height: 12, width: '85%' }} />
              <div className="skeleton" style={{ height: 12, width: '70%' }} />
              <div className="v3-read" style={{ marginTop: 4 }}>Reading the week…</div>
            </div>
          ) : result.kind === 'summary' ? (
            <>
              <div className="v3-cols">
                <p className="v3-narr">{result.narrative}</p>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 16, marginTop: 16 }}>
                {result.blocks.map((b) => (
                  // `.notice` is display:flex, so its children become row items
                  // — the label and the text need their own column.
                  <div key={b.key} className={b.tone === 'rule' ? 'notice accent' : undefined}
                       style={b.tone === 'rule' ? { display: 'flex', flexDirection: 'column', gap: 5 } : undefined}>
                    <div className="v3-lab">{b.label}</div>
                    <div style={{
                      fontSize: 13, lineHeight: 1.55,
                      color: b.tone === 'rule' ? 'var(--text)' : 'var(--text-secondary)',
                    }}>{b.text}</div>
                  </div>
                ))}
              </div>

              <div style={{ marginTop: 16, display: 'flex', gap: 8, alignItems: 'center' }}>
                <button type="button" className="btn btn-secondary btn-sm" onClick={onRegenerate}>
                  <RotateCcw size={12} aria-hidden="true" /> Regenerate
                </button>
                {usageLine(usage) && (
                  <span
                    className="v3-read"
                    role="status"
                    aria-label="AI token usage"
                  >{usageLine(usage)}</span>
                )}
              </div>
            </>
          ) : result.kind === 'refusal' ? (
            // The endpoint's own answer to "there is nothing to summarise".
            <div className="v3-empty">{result.message}</div>
          ) : error ? (
            // `weekly` is still null here, so closing and reopening would retry —
            // but that is not a thing anyone would think to try.
            <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
              Try again
            </button>
          ) : (
            <div className="v3-empty">No weekly synthesis yet.</div>
          )}
        </div>
      )}
    </section>
  );
}

/* ── the day's trades, with the grade carrying its reason ───────────────── */
export function DayTrades({ trades, gradeMap, loading, onOpen }) {
  if (loading) return <div className="v3-empty">Loading…</div>;
  if (!trades?.length) return <div className="v3-empty">No trades on this day.</div>;
  return (
    <div className="v3-scroll">
      <table className="v3-t">
        <thead>
          <tr>
            <th>Ticker</th>
            <th className="v3-hide-s">Side</th>
            <th className="v3-hide-s">Strategy</th>
            <th>Grade</th>
            <th className="r v3-hide-s">MFE / MAE</th>
            <th className="r v3-hide-s">Captured</th>
            <th className="r">R</th>
            <th className="r">P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => {
            const pnl = Number(t.net_pnl || 0);
            const g = gradeMap?.[t.trade_group];
            const open = () => onOpen && onOpen(t, trades);
            return (
              <tr
                key={t.id}
                className="clickable"
                tabIndex={0}
                onClick={open}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } }}
                aria-label={`Open ${t.ticker} trade`}
              >
                <td>
                  <div className="v3-tick">{t.ticker}</div>
                  <div className="v3-read">{t.instrument_type}</div>
                </td>
                <td className="v3-hide-s v3-side">
                  {t.side === 'LONG' ? 'Long' : t.side === 'SHORT' ? 'Short' : t.side}
                </td>
                <td className="v3-hide-s v3-read">{t.strategy || 'not set'}</td>
                <td onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                  {g ? <Grade grade={g.grade} reason={g.one_line} /> : <span className="v3-flat">&mdash;</span>}
                </td>
                <td className="r v3-mono v3-hide-s v3-read">
                  {t.mfe_pct == null && t.mae_pct == null ? '—' : (
                    <>
                      <span className="v3-pos">{t.mfe_pct == null ? '—' : `+${Number(t.mfe_pct).toFixed(1)}%`}</span>
                      <span className="v3-flat"> / </span>
                      <span className="v3-neg">{t.mae_pct == null ? '—' : `${Number(t.mae_pct).toFixed(1)}%`}</span>
                    </>
                  )}
                </td>
                <td className={`r v3-mono v3-hide-s ${t.exit_efficiency == null ? 'v3-flat' : t.exit_efficiency < 0 ? 'v3-neg' : t.exit_efficiency >= 50 ? 'v3-pos' : ''}`}>
                  {t.exit_efficiency == null ? '—' : `${Number(t.exit_efficiency).toFixed(0)}%`}
                </td>
                <td className={`r v3-mono ${t.r_multiple != null ? tone(t.r_multiple) : 'v3-flat'}`}>
                  {t.r_multiple != null ? `${t.r_multiple > 0 ? '+' : ''}${Number(t.r_multiple).toFixed(2)}R` : '—'}
                </td>
                <td className={`r v3-mono ${tone(pnl)}`} style={{ fontWeight: 600 }}>{money2(pnl)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
