"""`days_forward` on the chart window.

    cd backend && python -m pytest tests -q

`/api/chart/{ticker}/{date}` used to stop at the trade date: `end` was hardcoded
to it, so no candle after the exit could ever arrive. That is why what-if's
end-of-week horizon could not be answered — not by a bad lookup, but because the
price for Friday was never fetched.

Pins:
  * the window reaches past `date` only when asked, so the chart's own request
    (`days_back` alone) is byte-for-byte what it was;
  * the *start* of the window still hangs off `date`, so widening forward does
    not silently shrink how far back the chart can look;
  * the whole response stays a bounded window even with a large forward ask;
  * the HTTP route accepts and passes the parameter through.
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def bars_in(conn, ticker, date, timeframe="1Min", days_back=1, days_forward=0):
    import main
    resp = main._local_chart_bars(conn, ticker, date, timeframe, days_back, days_forward)
    return resp["bars"] if resp else None


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


def seed_day(conn, symbol, day, price=1.10):
    """One M1 bar a minute for the whole of `day`."""
    t = datetime.strptime(day, "%Y-%m-%d")
    for i in range(1440):
        stamp = t + timedelta(minutes=i)
        conn.execute(
            "INSERT INTO bars (symbol, time, open, high, low, close, tick_volume) "
            "VALUES (?,?,?,?,?,?,?)",
            (symbol, stamp.strftime("%Y-%m-%d %H:%M:%S"),
             price, price, price, price, 1),
        )
    conn.commit()


def test_without_days_forward_the_window_stops_at_the_trade_date(conn):
    """The chart's own request must not change: it asks for days_back only."""
    for d in ("2026-01-27", "2026-01-28", "2026-01-29"):
        seed_day(conn, "EURUSD", d)
    back = bars_in(conn, "EURUSD", "2026-01-28")
    assert back[-1]["t"] == "2026-01-28T23:59:00Z"
    assert not any(b["t"] >= "2026-01-29" for b in back)


def test_days_forward_reaches_past_the_trade_date(conn):
    """End of week needs Friday when the trade closed Wednesday."""
    seed_day(conn, "EURUSD", "2026-01-28")
    seed_day(conn, "EURUSD", "2026-01-29")
    seed_day(conn, "EURUSD", "2026-01-30")
    fwd = bars_in(conn, "EURUSD", "2026-01-28", days_forward=2)
    assert fwd[-1]["t"] == "2026-01-30T23:59:00Z"
    assert any(b["t"].startswith("2026-01-30") for b in fwd)


def test_widening_forward_does_not_move_the_start(conn):
    """days_back hangs off the trade date. Folding days_forward into both would
    make `days_back=1, days_forward=5` silently reach 6 days back instead."""
    # The trade date, its own day, and the five days after it — so the backward
    # half of the window is real data the forward half must not displace.
    for i in range(6):
        seed_day(conn, "EURUSD", (datetime(2026, 1, 25) + timedelta(days=i)).strftime("%Y-%m-%d"))
    one_back = bars_in(conn, "EURUSD", "2026-01-25", days_back=1)
    wide = bars_in(conn, "EURUSD", "2026-01-25", days_back=1, days_forward=5)
    assert one_back[0]["t"] == wide[0]["t"] == "2026-01-25T00:00:00Z"
    assert wide[-1]["t"] == "2026-01-30T23:59:00Z"
    assert len(wide) > len(one_back)


def test_the_window_stays_bounded(conn):
    """A large forward ask cannot be turned into a dump of the whole history."""
    # One bar a day for 200 days past the trade date, so the cap is visible as a
    # count of days rather than hidden inside a month of M1 data.
    for i in range(200):
        day = (datetime(2026, 1, 28) + timedelta(days=i + 1)).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO bars (symbol, time, open, high, low, close, tick_volume) "
            "VALUES (?,?,?,?,?,?,?)",
            ("EURUSD", f"{day} 12:00:00", 1.1, 1.1, 1.1, 1.1, 1),
        )
    conn.execute(
        "INSERT INTO bars (symbol, time, open, high, low, close, tick_volume) "
        "VALUES (?,?,?,?,?,?,?)",
        ("EURUSD", "2026-01-28 00:00:00", 1.1, 1.1, 1.1, 1.1, 1),
    )
    conn.commit()

    capped = bars_in(conn, "EURUSD", "2026-01-28", days_back=1, days_forward=10_000)
    # The ceiling for intraday timeframes is 90 days, applied to the forward ask.
    assert capped[-1]["t"] < "2026-04-30T00:00:00Z"
    # i.e. roughly 90 of the 200 forward days came back, not all of them.
    assert len(capped) < 100
    assert len(capped) > 50


def test_a_negative_forward_is_treated_as_none(conn):
    """Query() enforces ge=0, but the helper is also called directly."""
    seed_day(conn, "EURUSD", "2026-01-28")
    seed_day(conn, "EURUSD", "2026-01-29")
    out = bars_in(conn, "EURUSD", "2026-01-28", days_forward=-3)
    assert out[-1]["t"] == "2026-01-28T23:59:00Z"


def test_the_route_accepts_the_parameter(monkeypatch, tmp_path):
    """The HTTP path, since what-if reaches it that way: a forward window must
    survive the query-string round trip, not just the direct call."""
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
    for day in ("2026-01-28", "2026-01-30"):
        stamp = datetime.strptime(f"{day} 00:00:00", "%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?)",
                     ("EURUSD", stamp.strftime("%Y-%m-%d %H:%M:%S"), 1.1, 1.1, 1.1, 1.2, 1))
    conn.commit()
    conn.close()
    monkeypatch.setattr(main, "ALPACA_KEY", "")

    from fastapi.testclient import TestClient
    with TestClient(main.app) as client:
        plain = client.get("/api/chart/EURUSD/2026-01-28", params={"timeframe": "1Min"}).json()
        assert [b["t"] for b in plain["bars"]] == ["2026-01-28T00:00:00Z"]

        wide = client.get("/api/chart/EURUSD/2026-01-28",
                          params={"timeframe": "1Min", "days_forward": 3}).json()
        assert [b["t"] for b in wide["bars"]] == [
            "2026-01-28T00:00:00Z", "2026-01-30T00:00:00Z",
        ]
        # Negative is rejected by the route rather than trusted.
        assert client.get("/api/chart/EURUSD/2026-01-28",
                          params={"days_forward": -1}).status_code == 422
