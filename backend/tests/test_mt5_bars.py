"""Tests for the MT5 bars reader, the FX chart route and the MFE / MAE calculation.

Run from the backend folder:  python -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_TMP = tempfile.TemporaryDirectory()
os.environ['DATABASE_PATH'] = os.path.join(_TMP.name, 'boot.db')
os.environ['UPLOAD_DIR'] = os.path.join(_TMP.name, 'uploads')
os.environ.pop('MT5_SERVER_TIME_RULE', None)
os.environ.pop('DISPLAY_TIMEZONE', None)

import database  # noqa: E402
import csv_parser  # noqa: E402
import mt5_bars  # noqa: E402
import excursions  # noqa: E402

FIXTURE = (BACKEND / 'tests' / 'fixtures' / 'mt5_sample.csv').read_text()


def tearDownModule():
    # Leave nothing behind in the environment: the app reads these variables when it is imported, so a
    # later test module that re-imports it (test_reimport.py does) would otherwise find a deleted folder.
    for key in ('DATABASE_PATH', 'UPLOAD_DIR'):
        if os.environ.get(key, '').startswith(_TMP.name):
            os.environ.pop(key)
    _TMP.cleanup()


def write_bars(folder, symbol, day='2026.06.15', start_hour=8, end_hour=12, stop_at=None, spikes=None):
    """M1 bars in server time. Flat at 1.0800 with a small range; `spikes` overrides single minutes."""
    spikes = spikes or {}
    lines = ['time,open,high,low,close,tick_volume']
    t = datetime.strptime(f'{day} {start_hour:02d}:00:00', '%Y.%m.%d %H:%M:%S')
    end = datetime.strptime(f'{day} {end_hour:02d}:00:00', '%Y.%m.%d %H:%M:%S')
    while t < end:
        if stop_at and t >= stop_at:
            break
        h, l = 1.08002, 1.07998
        h = spikes.get((t.hour, t.minute, 'h'), h)
        l = spikes.get((t.hour, t.minute, 'l'), l)
        lines.append(f"{t.strftime('%Y.%m.%d %H:%M:%S')},1.08000,{h:.5f},{l:.5f},1.08000,10")
        t += timedelta(minutes=1)
    Path(folder, f'TDJournal_bars_{symbol}_M1.csv').write_text('\n'.join(lines) + '\n')


SPIKES = {(9, 40, 'h'): 1.08400, (10, 10, 'l'): 1.07900}


class BarsReaderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ['MT5_BARS_DIR'] = self.dir.name
        mt5_bars.clear_caches()
        write_bars(self.dir.name, 'EURUSD', spikes=SPIKES)

    def tearDown(self):
        os.environ.pop('MT5_BARS_DIR', None)
        mt5_bars.clear_caches()
        self.dir.cleanup()

    def window(self):
        # 2026-06-15 in Jakarta, as a UTC range
        return (datetime(2026, 6, 14, 17, tzinfo=timezone.utc), datetime(2026, 6, 15, 17, tzinfo=timezone.utc))

    def test_m1_times_are_utc_from_server_time(self):
        bars = mt5_bars.get_bars('EURUSD', *self.window(), '1Min')
        self.assertEqual(len(bars), 240)
        self.assertEqual(bars[0]['t'], '2026-06-15T05:00:00Z')   # server 08:00, summer GMT+3

    def test_five_minute_bars_are_built_from_m1(self):
        bars = {b['t']: b for b in mt5_bars.get_bars('EURUSD', *self.window(), '5Min')}
        spike = bars['2026-06-15T06:40:00Z']                    # server 09:40
        self.assertAlmostEqual(spike['h'], 1.084)
        self.assertAlmostEqual(spike['l'], 1.07998)
        self.assertEqual((spike['o'], spike['c'], spike['v']), (1.08, 1.08, 50.0))
        self.assertEqual(len(bars), 48)

    def test_hourly_and_daily_follow_the_server_clock(self):
        hourly = mt5_bars.get_bars('EURUSD', *self.window(), '1Hour')
        self.assertEqual([b['t'] for b in hourly][:2], ['2026-06-15T05:00:00Z', '2026-06-15T06:00:00Z'])
        daily = mt5_bars.get_bars('EURUSD', datetime(2026, 6, 14, 17, tzinfo=timezone.utc),
                                  datetime(2026, 6, 25, 17, tzinfo=timezone.utc), '1Day')
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]['t'], '2026-06-14T21:00:00Z')  # server midnight = 21:00 UTC in summer
        self.assertAlmostEqual(daily[0]['h'], 1.084)
        self.assertEqual(daily[0]['v'], 2400.0)

    def test_missing_file_and_empty_range(self):
        self.assertIsNone(mt5_bars.get_bars('GBPUSD', *self.window(), '5Min'))
        later = (datetime(2026, 7, 1, tzinfo=timezone.utc), datetime(2026, 7, 2, tzinfo=timezone.utc))
        self.assertEqual(mt5_bars.get_bars('EURUSD', *later, '5Min'), [])

    def test_broker_suffix_in_the_file_name(self):
        Path(self.dir.name, 'TDJournal_bars_EURUSD_M1.csv').rename(
            Path(self.dir.name, 'TDJournal_bars_EURUSD.m_M1.csv'))
        mt5_bars.clear_caches()
        self.assertIsNotNone(mt5_bars.get_bars('EURUSD', *self.window(), '5Min'))

    def test_file_changes_are_picked_up(self):
        self.assertEqual(len(mt5_bars.get_bars('EURUSD', *self.window(), '1Min')), 240)
        write_bars(self.dir.name, 'EURUSD', end_hour=10)
        self.assertEqual(len(mt5_bars.get_bars('EURUSD', *self.window(), '1Min')), 120)


class MeasureTests(unittest.TestCase):
    def series(self, **kw):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        os.environ['MT5_BARS_DIR'] = d.name
        self.addCleanup(lambda: os.environ.pop('MT5_BARS_DIR', None))
        mt5_bars.clear_caches()
        self.addCleanup(mt5_bars.clear_caches)
        write_bars(d.name, 'EURUSD', **kw)
        return mt5_bars.load_series('EURUSD')

    def times(self):
        # server 09:00 and 11:00 in summer are 06:00 and 08:00 UTC
        return (datetime(2026, 6, 15, 6, tzinfo=timezone.utc), datetime(2026, 6, 15, 8, tzinfo=timezone.utc))

    def test_long_and_short(self):
        s = self.series(spikes=SPIKES)
        entry, exit_ = self.times()
        mfe, mae = excursions.measure(s, 'LONG', 1.08, entry, exit_, 0.0001, realised=25.0)
        self.assertAlmostEqual(mfe, 40.0, places=1)
        self.assertAlmostEqual(mae, -10.0, places=1)
        mfe, mae = excursions.measure(s, 'SHORT', 1.08, entry, exit_, 0.0001, realised=-40.0)
        self.assertAlmostEqual(mfe, 10.0, places=1)
        self.assertAlmostEqual(mae, -40.0, places=1)          # bars only reach -40 here

    def test_result_is_never_better_or_worse_than_what_actually_happened(self):
        s = self.series()                                      # flat market: bars barely move
        entry, exit_ = self.times()
        mfe, mae = excursions.measure(s, 'LONG', 1.08, entry, exit_, 0.0001, realised=25.0)
        self.assertEqual(mfe, 25.0)                            # the exit fill beat the bar range
        mfe, mae = excursions.measure(s, 'LONG', 1.08, entry, exit_, 0.0001, realised=-30.0)
        self.assertEqual(mae, -30.0)

    def test_bars_that_stop_early_are_not_used(self):
        s = self.series(stop_at=datetime(2026, 6, 15, 10, 0))  # no bars for the last hour of the hold
        entry, exit_ = self.times()
        self.assertIsNone(excursions.measure(s, 'LONG', 1.08, entry, exit_, 0.0001, realised=25.0))

    def test_bars_that_start_late_are_not_used(self):
        s = self.series(start_hour=10)
        entry, exit_ = self.times()
        self.assertIsNone(excursions.measure(s, 'LONG', 1.08, entry, exit_, 0.0001, realised=25.0))


CLOSE_BY = (
    '7001,2001,2026.06.15 09:00:00,EURUSD,buy,in,1.0000,1.08000,-3.50,0.00,0.00,0.00,0,,USD,ICMarketsSC-Demo,'
    'Forex\\Majors\\EURUSD,5,0.0000100000,100000.00\n'
    '7002,2002,2026.06.15 09:20:00,EURUSD,sell,in,1.0000,1.08100,-3.50,0.00,0.00,0.00,0,,USD,ICMarketsSC-Demo,'
    'Forex\\Majors\\EURUSD,5,0.0000100000,100000.00\n'
    '7003,2002,2026.06.15 09:21:00,EURUSD,buy,out_by,1.0000,1.08000,0.00,0.00,0.00,100.00,0,#2002 by #2001,USD,'
    'ICMarketsSC-Demo,Forex\\Majors\\EURUSD,5,0.0000100000,100000.00\n'
    '7004,2001,2026.06.15 09:21:00,EURUSD,sell,out_by,1.0000,1.08100,0.00,0.00,0.00,0.00,0,#2002 by #2001,USD,'
    'ICMarketsSC-Demo,Forex\\Majors\\EURUSD,5,0.0000100000,100000.00\n'
)


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global main, TestClient
        from fastapi.testclient import TestClient
        import main  # noqa: F401

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bars = tempfile.TemporaryDirectory()
        os.environ['MT5_BARS_DIR'] = self.bars.name
        mt5_bars.clear_caches()
        database.DB_PATH = os.path.join(self.tmp.name, 'journal.db')
        self.cm = TestClient(main.app)
        self.client = self.cm.__enter__()
        self.acct = self.client.post('/api/accounts', json={
            'name': 'IC Markets Demo', 'type': 'day_trading', 'broker': 'IC Markets'}).json()

    def tearDown(self):
        self.cm.__exit__(None, None, None)
        os.environ.pop('MT5_BARS_DIR', None)
        mt5_bars.clear_caches()
        self.tmp.cleanup()
        self.bars.cleanup()

    def upload(self, content):
        return self.client.post('/api/import-csv', data={'account_id': self.acct['id'], 'broker': 'mt5'},
                                files={'file': ('deals.csv', content.encode(), 'text/csv')})

    def trade(self, position_id):
        rows = self.client.get('/api/trades', params={'account_id': self.acct['id']}).json()
        return next(r for r in rows if r['position_id'] == position_id)

    def test_import_measures_excursions_when_bars_exist(self):
        write_bars(self.bars.name, 'EURUSD', spikes=SPIKES)
        r = self.upload(FIXTURE).json()
        self.assertEqual(r['excursions']['computed'], 2)
        self.assertEqual(r['excursions']['no_bars'], 2)         # USDJPY and gold have no bars file
        self.assertEqual(r['excursions']['open'], 2)            # the US500 position and the orphan exit

        long_ = self.trade('1001')
        self.assertEqual((long_['mfe_pips'], long_['mae_pips']), (40.0, -10.0))
        self.assertEqual(long_['exit_efficiency'], 62.5)         # 25 pips captured of 40
        self.assertAlmostEqual(long_['mfe_pct'], 0.370, places=3)
        self.assertAlmostEqual(long_['mae_pct'], -0.093, places=3)

        short = self.trade('1002')                               # hedged, overlapping the long
        self.assertEqual((short['mfe_pips'], short['mae_pips']), (20.0, -30.0))
        self.assertEqual(short['exit_efficiency'], -100.0)
        self.assertIsNone(self.trade('1003')['mfe_pips'])

    def test_recalculate_after_the_bars_arrive(self):
        first = self.upload(FIXTURE).json()
        self.assertEqual(first['excursions']['computed'], 0)
        self.assertIsNone(self.trade('1001')['mfe_pips'])
        write_bars(self.bars.name, 'EURUSD', spikes=SPIKES)
        r = self.client.post('/api/mt5/recalculate-excursions', params={'account_id': self.acct['id']})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['computed'], 2)
        self.assertIn('Measured 2 trade', r.json()['message'])
        self.assertEqual(self.trade('1001')['mfe_pips'], 40.0)

    def test_close_by_legs_are_skipped_not_guessed(self):
        write_bars(self.bars.name, 'EURUSD', spikes=SPIKES)
        r = self.upload(FIXTURE + CLOSE_BY).json()
        self.assertEqual(r['excursions']['close_by'], 2)
        self.assertIsNone(self.trade('2001')['mfe_pips'])
        self.assertIsNone(self.trade('2002')['mfe_pips'])

    def test_fx_chart_uses_the_mt5_bars_and_a_full_local_day(self):
        write_bars(self.bars.name, 'EURUSD', spikes=SPIKES)
        self.upload(FIXTURE)
        r = self.client.get('/api/chart/EURUSD/2026-06-15', params={'timeframe': '5Min'}).json()
        self.assertEqual(r['source'], 'mt5')
        self.assertEqual(r['display_timezone'], 'Asia/Jakarta')
        self.assertEqual(len(r['bars']), 48)
        self.assertEqual(r['bars'][0]['t'], '2026-06-15T05:00:00Z')
        self.assertNotIn('warning', r)

        # zooming out loads earlier days; the export only covers the 15th, so the count is unchanged
        r = self.client.get('/api/chart/EURUSD/2026-06-15', params={'timeframe': '5Min', 'days_back': 3}).json()
        self.assertEqual(len(r['bars']), 48)

    def test_chart_warnings(self):
        self.upload(FIXTURE)                                     # symbols are known, no bars files yet
        r = self.client.get('/api/chart/EURUSD/2026-06-15').json()
        self.assertEqual(r['bars'], [])
        self.assertIn('ExportBarsCSV', r['warning'])

        write_bars(self.bars.name, 'EURUSD')
        r = self.client.get('/api/chart/EURUSD/2026-07-01').json()
        self.assertEqual(r['bars'], [])
        self.assertIn('do not cover', r['warning'])

    def test_stock_charts_are_untouched(self):
        from unittest import mock
        self.upload(FIXTURE)
        with mock.patch.object(main, 'ALPACA_KEY', ''):            # never reach the network in a test
            r = self.client.get('/api/chart/AAPL/2026-06-15').json()   # not an MT5 symbol
        self.assertNotIn('source', r)
        self.assertIn('APCA_API_KEY_ID', r['warning'])              # the Alpaca route answered


if __name__ == '__main__':
    unittest.main()
