"""The time-of-day report must count every entry it is given.

    cd backend && python -m pytest tests -q

The report used to floor entry times into a 09:30-16:00 grid — US cash-session
hours. An FX history stored in UTC does not live in that window: measured against
a real 1,346-trade journal, 837 entries (62%) fell outside the grid and were
dropped with `if bkey in bucket_pnl` and no message at all. The chart rendered
a confident, plausible-looking, mostly-empty picture.

Three things are pinned here:

  * coverage — every trade with an entry time lands in a bucket, and the report
    says so in `time_of_day_coverage` so a future drop cannot be silent again;
  * clock — MT5 fills are reported on the broker's server clock, not UTC, so
    the hours mean something against the chart the trade was taken on;
  * non-MT5 rows are not shifted. Only `source='mt5'` trades are converted;
    every other importer already stores wall-clock time, and converting those
    again would move them by a whole day.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

ATHENS = "Europe/Athens"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "journal.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    for name in ("database", "main"):
        sys.modules.pop(name, None)
    import database
    database.DB_PATH = str(db)
    import main
    monkeypatch.setattr(main, "_classify_dates", lambda conn, dates: 0, raising=False)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.db = str(db)
        yield c


def seed(conn, rows):
    """Insert trades shaped exactly as the MT5 importer leaves them."""
    for r in rows:
        conn.execute(
            "INSERT INTO trades (account_id, trade_group, date, ticker, "
            "instrument_type, side, gross_pnl, net_pnl, commissions, executions, source) "
            "VALUES (1,?,?,?,?,?,?,?,?,?,?)",
            (r["trade_group"], r["date"], r["ticker"],
             r.get("instrument_type", "FX"), r.get("side", "LONG"),
             r.get("gross_pnl", 0.0), r.get("net_pnl", 0.0), r.get("commissions", 0.0),
             json.dumps(r["executions"]), r.get("source", "mt5")),
        )
    conn.commit()


def edge(client):
    r = client.get("/api/edge-report")
    assert r.status_code == 200, r.text
    return r.json()


def mt5_trade(group, utc_date, utc_time, side="LONG", pnl=10.0, source="mt5",
              ticker=None):
    """One MT5-shaped trade whose entry leg carries a naive-UTC timestamp."""
    entry = "BOT" if side == "LONG" else "SOLD"
    exit_ = "SOLD" if side == "LONG" else "BOT"
    return {
        "trade_group": group,
        "date": utc_date,
        "ticker": ticker or group.split("_")[1],
        "side": side,
        "source": source,
        "net_pnl": pnl,
        "executions": [
            {"date": utc_date, "time": utc_time, "action": entry,
             "qty": 1.0, "price": 1.1, "commission": 0.0, "reported_profit": 0.0},
            {"date": utc_date, "time": "23:59:59", "action": exit_,
             "qty": 1.0, "price": 1.2, "commission": 0.0, "reported_profit": pnl},
        ],
    }


def test_every_entry_lands_in_a_bucket(client):
    conn = sqlite3.connect(client.db)
    if not conn.execute("SELECT 1 FROM accounts WHERE id=1").fetchone():
        conn.execute("INSERT INTO accounts (id, name, type) VALUES (1, 'Day', 'day_trading')")
        conn.commit()
    conn.execute("INSERT OR REPLACE INTO settings (id, key, value) VALUES "
                 "(1, 'mt5_server_timezone', ?)", (ATHENS,))
    conn.commit()

    # Entries spread across the whole day in UTC — the shape the old 09:30-16:00
    # grid could not hold. 03:00 and 20:00 UTC are both outside it.
    rows = [
        mt5_trade("MT5_EURUSD_FX_1", "2026-07-01", "03:00:00"),
        mt5_trade("MT5_EURUSD_FX_2", "2026-07-01", "20:15:00", pnl=-5.0),
        mt5_trade("MT5_EURUSD_FX_3", "2026-07-01", "12:45:00", pnl=2.0),
        mt5_trade("MT5_EURUSD_FX_4", "2026-07-01", "23:30:00", pnl=1.0),
    ]
    seed(conn, rows)

    data = edge(client)
    cov = data["time_of_day_coverage"]
    assert cov["entries"] == 4, cov
    assert cov["unplaced"] == 0, cov
    assert cov["placed"] == 4

    counted = sum(r["trade_count"] for r in data["time_of_day"])
    assert counted == 4, f"chart shows {counted} of {cov['entries']} trades"


def test_mt5_entries_are_reported_on_the_broker_clock(client):
    conn = sqlite3.connect(client.db)
    if not conn.execute("SELECT 1 FROM accounts WHERE id=1").fetchone():
        conn.execute("INSERT INTO accounts (id, name, type) VALUES (1, 'Day', 'day_trading')")
        conn.commit()
    conn.execute("INSERT OR REPLACE INTO settings (id, key, value) VALUES "
                 "(1, 'mt5_server_timezone', ?)", (ATHENS,))
    conn.commit()

    # 20:15 UTC in July is 23:15 in Athens (+3, EEST). Bucketed as UTC it would
    # sit in 20:00; on the broker's clock it belongs to 23:00.
    seed(conn, [mt5_trade("MT5_EURUSD_FX_20", "2026-07-01", "20:15:00")])

    data = edge(client)
    by_bucket = {r["bucket"]: r["trade_count"] for r in data["time_of_day"]}
    assert by_bucket["23:00"] == 1, by_bucket
    assert by_bucket["20:00"] == 0, "entry was reported on UTC, not server time"
    assert data["time_of_day_coverage"]["timezone"] == ATHENS


def test_a_winter_fill_uses_the_winter_offset(client):
    """Europe/Athens is +2 in winter and +3 in summer; the grid must follow it."""
    conn = sqlite3.connect(client.db)
    if not conn.execute("SELECT 1 FROM accounts WHERE id=1").fetchone():
        conn.execute("INSERT INTO accounts (id, name, type) VALUES (1, 'Day', 'day_trading')")
        conn.commit()
    conn.execute("INSERT OR REPLACE INTO settings (id, key, value) VALUES "
                 "(1, 'mt5_server_timezone', ?)", (ATHENS,))
    conn.commit()

    # 20:15 UTC in January is 22:15 in Athens (+2, EET), one hour behind July.
    seed(conn, [mt5_trade("MT5_EURUSD_FX_21", "2026-01-15", "20:15:00")])

    data = edge(client)
    by_bucket = {r["bucket"]: r["trade_count"] for r in data["time_of_day"]}
    assert by_bucket["22:00"] == 1, by_bucket
    assert by_bucket["23:00"] == 0, "summer offset was applied to a winter fill"


def test_non_mt5_fills_keep_their_own_clock(client):
    conn = sqlite3.connect(client.db)
    if not conn.execute("SELECT 1 FROM accounts WHERE id=1").fetchone():
        conn.execute("INSERT INTO accounts (id, name, type) VALUES (1, 'Day', 'day_trading')")
        conn.commit()
    conn.execute("INSERT OR REPLACE INTO settings (id, key, value) VALUES "
                 "(1, 'mt5_server_timezone', ?)", (ATHENS,))
    conn.commit()

    # A US-broker fill already stored as 14:30 wall clock must not be shifted
    # to 17:30 because an unrelated MT5 setting exists.
    seed(conn, [mt5_trade("2026-07-01_TSLA_STOCK_1", "2026-07-01", "14:30:00",
                          source="imported")])

    data = edge(client)
    by_bucket = {r["bucket"]: r["trade_count"] for r in data["time_of_day"]}
    assert by_bucket["14:00"] == 1, by_bucket
    assert by_bucket["17:00"] == 0, "a non-MT5 fill was converted by mistake"


def test_the_grid_is_a_full_day(client):
    """24 hourly buckets, so no hour of a 24h market is structurally excluded."""
    conn = sqlite3.connect(client.db)
    if not conn.execute("SELECT 1 FROM accounts WHERE id=1").fetchone():
        conn.execute("INSERT INTO accounts (id, name, type) VALUES (1, 'Day', 'day_trading')")
        conn.commit()
    conn.commit()
    data = edge(client)
    buckets = [r["bucket"] for r in data["time_of_day"]]
    assert len(buckets) == 24, buckets
    assert buckets[0] == "00:00" and buckets[-1] == "23:00", buckets


def test_an_unusable_timezone_degrades_instead_of_emptying_the_report(client):
    """A bad settings field must not blank every panel that depends on it."""
    conn = sqlite3.connect(client.db)
    if not conn.execute("SELECT 1 FROM accounts WHERE id=1").fetchone():
        conn.execute("INSERT INTO accounts (id, name, type) VALUES (1, 'Day', 'day_trading')")
        conn.commit()
    conn.execute("INSERT OR REPLACE INTO settings (id, key, value) VALUES "
                 "(1, 'mt5_server_timezone', 'Mars/Olympus_Mons')")
    conn.commit()

    seed(conn, [mt5_trade("MT5_EURUSD_FX_30", "2026-07-01", "13:20:00")])

    data = edge(client)          # must be a 200, not a 500
    cov = data["time_of_day_coverage"]
    assert cov["timezone_error"], cov
    assert cov["timezone"] == "stored-as-is", cov
    # Falls back to the stored clock: the entry still shows up, at its UTC hour.
    by_bucket = {r["bucket"]: r["trade_count"] for r in data["time_of_day"]}
    assert by_bucket["13:00"] == 1, by_bucket
    assert cov["placed"] == 1 and cov["unplaced"] == 0, cov
