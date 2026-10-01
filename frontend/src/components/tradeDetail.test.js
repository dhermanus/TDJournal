// TradeDetail's per-trade metrics: ordering legs by date, and what we are
// willing to compute for a return figure.
//
// Two defects are pinned here, both found by reading real journal data and
// both invisible in a screenshot:
//
//   1. entry/exit were ordered by *clock time alone*, so any position whose
//      legs span two days came out backwards — a trade opened 13:42 on
//      2025-08-12 and closed 09:22 on 2025-09-02 read "Opened 09:22 · Closed
//      13:42 · Held 4h 20m" (wrong ends, and 21 days read as 4h). The chart's
//      fill ordering already had this right; this brings the stats in line.
//
//   2. return-on-cost was net P&L ÷ (avg entry × quantity), which is only a
//      dollar ratio when one price unit is worth one currency unit per
//      quantity. On FX that denominator is price × lots, so a −$2.59 trade
//      measured −22,266% and 1,027 of 1,346 trades exceeded ±1000%.
import { computeStats, fmtHold, qtyLabel, QTY_LABELS, legTime } from './tradeMetrics';

const leg = (date, time, action, qty = 0.01, price = 1.16) =>
  ({ date, time, action, qty, price });

const fxTrade = (executions, extra = {}) => ({
  date: '2025-09-02', ticker: 'EURUSD', instrument_type: 'FX',
  side: 'LONG', net_pnl: -2.59, executions, ...extra,
});

describe('legs are ordered by date and time, not by the clock', () => {
  test('a position held across three weeks keeps its own two ends', () => {
    const s = computeStats(fxTrade([
      leg('2025-08-12', '13:42:24', 'BOT'),
      leg('2025-09-02', '09:22:09', 'SOLD'),
    ]));

    expect(s.openDate).toBe('2025-08-12');
    expect(s.openTime).toBe('13:42:24');
    expect(s.closeDate).toBe('2025-09-02');
    expect(s.closeTime).toBe('09:22:09');
    // 21 days less 4h20m, to the minute.
    expect(s.holdMinutes).toBe(21 * 24 * 60 - 4 * 60 - 20);
    expect(fmtHold(s.holdMinutes)).toBe('20d 19h');
  });

  test('the ends are never swapped when the exit clock precedes the entry clock', () => {
    const s = computeStats(fxTrade([
      leg('2025-10-08', '18:18:01', 'BOT'),
      leg('2025-10-09', '04:30:01', 'SOLD'),
    ]));
    // Clock-only ordering reported this as open 04:30, close 18:18.
    expect(s.openTime).toBe('18:18:01');
    expect(s.closeTime).toBe('04:30:01');
    expect(s.holdMinutes).toBe(10 * 60 + 12);
    expect(fmtHold(s.holdMinutes)).toBe('10h 12m');
  });

  test('an overnight hold is positive — never the negative clock difference', () => {
    const s = computeStats(fxTrade([
      leg('2026-07-01', '22:00:00', 'BOT'),
      leg('2026-07-02', '06:00:00', 'SOLD'),
    ]));
    expect(s.holdMinutes).toBe(8 * 60);
    expect(fmtHold(s.holdMinutes)).toBe('8h 0m');
  });

  test('short holds keep the minute readout', () => {
    const s = computeStats(fxTrade([
      leg('2026-07-01', '09:28:00', 'BOT'),
      leg('2026-07-01', '09:31:00', 'SOLD'),
    ]));
    expect(s.holdMinutes).toBe(3);
    expect(fmtHold(s.holdMinutes)).toBe('3m');
  });

  test('an open position has no hold and no exit time', () => {
    const s = computeStats(fxTrade([leg('2026-07-01', '09:28:00', 'BOT')]));
    expect(s.isClosed).toBe(false);
    expect(s.holdMinutes).toBeNull();
    expect(s.closeTime).toBeNull();
    expect(s.openTime).toBe('09:28:00');
  });

  test('a leg with no time cannot be ordered, and is not reported as one', () => {
    const s = computeStats(fxTrade([
      { date: '2026-07-01', action: 'BOT', qty: 0.01, price: 1.16 },
      leg('2026-07-01', '10:00:00', 'SOLD'),
    ]));
    expect(s.closeTime).toBe('10:00:00');
    expect(s.openTime).toBe('10:00:00'); // falls back to the only stamp we have
    expect(Number.isFinite(s.holdMinutes)).toBe(true);
  });

  test('a SHORT trade takes the SOLD leg as its entry', () => {
    const s = computeStats({
      ...fxTrade([
        leg('2026-07-01', '14:00:00', 'SOLD'),
        leg('2026-07-01', '15:00:00', 'BOT'),
      ]),
      side: 'SHORT',
    });
    expect(s.openTime).toBe('14:00:00');
    expect(s.closeTime).toBe('15:00:00');
    expect(s.holdMinutes).toBe(60);
  });
});

describe('a return is only reported when the denominator is real money', () => {
  test('a stock still gets ROI and adjusted cost', () => {
    const s = computeStats({
      date: '2026-07-01', ticker: 'AAPL', instrument_type: 'STOCK',
      side: 'LONG', net_pnl: 50,
      executions: [leg('2026-07-01', '10:00:00', 'BOT', 10, 100),
                   leg('2026-07-01', '11:00:00', 'SOLD', 10, 105)],
    });
    expect(s.adjustedCost).toBe(1000);
    expect(s.netRoi).toBeCloseTo(5, 10);
  });

  test('an FX trade reports no ROI rather than a meaningless one', () => {
    const s = computeStats(fxTrade([
      leg('2025-09-02', '13:42:24', 'BOT'),
      leg('2025-09-02', '13:44:00', 'SOLD'),
    ]));
    // The old figure for this shape of trade: net P&L ÷ (1.16 × 0.01).
    expect(s.adjustedCost).toBeNull();
    expect(s.netRoi).toBeNull();
  });

  test('withholding reaches every instrument whose price unit is not a dollar', () => {
    // INDEX is linear (price × qty × 1), so it keeps its ROI — the rule is
    // "one price unit is worth one currency unit per quantity", not a list of
    // instruments.
    for (const type of ['FX', 'METAL', 'OPTION', 'FUTURE']) {
      const s = computeStats({
        date: '2026-07-01', ticker: 'X', instrument_type: type,
        side: 'LONG', net_pnl: 10,
        executions: [leg('2026-07-01', '10:00:00', 'BOT', 1, 100),
                     leg('2026-07-01', '11:00:00', 'SOLD', 1, 101)],
      });
      expect(s.netRoi).toBeNull();
      expect(s.adjustedCost).toBeNull();
    }

    const index = computeStats({
      date: '2026-07-01', ticker: 'SPX', instrument_type: 'INDEX',
      side: 'LONG', net_pnl: 10,
      executions: [leg('2026-07-01', '10:00:00', 'BOT', 1, 100),
                   leg('2026-07-01', '11:00:00', 'SOLD', 1, 101)],
    });
    expect(index.netRoi).toBeCloseTo(10, 6);
  });
});

describe('a leg that happened on another day carries its date', () => {
  test('a same-day fill reads as a clock time', () => {
    expect(legTime('2025-09-02', '09:22:09', '2025-09-02')).toBe('09:22');
  });

  test('an entry from an earlier day cannot pass for today', () => {
    expect(legTime('2025-08-12', '13:42:24', '2025-09-02')).toBe('2025-08-12 13:42');
  });

  test('a fill with no leg date still renders its clock time', () => {
    expect(legTime(null, '09:22:09', '2025-09-02')).toBe('09:22');
    expect(legTime('2025-09-02', null, '2025-09-02')).toBeNull();
  });
});

describe('a quantity is named for what it counts', () => {
  test('an FX position counts lots, not stocks', () => {
    expect(qtyLabel('FX')).toBe('Lots traded');
    expect(qtyLabel('STOCK')).toBe('Stocks traded');
    expect(qtyLabel('FUTURE')).toBe('Contracts traded');
    expect(qtyLabel(undefined)).toBe('Stocks traded');
    expect(qtyLabel('WEIRD')).toBe('Quantity');
    expect(Object.keys(QTY_LABELS).length).toBe(6);
  });
});
