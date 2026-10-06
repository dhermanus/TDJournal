// What-if horizons for a closed trade: what the price was a few minutes later,
// at the end of the day, and at the end of the week.
//
// Kept apart from TradeDetail.js for the same reason tradeMetrics.js is: the
// rules here are the kind that fail silently, so they have to be assertable on
// their own. See whatIf.test.js.
//
// ── The bug this file replaces ───────────────────────────────────────────────
// The old lookup guessed a timezone off `bars[0]` and subtracted it from the
// scenario time:
//
//     const etOffset = firstUTCMin >= 780 ? -4 : -5;
//     const scenarioUTCMin = (h - etOffset) * 60 + m;
//
// Two things were wrong with it. `bars[0]` is 00:00 on any 24-hour instrument,
// so `firstUTCMin >= 780` was never true and the offset was unconditionally −5;
// and subtracting a negative moved every scenario *five hours forward* into the
// evening. On 2026-01-28 EURUSD it read the 19:05 candle to answer "+5 min" for
// a trade that exited at 14:00 — while the panel's own "Actual exit @ $1.20"
// line was reproducing the 14:00 candle (1.19565 → $1.20).
//
// The bar feed and the fill feed are already reconciled once, in chartTime:
// `toTs(stamp, isLocal)` is what puts a candle on the timeline the chart plots,
// and `execToTs(date, time)` is what puts a fill on it. Both sides of a marker
// meeting on one candle is asserted numerically in chartTime.test.js. This file
// reuses those two functions and adds nothing of its own — which is why a
// scenario now lands on the candle its own chart would draw the fill on.
//
// All comparisons happen in epoch space, so a source's shift is applied to bars
// only, exactly as the chart applies it, and a day or week boundary is derived
// from an epoch with UTC getters — the same thing TradingChart does.
import { toTs, execToTs } from './chartTime';
import { isPriceDeltaLinear } from '../instruments';

export const SCENARIOS = [
  { label: '+5 min',      kind: 'offset', minutes: 5 },
  { label: '+10 min',     kind: 'offset', minutes: 10 },
  { label: '+30 min',     kind: 'offset', minutes: 30 },
  { label: '+1 hour',     kind: 'offset', minutes: 60 },
  { label: 'End of day',  kind: 'day' },
  { label: 'End of week', kind: 'week' },
];

/** The `YYYY-MM-DD` a timeline epoch falls on, read with UTC getters because
 *  that is how chartTime stores wall-clock (see execToTs). */
const dayOf = (epoch) => new Date(epoch * 1000).toISOString().slice(0, 10);

const dayStart = (day) => execToTs(day, '00:00', 1);
const dayEnd   = (day) => execToTs(day, '23:59', 1);

/**
 * The Mon–Fri trading week that `dateStr` belongs to, as {start, end}.
 *
 * A week runs Monday to Friday, which is what both an equity session and this
 * journal's FX import describe: the FX bars here run Sunday 22:00 → Thursday
 * 15:36 with Friday and Saturday empty, so "Friday" still closes the week even
 * when no candle was printed on it.
 *
 * Sunday is the one exception. FX opens on Sunday evening, so a fill dated
 * Sunday belongs to the week that starts with it — putting it in the week before
 * would answer "end of week" with a close from five days earlier.
 */
export function weekBounds(dateStr) {
  const day = new Date(`${dateStr}T00:00:00Z`);
  const wd = day.getUTCDay();                     // 0=Sun … 6=Sat
  const start = new Date(day);
  const end = new Date(day);
  if (wd === 0) {
    end.setUTCDate(end.getUTCDate() + 4);         // Sunday → that week's Thursday
  } else {
    start.setUTCDate(start.getUTCDate() - (wd - 1));   // back to Monday
    end.setUTCDate(end.getUTCDate() + (5 - wd));       // forward to Friday
  }
  const iso = (d) => d.toISOString().slice(0, 10);
  return { start: iso(start), end: iso(end) };
}

/** Bars with the chart's timeline applied, in the order the chart plots them. */
const stamped = (bars, isLocal) => bars
  .map(b => ({ ts: toTs(b.t, isLocal), c: b.c }))
  .filter(b => Number.isFinite(b.ts));

/**
 * Compute the scenario table, or null when the trade cannot answer it.
 *
 * `source` comes from the chart response: 'local' bars are stored as the same
 * clock the fills are, remote bars arrive in UTC and are shifted to ET by
 * toTs(). Passing it through is what keeps a lookup in step with the candle the
 * chart draws — see chartTime.axisLabel for the same split on the axis.
 */
export function computeWhatIf(bars, stats, trade, source) {
  if (!bars || !bars.length || !stats.isClosed || !stats.avgExit || !stats.closeTime) return null;

  const day = stats.closeDate || trade.date;
  const exitTs = execToTs(day, stats.closeTime.slice(0, 5), 1);
  if (exitTs == null) return null;

  const series = stamped(bars, source === 'local');
  if (!series.length) return null;

  const exitDay = dayOf(exitTs);
  const week = weekBounds(exitDay);

  const insideDay = (d) => series.filter(b => b.ts >= dayStart(d) && b.ts <= dayEnd(d));
  const insideWeek = series.filter(b => b.ts >= dayStart(week.start) && b.ts <= dayEnd(week.end));
  const exitBars = insideDay(exitDay);

  const sideSign = trade.side === 'LONG' ? 1 : -1;
  const linear = isPriceDeltaLinear(trade.instrument_type);

  return SCENARIOS.map(({ label, kind, minutes }) => {
    let pick = null;
    let stamp = null;

    if (kind === 'offset') {
      // Rollover past midnight belongs to the next day: the old arithmetic
      // built an hour like "24:30", which `new Date` rejects outright.
      const target = exitTs + minutes * 60;
      const bucket = insideDay(dayOf(target));
      // After the close there is no candle at the scenario time. The day's last
      // price is the answer a trader means by "it was still X", so fall back to
      // it rather than reporting a bar that closed *before* the horizon.
      pick = bucket.find(b => b.ts >= target) || bucket[bucket.length - 1];
      if (pick) stamp = `${dayOf(pick.ts)} ${new Date(pick.ts * 1000).toISOString().slice(11, 16)}`;
    } else if (kind === 'day') {
      // The final bar of the trade's own day — not a clock time. The previous
      // version hardcoded 16:00, which is meaningless for a 24-hour session.
      pick = exitBars[exitBars.length - 1];
    } else {
      pick = insideWeek[insideWeek.length - 1];
    }

    if (!pick) return { label, kind, price: null, deltaPnl: null, whatIfPnl: null, stamp: null };
    if (kind !== 'offset') {
      // Name the bar actually read, not the boundary asked for: a week whose
      // Thursday was the last session answered with Thursday's close, and
      // labelling it Friday would claim a candle that was never printed.
      stamp = dayOf(pick.ts);
    }

    // Withheld unless one price unit is worth one currency unit per quantity:
    // for FX a 0.00250 move is $250, not $0.0025, so the arithmetic would lie.
    const deltaPnl = linear ? (pick.c - stats.avgExit) * stats.totalQty * sideSign : null;
    return {
      label,
      kind,
      price: pick.c,
      stamp,
      deltaPnl,
      whatIfPnl: deltaPnl != null ? (trade.net_pnl ?? 0) + deltaPnl : null,
    };
  });
}
