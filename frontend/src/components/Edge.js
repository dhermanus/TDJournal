// The four behavioural charts, split out so Reports can place them in its own
// tabs. There is no Edge page: `export default function Edge` used to be one
// and nothing ever routed it — Help still advertised it — so it was deleted
// along with the `Section` shell and the fetch it owned. These stay as charts.
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell, ReferenceLine,
} from 'recharts';

const fmt$ = (v) =>
  `$${Number(v || 0).toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;

function NoData({ msg }) {
  return (
    <div className="empty" style={{ padding: '24px 0' }}>
      {msg || 'Not enough data yet.'}
    </div>
  );
}

const barTip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  const v = payload[0]?.value || 0;
  return (
    <div style={{
      background: 'var(--surface-control)', border: '1px solid var(--divider-strong)',
      borderRadius: 'var(--radius-md)', padding: '8px 12px', fontSize: 13, boxShadow: 'var(--shadow-dropdown)',
    }}>
      <div style={{ color: 'var(--text-secondary)', marginBottom: 2 }}>{label}</div>
      <div className="num" style={{ fontWeight: 600 }}>{v}</div>
    </div>
  );
};


export function RMultipleDist({ data }) {
  if (!data || data.every(d => d.count === 0)) return <NoData msg="No R-multiple data. Log stop loss and fill in R-multiple per trade." />;
  const filtered = data.filter(d => d.count > 0);
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={filtered} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--divider-soft)" />
        <XAxis dataKey="bucket" tick={{ fill: 'var(--text-secondary)', fontSize: 12 }} axisLine={false} tickLine={false}
          tickFormatter={v => `${Number(v) >= 0 ? '+' : ''}${v}R`} />
        <YAxis tick={{ fill: 'var(--text-secondary)', fontSize: 12 }} axisLine={false} tickLine={false} />
        <ReferenceLine x="0" stroke="var(--divider-strong)" />
        <Tooltip content={barTip} cursor={{ fill: 'var(--surface-hover)' }} />
        <Bar dataKey="count" radius={[3, 3, 0, 0]}>
          {filtered.map((entry, i) => (
            <Cell key={i} fill={Number(entry.bucket) >= 0 ? 'var(--result-pos)' : 'var(--result-neg)'} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function EmotionTable({ data }) {
  if (!data || data.length === 0) return <NoData msg="No emotion data. Log emotional state per trade." />;

  return (
    <div className="scroll-x">
    <table style={{ minWidth: 520 }}>
      <thead>
        <tr>
          {['State', 'Trades', 'Win Rate', 'Avg P&L', 'Avg R'].map(h => (
            <th key={h} className={h === 'State' ? undefined : 'num'}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.map((row, i) => {
          const pnlColor = row.avg_pnl >= 0 ? 'var(--result-pos)' : 'var(--result-neg)';
          const wr = row.win_rate;
          const wrColor = wr >= 55 ? 'var(--result-pos)' : wr >= 40 ? 'var(--caution)' : 'var(--result-neg)';
          return (
            <tr key={i}>
              <td style={{ textTransform: 'capitalize', fontWeight: 600 }}>
                {row.state}
              </td>
              <td className="num">{row.trade_count}</td>
              <td className="num" style={{ color: wrColor, fontWeight: 600 }}>
                {wr.toFixed(1)}%
              </td>
              <td className="num" style={{ color: pnlColor, fontWeight: 600 }}>
                {row.avg_pnl >= 0 ? '+' : '-'}{fmt$(Math.abs(row.avg_pnl))}
              </td>
              <td className="num" style={{ color: row.avg_r != null ? (row.avg_r >= 0 ? 'var(--result-pos)' : 'var(--result-neg)') : 'var(--text-secondary)' }}>
                {row.avg_r != null ? `${row.avg_r >= 0 ? '+' : ''}${row.avg_r.toFixed(2)}R` : 'n/a'}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
    </div>
  );
}

export function MistakeFreq({ data }) {
  if (!data || data.length === 0) return <NoData msg="No mistake data. Tag mistakes per trade." />;
  return (
    <ResponsiveContainer width="100%" height={Math.max(160, data.length * 36)}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 8, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--divider-soft)" horizontal={false} />
        <XAxis type="number" tick={{ fill: 'var(--text-secondary)', fontSize: 12 }} axisLine={false} tickLine={false} />
        <YAxis type="category" dataKey="mistake" tick={{ fill: 'var(--text-secondary)', fontSize: 12.5 }} axisLine={false} tickLine={false} width={170} />
        <Tooltip content={barTip} cursor={{ fill: 'var(--surface-hover)' }} />
        <Bar dataKey="count" fill="var(--caution)" radius={[0, 3, 3, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function HoldTime({ data }) {
  if (!data || (data.winners_avg_min == null && data.losers_avg_min == null)) {
    return <NoData msg="Not enough trade timing data yet." />;
  }
  const bars = [
    { label: 'Winners', value: data.winners_avg_min, fill: 'var(--result-pos)' },
    { label: 'Losers', value: data.losers_avg_min, fill: 'var(--result-neg)' },
  ].filter(b => b.value != null);

  const fmtMin = (m) => {
    if (m == null) return 'n/a';
    if (m < 60) return `${Math.round(m)}m`;
    return `${Math.floor(m / 60)}h ${Math.round(m % 60)}m`;
  };

  return (
    <div style={{ display: 'flex', gap: 24, alignItems: 'center', flexWrap: 'wrap', paddingTop: 8 }}>
      {bars.map(b => (
        <div key={b.label} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, flex: '1 1 120px' }}>
          <div className="num" style={{ fontSize: 30, fontWeight: 600, fontFamily: 'var(--font-display)', color: b.fill, lineHeight: 1 }}>
            {fmtMin(b.value)}
          </div>
          <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Avg hold ({b.label.toLowerCase()})</div>
        </div>
      ))}
      {bars.length === 2 && bars[1].value != null && bars[0].value != null && (
        <div style={{ flex: '1 1 160px', fontSize: 14, color: 'var(--text-secondary)', lineHeight: 1.55 }}>
          {bars[1].value > bars[0].value
            ? `You hold losers ${(bars[1].value / bars[0].value).toFixed(1)}x longer than winners. Consider cutting losses faster.`
            : `You hold winners ${(bars[0].value / bars[1].value).toFixed(1)}x longer than losers. Good discipline, letting winners run.`}
        </div>
      )}
    </div>
  );
}
