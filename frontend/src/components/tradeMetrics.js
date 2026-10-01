import { isPriceDeltaLinear } from '../instruments';
import { fillTs } from './chartTime';

// Per-trade numbers shown in Trade Detail. Kept apart from the component so the
// two rules it depends on can be asserted directly:
//
//   * fills are ordered by (date, time) — clock time alone cannot tell a
//     position held past midnight from one whose legs span two days, and
//   * a return is only reported when price × quantity is actually a dollar
//     figure. See computeStats below for what each rule cost when it was
//     missing.

export function parseExecs(trade) {
  const raw = trade.executions;
  if (!raw) return [];
  if (Array.isArray(raw)) return raw;
  try { return JSON.parse(raw); } catch { return []; }
}

export function computeStats(trade) {
  const execs = parseExecs(trade);
  const side = trade.side;
  const entryFills = execs.filter(e => side === 'LONG' ? e.action === 'BOT' : e.action === 'SOLD');
  const exitFills  = execs.filter(e => side === 'LONG' ? e.action === 'SOLD' : e.action === 'BOT');

  const avgPrice = (fills) => {
    const qty = fills.reduce((s, f) => s + (f.qty || 0), 0);
    if (!qty) return null;
    return fills.reduce((s, f) => s + (f.qty || 0) * (f.price || 0), 0) / qty;
  };

  const avgEntry = avgPrice(entryFills);
  const avgExit  = avgPrice(exitFills);
  const totalQty = entryFills.reduce((s, f) => s + (f.qty || 0), 0);

  // "Cost basis" is price × quantity, and that is only a dollar figure when
  // one price unit is worth one currency unit per quantity. On FX a 0.00250
  // move is $250 a lot, so net P&L over price × lots reported a −$2.59 trade
  // at −22,266% and put every one of 1,346 FX trades past ±1000%. The contract
  // size is not on the row, so the arithmetic cannot be corrected client-side:
  // withhold it, the same way the what-if estimate is withheld for the same
  // reason (see instruments.isPriceDeltaLinear).
  const adjustedCost = isPriceDeltaLinear(trade.instrument_type) && avgEntry ? avgEntry * totalQty : null;
  const netRoi = adjustedCost ? (trade.net_pnl / adjustedCost * 100) : null;

  // Order the fills by (date, time). A position opened 13:42 on 12 Aug and
  // closed 09:22 on 2 Sep was sorting as 09:22 → 13:42: swapped ends, and 21
  // days read as 4h 20m. The chart has ordered fills this way all along; the
  // stats are catching up.
  const stamp = (f) => (f ? fillTs(f, trade.date, 1) : NaN);
  const byTime = (fills) => fills
    .map(f => ({ f, t: stamp(f) }))
    .filter(x => Number.isFinite(x.t))
    .sort((a, b) => a.t - b.t);
  const timed = byTime(execs);
  const firstEntry = (byTime(entryFills)[0] || timed[0])?.f;
  const lastExit = byTime(exitFills).slice(-1)[0]?.f;

  const openTime  = firstEntry?.time || null;
  const openDate  = firstEntry?.date || trade.date || null;
  const closeTime = lastExit?.time || null;
  const closeDate = lastExit?.date || trade.date || null;

  let holdMinutes = null;
  if (Number.isFinite(stamp(firstEntry)) && Number.isFinite(stamp(lastExit))) {
    holdMinutes = Math.max(0, Math.round((stamp(lastExit) - stamp(firstEntry)) / 60));
  }

  const isClosed = exitFills.length > 0;
  const isWin = (trade.net_pnl || 0) > 0;

  return { avgEntry, avgExit, totalQty, adjustedCost, netRoi, openTime, openDate, closeTime, closeDate, holdMinutes, isClosed, isWin, entryFills, exitFills };
}

/** A hold read in minutes: minutes, then hours, then days — never negative. */
export const fmtHold = (m) => {
  if (m == null) return '—';
  if (m < 60) return `${m}m`;
  if (m < 1440) return `${Math.floor(m / 60)}h ${m % 60}m`;
  return `${Math.floor(m / 1440)}d ${Math.floor((m % 1440) / 60)}h`;
};

/** What a quantity counts for this instrument — a share, a lot, a contract. */
export const QTY_LABELS = {
  STOCK: 'Stocks traded',
  FX: 'Lots traded',
  METAL: 'Lots traded',
  OPTION: 'Contracts traded',
  FUTURE: 'Contracts traded',
  INDEX: 'Units traded',
};
export const qtyLabel = (t) => QTY_LABELS[(t || 'STOCK').toUpperCase()] || 'Quantity';

/** `HH:MM`, carrying the leg's date when the fill happened on another day. */
export const legTime = (date, time, tradeDate) => {
  if (!time) return null;
  const hhmm = time.slice(0, 5);
  return date && tradeDate && date !== tradeDate ? `${date} ${hhmm}` : hhmm;
};
