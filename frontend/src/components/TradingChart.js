import { useState, useEffect, useRef } from 'react';
import { createChart, ColorType, CrosshairMode, LineStyle } from 'lightweight-charts';
import { chartApi } from '../api';
import { toTs, fillTs, axisLabel } from './chartTime';
import { priceDecimals } from '../instruments';

// lightweight-charts paints to canvas and cannot resolve CSS var(), so colours
// are read from the design tokens at render time. Fallbacks are the token values.
function cssVar(name, fallback) {
  if (typeof window === 'undefined') return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
function withAlpha(hex, alpha) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}
function chartTheme() {
  return {
    bg: cssVar('--surface-panel', '#1B222D'),
    text: cssVar('--text-secondary', '#96A4B6'),
    grid: cssVar('--divider-soft', '#262F3D'),
    border: cssVar('--divider', '#303B4B'),
    up: cssVar('--result-pos', '#66D7AC'),
    down: cssVar('--result-neg', '#F28B94'),
    vwap: cssVar('--text-secondary', '#96A4B6'),
    stop: cssVar('--caution', '#E9BA78'),
    target: cssVar('--accent-line', '#91A8FF'),
  };
}

// lightweight-charts formats time using UTC getters and has no timezone option.
// `toTs` / `execToTs` live in ./chartTime so the ET-vs-UTC source handling is
// testable on its own — see chartTime.test.js for why a marker and its candle
// must stay on one timeline.

// Daily/weekly bars are stamped at session open, already whole calendar days —
// no ET/UTC shift needed there, just a straight epoch conversion.
const toDayTs = (isoUtcStr) => Math.floor(new Date(isoUtcStr).getTime() / 1000);

const avgPrice = (fills) => {
  const qty = fills.reduce((s, f) => s + (f.qty || 0), 0);
  if (!qty) return null;
  return fills.reduce((s, f) => s + (f.qty || 0) * (f.price || 0), 0) / qty;
};

const TIMEFRAMES = [
  { id: '1Min', label: '1m' },
  { id: '3Min', label: '3m' },
  { id: '5Min', label: '5m' },
  { id: '10Min', label: '10m' },
  { id: '15Min', label: '15m' },
  { id: '30Min', label: '30m' },
  { id: '1Hour', label: '1H' },
  { id: '1Day', label: 'Daily' },
  { id: '1Week', label: 'Weekly' },
];
const TF_MINUTES = { '1Min': 1, '3Min': 3, '5Min': 5, '10Min': 10, '15Min': 15, '30Min': 30, '1Hour': 60 };
const WIDE_RANGE_TFS = new Set(['1Day', '1Week']);
// How many calendar days of backward history each timeframe will load before
// the lazy-load-on-zoom-out gives up. Finer intraday bars get a small cap so a
// single request doesn't balloon (1-min bars for 90 days would be ~35k bars);
// daily/weekly can go much further back since even a decade of daily bars is
// only ~2500 rows.
const MAX_DAYS_BACK = {
  '1Min': 5, '3Min': 10, '5Min': 20, '10Min': 30, '15Min': 45, '30Min': 60, '1Hour': 90,
  '1Day': 3650, '1Week': 5475,
};
// Default window per timeframe. 1m-15m load the trade day and open on its regular
// session (9:30-16:00 ET); zooming out past the first bar pulls in earlier days.
// 30m/1H open on the last month and daily/weekly on the last year, ending on the
// trade date.
const INITIAL_DAYS_BACK = { '30Min': 31, '1Hour': 31, '1Day': 366, '1Week': 366 };
const SESSION_TFS = new Set(['1Min', '3Min', '5Min', '10Min', '15Min']);
// Chart times live on the ET-shifted timeline (see toTs), so ET wall-clock
// times parse as literal UTC.
const etWallTs = (dateStr, hhmm) => Math.floor(new Date(`${dateStr}T${hhmm}:00Z`).getTime() / 1000);
// Legend entries that can be switched on and off. Not remembered between trades.
const DEFAULT_VISIBLE = { buy: true, sell: true, vwap: true, sl: true, target: true };

// Show or hide the toggleable layers on an existing chart.
function applyLayers(layers, visible) {
  if (!layers) return;
  (layers.vwap || []).forEach(line => line.applyOptions({ visible: visible.vwap }));
  if (layers.sl) layers.sl.applyOptions({ lineVisible: visible.sl, axisLabelVisible: visible.sl, title: visible.sl ? 'SL' : '' });
  if (layers.target) layers.target.applyOptions({ lineVisible: visible.target, axisLabelVisible: visible.target, title: visible.target ? 'Target' : '' });
  if (layers.candles) {
    const shown = layers.markers
      .filter(m => (m.isBuy ? visible.buy : visible.sell))
      .map(({ isBuy, ...m }) => m);
    layers.candles.setMarkers(shown);
  }
}

export default function TradingChart({
  ticker, date, defaultTimeframe = '5Min',
  executions = [], side = 'LONG', analysis = null,
  height = 320,
  instrumentType = 'STOCK',
}) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const [timeframe, setTimeframe] = useState(defaultTimeframe);
  const [bars, setBars] = useState([]);
  // Which feed answered: 'local' is imported broker bars (naive UTC, no ET
  // shift, no US session), 'remote' is the Alpaca equity feed, and null is
  // still loading — the axis label is held back until we know which it is.
  const [source, setSource] = useState(null);
  const [daysBack, setDaysBack] = useState(() => INITIAL_DAYS_BACK[defaultTimeframe] || 1);
  const [warning, setWarning] = useState(null);
  const [loading, setLoading] = useState(true);
  const isWide = WIDE_RANGE_TFS.has(timeframe);
  const [visible, setVisible] = useState(DEFAULT_VISIBLE);
  const visibleRef = useRef(DEFAULT_VISIBLE);
  // Handles to everything a legend toggle controls, so toggling never rebuilds
  // the chart (and never loses the current zoom).
  const layersRef = useRef({ candles: null, vwap: [], markers: [], sl: null, target: null });
  // Set right before a zoom-out-triggered fetch, holding the visible window so
  // it can be restored once the wider dataset lands — otherwise the chart would
  // jump back to fitContent() every time more history streams in.
  const savedRangeRef = useRef(null);
  const loadingMoreRef = useRef(false);
  // True only for the very first fetch after a ticker/date/timeframe change —
  // distinct from daysBack itself, since daily/weekly start at a large
  // default (240/730) rather than 1, so "daysBack === 1" can't tell an
  // initial load apart from a background zoom-out extension for those.
  const isInitialFetchRef = useRef(true);
  // Identifies the current ticker/date/timeframe selection, so a single effect
  // can tell "this is a new selection" apart from "daysBack grew from a
  // zoom-out" without needing a second effect — two effects both reacting to
  // a timeframe change fire in the same commit before state settles, which
  // fired the fetch once with the stale daysBack and again with the reset
  // value once it caught up.
  const selectionKeyRef = useRef(null);
  // Mirrors `executions` for the fetch effect below. The window-widening check
  // needs the fills, but listing `executions` in that effect's deps would make
  // every fill edit re-fetch the chart — recording an exit would reload it.
  // Kept in an effect rather than assigned during render so the first commit
  // has a value before the fetch effect runs.
  const executionsRef = useRef(executions);
  useEffect(() => { executionsRef.current = executions; });

  useEffect(() => {
    const key = `${ticker}|${date}|${timeframe}`;
    const isNewSelection = selectionKeyRef.current !== key;

    if (isNewSelection) {
      selectionKeyRef.current = key;
      savedRangeRef.current = null;
      loadingMoreRef.current = false;
      isInitialFetchRef.current = true;
      const initialDaysBack = INITIAL_DAYS_BACK[timeframe] || 1;
      if (initialDaysBack !== daysBack) {
        // Resolve daysBack first and let the re-render (with settled state)
        // do the actual fetch, instead of fetching now with the stale value.
        setDaysBack(initialDaysBack);
        return;
      }
    }

    const isInitialLoad = isInitialFetchRef.current;
    if (isInitialLoad) { setLoading(true); setBars([]); setWarning(null); }
    chartApi.get(ticker, date, timeframe, daysBack)
      .then(r => {
        setBars(r.data.bars || []);
        setWarning(r.data.warning || null);
        const isLocal = r.data.source === 'local';
        setSource(r.data.source === 'local' ? 'local' : 'remote');
        // The request window reaches back `daysBack` days from the trade's
        // *close* date. A fill from an earlier day sits outside it, so widen
        // once to cover the earliest execution — otherwise that fill is plotted
        // with no candles under it. Guarded so it cannot loop: the needed days
        // only ever shrink relative to what has already been asked for.
        if (isLocal) {
          const fills = executionsRef.current.filter(f => f.date);
          if (fills.some(f => f.date !== date)) {
            const earliest = fills.map(f => f.date).sort()[0];
            const span = Math.round(
              (new Date(`${date}T00:00:00Z`) - new Date(`${earliest}T00:00:00Z`)) / 86400000
            ) + 1;
            if (span > daysBack) setDaysBack(span);
          }
        }
      })
      .catch(() => { if (isInitialLoad) setWarning('Failed to load chart data'); })
      .finally(() => {
        if (isInitialLoad) setLoading(false);
        isInitialFetchRef.current = false;
        loadingMoreRef.current = false;
      });
  }, [ticker, date, timeframe, daysBack]);

  useEffect(() => {
    if (loading || !bars.length || !containerRef.current) return;

    if (chartRef.current) { chartRef.current.remove(); chartRef.current = null; }

    // Wide views (daily/weekly) never took the ET shift and still do not —
    // those bars are stamped at session open, already whole calendar days.
    const barTs = (stamp) => (isWide ? toDayTs(stamp) : toTs(stamp, source === 'local'));
    const T = chartTheme();
    layersRef.current = { candles: null, vwap: [], markers: [], sl: null, target: null };

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: T.bg },
        textColor: T.text,
        fontSize: 11,
      },
      grid: {
        vertLines: { color: T.grid },
        horzLines: { color: T.grid },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: {
        borderColor: T.border,
        scaleMargins: { top: 0.1, bottom: 0.22 },
      },
      timeScale: {
        borderColor: T.border,
        timeVisible: !isWide,
        secondsVisible: false,
      },
      width: containerRef.current.clientWidth,
      height,
    });
    chartRef.current = chart;

    // ── Candlestick series ────────────────────────────────────────────────
    const candleSeries = chart.addCandlestickSeries({
      upColor: T.up,
      downColor: T.down,
      borderUpColor: T.up,
      borderDownColor: T.down,
      wickUpColor: T.up,
      wickDownColor: T.down,
      // Without this the axis defaults to two decimals, which renders a 1.15298
      // EURUSD print as "1.15" on every tick label — the axis then disagrees
      // with the entry/exit lines drawn over it, which are priced at full
      // precision. minMove is the same unit priceDecimals describes, so the
      // labels can only show values the instrument actually trades on.
      priceFormat: {
        type: 'price',
        precision: priceDecimals(instrumentType, ticker),
        minMove: 1 / 10 ** priceDecimals(instrumentType, ticker),
      },
    });

    const candleData = bars.map(b => ({
      time: barTs(b.t),
      open: b.o, high: b.h, low: b.l, close: b.c,
    }));
    candleSeries.setData(candleData);
    layersRef.current.candles = candleSeries;

    // ── VWAP line ─────────────────────────────────────────────────────────
    // Alpaca's per-bar `vw` is just that bar's own volume-weighted price, which
    // tracks the candles almost exactly and isn't a useful indicator on its own.
    // A session VWAP is the running cumulative average from the open, so it's
    // built here as a running sum rather than plotted bar-by-bar. Only meaningful
    // within a single session, so skip it on the daily/weekly wide-context view.
    //
    // Local MT5 bars carry tick_volume but no per-bar vw, and trade 24 hours
    // with no US session to reset against — so there is nothing to build a
    // session VWAP from, and the legend hides the toggle rather than showing
    // one that plots no line.
    if (!isWide && source !== 'local') {
      // Restarts at 9:30 ET each day: bars are on the ET-shifted timeline, so the
      // UTC date and minutes read back out are ET. Pre-market and after-hours bars
      // get no VWAP. One line per session, so days are not joined to each other.
      const sessions = [];
      let current = null;
      let cumPV = 0;
      let cumVol = 0;
      for (const b of bars) {
        const ts = barTs(b.t);
        const d = new Date(ts * 1000);
        const minuteOfDay = d.getUTCHours() * 60 + d.getUTCMinutes();
        if (minuteOfDay < 9 * 60 + 30 || minuteOfDay >= 16 * 60) continue;
        const day = d.toISOString().slice(0, 10);
        if (!current || current.day !== day) {
          current = { day, points: [] };
          sessions.push(current);
          cumPV = 0;
          cumVol = 0;
        }
        if (b.vw != null && b.v) {
          cumPV += b.vw * b.v;
          cumVol += b.v;
        }
        if (cumVol > 0) current.points.push({ time: ts, value: cumPV / cumVol });
      }
      const withPoints = sessions.filter(sess => sess.points.length);
      layersRef.current.vwap = withPoints.map((sess, i) => {
        const isLast = i === withPoints.length - 1;
        const line = chart.addLineSeries({
          color: T.vwap,
          lineWidth: 2,
          lineStyle: LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: isLast,
          title: isLast ? 'VWAP' : '',
          crosshairMarkerVisible: isLast,
        });
        line.setData(sess.points);
        return line;
      });
    }

    // ── Volume histogram ──────────────────────────────────────────────────
    const volSeries = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
    });
    chart.priceScale('vol').applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
      borderVisible: false,
    });
    volSeries.setData(bars.map(b => ({
      time: barTs(b.t),
      value: b.v,
      color: b.c >= b.o ? withAlpha(T.up, 0.32) : withAlpha(T.down, 0.32),
    })));

    // ── Price lines: entry, exit, stop, target ────────────────────────────
    const entryFills = executions.filter(e => side === 'LONG' ? e.action === 'BOT' : e.action === 'SOLD');
    const exitFills  = executions.filter(e => side === 'LONG' ? e.action === 'SOLD' : e.action === 'BOT');
    const ae = avgPrice(entryFills);
    const ax = avgPrice(exitFills);

    if (ae) candleSeries.createPriceLine({ price: ae, color: T.up, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'Entry' });
    if (ax) candleSeries.createPriceLine({ price: ax, color: T.down, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'Exit' });
    if (analysis?.stop_loss) layersRef.current.sl = candleSeries.createPriceLine({ price: Number(analysis.stop_loss), color: T.stop, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'SL' });
    if (analysis?.target_price) layersRef.current.target = candleSeries.createPriceLine({ price: Number(analysis.target_price), color: T.target, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'Target' });

    // ── Execution markers ────────────────────────────────────────────────
    // Colored and shaped by the actual fill action (matches the Buy/Sell legend
    // below the chart), not by entry/exit role — a short's opening fill is a SELL,
    // so styling it as "entry = green/up" made it look like a buy. Only meaningful
    // at intraday resolution: on the daily/weekly view a 35-minute trade is a
    // fraction of one bar, so there's nothing sensible to anchor a marker to.
    if (!isWide) {
      const bucketMin = TF_MINUTES[timeframe] || 5;
      const markers = executions
        .filter(f => f.time)
        .map(f => {
          // Each fill is plotted on its own date, falling back to the trade's
          // when it carries none. `date` is the trade's *close* date, so using
          // it for every fill would move an entry from an earlier day onto the
          // close date — four of 1,346 real positions span more than one day.
          const ts = fillTs(f, date, bucketMin);
          if (!ts) return null;
          const isBuy = f.action === 'BOT';
          return {
            isBuy,
            time: ts,
            position: isBuy ? 'belowBar' : 'aboveBar',
            color: isBuy ? T.up : T.down,
            shape: isBuy ? 'arrowUp' : 'arrowDown',
            // Full precision, same `qty@price` shape it has always had: the
            // chart axis above now labels in these digits too, so a marker
            // reading "1.15" under a "1.15298" axis would disagree with it.
            text: `${f.qty}@${Number(f.price).toFixed(priceDecimals(instrumentType, ticker))}`,
          };
        })
        .filter(Boolean)
        .sort((a, b) => a.time - b.time);
      layersRef.current.markers = markers;
    }
    applyLayers(layersRef.current, visibleRef.current);

    // Restore the pre-fetch window when this render is a background history
    // extension (below), so the view holds still instead of snapping back to
    // fitContent() every time more bars land.
    if (savedRangeRef.current) {
      chart.timeScale().setVisibleRange(savedRangeRef.current);
      savedRangeRef.current = null;
    } else if (source === 'local' && layersRef.current.markers?.length) {
      // Local bars trade 24h, so there is no US session to open on. Frame the
      // window around the trade's own fills instead: the point of this chart is
      // what happened between entry and exit, not the whole day around them.
      const times = layersRef.current.markers.map(m => m.time);
      const pad = Math.max((TF_MINUTES[timeframe] || 5) * 60 * 6, 60);
      chart.timeScale().setVisibleRange({
        from: Math.min(...times) - pad,
        to: Math.max(...times) + pad,
      });
    } else if (SESSION_TFS.has(timeframe)) {
      // Open on the trade day's regular session. Falls back to the whole load
      // when the day has no bars inside 9:30-16:00.
      const bucketSec = (TF_MINUTES[timeframe] || 5) * 60;
      const from = etWallTs(date, '09:30');
      const to = etWallTs(date, '16:00') - bucketSec;
      const inSession = candleData.some(c => c.time >= from && c.time <= to);
      if (inSession) chart.timeScale().setVisibleRange({ from, to });
      else chart.timeScale().fitContent();
    } else {
      chart.timeScale().fitContent();
    }

    // Zoom-out-to-load-more-history: barsBefore counts real bars between the
    // left edge of the visible window and the first loaded bar. Right after
    // fitContent() it's exactly 0 — the window's left edge sits precisely on
    // the first bar, showing only what's loaded (a single session for the
    // fine intraday timeframes, by design). It only goes NEGATIVE once the
    // user actually zooms or pans past the start of the data and blank space
    // is on screen — that's the real trigger. A positive threshold like the
    // "< 10" this used to be misfires on every initial load, since a fresh
    // fitContent() view already satisfies it at 0.
    const maxDays = MAX_DAYS_BACK[timeframe] || 30;
    // Only a zoom or pan by the user loads more history. Resizes and the initial
    // positioning also move the visible range and must not pull in extra days.
    let userMoved = false;
    const markUserMove = () => { userMoved = true; };
    const el = containerRef.current;
    ['wheel', 'mousedown', 'touchstart'].forEach(evt => el.addEventListener(evt, markUserMove, { passive: true }));
    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (!range || !userMoved || loadingMoreRef.current || daysBack >= maxDays) return;
      const barsInfo = candleSeries.barsInLogicalRange(range);
      if (barsInfo != null && barsInfo.barsBefore < -1) {
        loadingMoreRef.current = true;
        savedRangeRef.current = chart.timeScale().getVisibleRange();
        setDaysBack(d => Math.min(maxDays, Math.max(d * 3, d + 4)));
      }
    });

    const ro = new ResizeObserver(() => {
      if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth });
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      ['wheel', 'mousedown', 'touchstart'].forEach(evt => el.removeEventListener(evt, markUserMove));
      chart.remove();
      chartRef.current = null;
    };
  }, [bars, executions, side, analysis, height, loading, date, timeframe, isWide, daysBack, source, instrumentType, ticker]);

  useEffect(() => {
    visibleRef.current = visible;
    applyLayers(layersRef.current, visible);
  }, [visible]);

  const toggle = (key) => setVisible(v => ({ ...v, [key]: !v[key] }));
  const legendItems = [
    { key: 'buy', label: 'Buy fill', swatch: { width: 9, height: 9, borderRadius: '50%', background: 'var(--result-pos)' } },
    { key: 'sell', label: 'Sell fill', swatch: { width: 9, height: 9, borderRadius: '50%', background: 'var(--result-neg)' } },
    !isWide && source !== 'local' && { key: 'vwap', label: 'VWAP', swatch: { width: 16, height: 2, background: 'var(--text-secondary)' } },
    analysis?.stop_loss && { key: 'sl', label: 'SL', swatch: { width: 16, height: 2, background: 'var(--caution)' } },
    analysis?.target_price && { key: 'target', label: 'Target', swatch: { width: 16, height: 2, background: 'var(--accent-line)' } },
  ].filter(Boolean);

  return (
    <div>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        marginBottom: 8, flexWrap: 'wrap', gap: 8,
      }}>
        <h2 className="section-title" style={{ fontSize: 17 }}>
          {ticker} · {TIMEFRAMES.find(t => t.id === timeframe)?.label} Chart · <span className="num text-muted" style={{ fontWeight: 500 }}>{date}</span>
          <span className="text-muted" style={{ fontSize: 11, fontWeight: 500, marginLeft: 7 }}>{axisLabel(source)} axis</span>
        </h2>
        <div className="seg" role="group" aria-label="Chart timeframe">
          {TIMEFRAMES.map(tf => (
            <button
              type="button"
              key={tf.id}
              className="seg-btn"
              aria-pressed={timeframe === tf.id}
              onClick={() => setTimeframe(tf.id)}
            >
              {tf.label}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="skeleton" style={{ height, borderRadius: 8 }} />
      ) : warning && !bars.length ? (
        <div role="status" style={{
          display: 'flex', alignItems: 'center', gap: 10,
          color: 'var(--text-secondary)', fontSize: 13.5,
          background: 'var(--surface-inset)', borderRadius: 'var(--radius-md)',
          padding: '14px 16px', borderLeft: '2px solid var(--caution)',
        }}>
          {warning}
        </div>
      ) : (
        <>
          {warning && <div role="status" style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 4 }}>{warning}</div>}
          <div className="chart-legend" role="group" aria-label="Show or hide chart layers">
            {legendItems.map(item => (
              <button
                type="button"
                key={item.key}
                className="chart-legend-item"
                aria-pressed={visible[item.key]}
                title={`${visible[item.key] ? 'Hide' : 'Show'} ${item.label}`}
                onClick={() => toggle(item.key)}
              >
                <span aria-hidden="true" style={{ display: 'inline-block', ...item.swatch }} />
                {item.label}
              </button>
            ))}
          </div>
          <div ref={containerRef} style={{ width: '100%', background: 'var(--surface-panel)', borderRadius: 'var(--radius-md)' }} />
        </>
      )}
    </div>
  );
}
