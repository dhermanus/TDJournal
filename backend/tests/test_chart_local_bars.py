"""The chart endpoint's local-bar branch.

    cd backend && python -m pytest tests -q

`/api/chart/{ticker}/{date}` used to be Alpaca-only, so an imported MT5 trade
had no candles at all — Alpaca does not carry FX, and without a key the endpoint
returned an empty list. This file pins the behaviour that lets imported M1 bars
drive the chart while leaving the remote path for symbols with no local history.

What is checked:
  * a symbol with local bars is served locally, with no key needed;
  * a symbol without them still reaches the remote path (empty + the key warning);
  * the window is bounded by days_back rather than returning the whole history;
  * higher timeframes aggregate from M1 in UTC buckets, open first / close last;
  * absent minutes produce no candle — the exporter's real gaps must show as
    gaps, not as a zero-price bar nobody traded.
"""
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timedelta

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def local_rows(conn, ticker, date, timeframe="1Min", days_back=1):
    """Call the helper directly so a test failure names the data layer."""
    import main
    return main._local_chart_bars(conn, ticker, date, timeframe, days_back)


@pytest.fixture
def conn(tmp_path):
    db = sqlite3.connect(tmp_path / "chart.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE bars (
            symbol TEXT NOT NULL, time TEXT NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            close REAL NOT NULL, tick_volume INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (symbol, time)
        );
    """)
    yield db
    db.close()


def seed(conn, symbol, start, count, base=1.10000, gap_at=None):
    """A run of M1 bars one minute apart, optionally skipping `gap_at`."""
    t = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    for i in range(count):
        stamp = t + timedelta(minutes=i)
        if gap_at and i == gap_at:
            continue                      # the exporter's missing minute
        price = base + i * 0.0001
        conn.execute(
            "INSERT INTO bars (symbol, time, open, high, low, close, tick_volume) "
            "VALUES (?,?,?,?,?,?,?)",
            (symbol, stamp.strftime("%Y-%m-%d %H:%M:%S"),
             price, price + 0.0002, price - 0.0002, price + 0.0001, 10 + i),
        )
    conn.commit()


def test_a_symbol_with_local_bars_is_served_locally(conn):
    """No API key should be required to chart a trade from the user's own export."""
    seed(conn, "EURUSD", "2026-01-07 00:00:00", 5)
    resp = local_rows(conn, "EURUSD", "2026-01-07")
    assert resp is not None
    assert resp["source"] == "local"
    # The axis is only honestly UTC if the feed says so — the UI labels the
    # chart from this field, and the remote feed's axis reads ET instead.
    assert resp["time_basis"] == "UTC"
    assert resp["warning"] is None
    assert len(resp["bars"]) == 5
    # Timestamps carry the UTC suffix the chart's ISO parser expects, with no
    # offset correction left for the browser to guess at.
    assert resp["bars"][0]["t"] == "2026-01-07T00:00:00Z"
    assert resp["bars"][0]["o"] == pytest.approx(1.10000)


def test_a_symbol_without_local_bars_falls_through(conn):
    """Returns None so the remote provider still gets its turn — otherwise
    every US equity chart would break the moment this branch landed."""
    seed(conn, "EURUSD", "2026-01-07 00:00:00", 5)
    assert local_rows(conn, "AAPL", "2026-01-07") is None


def test_symbol_matching_ignores_case(conn):
    """The exporter writes the symbol as the broker displays it; the chart is
    called with the trade's ticker. They must meet without an exact-case join."""
    seed(conn, "EURUSD", "2026-01-07 00:00:00", 3)
    assert local_rows(conn, "eurusd", "2026-01-07")["bars"]


def test_an_empty_window_is_empty_not_a_remote_fallback(conn):
    """A window with no bars for a symbol we do have local data for is an empty
    chart, not a silent switch to a different price source."""
    seed(conn, "EURUSD", "2026-01-07 00:00:00", 3)
    resp = local_rows(conn, "EURUSD", "2026-06-01")
    assert resp is not None
    assert resp["bars"] == []
    assert resp["warning"]


def test_the_window_is_bounded_by_days_back(conn):
    """74k EURUSD bars in one response would lock the chart up; days_back is
    what lets the UI widen the window on zoom-out without shipping everything."""
    seed(conn, "EURUSD", "2026-01-01 00:00:00", 1440)     # a full day, per minute
    seed(conn, "EURUSD", "2026-01-02 00:00:00", 1440)     # and the next

    one_day = local_rows(conn, "EURUSD", "2026-01-02", "1Min", days_back=1)
    two_days = local_rows(conn, "EURUSD", "2026-01-02", "1Min", days_back=2)
    assert len(one_day["bars"]) == 1440
    assert len(two_days["bars"]) == 2880
    assert one_day["bars"][0]["t"] >= "2026-01-02T00:00:00Z"


def test_the_window_reaches_past_the_trade_date(conn):
    """days_back looks *backwards* from the trade date, so a chart opened on the
    close date still includes the day the position was opened."""
    seed(conn, "EURUSD", "2026-01-07 00:00:00", 1440)
    seed(conn, "EURUSD", "2026-01-08 00:00:00", 1440)
    resp = local_rows(conn, "EURUSD", "2026-01-08", "1Min", days_back=2)
    assert resp["bars"][0]["t"] == "2026-01-07T00:00:00Z"


def test_five_minute_bars_aggregate_from_m1(conn):
    """Open of the first M1, high/low over the bucket, close of the last — the
    same convention any candle builder uses, or the shape lies."""
    seed(conn, "EURUSD", "2026-01-07 00:00:00", 15)   # three 5-minute buckets
    resp = local_rows(conn, "EURUSD", "2026-01-07", "5Min")
    assert len(resp["bars"]) == 3
    first = resp["bars"][0]
    assert first["t"] == "2026-01-07T00:00:00Z"
    # base 1.10000, +0.0001 per minute; the bucket's last M1 is minute 4, whose
    # close is (1.10000 + 4*0.0001) + 0.0001.
    assert first["o"] == pytest.approx(1.10000)
    assert first["c"] == pytest.approx(1.10050)
    assert first["h"] == pytest.approx(1.1004 + 0.0002)
    assert first["l"] == pytest.approx(1.10000 - 0.0002)
    assert first["v"] == sum(10 + i for i in range(5))   # volume accumulates


def test_hourly_bars_aggregate_too(conn):
    seed(conn, "EURUSD", "2026-01-07 00:00:00", 180)    # three hours
    resp = local_rows(conn, "EURUSD", "2026-01-07", "1Hour")
    assert len(resp["bars"]) == 3
    assert [b["t"] for b in resp["bars"]] == [
        "2026-01-07T00:00:00Z", "2026-01-07T01:00:00Z", "2026-01-07T02:00:00Z",
    ]


def test_daily_buckets_group_whole_utc_days(conn):
    """Buckets are UTC because the stored times are UTC. Grouping on local time
    would put a midnight-boundary bar in the wrong day."""
    seed(conn, "EURUSD", "2026-01-07 00:00:00", 1440)
    seed(conn, "EURUSD", "2026-01-08 00:00:00", 1440)
    resp = local_rows(conn, "EURUSD", "2026-01-08", "1Day", days_back=2)
    assert [b["t"] for b in resp["bars"]] == [
        "2026-01-07T00:00:00Z", "2026-01-08T00:00:00Z",
    ]


def test_a_missing_minute_produces_no_candle(conn):
    """The exporter skips `23:59` every day and drops whole weekend windows.
    Synthesizing a bar there would draw a trade nobody made — the chart has to
    show the hole."""
    seed(conn, "EURUSD", "2026-01-07 00:00:00", 10, gap_at=5)
    resp = local_rows(conn, "EURUSD", "2026-01-07", "1Min")
    times = [b["t"] for b in resp["bars"]]
    assert len(times) == 9
    assert "2026-01-07T00:05:00Z" not in times
    # No zero-priced stand-in for the gap either.
    assert all(b["o"] > 0 for b in resp["bars"])


def test_the_window_never_serves_the_entire_history(conn):
    """Hard cap: even with a large days_back the response stays a window."""
    seed(conn, "EURUSD", "2026-01-01 00:00:00", 1440 * 5)
    resp = local_rows(conn, "EURUSD", "2026-01-05", "1Min", days_back=1)
    assert len(resp["bars"]) == 1440


def test_the_endpoint_returns_local_bars_without_any_key(monkeypatch, tmp_path):
    """The full HTTP path: with no Alpaca key configured, a local symbol still
    charts. The remote branch would have returned a warning instead."""
    db = tmp_path / "chart.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    for name in ("database", "main"):
        sys.modules.pop(name, None)
    import database
    database.DB_PATH = str(db)
    import main
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE bars (symbol TEXT NOT NULL, time TEXT NOT NULL,
        open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
        close REAL NOT NULL, tick_volume INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (symbol, time))
    """)
    conn.execute(
        "INSERT INTO bars VALUES ('EURUSD','2026-01-07 00:00:00',"
        "1.1,1.2,1.0,1.15,42)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(main, "ALPACA_KEY", "")

    from fastapi.testclient import TestClient
    with TestClient(main.app) as client:
        r = client.get("/api/chart/EURUSD/2026-01-07", params={"timeframe": "1Min"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["source"] == "local"
        assert data["bars"][0]["o"] == pytest.approx(1.1)

        # And a symbol with no local history still behaves as it did before:
        # no key, so a warning rather than a hard failure.
        r2 = client.get("/api/chart/AAPL/2026-01-07")
        assert r2.status_code == 200
        assert r2.json()["bars"] == []
        assert "APCA_API_KEY_ID" in r2.json()["warning"]
