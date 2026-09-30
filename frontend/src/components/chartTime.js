// lightweight-charts formats timestamps using UTC getters and has no timezone
// option. Preserve the existing Alpaca ET-wall-clock display while passing
// imported MT5 bars through as UTC; fills are already stored on the matching
// wall-clock timeline for each source.
const ET_UTC_OFFSET_SEC = 4 * 3600;

export const toTs = (isoUtcStr, localSource = false) => {
  const epoch = Math.floor(new Date(isoUtcStr).getTime() / 1000);
  return localSource ? epoch : epoch - ET_UTC_OFFSET_SEC;
};

export const execToTs = (dateStr, timeStr, bucketMin = 5) => {
  if (!timeStr) return null;
  const [h, m] = timeStr.slice(0, 5).split(':').map(Number);
  const totalMin = Math.floor((h * 60 + m) / bucketMin) * bucketMin;
  const rh = Math.floor(totalMin / 60);
  const rm = totalMin % 60;
  const hh = String(rh).padStart(2, '0');
  const mm = String(rm).padStart(2, '0');
  // No offset correction here on either path, deliberately. Both timelines are
  // "stored wall-clock rendered as if it were UTC": Alpaca bars are shifted down
  // to ET by toTs() while an equity fill is already stored as ET wall-clock, and
  // MT5 bars and fills are both stored as UTC. Applying an offset to markers but
  // not bars (or vice versa) would put every fill four hours from its candle.
  return Math.floor(new Date(`${dateStr}T${hh}:${mm}:00Z`).getTime() / 1000);
};

// Where a fill is plotted. Each fill carries its own date; only a fill without
// one falls back to the trade's date — which is its *close* date, so applying
// it unconditionally moves an entry from an earlier day onto the close date.
// (Four of 1,346 real positions span more than one day.)
export const fillTs = (fill, tradeDate, bucketMin = 5) =>
  execToTs(fill.date || tradeDate, fill.time, bucketMin);

// What the chart tells the user it is reading. The remote feed's axis is
// ET-shifted; imported bars pass through as UTC, and a chart that silently
// changes basis between symbols invites misreading every timestamp.
export const axisLabel = (source) => (source === 'local' ? 'UTC' : 'ET');
