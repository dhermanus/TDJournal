"""M1 price bars exported from MetaTrader 5 by scripts/mt5/ExportBarsCSV.mq5.

The script writes one file per symbol, TDJournal_bars_<SYMBOL>_M1.csv, into MT5's shared
Common\\Files folder, with columns time,open,high,low,close,tick_volume in raw broker server
time. This module reads those files (nothing is copied or uploaded), converts server time to
UTC using the same rule as the deal import, and builds the chart timeframes from M1.

MT5_BARS_DIR (env) points at the folder. When it is not set, the standard Windows location of
MT5's Common\\Files folder is tried.
"""
import bisect
import csv
import glob
import os
from datetime import datetime, timezone

import csv_parser

# The timeframes the trade chart asks for (same names as the Alpaca chart route).
TIMEFRAME_MINUTES = {
    '1Min': 1, '3Min': 3, '5Min': 5, '10Min': 10, '15Min': 15, '30Min': 30, '1Hour': 60,
}
WIDE_TIMEFRAMES = {'1Day', '1Week'}

_EPOCH = datetime(1970, 1, 1)


class Series:
    """One symbol's M1 bars, ascending. utc / server are epoch seconds (server time is treated
    as if it were UTC, only so that day and week buckets can follow the broker's clock)."""
    __slots__ = ('utc', 'server', 'o', 'h', 'l', 'c', 'v')

    def __init__(self):
        self.utc, self.server = [], []
        self.o, self.h, self.l, self.c, self.v = [], [], [], [], []


_offset_cache: dict = {}
_series_cache: dict = {}


def clear_caches() -> None:
    _offset_cache.clear()
    _series_cache.clear()


def bars_dir() -> str | None:
    folder = (os.getenv('MT5_BARS_DIR') or '').strip()
    if folder:
        return folder
    appdata = os.getenv('APPDATA')
    if appdata:
        return os.path.join(appdata, 'MetaQuotes', 'Terminal', 'Common', 'Files')
    return None


def find_file(ticker: str) -> str | None:
    folder = bars_dir()
    if not folder or not os.path.isdir(folder):
        return None
    exact = os.path.join(folder, f"TDJournal_bars_{ticker}_M1.csv")
    if os.path.isfile(exact):
        return exact
    # a broker suffix on the symbol name, e.g. TDJournal_bars_EURUSD.m_M1.csv
    pattern = os.path.join(folder, f"TDJournal_bars_{glob.escape(ticker)}*_M1.csv")
    matches = sorted(glob.glob(pattern))
    return matches[0] if matches else None


def _server_offset_seconds(server_dt: datetime) -> int:
    """server time minus UTC, in seconds, at this hour (cached: it only changes with daylight saving)."""
    hour = server_dt.replace(minute=0, second=0, microsecond=0)
    key = (os.getenv('MT5_SERVER_TIME_RULE'), hour)
    if key not in _offset_cache:
        utc = csv_parser.mt5_server_to_utc(hour).replace(tzinfo=None)
        _offset_cache[key] = int((hour - utc).total_seconds())
    return _offset_cache[key]


def _epoch(dt: datetime) -> int:
    return int((dt - _EPOCH).total_seconds())


def load_series(ticker: str) -> Series | None:
    """The symbol's bars, or None when there is no export for it. Re-read when the file changes."""
    path = find_file(ticker)
    if not path:
        return None
    stat = os.stat(path)
    stamp = (stat.st_mtime_ns, stat.st_size, os.getenv('MT5_SERVER_TIME_RULE'))
    cached = _series_cache.get(path)
    if cached and cached[0] == stamp:
        return cached[1]

    rows = []
    with open(path, newline='', encoding='utf-8-sig', errors='replace') as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header
        for cells in reader:
            if len(cells) < 5:
                continue
            when = csv_parser._mt5_time(cells[0])
            if when is None:
                continue
            try:
                o, h, l, c = (float(x) for x in cells[1:5])
                v = float(cells[5]) if len(cells) > 5 and cells[5].strip() else 0.0
            except ValueError:
                continue
            rows.append((when, o, h, l, c, v))
    rows.sort(key=lambda r: r[0])

    series = Series()
    last = None
    for when, o, h, l, c, v in rows:
        if when == last:
            continue  # overlapping export windows can repeat a minute
        last = when
        s = _epoch(when)
        series.server.append(s)
        series.utc.append(s - _server_offset_seconds(when))
        series.o.append(o)
        series.h.append(h)
        series.l.append(l)
        series.c.append(c)
        series.v.append(v)
    _series_cache[path] = (stamp, series)
    return series


def _bucket_start(server_epoch: int, timeframe: str) -> int:
    if timeframe in TIMEFRAME_MINUTES:
        size = TIMEFRAME_MINUTES[timeframe] * 60
        return server_epoch - server_epoch % size
    day = server_epoch // 86400
    if timeframe == '1Day':          # the broker's day, which starts at server midnight
        return day * 86400
    return (day - (day + 3) % 7) * 86400   # 1Week: Monday 00:00 server (1970-01-01 was a Thursday)


def get_bars(ticker: str, start_utc: datetime, end_utc: datetime, timeframe: str) -> list[dict] | None:
    """Bars in [start_utc, end_utc) as {t, o, h, l, c, v, vw}, t being UTC ISO like the Alpaca route.

    Returns None when there is no bars file for the symbol, [] when the file has nothing in the range.
    """
    series = load_series(ticker)
    if series is None:
        return None
    lo = bisect.bisect_left(series.utc, int(start_utc.timestamp()))
    hi = bisect.bisect_left(series.utc, int(end_utc.timestamp()))

    out: list[dict] = []
    current = None  # [bucket_server_epoch, o, h, l, c, v]
    for i in range(lo, hi):
        bucket = _bucket_start(series.server[i], timeframe)
        if current is None or current[0] != bucket:
            if current is not None:
                out.append(_finish(current))
            current = [bucket, series.o[i], series.h[i], series.l[i], series.c[i], series.v[i]]
        else:
            current[2] = max(current[2], series.h[i])
            current[3] = min(current[3], series.l[i])
            current[4] = series.c[i]
            current[5] += series.v[i]
    if current is not None:
        out.append(_finish(current))
    return out


def _finish(bucket: list) -> dict:
    server_dt = datetime.fromtimestamp(bucket[0], timezone.utc).replace(tzinfo=None)
    utc_epoch = bucket[0] - _server_offset_seconds(server_dt)
    return {
        't': datetime.fromtimestamp(utc_epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'o': bucket[1], 'h': bucket[2], 'l': bucket[3], 'c': bucket[4],
        'v': bucket[5], 'vw': None,
    }


def no_data_hint(ticker: str) -> str:
    folder = bars_dir()
    if not folder:
        return (f"No MT5 bars for {ticker}. Run scripts/mt5/ExportBarsCSV.mq5 in MetaTrader 5 and "
                "set MT5_BARS_DIR in backend/.env to the folder it prints.")
    if not os.path.isdir(folder):
        return f"The MT5 bars folder does not exist: {folder}. Check MT5_BARS_DIR in backend/.env."
    return (f"No bars file for {ticker} in {folder}. Run scripts/mt5/ExportBarsCSV.mq5 "
            "in MetaTrader 5 (it writes TDJournal_bars_<symbol>_M1.csv).")

