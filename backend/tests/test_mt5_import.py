"""Tests for the MetaTrader 5 importer, the FX guards and the database upgrade.

Run from the backend folder with the project's virtual environment active:

    python -m unittest discover -s tests -v

Needs only what the backend already installs (plus tzdata on Windows).
"""
import glob
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
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

FIXTURE = (BACKEND / 'tests' / 'fixtures' / 'mt5_sample.csv').read_text()


def tearDownModule():
    # Leave nothing behind in the environment: the app reads these variables when it is imported, so a
    # later test module that re-imports it (test_reimport.py does) would otherwise find a deleted folder.
    for key in ('DATABASE_PATH', 'UPLOAD_DIR'):
        if os.environ.get(key, '').startswith(_TMP.name):
            os.environ.pop(key)
    _TMP.cleanup()


def by_position(trades):
    return {t['position_id']: t for t in trades}


class ClassificationTests(unittest.TestCase):
    def test_path_decides_the_class(self):
        c = csv_parser.classify_mt5_instrument
        self.assertEqual(c('EURUSD', 'Forex\\Majors\\EURUSD'), 'FOREX')
        self.assertEqual(c('XAUUSD', 'Metals\\XAUUSD'), 'METAL')
        self.assertEqual(c('US500', 'Indices\\US500'), 'INDEX')
        self.assertEqual(c('US500', 'Stock Indices\\US500'), 'INDEX')  # not SHARE_CFD
        self.assertEqual(c('BTCUSD', 'Crypto\\BTCUSD'), 'CRYPTO')
        self.assertEqual(c('XTIUSD', 'Commodities\\Energies\\XTIUSD'), 'COMMODITY')
        self.assertEqual(c('AAPL', 'Shares\\US\\AAPL'), 'SHARE_CFD')
        self.assertEqual(c('US10Y', 'Bonds\\US10Y'), 'OTHER')

    def test_falls_back_to_the_symbol_name_without_a_path(self):
        c = csv_parser.classify_mt5_instrument
        self.assertEqual(c('GBPJPY'), 'FOREX')
        self.assertEqual(c('EURUSD.m'), 'FOREX')
        self.assertEqual(c('XAGUSD'), 'METAL')
        self.assertEqual(c('BTCUSD'), 'OTHER')

    def test_fx_suffixes_are_dropped_but_other_symbols_are_kept(self):
        n = csv_parser.normalize_mt5_symbol
        self.assertEqual(n('eurusd.m', 'FOREX'), 'EURUSD')
        self.assertEqual(n('EURUSDm', 'FOREX'), 'EURUSD')
        self.assertEqual(n('US500.cash', 'INDEX'), 'US500.CASH')


class TimeTests(unittest.TestCase):
    def test_new_york_plus_seven_to_jakarta_across_daylight_saving(self):
        f = csv_parser.mt5_server_to_display
        # winter: server is GMT+2, Jakarta is GMT+7 -> +5h
        self.assertEqual(f(datetime(2026, 1, 15, 10, 0)), datetime(2026, 1, 15, 15, 0))
        # summer: server is GMT+3 -> +4h
        self.assertEqual(f(datetime(2026, 6, 15, 10, 0)), datetime(2026, 6, 15, 14, 0))

    def test_rule_and_zone_can_be_configured(self):
        f = csv_parser.mt5_server_to_display
        os.environ['MT5_SERVER_TIME_RULE'] = 'utc+2'
        os.environ['DISPLAY_TIMEZONE'] = 'UTC'
        try:
            self.assertEqual(f(datetime(2026, 6, 15, 10, 0)), datetime(2026, 6, 15, 8, 0))
        finally:
            os.environ.pop('MT5_SERVER_TIME_RULE')
            os.environ.pop('DISPLAY_TIMEZONE')


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.trades, self.skipped = csv_parser.parse_mt5_csv(FIXTURE, account_id=1)
        self.t = by_position(self.trades)

    def test_one_trade_per_position_and_repeat_deals_are_skipped(self):
        self.assertEqual(len(self.trades), 6)
        self.assertEqual(self.skipped, 1)  # the repeated deal ticket

    def test_hedged_positions_stay_separate(self):
        # Netting by "position returns to zero" would merge these two overlapping EURUSD positions.
        long_, short = self.t['1001'], self.t['1002']
        self.assertEqual((long_['side'], short['side']), ('LONG', 'SHORT'))
        self.assertAlmostEqual(long_['net_pnl'], 243.00)     # 250 - 3.50 - 3.50
        self.assertAlmostEqual(short['gross_pnl'], -100.00)
        self.assertAlmostEqual(short['commissions'], 3.50)
        self.assertAlmostEqual(short['swap'], -0.40)
        self.assertAlmostEqual(short['net_pnl'], -103.90)    # -100 - 3.50 + (-0.40)
        self.assertEqual(long_['pips'], 25.0)
        self.assertEqual(short['pips'], -20.0)

    def test_partial_close_is_one_trade_with_weighted_exit(self):
        p = self.t['1003']
        self.assertEqual(p['ticker'], 'USDJPY')
        self.assertAlmostEqual(p['gross_pnl'], 994.48)
        self.assertAlmostEqual(p['commissions'], 14.00)
        self.assertAlmostEqual(p['net_pnl'], 980.48)
        self.assertEqual(p['pips'], 75.0)                    # JPY pair: pip = 0.01
        self.assertEqual(len(json.loads(p['executions'])), 3)

    def test_other_classes_use_points(self):
        gold = self.t['1004']
        self.assertEqual(gold['instrument_type'], 'METAL')
        self.assertEqual(gold['pips'], 450.0)                # 4.50 / point 0.01
        self.assertAlmostEqual(gold['net_pnl'], 43.60)

    def test_open_position_has_no_realised_pnl(self):
        o = self.t['1005']
        self.assertEqual(o['instrument_type'], 'INDEX')
        self.assertEqual((o['gross_pnl'], o['net_pnl']), (0.0, 0.0))
        self.assertIsNone(o['pips'])

    def test_position_whose_entry_is_before_the_export_range(self):
        p = self.t['1006']
        self.assertEqual(p['ticker'], 'EURUSD')              # ".m" suffix dropped
        self.assertEqual(p['side'], 'LONG')                  # a sell closes a long
        self.assertAlmostEqual(p['net_pnl'], 9.30)
        self.assertIsNone(p['pips'])

    def test_times_are_converted_and_dates_follow_the_exit(self):
        execs = json.loads(self.t['1001']['executions'])
        self.assertEqual(execs[0]['time'], '13:00:00')       # summer: server 09:00 -> Jakarta 13:00
        winter = json.loads(self.t['1003']['executions'])
        self.assertEqual(winter[0]['time'], '14:00:00')      # winter: server 09:00 -> Jakarta 14:00
        self.assertEqual(self.t['1001']['date'], '2026-06-15')

    def test_trade_group_is_stable_and_has_no_date(self):
        self.assertEqual(self.t['1001']['trade_group'], 'MT5_EURUSD_1001')

    def test_bad_rows_are_reported_not_skipped(self):
        broken = FIXTURE.replace('1.08250', 'abc')
        with self.assertRaises(ValueError) as cm:
            csv_parser.parse_mt5_csv(broken, 1)
        self.assertIn('price', str(cm.exception))

    def test_netting_reversals_are_refused(self):
        rows = FIXTURE.replace(',sell,out,1.0000,1.08250', ',sell,inout,1.0000,1.08250')
        with self.assertRaises(ValueError) as cm:
            csv_parser.parse_mt5_csv(rows, 1)
        self.assertIn('netting', str(cm.exception))

    def test_other_importers_are_untouched(self):
        self.assertEqual(csv_parser.detect_broker(FIXTURE), 'mt5')
        self.assertEqual(csv_parser.detect_broker('date,time,symbol,side,quantity,price\n'), 'generic')
        self.assertEqual(csv_parser.detect_broker('Statement,Header,Field Name,Field Value\n'), 'ibkr')
        generic = ('date,time,symbol,side,quantity,price\n'
                   '2026-01-05,09:31:00,AAPL,BUY,10,100\n'
                   '2026-01-05,09:45:00,AAPL,SELL,10,101\n')
        trades, _ = csv_parser.parse_generic_csv(generic, 1)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]['net_pnl'], 10.0)


class EndpointTests(unittest.TestCase):
    """Whole import through the real FastAPI app on a throw-away database."""

    @classmethod
    def setUpClass(cls):
        global main, TestClient
        from fastapi.testclient import TestClient
        import main  # noqa: F401

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.tmp.name, 'journal.db')
        self.cm = TestClient(main.app)
        self.client = self.cm.__enter__()  # runs the lifespan, which builds the schema

    def tearDown(self):
        self.cm.__exit__(None, None, None)
        self.tmp.cleanup()

    def account(self, **kw):
        body = {'name': 'IC Markets Demo', 'type': 'day_trading', 'broker': 'IC Markets'}
        body.update(kw)
        r = self.client.post('/api/accounts', json=body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def upload(self, account_id, content, broker='mt5', name='deals.csv'):
        data = content if isinstance(content, bytes) else content.encode()
        return self.client.post('/api/import-csv', data={'account_id': account_id, 'broker': broker},
                                files={'file': (name, data, 'text/csv')})

    def trades(self, account_id):
        return self.client.get('/api/trades', params={'account_id': account_id}).json()

    def test_import_then_reimport_is_idempotent(self):
        acct = self.account()
        self.assertEqual(acct['currency'], 'USD')
        first = self.upload(acct['id'], FIXTURE)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()['imported'], 6)
        rows = self.trades(acct['id'])
        self.assertEqual(len(rows), 6)
        self.assertEqual({r['instrument_type'] for r in rows}, {'FOREX', 'METAL', 'INDEX'})
        self.assertEqual(next(r for r in rows if r['position_id'] == '1001')['pips'], 25.0)

        second = self.upload(acct['id'], FIXTURE).json()
        self.assertEqual(second['imported'], 0)
        self.assertEqual(second['skipped'], 12)              # 11 stored deals + the repeated ticket
        self.assertEqual(len(self.trades(acct['id'])), 6)

    def test_an_open_position_that_later_closes_updates_in_place(self):
        acct = self.account()
        self.upload(acct['id'], FIXTURE)
        closing = ('5031,1005,2026.06.16 15:00:00,US500,buy,out,1.0000,5490.0,0.00,0.00,-1.20,10.00,0,,'
                   'USD,ICMarketsSC-Demo,Indices\\US500,1,0.1000000000,1.00\n')
        again = self.upload(acct['id'], FIXTURE + closing).json()
        self.assertEqual(again['imported'], 1)
        rows = self.trades(acct['id'])
        self.assertEqual(len(rows), 6)                       # same trade_group, not a new row
        us500 = next(r for r in rows if r['position_id'] == '1005')
        self.assertAlmostEqual(us500['net_pnl'], 8.80)       # 10.00 profit - 1.20 swap
        self.assertEqual(us500['pips'], 100.0)               # 10 index points / 0.1 point size

    def test_utf16_upload_and_auto_detection(self):
        acct = self.account()
        r = self.upload(acct['id'], FIXTURE.encode('utf-16'), broker='auto')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['imported'], 6)

    def test_symbol_specs_are_saved(self):
        acct = self.account()
        self.upload(acct['id'], FIXTURE)
        conn = sqlite3.connect(database.DB_PATH)
        row = conn.execute("SELECT instrument_type, digits, contract_size, category_path FROM symbol_specs "
                           "WHERE account_id=? AND symbol='XAUUSD'", (acct['id'],)).fetchone()
        conn.close()
        self.assertEqual(row, ('METAL', 2, 100.0, 'Metals\\XAUUSD'))

    def test_empty_account_adopts_the_file_currency(self):
        acct = self.account()
        eur = FIXTURE.replace(',USD,ICMarketsSC-Demo', ',EUR,ICMarketsSC-Demo')
        self.assertEqual(self.upload(acct['id'], eur).status_code, 200)
        accounts = {a['id']: a for a in self.client.get('/api/accounts').json()}
        self.assertEqual(accounts[acct['id']]['currency'], 'EUR')

    def test_currency_mismatch_on_an_account_with_trades_is_refused(self):
        acct = self.account()
        stock = {'account_id': acct['id'], 'date': '2026-06-01', 'ticker': 'AAPL', 'side': 'LONG',
                 'entry_price': 100, 'exit_price': 101, 'quantity': 10}
        self.assertEqual(self.client.post('/api/trades', json=stock).status_code, 201)
        eur = FIXTURE.replace(',USD,ICMarketsSC-Demo', ',EUR,ICMarketsSC-Demo')
        r = self.upload(acct['id'], eur)
        self.assertEqual(r.status_code, 400)
        self.assertIn('separate account', r.json()['error'])
        self.assertEqual(len(self.trades(acct['id'])), 1)    # nothing was imported

    def test_fx_trades_cannot_be_entered_or_edited_by_hand(self):
        acct = self.account()
        manual = {'account_id': acct['id'], 'date': '2026-06-01', 'ticker': 'EURUSD', 'side': 'LONG',
                  'instrument_type': 'FOREX', 'entry_price': 1.08, 'exit_price': 1.09, 'quantity': 1}
        r = self.client.post('/api/trades', json=manual)
        self.assertEqual(r.status_code, 400)
        self.assertIn('MT5 export', r.json()['error'])

        self.upload(acct['id'], FIXTURE)
        trade = self.trades(acct['id'])[0]
        r = self.client.post(f"/api/trades/{trade['id']}/executions",
                             json={'action': 'BOT', 'qty': 1, 'price': 1.0})
        self.assertEqual(r.status_code, 400)
        self.assertIn('re-import', r.json()['error'])

    def test_currency_validation(self):
        r = self.client.post('/api/accounts', json={'name': 'x', 'type': 'day_trading', 'currency': 'DOLLARS'})
        self.assertEqual(r.status_code, 400)


OLD_TRADES_DDL = """
    CREATE TABLE trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        trade_group TEXT NOT NULL,
        date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        instrument_type TEXT NOT NULL CHECK(instrument_type IN ('STOCK','OPTION','FUTURE')),
        side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
        gross_pnl REAL,
        net_pnl REAL,
        commissions REAL DEFAULT 0,
        executions TEXT NOT NULL DEFAULT '[]',
        option_expiry TEXT,
        option_strike REAL,
        option_type TEXT CHECK(option_type IN ('CALL','PUT',NULL)),
        source TEXT NOT NULL DEFAULT 'imported',
        imported_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(trade_group, account_id)
    )
"""


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'old.db')
        database.DB_PATH = self.path
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('day_trading','swing_trading','investment')),
                color TEXT NOT NULL DEFAULT '#6366f1', broker TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
        """ + OLD_TRADES_DDL + """;
            CREATE INDEX idx_trades_account_date ON trades(account_id, date);
            CREATE INDEX idx_trades_group ON trades(trade_group);
            INSERT INTO accounts (name, type, broker) VALUES ('Old', 'day_trading', 'Thinkorswim');
            INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, side,
                                gross_pnl, net_pnl, commissions, executions)
            VALUES (1, '9/10/26_META_STOCK_1', '2026-09-10', 'META', 'STOCK', 'LONG', 50, 48.5, 1.5, '[]');
        """)
        # a column the app added later, filled on the existing row
        conn.execute("ALTER TABLE trades ADD COLUMN setup TEXT")
        conn.execute("UPDATE trades SET setup='VWAP Reclaim'")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_existing_database_is_upgraded_once_and_keeps_its_data(self):
        database.init_db()

        backups = glob.glob(self.path + '.pre-fx-*.bak')
        self.assertEqual(len(backups), 1)

        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM trades").fetchone()
        self.assertEqual((row['ticker'], row['net_pnl'], row['setup']), ('META', 48.5, 'VWAP Reclaim'))
        self.assertEqual(row['swap'], 0)
        self.assertEqual(conn.execute("SELECT currency FROM accounts").fetchone()[0], 'USD')

        conn.execute("INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, side) "
                     "VALUES (1, 'MT5_EURUSD_1', '2026-09-10', 'EURUSD', 'FOREX', 'LONG')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, side) "
                         "VALUES (1, 'x', '2026-09-10', 'X', 'NOPE', 'LONG')")
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn('idx_trades_account_date', names)
        conn.close()

        database.init_db()                                   # second start: nothing to do
        self.assertEqual(len(glob.glob(self.path + '.pre-fx-*.bak')), 1)

    def test_fresh_install_needs_no_rebuild(self):
        fresh = os.path.join(self.tmp.name, 'fresh.db')
        database.DB_PATH = fresh
        database.init_db()
        self.assertEqual(glob.glob(fresh + '.pre-fx-*.bak'), [])
        conn = sqlite3.connect(fresh)
        self.assertIn("'FOREX'", conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='trades'").fetchone()[0])
        conn.close()


if __name__ == '__main__':
    unittest.main()
