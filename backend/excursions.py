"""MFE / MAE and exit efficiency for MetaTrader 5 trades, measured from exported M1 bars.

For each closed trade the bars between its first entry and last exit are scanned:

  MFE  the best the trade got, in pips (FOREX) or points (other MT5 classes), never below 0
  MAE  the worst it got, in the same unit, never above 0 (stored negative, like mae_pct)
  exit_efficiency  realised move divided by MFE, as a percentage

The same figures are also written as a percentage of the entry price into mfe_pct / mae_pct,
which the existing Trade View and Review screens already display.

Bars are bid prices and the minute of entry and exit can include a little movement outside the
hold, so MFE is never allowed below the realised result and MAE never above it.

Positions that were closed with MT5's "close by" are skipped: MT5 books the pair's whole profit
on one leg and prices each leg at the other's open, so per-leg excursions would be misleading.
"""
import bisect
import json
from datetime import datetime

import csv_parser
import mt5_bars

MT5_TYPES = sorted(csv_parser.MT5_INSTRUMENT_TYPES)
# How far from the entry or exit minute the nearest bar may be before the bars are treated as missing.
GAP_SECONDS = 120
_EMPTY_STATS = {'computed': 0, 'no_bars': 0, 'close_by': 0, 'open': 0, 'reimport': 0, 'no_pips': 0}


def _hold_window(execs: list[dict]):
    """(side-independent) entry/exit prices and UTC times from a trade's executions, or None if not closed."""
    if any('entry' not in e for e in execs):
        return 'reimport'   # imported before the deal kind was stored; re-importing the CSV adds it
    if any(e.get('entry') == 'out_by' for e in execs):
        return 'close_by'
    ins = [e for e in execs if e.get('entry') == 'in']
    outs = [e for e in execs if e.get('entry') == 'out']
    if not ins or not outs:
        return 'open'
    vol_in = sum(e['qty'] for e in ins)
    vol_out = sum(e['qty'] for e in outs)
    if abs(vol_in - vol_out) > 1e-6:
        return 'open'

    def when(e):
        return datetime.strptime(f"{e['date']} {e['time']}", '%Y-%m-%d %H:%M:%S')

    entry_price = sum(e['price'] * e['qty'] for e in ins) / vol_in
    exit_price = sum(e['price'] * e['qty'] for e in outs) / vol_out
    entry_time = csv_parser.display_to_utc(min(when(e) for e in ins))
    exit_time = csv_parser.display_to_utc(max(when(e) for e in outs))
    return entry_price, exit_price, entry_time, exit_time


def measure(series, side: str, entry_price: float, entry_time: datetime, exit_time: datetime,
            unit: float, realised: float):
    """(mfe, mae) in units, or None if the bars do not cover the hold."""
    lo_ts = int(entry_time.timestamp()) // 60 * 60
    hi_ts = int(exit_time.timestamp()) // 60 * 60
    lo = bisect.bisect_left(series.utc, lo_ts)
    hi = bisect.bisect_right(series.utc, hi_ts)
    if hi <= lo:
        return None
    if series.utc[lo] - lo_ts > GAP_SECONDS or hi_ts - series.utc[hi - 1] > GAP_SECONDS:
        return None
    high = max(series.h[lo:hi])
    low = min(series.l[lo:hi])
    if side == 'LONG':
        mfe, mae = (high - entry_price) / unit, (low - entry_price) / unit
    else:
        mfe, mae = (entry_price - low) / unit, (entry_price - high) / unit
    return max(mfe, realised, 0.0), min(mae, realised, 0.0)


def recalc_excursions(conn, account_id: int | None = None, trade_groups: list[str] | None = None) -> dict:
    """Fill mfe_pips, mae_pips, mfe_pct, mae_pct and exit_efficiency for MT5 trades.

    Returns counts: computed, and why the others were skipped (no_bars, close_by, open, reimport, no_pips).
    """
    sql = ("SELECT id, account_id, ticker, instrument_type, side, executions, pips, trade_group "
           f"FROM trades WHERE instrument_type IN ({','.join('?' * len(MT5_TYPES))})")
    params: list = list(MT5_TYPES)
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    if trade_groups is not None:
        if not trade_groups:
            return dict(_EMPTY_STATS)
        sql += f" AND trade_group IN ({','.join('?' * len(trade_groups))})"
        params.extend(trade_groups)

    stats = dict(_EMPTY_STATS)
    for row in conn.execute(sql, params).fetchall():
        execs = json.loads(row['executions'] or '[]')
        window = _hold_window(execs)
        if window in ('close_by', 'open', 'reimport'):
            stats[window] += 1
            continue
        entry_price, _exit_price, entry_time, exit_time = window

        spec = conn.execute(
            "SELECT digits, point FROM symbol_specs WHERE account_id = ? AND symbol = ?",
            (row['account_id'], row['ticker']),
        ).fetchone()
        unit = csv_parser._mt5_unit(row['instrument_type'], row['ticker'],
                                    spec['digits'] if spec else None, spec['point'] if spec else None)
        if not unit or row['pips'] is None:
            stats['no_pips'] += 1
            continue

        series = mt5_bars.load_series(row['ticker'])
        result = measure(series, row['side'], entry_price, entry_time, exit_time, unit, row['pips']) if series else None
        if result is None:
            stats['no_bars'] += 1
            continue

        mfe, mae = result
        pct = unit / entry_price * 100
        efficiency = round(row['pips'] / mfe * 100, 2) if mfe > 0 else None
        conn.execute(
            "UPDATE trades SET mfe_pips=?, mae_pips=?, mfe_pct=?, mae_pct=?, exit_efficiency=? WHERE id=?",
            (round(mfe, 1), round(mae, 1), round(mfe * pct, 3), round(mae * pct, 3), efficiency, row['id']),
        )
        stats['computed'] += 1
    return stats
