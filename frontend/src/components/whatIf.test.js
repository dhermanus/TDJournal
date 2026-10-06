// What-if horizons: which candle each scenario reads, and on whose clock.
//
// The table this file exists to defend used to be wrong in a way a screenshot
// could not show. Five rows all printed "$1.19" while the prices behind them
// were 1.19050–1.19449, and every one of them had been read five hours past the
// exit — `getPriceAt` derived an ET offset from `bars[0]`, which is 00:00 on any
// 24-hour instrument, so the branch that would have chosen −4 never ran and the
// offset was unconditionally −5, subtracted from the target time.
//
// Three assertions carry the weight:
//
//   1. a scenario lands on the candle its own chart would draw the fill on —
//      checked by reusing chartTime's toTs/execToTs, i.e. the same functions the
//      chart uses, then reading the number back out;
//   2. "end of day" is the day's last close, so it cannot disagree with the
//      panel's own "Actual exit @ …" line;
//   3. "end of week" reads the last session of the trade's Mon–Fri week, never
//      a bar from the week after it.
//
// Fixtures price every minute as `base + minuteOfDay * step`. Prices are derived
// from that formula rather than typed as constants: the first draft asserted
// hand-computed numbers and the arithmetic did not match the fixture, which
// failed the suite without telling us anything about the feature.
import { computeWhatIf, weekBounds, SCENARIOS } from './whatIf';
import { fmtPrice as scenarioPrice } from '../instruments';

const iso = (d, hhmmss) => Math.floor(new Date(`${d}T${hhmmss}Z`).getTime() / 1000);

const bar = (d, hhmm, c) => ({ t: `${d}T${hhmm}:00Z`, c });

// One basis for every fixture: a cent of drift across a full 24-hour day. That
// is deliberately in the range real data moves — the journal's 2026-01-28
// EURUSD session spanned 1.19040–1.20250 — because the point of one test is that
// this whole range collapses when rendered at two decimal places.
const STEP = 0.00001;
const priceAt = (base, hhmm) => {
  const [h, m] = hhmm.split(':').map(Number);
  return base + (h * 60 + m) * STEP;
};

/** M1 bars over `day`, one per minute, close = base + minuteOfDay * STEP. */
function dayBars(d, { base = 1.19, fromHour = 0, toHour = 23 } = {}) {
  const out = [];
  for (let h = fromHour; h <= toHour; h++) {
    for (let m = 0; m < 60; m++) {
      out.push(bar(d, `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`,
        priceAt(base, `${h}:${m}`)));
    }
  }
  return out;
}

/** Every `nth` bar — an hourly view of the same series, for the week fixtures. */
const hourly = (bars) => bars.filter((_, i) => i % 60 === 0);

const fxTrade = (extra = {}) => ({
  date: '2026-01-28', ticker: 'EURUSD', instrument_type: 'FX',
  side: 'LONG', net_pnl: 130.69,
  executions: JSON.stringify([
    { date: '2026-01-28', time: '13:55:00', action: 'BOT', qty: 2.77, price: 1.1954 },
    { date: '2026-01-28', time: '14:00:00', action: 'SOLD', qty: 2.77, price: 1.19568 },
  ]),
  ...extra,
});

// Only the fields what-if reads; computeStats supplies them in the component.
const statsFor = (over = {}) => ({
  isClosed: true, avgExit: priceAt(1.19, '14:00'), closeTime: '14:00:00',
  closeDate: '2026-01-28', totalQty: 2.77, ...over,
});

const prices = (rows) => rows.map(r => r.price);
const byLabel = (rows, label) => rows.find(r => r.label === label);

describe('a scenario reads the candle it says it reads', () => {
  const bars = dayBars('2026-01-28');
  const rows = computeWhatIf(bars, statsFor(), fxTrade(), 'local');

  test('every horizon is present, end of week included', () => {
    expect(rows.map(r => r.label)).toEqual(SCENARIOS.map(s => s.label));
    expect(rows.every(r => r.price != null)).toBe(true);
  });

  test('+5 min reads 14:05, not 19:05', () => {
    // The whole defect in one line: the old lookup landed five hours later.
    expect(byLabel(rows, '+5 min').price).toBe(priceAt(1.19, '14:05'));
    expect(byLabel(rows, '+5 min').stamp).toBe('2026-01-28 14:05');
    // and it is provably not the 19:05 candle the old code returned
    expect(byLabel(rows, '+5 min').price).not.toBe(priceAt(1.19, '19:05'));
  });

  test('each step moves forward, never backwards', () => {
    const p = prices(rows);
    const i = (l) => rows.findIndex(r => r.label === l);
    expect(p[i('+10 min')]).toBeGreaterThan(p[i('+5 min')]);
    expect(p[i('+30 min')]).toBeGreaterThan(p[i('+10 min')]);
    expect(p[i('+1 hour')]).toBeGreaterThan(p[i('+30 min')]);
    expect(p[i('End of day')]).toBeGreaterThan(p[i('+1 hour')]);
  });

  test('the horizons are not one number printed five times', () => {
    // What toFixed(2) hid: distinct prices that all round to the same cent.
    const offsets = ['+5 min', '+10 min', '+30 min', '+1 hour'].map(l => byLabel(rows, l).price);
    expect(new Set(offsets).size).toBe(4);
    expect(new Set(offsets.map(v => v.toFixed(2))).size).toBe(1);
    // The precision helper is what stops that reaching the screen.
    expect(new Set(offsets.map(v => scenarioPrice(v, fxTrade()))).size).toBe(4);
  });

  test('the bar each price came from is named in the row', () => {
    expect(byLabel(rows, '+30 min').stamp).toBe('2026-01-28 14:30');
    expect(byLabel(rows, 'End of day').stamp).toBe('2026-01-28');
  });
});

describe('end of day is the day\'s last bar, not a clock time', () => {
  test('it is the final candle of the exit day', () => {
    const bars = dayBars('2026-01-28');
    const rows = computeWhatIf(bars, statsFor(), fxTrade(), 'local');
    const eod = byLabel(rows, 'End of day');
    expect(eod.stamp).toBe('2026-01-28');
    expect(eod.price).toBe(priceAt(1.19, '23:59'));
    expect(eod.price).toBe(bars[bars.length - 1].c);
  });

  test('a market that closes at 15:36 stops there', () => {
    // Real imported data: this journal's 2026-01-29 session ends 15:36, and the
    // old hardcoded 16:00 would have read an hour past the close.
    const bars = dayBars('2026-01-29', { toHour: 15 }).slice(0, 15 * 60 + 37);
    const rows = computeWhatIf(bars, statsFor({ closeDate: '2026-01-29' }), fxTrade(), 'local');
    const eod = byLabel(rows, 'End of day');
    expect(eod.stamp).toBe('2026-01-29');
    expect(eod.price).toBe(bars[bars.length - 1].c);
    expect(eod.price).toBe(priceAt(1.19, '15:36'));
  });

  test('it cannot disagree with the panel\'s own exit line', () => {
    // Stop the series at the exit: "end of day" then *is* the exit print, so the
    // table row and "Actual exit @ …" must render the identical string. That is
    // the property that was broken — the row said $1.19 and the line said $1.20.
    const bars = dayBars('2026-01-28')
      .filter(b => iso('2026-01-28', b.t.slice(11, 19)) <= iso('2026-01-28', '14:00:00'));
    const exitPrice = bars[bars.length - 1].c;
    const rows = computeWhatIf(bars, statsFor({ avgExit: exitPrice }), fxTrade(), 'local');
    const eod = byLabel(rows, 'End of day');
    expect(eod.price).toBe(exitPrice);
    expect(scenarioPrice(eod.price, fxTrade())).toBe(scenarioPrice(statsFor().avgExit, fxTrade()));
    // On the real journal this same comparison reads the 14:00 candle, 1.19565,
    // against the exit average of 1.19568 — within half a pip, so "$1.19565" and
    // "$1.19568" sit side by side instead of $1.19 against $1.20.
  });
});

describe('end of week reaches Friday and not the previous one', () => {
  // Each day is flat and distinct, so which day was read is unambiguous.
  const week = [
    ...hourly(dayBars('2026-01-26', { base: 1.10 })),   // Mon
    ...hourly(dayBars('2026-01-27', { base: 1.11 })),   // Tue
    ...hourly(dayBars('2026-01-28', { base: 1.12 })),   // Wed — the trade
    ...hourly(dayBars('2026-01-29', { base: 1.13 })),   // Thu
    ...hourly(dayBars('2026-01-30', { base: 1.14 })),   // Fri
    ...hourly(dayBars('2026-02-02', { base: 1.30 })),   // the following Monday
  ];
  const rows = computeWhatIf(week, statsFor(), fxTrade(), 'local');
  const eow = byLabel(rows, 'End of week');

  test('bounds are Monday to Friday of the trade\'s week', () => {
    expect(weekBounds('2026-01-28')).toEqual({ start: '2026-01-26', end: '2026-01-30' });
  });

  test('it reads Friday, after every earlier horizon', () => {
    expect(eow.stamp).toBe('2026-01-30');
    expect(eow.price).toBe(priceAt(1.14, '23:00'));
    expect(eow.price).toBeGreaterThan(byLabel(rows, 'End of day').price);
  });

  test('the next week\'s higher prices are out of bounds', () => {
    // Guards the off-by-one that would answer "end of week" with a Monday bar.
    expect(week.some(b => b.c >= 1.30)).toBe(true);
    expect(eow.price).toBeLessThan(1.30);
  });

  test('a week with no Friday bar names the session it actually read', () => {
    // This journal's FX week ends Thursday 15:36 with Friday and Saturday empty.
    // Claiming "Friday" would show a candle nobody traded.
    const noFriday = week.filter(b => !b.t.startsWith('2026-01-30'));
    const rows2 = computeWhatIf(noFriday, statsFor(), fxTrade(), 'local');
    expect(byLabel(rows2, 'End of week').stamp).toBe('2026-01-29');
    expect(byLabel(rows2, 'End of week').price).toBe(priceAt(1.13, '23:00'));
  });

  test('a Sunday fill starts the week that it opens', () => {
    // FX opens Sunday evening (this journal: Sun 22:00 → Thu 15:36). Putting a
    // Sunday fill in the previous week answers with a close from days earlier.
    expect(weekBounds('2026-01-25')).toEqual({ start: '2026-01-25', end: '2026-01-29' });
    expect(weekBounds('2026-01-26')).toEqual({ start: '2026-01-26', end: '2026-01-30' });
    expect(weekBounds('2026-01-24')).toEqual({ start: '2026-01-19', end: '2026-01-23' });  // Saturday is last week's
  });
});

describe('the exit day is the day of the *close*', () => {
  test('a position closed on another day anchors to its own session', () => {
    // 4 of 1,346 real positions span more than one day. computeStats reports
    // closeDate from the exit leg, so what-if follows it rather than the row.
    const bars = [
      ...hourly(dayBars('2026-01-27', { base: 1.10 })),
      ...hourly(dayBars('2026-01-28', { base: 1.25 })),
    ];
    const rows = computeWhatIf(bars, statsFor({ closeDate: '2026-01-28' }),
      fxTrade({ date: '2026-01-27' }), 'local');
    expect(byLabel(rows, 'End of day').stamp).toBe('2026-01-28');
    expect(byLabel(rows, 'End of day').price).toBe(priceAt(1.25, '23:00'));
    // The fixture has no bars after Wednesday, so end-of-week reads Wednesday —
    // the last session inside the Mon 26 → Fri 30 window. Stamping it 30 would
    // name a Friday candle that was never printed (see the no-Friday test).
    expect(byLabel(rows, 'End of week').stamp).toBe('2026-01-28');
    expect(byLabel(rows, 'End of week').price).toBe(priceAt(1.25, '23:00'));
  });
});

describe('the timeline follows the chart, not a guess', () => {
  const localBars = dayBars('2026-01-28');

  test('local (imported MT5) bars are read as-is: same clock as the fill', () => {
    // chartTime passes local stamps through untouched, because MT5 fills are
    // stored as UTC too. The what-if must do the same, or the marker and the
    // scenario disagree by five hours.
    const rows = computeWhatIf(localBars, statsFor(), fxTrade(), 'local');
    expect(byLabel(rows, '+5 min').price).toBe(priceAt(1.19, '14:05'));
    expect(byLabel(rows, '+5 min').stamp).toBe('2026-01-28 14:05');
  });

  test('remote bars shift back four hours, exactly as the chart plots them', () => {
    // Alpaca stamps intraday bars in UTC; toTs moves them to ET wall-clock and
    // equity fills are stored as ET. Asking for a 14:05 fill therefore reads the
    // 18:05 UTC stamp — the same candle the chart puts at 14:05.
    const local = computeWhatIf(localBars, statsFor(), fxTrade(), 'local');
    const shifted = computeWhatIf(localBars, statsFor(), fxTrade(), 'remote');
    expect(shifted[0].stamp).toBe('2026-01-28 14:05');
    expect(shifted[0].price).not.toBe(local[0].price);
    expect(shifted[0].price).toBe(priceAt(1.19, '18:05'));
  });

  test('a scenario after the close falls back to the day\'s last print', () => {
    // The window stops at 16:00; "+1 hour" from 15:45 has no candle. Reporting
    // the day's close says "it was still X" — reporting a 15:45 bar would claim
    // a price for 16:45 that was observed an hour earlier.
    const bars = localBars
      .filter(b => iso('2026-01-28', b.t.slice(11, 19)) <= iso('2026-01-28', '16:00:00'));
    const rows = computeWhatIf(bars, statsFor({ closeTime: '15:45:00' }), fxTrade(), 'local');
    const plusHour = byLabel(rows, '+1 hour');
    expect(plusHour.stamp).toBe('2026-01-28 16:00');
    expect(plusHour.price).toBe(bars[bars.length - 1].c);
  });

  test('an exit just before midnight does not build hour 24:30', () => {
    // `new Date('…T24:30:00Z')` is an invalid date, so the old padStart
    // arithmetic returned nothing at all for the row. The next day's bars must be
    // real bars dated the next day, or the window has nothing to read.
    const bars = [
      ...dayBars('2026-01-28').filter(b => Number(b.t.slice(11, 13)) >= 23),  // 23:00–23:59
      ...dayBars('2026-01-29').filter(b => Number(b.t.slice(11, 13)) === 0),   // 00:00–00:59
    ];
    const rows = computeWhatIf(bars, statsFor({ closeTime: '23:50:00' }), fxTrade(), 'local');
    const plus30 = byLabel(rows, '+30 min');
    expect(plus30.stamp).toBe('2026-01-29 00:20');
    expect(plus30.price).toBe(priceAt(1.19, '00:20'));
    // and end of day still answers for the *exit* day, not the next one.
    expect(byLabel(rows, 'End of day').stamp).toBe('2026-01-28');
    expect(byLabel(rows, 'End of day').price).toBe(priceAt(1.19, '23:59'));
  });
});

describe('nothing to answer means no table', () => {
  test('a trade with no exit has no horizons', () => {
    expect(computeWhatIf(dayBars('2026-01-28'), statsFor({ isClosed: false }), fxTrade(), 'local')).toBeNull();
    expect(computeWhatIf(dayBars('2026-01-28'), statsFor({ avgExit: null }), fxTrade(), 'local')).toBeNull();
  });

  test('no bars is no table, not a crash', () => {
    expect(computeWhatIf([], statsFor(), fxTrade(), 'local')).toBeNull();
    expect(computeWhatIf(null, statsFor(), fxTrade(), 'local')).toBeNull();
  });

  test('rows keep their name when no horizon has a candle', () => {
    // Bars exist but none fall inside the exit's day or week — a symbol whose
    // only history is an earlier week. The table still renders, every row a dash,
    // rather than dropping rows or throwing on a missing bar.
    const rows = computeWhatIf(dayBars('2026-01-19'), statsFor(), fxTrade(), 'local');
    expect(rows).not.toBeNull();
    expect(rows.map(r => r.label)).toEqual(SCENARIOS.map(s => s.label));
    expect(rows.every(r => r.price == null && r.stamp == null)).toBe(true);
    expect(scenarioPrice(byLabel(rows, 'End of week').price, fxTrade())).toBe('—');
  });
});

describe('price precision matches the instrument', () => {
  test('FX shows five digits, JPY pairs three, stocks two', () => {
    expect(scenarioPrice(1.19050, fxTrade())).toBe('$1.19050');
    expect(scenarioPrice(148.25, fxTrade({ ticker: 'USDJPY' }))).toBe('$148.250');
    expect(scenarioPrice(1.19050, fxTrade({ instrument_type: 'STOCK', ticker: 'AAPL' }))).toBe('$1.19');
  });

  test('a null price renders as a dash', () => {
    expect(scenarioPrice(null, fxTrade())).toBe('—');
  });
});
