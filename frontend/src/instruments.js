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
