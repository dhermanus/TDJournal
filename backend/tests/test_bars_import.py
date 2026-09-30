"""M1 bar import and the excursion metrics recomputed from it.

    cd backend && python -m pytest tests -q

Three things are checked that a naive implementation gets wrong:

  * bar timestamps must convert on the same DST-aware rule as deals, or the
    candles land an hour away from the fills they are supposed to measure;
  * a trade with no bars gets NULL, never 0 — zero asserts the price never
    moved, which is a claim about the market rather than about our data;
  * a position that never went into profit has no exit efficiency, and writing
    0 there would average in as a measurement and drag the winners-only figure.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import csv_parser  # noqa: E402
import excursions  # noqa: E402

ATHENS = "Europe/Athens"

BAR_HEADER = "time,open,high,low,close,tick_volume"


def bars_csv(rows, header=BAR_HEADER):
    """rows are (server_time, open, high, low, close, tick_volume)."""
    out = [header]
    for r in rows:
        out.append(",".join(str(v) for v in r))
    return "\n".join(out) + "\n"


def parse(content, tz=ATHENS):
    return csv_parser.parse_mt5_bars_csv(content, tz)


@pytest.fixture
def conn(tmp_path):
    """Schema complete enough for import and recompute, built the same way
    `database.init_db` builds it, so the test cannot pass on a shape the app
    does not actually use."""
    db = sqlite3.connect(tmp_path / "bars.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            trade_group TEXT NOT NULL,
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            instrument_type TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
            gross_pnl REAL, net_pnl REAL, commissions REAL DEFAULT 0,
            executions TEXT NOT NULL DEFAULT '[]',
            source TEXT NOT NULL DEFAULT 'imported',
            mfe_pct REAL, mae_pct REAL, exit_efficiency REAL,
            UNIQUE(trade_group, account_id)
        );
        CREATE TABLE bars (
            symbol TEXT NOT NULL, time TEXT NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            close REAL NOT NULL, tick_volume INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (symbol, time)
        );
        INSERT INTO accounts (id, name) VALUES (1, 'test');
    """)
    yield db
    db.close()


def fill(side, date, time, price, qty=1.0):
    """One execution in the shape MT5 and the generic template both produce."""
    entry = side == "LONG"
    return {
        "date": date, "time": time,
        "action": "BOT" if entry else "SOLD",
        "qty": qty, "price": price,
    }


def close(side, date, time, price, qty=1.0):
    e = fill(side, date, time, price, qty)
    e["action"] = "SOLD" if side == "LONG" else "BOT"
    return e


def add_trade(conn, ticker, side, executions, group="G1", **extra):
    conn.execute(
        "INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, "
        "side, gross_pnl, net_pnl, commissions, executions, source, mfe_pct, mae_pct, "
        "exit_efficiency) VALUES (1,?,?,?,?,?,0,0,0,?,'imported',1.0,-1.0,10.0)",
        (group, executions[0]["date"], ticker, "FX", side,
         json.dumps(executions)),
    )
    conn.commit()


def add_bars(conn, symbol, rows):
    for t, o, h, l, c in rows:
        conn.execute(
            "INSERT INTO bars (symbol, time, open, high, low, close, tick_volume) "
            "VALUES (?,?,?,?,?,?,0) ON CONFLICT(symbol, time) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close",
            (symbol, t, o, h, l, c),
        )
    conn.commit()


def read_back(conn, group="G1"):
    row = conn.execute(
        "SELECT mfe_pct, mae_pct, exit_efficiency FROM trades WHERE trade_group=?",
        (group,),
    ).fetchone()
    return None if row is None else dict(row)


# ── Parsing ────────────────────────────────────────────────────────────────────

def test_a_bar_file_is_recognised():
    assert csv_parser.parse_mt5_bars_csv is not None
    bars, report = parse(bars_csv([
        ("2026.01.07 05:07:00", 1.10000, 1.10050, 1.09950, 1.10020, 42),
    ]))
    assert report["bars"] == 1
    assert bars[0]["high"] == pytest.approx(1.10050)


def test_bar_times_convert_with_the_same_winter_rule_as_deals():
    """05:07 Athens in January is 03:07 UTC, not the 02:07 a flat +3 would give.
    Bars and fills have to share a timeline or every measurement is an hour out."""
    bars, _ = parse(bars_csv([("2026.01.07 05:07:00", 1, 1, 1, 1, 1)]))
    assert bars[0]["time"] == "2026-01-07 03:07:00"
    assert bars[0]["time"] != "2026-01-07 02:07:00"


def test_bar_times_convert_with_the_summer_rule_too():
    bars, _ = parse(bars_csv([("2026.07.15 10:00:00", 1, 1, 1, 1, 1)]))
    assert bars[0]["time"] == "2026-07-15 07:00:00"


def test_importing_without_a_server_timezone_is_refused():
    """A guessed offset would shift every candle an hour off its fills."""
    with pytest.raises(ValueError) as exc:
        parse(bars_csv([("2026.01.07 05:07:00", 1, 1, 1, 1, 1)]), tz="")
    assert "timezone" in str(exc.value)
    assert "Settings" in str(exc.value)


def test_a_file_that_is_not_a_bar_export_says_so():
    with pytest.raises(ValueError) as exc:
        parse("date,symbol,side,quantity,price\n2026-01-01,TSLA,BUY,10,100\n")
    assert "time" in str(exc.value) and "bar" in str(exc.value)


def test_a_missing_ohlc_column_is_named():
    with pytest.raises(ValueError) as exc:
        parse("time,open,high,close,tick_volume\n2026.01.07 05:07:00,1,1,1,1\n")
    assert "low" in str(exc.value)


def test_a_row_that_cannot_be_read_stops_the_import():
    with pytest.raises(ValueError) as exc:
        parse(bars_csv([
            ("2026.01.07 05:07:00", 1.1, 1.1, 1.1, 1.1, 1),
            ("2026.01.07 05:08:00", 1.1, "not-a-price", 1.1, 1.1, 1),
        ]))
    assert "high" in str(exc.value) and "row" in str(exc.value)


def test_a_header_with_no_bars_is_refused_rather_than_stored_empty():
    with pytest.raises(ValueError) as exc:
        parse(BAR_HEADER + "\n")
    assert "no bars" in str(exc.value)


def test_a_blank_tick_volume_is_zero_not_a_crash():
    bars, _ = parse(bars_csv([("2026.01.07 05:07:00", 1, 1, 1, 1, "")]))
    assert bars[0]["tick_volume"] == 0


def test_a_bar_that_cannot_happen_is_refused():
    """high < low describes a price range that never printed. Keeping it would
    make the extremes it feeds MFE and MAE meaningless."""
    with pytest.raises(ValueError) as exc:
        parse(bars_csv([("2026.01.07 05:07:00", 1.1, 1.05, 1.2, 1.1, 1)]))
    assert "high" in str(exc.value) and "row" in str(exc.value)


def test_a_low_above_the_open_is_refused():
    """The inverse shape: the low claims a price the bar never reached, which
    would invent adverse excursion out of a malformed row. low 1.25 sits above
    the open of 1.20, so the bar's real low cannot be below the open."""
    with pytest.raises(ValueError) as exc:
        parse(bars_csv([("2026.01.07 05:07:00", 1.2, 1.3, 1.25, 1.28, 1)]))
    assert "low" in str(exc.value)


def test_a_bar_off_the_minute_is_refused():
    """M1 bars are stamped on the minute. A second-level timestamp means a
    different exporter or a mangled column, and mixing the two would corrupt the
    per-minute key the upsert relies on."""
    with pytest.raises(ValueError) as exc:
        parse(bars_csv([("2026.01.07 05:07:57", 1.1, 1.2, 1.0, 1.15, 1)]))
    assert "minute" in str(exc.value)


def test_a_negative_or_fractional_tick_volume_is_refused():
    for bad in ("-5", "12.5"):
        with pytest.raises(ValueError) as exc:
            parse(bars_csv([("2026.01.07 05:07:00", 1.1, 1.2, 1.0, 1.15, bad)]))
        assert "tick_volume" in str(exc.value)


def test_the_symbol_is_read_from_the_name_the_exporter_writes():
    assert csv_parser.symbol_from_bar_filename("TDJournal_bars_EURUSD_M1.csv") == "EURUSD"
    assert csv_parser.symbol_from_bar_filename("TDJournal_bars_USDJPY_M1.csv") == "USDJPY"
    # An upload arrives with a UUID prefix; the tail is what identifies it.
    assert csv_parser.symbol_from_bar_filename(
        "a42f647e-1790684753987_TDJournal_bars_EURUSD_M1.csv") == "EURUSD"


def test_a_filename_that_names_no_symbol_is_refused():
    assert csv_parser.symbol_from_bar_filename("TDJournal_deals.csv") is None
    assert csv_parser.symbol_from_bar_filename("") is None


# ── Storage ────────────────────────────────────────────────────────────────────

def test_bars_key_on_symbol_and_time_so_a_reimport_refreshes(conn):
    """The export is regenerated constantly; without an upsert every run would
    double the table and the recompute would read each candle twice."""
    add_bars(conn, "EURUSD", [("2026-01-07 03:07:00", 1.1, 1.2, 1.0, 1.15)])
    add_bars(conn, "EURUSD", [("2026-01-07 03:07:00", 1.1, 1.3, 1.0, 1.20)])
    rows = conn.execute("SELECT * FROM bars").fetchall()
    assert len(rows) == 1
    assert rows[0]["high"] == pytest.approx(1.3)   # refreshed, not a second row


def test_the_same_minute_on_two_symbols_stays_separate(conn):
    """Market data is per symbol: EURUSD's 12:00 and GBPUSD's 12:00 are
    unrelated prices and must not collide."""
    add_bars(conn, "EURUSD", [("2026-01-07 03:07:00", 1.1, 1.2, 1.0, 1.15)])
    add_bars(conn, "GBPUSD", [("2026-01-07 03:07:00", 1.3, 1.4, 1.2, 1.35)])
    assert conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 2


# ── The measurement ────────────────────────────────────────────────────────────

def test_a_long_measures_the_high_as_mfe_and_the_low_as_mae(conn):
    """Entry 1.10000, the bar reaches 1.10550 then 1.09450, exit 1.10200.

    MFE (1.10550-1.10000)/1.10 = +0.50%, MAE (1.09450-1.10000)/1.10 = -0.50%,
    efficiency 0.181818/0.50 = 36.3636%. Every figure here is derivable by hand
    from the four candles, so a sign flip or a wrong base cannot survive.
    """
    execs = [fill("LONG", "2026-01-07", "03:07:00", 1.10000),
             close("LONG", "2026-01-07", "03:10:00", 1.10200)]
    add_trade(conn, "EURUSD", "LONG", execs)
    add_bars(conn, "EURUSD", [
        ("2026-01-07 03:07:00", 1.10000, 1.10100, 1.09900, 1.10050),
        ("2026-01-07 03:08:00", 1.10050, 1.10550, 1.10000, 1.10400),
        ("2026-01-07 03:09:00", 1.10400, 1.10450, 1.09450, 1.09500),
        ("2026-01-07 03:10:00", 1.09500, 1.10250, 1.09450, 1.10200),
    ])
    report = excursions.recompute(conn, ["EURUSD"])
    got = read_back(conn)
    assert report["measured"] == 1
    assert got["mfe_pct"] == pytest.approx(0.50, abs=1e-6)
    assert got["mae_pct"] == pytest.approx(-0.50, abs=1e-6)
    assert got["exit_efficiency"] == pytest.approx(36.3636, abs=1e-4)


def test_a_short_flips_both_directions(conn):
    """The mirror image: a short profits as price falls, so the LOW is the
    favourable extreme and the HIGH is the heat."""
    execs = [fill("SHORT", "2026-01-07", "03:07:00", 1.10000),
             close("SHORT", "2026-01-07", "03:10:00", 1.09800)]
    add_trade(conn, "EURUSD", "SHORT", execs, group="G2")
    add_bars(conn, "EURUSD", [
        ("2026-01-07 03:07:00", 1.10000, 1.10100, 1.09900, 1.10050),
        ("2026-01-07 03:08:00", 1.10050, 1.10550, 1.10000, 1.10400),
        ("2026-01-07 03:09:00", 1.10400, 1.10450, 1.09450, 1.09500),
        ("2026-01-07 03:10:00", 1.09500, 1.10250, 1.09450, 1.09800),
    ])
    excursions.recompute(conn, ["EURUSD"])
    got = read_back(conn, "G2")
    assert got["mfe_pct"] == pytest.approx(0.50, abs=1e-6)   # 1.10000 -> 1.09450
    assert got["mae_pct"] == pytest.approx(-0.50, abs=1e-6)   # 1.10000 -> 1.10550
    assert got["exit_efficiency"] == pytest.approx(36.3636, abs=1e-4)


def test_a_trade_with_no_bars_is_null_not_zero(conn):
    """Zero would mean "the price never moved" — an assertion about the market.
    NULL says only that nothing was measured, which is what a gap really is.
    Leftover values from an earlier run must not survive either."""
    execs = [fill("LONG", "2026-01-07", "03:07:00", 1.10000),
             close("LONG", "2026-01-07", "03:10:00", 1.10200)]
    add_trade(conn, "XAUUSD", "LONG", execs, group="G3")
    add_bars(conn, "EURUSD", [("2026-01-07 03:07:00", 1.1, 1.2, 1.0, 1.15)])
    report = excursions.recompute(conn, ["XAUUSD"])
    assert report["without_bars"] == 1 and report["measured"] == 0
    assert read_back(conn, "G3") == {"mfe_pct": None, "mae_pct": None,
                                     "exit_efficiency": None}


def test_a_trade_that_never_went_into_profit_has_no_efficiency(conn):
    """Buying at 1.10000 when the best the bar ever offered was 1.09900 means
    there was no favourable excursion to take a share of. Efficiency 0 would
    average in as a real measurement and pull the winners-only figure down."""
    execs = [fill("LONG", "2026-01-07", "03:07:00", 1.10000),
             close("LONG", "2026-01-07", "03:10:00", 1.09950)]
    add_trade(conn, "EURUSD", "LONG", execs, group="G4")
    add_bars(conn, "EURUSD", [
        ("2026-01-07 03:07:00", 1.10000, 1.09900, 1.09800, 1.09850),
        ("2026-01-07 03:10:00", 1.09850, 1.09980, 1.09850, 1.09950),
    ])
    excursions.recompute(conn, ["EURUSD"])
    got = read_back(conn, "G4")
    assert got["mfe_pct"] == 0.0                    # clamped, never negative
    assert got["mae_pct"] < 0
    assert got["exit_efficiency"] is None


def test_a_winner_that_never_pulled_back_has_no_heat(conn):
    """MAE is clamped at 0: a trade that went straight up took no heat, and a
    positive MAE would read as profit in a figure the UI labels loss."""
    execs = [fill("LONG", "2026-01-07", "03:07:00", 1.10000),
             close("LONG", "2026-01-07", "03:10:00", 1.10500)]
    add_trade(conn, "EURUSD", "LONG", execs, group="G5")
    add_bars(conn, "EURUSD", [
        ("2026-01-07 03:07:00", 1.10000, 1.10100, 1.10000, 1.10100),
        ("2026-01-07 03:10:00", 1.10100, 1.10550, 1.10100, 1.10500),
    ])
    excursions.recompute(conn, ["EURUSD"])
    got = read_back(conn, "G5")
    assert got["mae_pct"] == 0.0
    assert got["mfe_pct"] == pytest.approx(0.50, abs=1e-6)


def test_the_entry_bar_is_inside_the_window(conn):
    """A fill at 03:07:57 must be measured against the 03:07 bar. Bounding on
    the fill's own seconds would exclude the candle containing the entry, which
    is exactly where the entry price came from — the trade would then be
    measured over its tail alone and its MAE would be wrong."""
    execs = [fill("LONG", "2026-01-07", "03:07:57", 1.10000),
             close("LONG", "2026-01-07", "03:10:03", 1.10200)]
    add_trade(conn, "EURUSD", "LONG", execs, group="G6")
    add_bars(conn, "EURUSD", [
        # Deep low in the entry bar itself: only reachable if that bar counts.
        ("2026-01-07 03:07:00", 1.10000, 1.10100, 1.09000, 1.10050),
        ("2026-01-07 03:10:00", 1.10050, 1.10250, 1.10000, 1.10200),
    ])
    excursions.recompute(conn, ["EURUSD"])
    got = read_back(conn, "G6")
    assert got["mae_pct"] == pytest.approx(-0.9090909, abs=1e-6)


def test_a_partial_scale_in_weights_each_fill_by_its_size(conn):
    """10 @ 1.10000 then 90 @ 1.09000 is 1.09100, not the 1.09500 a plain
    average of the two prices would claim — the second fill is 9x the size."""
    execs = [fill("LONG", "2026-01-07", "03:07:00", 1.10000, qty=10),
             fill("LONG", "2026-01-07", "03:08:00", 1.09000, qty=90),
             close("LONG", "2026-01-07", "03:10:00", 1.09100, qty=100)]
    add_trade(conn, "EURUSD", "LONG", execs, group="G7")
    add_bars(conn, "EURUSD", [
        ("2026-01-07 03:07:00", 1.10000, 1.10000, 1.09000, 1.09000),
        ("2026-01-07 03:08:00", 1.09000, 1.09100, 1.08900, 1.08950),
        ("2026-01-07 03:10:00", 1.08950, 1.10000, 1.08950, 1.09100),
    ])
    excursions.recompute(conn, ["EURUSD"])
    got = read_back(conn, "G7")
    # Entry 1.09100, best 1.10000 -> +0.824931%. Exit 1.09100 -> 0% captured.
    assert got["mfe_pct"] == pytest.approx(0.824931, abs=1e-5)
    assert got["exit_efficiency"] == pytest.approx(0.0, abs=1e-6)


def test_an_open_position_is_measured_to_the_last_available_bar(conn):
    """No exit fill means the window has no natural end. Using "now" would ask
    for bars past the end of the export, so the newest stored candle bounds it.
    A bar from before the entry must not reach back into the measurement."""
    execs = [fill("LONG", "2026-01-07", "03:07:00", 1.10000)]
    add_trade(conn, "EURUSD", "LONG", execs, group="G8")
    add_bars(conn, "EURUSD", [
        # Before the entry: a spike here must not count as opportunity.
        ("2026-01-07 01:00:00", 1.10000, 5.00000, 1.09900, 1.10000),
        ("2026-01-07 03:07:00", 1.10000, 1.10100, 1.09900, 1.10000),
        # Hours after the fill, still in the hold: the position's real best.
        ("2026-01-07 09:00:00", 1.11000, 1.12000, 1.10900, 1.11500),
    ])
    excursions.recompute(conn, ["EURUSD"])
    got = read_back(conn, "G8")
    assert got["mfe_pct"] == pytest.approx(1.8181818, abs=1e-6)   # 1.10000 -> 1.12000
    assert got["mae_pct"] == pytest.approx(-0.0909090, abs=1e-6)  # -> 1.09900
    assert got["exit_efficiency"] is None   # never closed


def test_only_the_named_symbol_is_remeasured(conn):
    """Importing GBPUSD bars must not touch EURUSD trades — their stored values
    were measured against their own candles and are still correct."""
    add_trade(conn, "EURUSD", "LONG", [fill("LONG", "2026-01-07", "03:07:00", 1.1)], group="G1")
    add_trade(conn, "GBPUSD", "LONG", [fill("LONG", "2026-01-07", "03:07:00", 1.3)], group="G2")
    add_bars(conn, "EURUSD", [("2026-01-07 03:07:00", 1.1, 1.1, 1.1, 1.1)])
    excursions.recompute(conn, ["GBPUSD"])
    assert read_back(conn, "G1")["mfe_pct"] == 1.0      # untouched placeholder
    assert read_back(conn, "G2")["mfe_pct"] is None      # its own symbol, no bars


# ── Aggregation ────────────────────────────────────────────────────────────────

def test_excursion_kpis_count_every_instrument_not_just_stocks(conn):
    """The filter used to read instrument_type='STOCK', so correctly measured
    FX trades — the whole point of importing bars — never appeared in Reports."""
    import main
    for i, (ticker, instr) in enumerate([("EURUSD", "FX"), ("XAUUSD", "METAL"),
                                         ("AAPL", "STOCK")]):
        add_trade(conn, ticker, "LONG",
                  [fill("LONG", "2026-01-07", "03:07:00", 100.0)],
                  group=f"K{i}")
        conn.execute(
            "UPDATE trades SET instrument_type=?, mfe_pct=2.0, mae_pct=-1.0, "
            "exit_efficiency=50.0, net_pnl=10.0 WHERE trade_group=?",
            (instr, f"K{i}"))
    conn.commit()
    kpis = main._excursion_kpis(conn, 1)
    assert kpis["excursion_n"] == 3
    assert kpis["avg_mfe"] == pytest.approx(2.0)
