"""The bar import endpoint, exercised over HTTP rather than unit-style.

    cd backend && python -m pytest tests -q

The unit tests in test_bars_import.py prove the arithmetic. This file proves the
wiring: that the endpoint stores bars under the symbol read from the filename,
recomputes only that symbol's trades, and reports what it did — the parts a
pure-function test cannot see.

The fixtures are cut down from the real ExportBarsCSV output, keeping the same
header, timestamp format and precision, so a change in the file the exporter
actually writes breaks here.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

FIXTURE_DIR = Path(__file__).parent / "fixtures"

BAR_HEADER = "time,open,high,low,close,tick_volume"
ATHENS = "Europe/Athens"


def bars_csv(rows):
    out = [BAR_HEADER]
    out += [",".join(str(v) for v in r) for r in rows]
    return "\n".join(out) + "\n"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A running app against a fresh database, the way test_reimport does it."""
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


def seed(conn):
    """One long EURUSD trade and one with no bars, both with placeholder metrics."""
    conn.execute("INSERT INTO accounts (id, name, type) VALUES (1, 'a', 'day_trading')")
    for group, ticker in (("E1", "EURUSD"), ("E2", "GBPUSD")):
        conn.execute(
            "INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, "
            "side, gross_pnl, net_pnl, commissions, executions, source, mfe_pct, mae_pct, "
            "exit_efficiency) VALUES (1,?,?,?,'FX','LONG',0,0,0,?, 'imported', 9.9,-9.9,50.0)",
            (group, "2026-01-07", ticker, json.dumps([
                {"date": "2026-01-07", "time": "03:07:57", "action": "BOT",
                 "qty": 1.0, "price": 1.10000},
                {"date": "2026-01-07", "time": "03:10:03", "action": "SOLD",
                 "qty": 1.0, "price": 1.10200},
            ])),
        )
    conn.execute(
        "INSERT INTO settings (account_id, key, value) VALUES (0, 'mt5_server_timezone', ?)",
        (ATHENS,),
    )
    conn.commit()


def set_timezone(conn, value):
    conn.execute(
        "INSERT INTO settings (account_id, key, value) VALUES (0, 'mt5_server_timezone', ?) "
        "ON CONFLICT(account_id, key) DO UPDATE SET value=excluded.value", (value,))
    conn.commit()


def post(client, content, filename="TDJournal_bars_EURUSD_M1.csv"):
    return client.post("/api/import-bars", files={"file": (filename, content.encode(), "text/csv")})


def bar_rows(client):
    conn = sqlite3.connect(client.db)
    try:
        return conn.execute("SELECT symbol, time, high, low FROM bars ORDER BY symbol, time").fetchall()
    finally:
        conn.close()


def trade_values(client, group):
    conn = sqlite3.connect(client.db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT mfe_pct, mae_pct, exit_efficiency FROM trades WHERE trade_group=?",
            (group,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def test_importing_bars_stores_them_and_remeasures_that_symbol(client):
    conn = sqlite3.connect(client.db)
    conn.row_factory = sqlite3.Row
    seed(conn)

    # Real M1 bars are stamped on the minute; a fill at 03:07:57 falls inside
    # the 03:07 bar, which is why the bar times are :00 and the fills are not.
    body = bars_csv([
        ("2026.01.07 05:07:00", 1.10000, 1.10100, 1.09900, 1.10050, 40),
        ("2026.01.07 05:08:00", 1.10050, 1.10550, 1.10000, 1.10400, 55),
        ("2026.01.07 05:10:00", 1.10400, 1.10450, 1.09450, 1.10200, 61),
    ])
    r = post(client, body)
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["symbol"] == "EURUSD"
    assert data["bars"] == 3
    assert data["stored"] == 3 and data["refreshed"] == 0
    # Server time 05:07 Athens in January -> 03:07 UTC, the fill's own minute.
    assert data["first"] == "2026-01-07 03:07:00"
    assert data["measured"] == 1
    assert "3 EURUSD M1 bar" in data["message"]

    # Only the symbol the file named was remeasured; GBPUSD kept its placeholder.
    e1 = trade_values(client, "E1")
    e2 = trade_values(client, "E2")
    assert e1["mfe_pct"] == pytest.approx(0.50, abs=1e-4)
    assert e1["mae_pct"] == pytest.approx(-0.50, abs=1e-4)
    assert e1["exit_efficiency"] == pytest.approx(36.3636, abs=1e-3)
    assert e2["mfe_pct"] == pytest.approx(9.9, abs=1e-6)   # untouched

    assert [r[0] for r in bar_rows(client)] == ["EURUSD"] * 3


def test_importing_the_same_file_again_refreshes_instead_of_duplicating(client):
    conn = sqlite3.connect(client.db)
    conn.row_factory = sqlite3.Row
    seed(conn)
    body = bars_csv([
        ("2026.01.07 05:07:00", 1.10000, 1.10100, 1.09900, 1.10050, 40),
        ("2026.01.07 05:08:00", 1.10050, 1.10550, 1.10000, 1.10400, 55),
    ])
    first = post(client, body).json()
    assert first["stored"] == 2

    # A later export of the same minutes, with one corrected bar.
    body2 = bars_csv([
        ("2026.01.07 05:07:00", 1.10000, 1.10100, 1.09900, 1.10050, 40),
        ("2026.01.07 05:08:00", 1.10050, 1.99999, 1.10000, 1.10400, 55),
    ])
    second = post(client, body2).json()
    assert second["stored"] == 0 and second["refreshed"] == 2

    rows = bar_rows(client)
    assert len(rows) == 2
    assert rows[1][2] == pytest.approx(1.99999)   # refreshed in place
    # The refreshed high raises MFE, so the recompute followed the new bar.
    assert trade_values(client, "E1")["mfe_pct"] > 0.50


def test_the_symbol_comes_from_the_filename_the_exporter_writes(client):
    conn = sqlite3.connect(client.db)
    conn.row_factory = sqlite3.Row
    seed(conn)
    body = bars_csv([("2026.01.07 05:07:00", 1.3, 1.4, 1.2, 1.35, 7)])
    r = post(client, body, filename="TDJournal_bars_GBPUSD_M1.csv")
    assert r.status_code == 200, r.text
    assert r.json()["symbol"] == "GBPUSD"
    assert {row[0] for row in bar_rows(client)} == {"GBPUSD"}
    # GBPUSD's own trade is now measured; EURUSD's is still the placeholder.
    assert trade_values(client, "E2")["mfe_pct"] != pytest.approx(9.9)
    assert trade_values(client, "E1")["mfe_pct"] == pytest.approx(9.9)


def test_a_filename_that_names_no_symbol_is_refused(client):
    """Storing bars under a guessed symbol would leave them unreachable and
    silently measure the wrong trades."""
    conn = sqlite3.connect(client.db)
    conn.row_factory = sqlite3.Row
    seed(conn)
    r = post(client, bars_csv([("2026.01.07 05:07:00", 1.1, 1.1, 1.1, 1.1, 1)]),
             filename="TDJournal_deals.csv")
    assert r.status_code == 400
    assert "symbol" in r.text
    assert bar_rows(client) == []


def test_importing_without_a_server_timezone_is_refused(client):
    conn = sqlite3.connect(client.db)
    conn.row_factory = sqlite3.Row
    seed(conn)
    set_timezone(conn, "")
    r = post(client, bars_csv([("2026.01.07 05:07:00", 1.1, 1.1, 1.1, 1.1, 1)]))
    assert r.status_code == 400
    assert "timezone" in r.text
    assert bar_rows(client) == []


def test_a_non_csv_file_is_refused(client):
    r = client.post("/api/import-bars",
                    files={"file": ("TDJournal_bars_EURUSD_M1.txt", b"x", "text/plain")})
    assert r.status_code == 400
    assert ".csv" in r.text


def test_a_file_whose_rows_cannot_be_read_imports_nothing(client):
    """Partial ingestion would leave the table holding a half of a session, and
    the recompute would then measure against data that was never there."""
    conn = sqlite3.connect(client.db)
    conn.row_factory = sqlite3.Row
    seed(conn)
    body = bars_csv([
        ("2026.01.07 05:07:00", 1.10000, 1.10100, 1.09900, 1.10050, 40),
        ("2026.01.07 05:08:00", 1.10050, "oops", 1.10000, 1.10400, 55),
    ])
    r = post(client, body)
    assert r.status_code == 400
    assert "high" in r.text and "row" in r.text
    assert bar_rows(client) == []
    assert trade_values(client, "E1")["mfe_pct"] == pytest.approx(9.9)


def test_a_trade_whose_bars_do_not_reach_it_is_cleared_and_reported(client):
    """The message is the only place the user learns which trades are still
    unmeasured — a silent zero would read as a measured result.

    The file names the right symbol but covers a different period, so the
    trade's window comes back empty. Its stale placeholder must go: a number
    measured against candles that were never there cannot stand.
    """
    conn = sqlite3.connect(client.db)
    conn.row_factory = sqlite3.Row
    seed(conn)
    # GBPUSD bars, but six months away from the trade's window.
    r = post(client, bars_csv([("2026.07.01 10:00:00", 1.3, 1.4, 1.2, 1.35, 7)]),
             filename="TDJournal_bars_GBPUSD_M1.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["measured"] == 0
    assert data["without_bars"] == 1
    assert "no bars to measure" in data["message"]
    assert trade_values(client, "E2") == {"mfe_pct": None, "mae_pct": None,
                                          "exit_efficiency": None}
    # The other symbol's trade was not in scope, so it keeps its value.
    assert trade_values(client, "E1")["mfe_pct"] == pytest.approx(9.9)


def test_importing_one_symbol_never_touches_another(client):
    """Scoping the recompute is what lets bars for one symbol arrive without
    invalidating measurements already made against another's candles."""
    conn = sqlite3.connect(client.db)
    conn.row_factory = sqlite3.Row
    seed(conn)
    r = post(client, bars_csv([("2026.01.07 05:07:00", 1.3, 1.4, 1.2, 1.35, 7)]),
             filename="TDJournal_bars_GBPUSD_M1.csv")
    assert r.status_code == 200
    assert trade_values(client, "E1")["mfe_pct"] == pytest.approx(9.9)
    assert trade_values(client, "E2")["mfe_pct"] != pytest.approx(9.9)
