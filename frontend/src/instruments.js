// The instrument types TDJournal accepts, mirrored from backend/instruments.py.
//
// Kept in a module rather than repeated in four components so a type added for
// MT5 shows up in every select, filter and badge at once.

export const INSTRUMENT_TYPES = ['STOCK', 'OPTION', 'FUTURE', 'FX', 'METAL', 'INDEX'];

export const INSTRUMENT_LABELS = {
  STOCK: 'Stock',
  OPTION: 'Option',
  FUTURE: 'Future',
  FX: 'Forex',
  METAL: 'Metal',
  INDEX: 'Index',
};

/** Singular label for a type — matches the word in a single-trade subtitle. */
export const instrumentLabel = (t) => {
  if (!t) return 'Stock';
  return INSTRUMENT_LABELS[t.toUpperCase()] || t;
};

/** Badge and chart colour per type. Unknown types fall back to neutral grey. */
export const INSTRUMENT_COLORS = {
  STOCK: '#5bb0d7',
  OPTION: '#e8a95c',
  FUTURE: '#6bc987',
  FX: '#a68fe0',
  METAL: '#e0c15b',
  INDEX: '#e07a9a',
};

export const instrumentColor = (t) => INSTRUMENT_COLORS[t] || '#8f9297';

/**
 * True when one price unit is worth exactly one currency unit per quantity.
 *
 * What-if scenarios estimate P&L as (price - exit) × qty, which is only right
 * when a 0.00250 move on EURUSD is worth $0.0025 rather than $250. FX and
 * metals have a contract size, options have a 100x multiplier, futures have a
 * point value — all of them would be off by orders of magnitude, so the
 * estimate is withheld rather than printed wrong.
 */
export const isPriceDeltaLinear = (t) => {
  const v = (t || 'STOCK').toUpperCase();
  return v === 'STOCK' || v === 'INDEX';
};

/**
 * The chart behind what-if shows equities from Alpaca, so only stock trades
 * have bars to walk. This is about data availability, not about P&L shape.
 */
export const hasEquityChart = (t) => !t || t.toUpperCase() === 'STOCK';

/**
 * How many decimal places a *price* for this instrument is quoted at.
 *
 * Five-digit FX rendered through toFixed(2) collapsed an entire what-if table
 * into five identical "$1.19" rows while the real prices spanned 1.19050–
 * 1.19449 — four ten-thousandths from flipping the last row to "$1.20". The
 * figure was correct; only the display was hiding it.
 *
 * Yen-quoted FX is three decimals (USDJPY ~148.250), everything else two.
 */
export const priceDecimals = (t, ticker) => {
  const v = (t || 'STOCK').toUpperCase();
  const pair = (ticker || '').toUpperCase();
  if (v === 'FX') return /JPY$/.test(pair) ? 3 : 5;
  if (v === 'METAL') return 2;
  return 2;
};

/**
 * A quoted price at the precision the instrument trades at.
 *
 * Lives here rather than with any one view, because it answers a question the
 * whole UI shares: a EURUSD entry at 1.15298 and an exit at 1.15283 both read
 * "$1.15" through `toFixed(2)` — identical — and the point of showing two
 * columns is the difference between them.
 *
 * `null`/undefined/empty renders as a dash. `Number('')` is 0 rather than NaN,
 * so the empty case has to be checked before it — otherwise a cleared input
 * shows `$0.00000`, a value we never observed.
 */
export const fmtPrice = (price, trade) =>
  price === null || price === undefined || price === '' || Number.isNaN(Number(price))
    ? '—'
    : `$${Number(price).toFixed(priceDecimals(trade.instrument_type, trade.ticker))}`;

/**
 * The `step` a price input should offer.
 *
 * `<input type="number" step="0.01">` does not merely suggest a granularity —
 * the browser rejects the *whole form* on submit if the value is not a multiple
 * of the step. A EURUSD entry of 1.15300 therefore could not be added at all
 * through Add Trade, which is a broken field rather than a formatting choice.
 *
 * Derived from priceDecimals, so the two can never disagree. Any step ≤ the
 * precision works for the browser's check; the smallest whole unit of the
 * quoted price is the obvious one.
 */
export const priceStep = (t, ticker) => 1 / 10 ** priceDecimals(t, ticker);

/**
 * Whether the notice about an unreliable estimate should appear.
 * True when we display the scenario table without a usable delta.
 */
export const needsWhatIfCaveat = (t) => !isPriceDeltaLinear(t);

/** Why the what-if estimate is withheld, phrased per instrument family. */
export const whatIfCaveat = (t) => {
  const v = (t || 'STOCK').toUpperCase();
  if (v === 'OPTION') {
    return 'Prices shown are the underlying stock. Option P&L depends on delta, theta, and time value, so estimated P&L is not computed.';
  }
  if (v === 'FX' || v === 'METAL') {
    return 'Prices shown are for the instrument itself. A move of one tick is not worth one dollar — position size is measured in lots — so estimated P&L is not computed.';
  }
  if (v === 'FUTURE') {
    return 'Prices shown are index points. A contract is worth points × multiplier, so estimated P&L is not computed.';
  }
  return 'Estimated P&L is not computed for this instrument.';
};
