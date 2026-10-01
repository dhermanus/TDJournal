"""Reports → Timing must show the whole journal, on the broker's clock.

    cd backend && python -m pytest tests -q

`/api/reports` had the same two faults the Dashboard chart had, plus one of its
own:

  * the grid was six fixed US cash-session windows (09:30-16:00). A pre-fix
    probe measured 836/1,346 entries (62.2%) forced into a fallback window that
    did not contain the entry time;
  * times were read straight from storage (UTC for MT5) rather than the broker
    server clock the dashboard uses;
  * `net_pnl <> 0` filtered two trades out of the timing tab while the dashboard
    and the KPIs counted them.

Regression tests also pin two adjacent defects found during verification:
clock-only hold-time subtraction silently dropped three overnight positions,
and clock-only leg sorting misidentified the first fill when a position was
added to across dates. Hold durations now use stored leg dates; only their
entry-hour classification converts to server time.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

ATHENS = "Europe/Athens"
HOUR_KEYS = [f"{h:02d}:00" for h in range(24)]


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


def setup_db(client, timezone=ATHENS):
    conn = sqlite3.connect(client.db)
    if not conn.execute("SELECT 1 FROM accounts WHERE id=1").fetchone():
        conn.execute("INSERT INTO accounts (id, name, type) VALUES (1, 'Day', 'day_trading')")
    if timezone is not None:
        conn.execute("INSERT OR REPLACE INTO settings (id, key, value) VALUES "
                     "(1, 'mt5_server_timezone', ?)", (timezone,))
    else:
        conn.execute("DELETE FROM settings WHERE key = 'mt5_server_timezone'")
    conn.commit()
    return conn


def seed(conn, rows):
    """Insert trades shaped exactly as the MT5 importer leaves them."""
    for r in rows:
        conn.execute(
            "INSERT INTO trades (account_id, trade_group, date, ticker, "
            "instrument_type, side, gross_pnl, net_pnl, commissions, executions, source) "
            "VALUES (1,?,?,?,?,?,?,?,?,?,?)",
            (r["trade_group"], r["date"], r["ticker"], r.get("instrument_type", "FX"),
             r.get("side", "LONG"), r.get("gross_pnl", 0.0), r.get("net_pnl", 0.0),
             r.get("commissions", 0.0), json.dumps(r["executions"]),
             r.get("source", "mt5")),
        )
    conn.commit()


def reports(client, **params):
    r = client.get("/api/reports", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def mt5_trade(group, utc_date, utc_time, side="LONG", pnl=10.0, source="mt5",
              exit_date=None, exit_time="23:59:59", instrument_type="FX"):
    """One MT5-shaped trade; entry carries a naive-UTC timestamp."""
    entry = "BOT" if side == "LONG" else "SOLD"
    exit_ = "SOLD" if side == "LONG" else "BOT"
    return {
        "trade_group": group,
        "date": utc_date,
        "ticker": group.split("_")[1],
        "side": side,
        "source": source,
        "instrument_type": instrument_type,
        "net_pnl": pnl,
        "executions": [
            {"date": utc_date, "time": utc_time, "action": entry,
             "qty": 1.0, "price": 1.1, "commission": 0.0, "reported_profit": 0.0},
            {"date": exit_date or utc_date, "time": exit_time, "action": exit_,
             "qty": 1.0, "price": 1.2, "commission": 0.0, "reported_profit": pnl},
        ],
    }


def bucket_map(data):
    return {r["key"]: r for r in data["by_session"]}


def test_the_grid_covers_a_whole_day(client):
    """24 hourly buckets: no hour of a 24h market is structurally excluded."""
    setup_db(client)
    data = reports(client)
    assert data["has_data"] is False
    assert data["time_of_day_coverage"] == {
        "entries": 0, "placed": 0, "unplaced": 0,
        "timezone": ATHENS, "timezone_error": None,
    }


def test_an_overnight_hold_is_not_dropped(client):
    """Clock-only subtraction goes negative past midnight, and the bucket then
    returned None — silently excluding the trade from By Hold Time."""
    conn = setup_db(client)
    seed(conn, [
        # 4.3 hours of clock difference but held over three weeks: the old code
        # read -260 minutes and dropped it.
        mt5_trade("MT5_EURUSD_FX_1181579340", "2025-08-12", "13:42:24",
                  exit_date="2025-09-02", exit_time="09:22:09", pnl=-2.59),
        mt5_trade("MT5_EURUSD_FX_1258415445", "2025-10-08", "18:18:01",
                  exit_date="2025-10-09", exit_time="04:30:01", pnl=103.15),
        mt5_trade("MT5_EURUSD_FX_1377456606", "2025-12-22", "23:59:49",
                  exit_date="2025-12-23", exit_time="00:00:08", pnl=-1.17),
    ])

    data = reports(client)
    holds = data["by_hold_time"]
    assert sum(r["trades"] for r in holds) == 3, (
        "an overnight hold went negative and was dropped")
    assert {r["key"]: r["trades"] for r in holds}["2+ hrs"] == 2, holds
    assert {r["key"]: r["trades"] for r in holds}["0-5 min"] == 1, holds


def test_the_holding_window_uses_leg_dates_not_clock_order(client):
    """Entry legs are ordered by (date, time), not by clock time alone."""
    conn = setup_db(client)
    trade = mt5_trade("MT5_EURUSD_FX_500", "2025-10-07", "18:00:00", pnl=10.0)
    # A position added to on day 2 with an earlier clock time than day 1.
    trade["executions"].insert(1, {
        "date": "2025-10-08", "time": "09:00:00", "action": "BOT",
        "qty": 1.0, "price": 1.2, "commission": 0.0, "reported_profit": 0.0,
    })
    seed(conn, [trade])

    data = reports(client)
    # First entry by date is 2025-10-07 18:00 UTC → 21:00 server (July offset
    # does not apply; October is +3 in Athens).
    assert data["time_of_day_coverage"]["placed"] == 1
    assert bucket_map(data)["21:00"]["trades"] == 1, (
        "the second day's entry was treated as the first entry")


def test_an_entry_cannot_land_in_a_window_that_omits_it(client):
    """The old grid's catch-alls: before 09:30 read as 09:30, after 16:00 as 15:30."""
    conn = setup_db(client)
    seed(conn, [
        mt5_trade("MT5_EURUSD_FX_1", "2026-07-01", "04:00:00"),   # 07:00 server
        mt5_trade("MT5_EURUSD_FX_2", "2026-07-01", "18:00:00"),   # 21:00 server
        mt5_trade("MT5_EURUSD_FX_3", "2026-07-01", "11:00:00"),   # 14:00 server
    ])

    data = reports(client)
    buckets = bucket_map(data)
    assert data["trade_count"] == 3

    for r in data["by_session"]:
        lo = int(r["key"].split(":")[0])
        assert r["label"] == f"{lo:02d}:00", r
    assert buckets["07:00"]["trades"] == 1, "04:00 UTC was not reported on server time"
    assert buckets["21:00"]["trades"] == 1, "18:00 UTC was not reported on server time"
    assert buckets["14:00"]["trades"] == 1
    # The two windows the old code forced these into.
    assert "09:30-09:45" not in buckets and "15:30-16:00" not in buckets

    total = sum(r["trades"] for r in data["by_session"])
    assert total == data["trade_count"], f"{total} of {data['trade_count']} trades bucketed"


def test_coverage_says_how_many_entries_were_placed(client):
    conn = setup_db(client)
    seed(conn, [
        mt5_trade("MT5_EURUSD_FX_1", "2026-07-01", "03:00:00"),
        mt5_trade("MT5_EURUSD_FX_2", "2026-07-01", "20:15:00", pnl=-5.0),
        # No entry leg at all: counted as an entry attempt, not as placed.
        {"trade_group": "MT5_EURUSD_FX_3", "date": "2026-07-02", "ticker": "EURUSD",
         "side": "LONG", "source": "mt5", "net_pnl": 0.0,
         "executions": [{"date": "2026-07-02", "time": "12:00:00", "action": "SOLD",
                         "qty": 1.0, "price": 1.2, "commission": 0.0,
                         "reported_profit": 0.0}]},
    ])

    cov = reports(client)["time_of_day_coverage"]
    assert cov["entries"] == 2, cov
    assert cov["placed"] == 2, cov
    assert cov["unplaced"] == 0, cov
    assert cov["timezone"] == ATHENS
    assert cov["timezone_error"] is None


def test_mt5_entries_use_the_broker_clock(client):
    conn = setup_db(client)
    # 20:15 UTC in July is 23:15 in Athens (+3). On UTC it would read 20:00.
    seed(conn, [mt5_trade("MT5_EURUSD_FX_20", "2026-07-01", "20:15:00")])
    buckets = bucket_map(reports(client))
    assert buckets["23:00"]["trades"] == 1, buckets
    assert buckets["20:00"]["trades"] == 0, "reported on UTC, not server time"


def test_a_winter_fill_uses_the_winter_offset(client):
    conn = setup_db(client)
    # +2 in January versus +3 in July.
    seed(conn, [mt5_trade("MT5_EURUSD_FX_21", "2026-01-15", "20:15:00")])
    buckets = bucket_map(reports(client))
    assert buckets["22:00"]["trades"] == 1, buckets
    assert buckets["23:00"]["trades"] == 0, "summer offset applied to a winter fill"


def test_non_mt5_fills_keep_their_own_clock(client):
    conn = setup_db(client)
    # A US-broker fill stored as 14:30 wall clock must not be shifted to 17:30.
    seed(conn, [mt5_trade("2026-07-01_TSLA_STOCK_1", "2026-07-01", "14:30:00",
                          source="imported", instrument_type="STOCK")])
    buckets = bucket_map(reports(client))
    assert buckets["14:00"]["trades"] == 1, buckets
    assert buckets["17:00"]["trades"] == 0, "a non-MT5 fill was converted by mistake"


def test_the_zero_pnl_trades_are_counted(client):
    """`net_pnl <> 0` dropped trades the dashboard and KPIs both count."""
    conn = setup_db(client)
    seed(conn, [
        mt5_trade("MT5_EURUSD_FX_1", "2026-07-01", "09:00:00", pnl=12.0),
        mt5_trade("MT5_EURUSD_FX_2", "2026-07-01", "09:30:00", pnl=0.0),
        mt5_trade("MT5_EURUSD_FX_3", "2026-07-01", "10:00:00", pnl=0.0),
    ])

    data = reports(client)
    assert data["trade_count"] == 3, "scratch trades were filtered out"
    assert sum(r["trades"] for r in data["by_session"]) == 3
    assert sum(r["trades"] for r in data["by_hold_time"]) == 3


def test_a_scratch_trade_does_not_extend_the_losing_streak(client):
    """Including zeros must not quietly redefine what a losing streak means."""
    conn = setup_db(client)
    seed(conn, [
        mt5_trade("MT5_EURUSD_FX_1", "2026-07-01", "09:00:00", pnl=-10.0),
        mt5_trade("MT5_EURUSD_FX_2", "2026-07-02", "09:00:00", pnl=-10.0),
        mt5_trade("MT5_EURUSD_FX_3", "2026-07-03", "09:00:00", pnl=0.0),
        mt5_trade("MT5_EURUSD_FX_4", "2026-07-04", "09:00:00", pnl=-10.0),
        mt5_trade("MT5_EURUSD_FX_5", "2026-07-05", "09:00:00", pnl=-10.0),
    ])

    summary = reports(client)["summary"]
    assert summary["longest_loss_streak"] == 2, (
        "the scratch trade was counted as a loss, making a 2-streak look like 4")


def test_hold_time_is_still_measured_on_the_stored_clock(client):
    """Converting only the entry end to broker time would invert overnight holds."""
    conn = setup_db(client)
    # Entry 22:00 UTC, exit 06:00 UTC the next day: an 8-hour hold.
    seed(conn, [mt5_trade("MT5_EURUSD_FX_30", "2026-07-01", "22:00:00",
                          exit_date="2026-07-02", exit_time="06:00:00", pnl=5.0)])

    data = reports(client)
    holds = bucket_map({**data, "by_session": data["by_hold_time"]})
    assert holds["2+ hrs"]["trades"] == 1, (
        "hold was computed from a converted entry minus an unconverted exit")
    assert data["time_of_day_coverage"]["placed"] == 1
    assert bucket_map(data)["01:00"]["trades"] == 1, "entry not on the server clock"


def test_an_unusable_timezone_degrades_instead_of_failing(client):
    conn = setup_db(client, timezone="Mars/Olympus_Mons")
    seed(conn, [mt5_trade("MT5_EURUSD_FX_31", "2026-07-01", "13:20:00")])

    data = reports(client)          # 200, not 500
    cov = data["time_of_day_coverage"]
    assert cov["timezone_error"], cov
    assert cov["timezone"] == "stored-as-is", cov
    assert bucket_map(data)["13:00"]["trades"] == 1, "fell back to the stored clock"
    assert cov["placed"] == 1 and cov["unplaced"] == 0, cov


def test_the_report_is_the_same_size_as_the_journal(client):
    """The timing tab, the dashboard and the KPIs must agree on the denominator."""
    conn = setup_db(client)
    seed(conn, [
        mt5_trade(f"MT5_EURUSD_FX_{h:02d}", "2026-07-01", f"{h:02d}:15:00",
                  pnl=0.0 if h % 7 == 0 else float(h))
        for h in range(24)
    ])
    seed(conn, [mt5_trade("MT5_EURUSD_FX_99", "2026-07-02", "25:00:00", pnl=1.0)])
    # ^ an unusable stored hour: reported as unplaced, not silently dropped.

    data = reports(client)
    cov = data["time_of_day_coverage"]
    assert data["trade_count"] == 25
    assert cov["entries"] == 25
    assert cov["placed"] == 24 and cov["unplaced"] == 1, cov
    assert sum(r["trades"] for r in data["by_session"]) == cov["placed"]
    # All 24 hours are listed, traded-in or not.
    assert len(data["by_session"]) == 24
    assert [r["key"] for r in data["by_session"]] == HOUR_KEYS
    assert sum(1 for r in data["by_session"] if r["trades"] == 0) == 0
