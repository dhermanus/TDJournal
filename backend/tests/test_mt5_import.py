"""MT5 deal-history import: grouping, P&L, timestamps and deduplication.

    cd backend && python -m pytest tests -q

The stock pipeline cannot be reused here. Its execution fingerprint is
date|time|symbol|side|qty|price, and MT5 emits that exact tuple twice within
one second when it closes two same-sized hedges — 201 such pairs in a
2,703-deal export would be merged into one fill and lose their profit.

Three things are checked against values taken from the real IC Markets export
rather than round numbers a bug could satisfy by accident:

  * gross/commissions/net reconcile to the broker's own totals,
  * a winter deal converts on the +2 rule and a summer one on +3,
  * a repeat import skips every deal it has already seen.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import csv_parser  # noqa: E402
import main  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "mt5_deals.csv"
HEADER = "deal_ticket,position_id,time_server,time_utc,utc_offset_sec,symbol,type," \
         "entry,volume,price,commission,fee,swap,profit,magic,comment,account_currency," \
         "server,symbol_path,digits,point,contract_size"
ATHENS = "Europe/Athens"


def read():
    return FIXTURE.read_text(encoding="utf-8")


def parse(content=None, conn=None, tz=ATHENS, account_id=1):
    return csv_parser.parse_mt5_csv(content or read(), account_id, conn, tz_name=tz)


def by_position(parsed):
    """Accept either (trades, report) or a bare trade list."""
    trades = parsed[0] if isinstance(parsed, tuple) else parsed
    return {t["position_id"]: t for t in trades}


@pytest.fixture
def conn(tmp_path):
    """A schema-complete database, so dedup can be exercised for real."""
    db = sqlite3.connect(tmp_path / "mt5.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            trade_group TEXT NOT NULL,
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            instrument_type TEXT NOT NULL CHECK(instrument_type IN
                ('STOCK','OPTION','FUTURE','FX','METAL','INDEX')),
            side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
            gross_pnl REAL, net_pnl REAL, commissions REAL DEFAULT 0,
            executions TEXT NOT NULL DEFAULT '[]',
            option_expiry TEXT, option_strike REAL,
            option_type TEXT CHECK(option_type IN ('CALL','PUT',NULL)),
            source TEXT NOT NULL DEFAULT 'imported',
            imported_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(trade_group, account_id)
        );
    """)
    yield db
    db.close()


def store(trades, conn):
    for t in trades:
        conn.execute(
            "INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, "
            "side, gross_pnl, net_pnl, commissions, executions, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_group, account_id) DO UPDATE SET "
            "date=excluded.date, side=excluded.side, gross_pnl=excluded.gross_pnl, "
            "net_pnl=excluded.net_pnl, commissions=excluded.commissions, "
            "executions=excluded.executions, source=excluded.source",
            (t["account_id"], t["trade_group"], t["date"], t["ticker"],
             t["instrument_type"], t["side"], t["gross_pnl"], t["net_pnl"],
             t["commissions"], t["executions"], t["source"]),
        )
    conn.commit()


# ── Detection and prerequisites ───────────────────────────────────────────────

def test_the_header_is_recognised_without_choosing_the_broker():
    assert csv_parser.detect_broker(read()) == "mt5"


def test_importing_without_a_server_timezone_is_refused():
    """A guessed offset silently moves half the year's trades by an hour."""
    with pytest.raises(ValueError) as exc:
        parse(tz="")
    assert "timezone" in str(exc.value)
    assert "Settings" in str(exc.value)


def test_an_unknown_zone_is_refused():
    with pytest.raises(Exception):
        parse(tz="Mars/Olympus_Mons")


def test_a_file_that_is_not_an_mt5_export_says_so():
    with pytest.raises(ValueError) as exc:
        csv_parser.parse_mt5_csv("date,time,symbol,side,quantity,price\n"
                                 "2026-01-01,09:30,TSLA,BUY,10,100\n", 1, None,
                                 tz_name=ATHENS)
    assert "deal_ticket" in str(exc.value)


def test_a_missing_required_column_is_named():
    bad = HEADER.replace("position_id,", "")
    with pytest.raises(ValueError) as exc:
        csv_parser.parse_mt5_csv(bad + "\n1,2026.01.07 05:07:14,x,x,EURUSD,buy,in,1,1,0,0,0,0,0,,EUR,srv,p,5,0.00001,100000\n",
                                 1, None, tz_name=ATHENS)
    assert "position_id" in str(exc.value)


def test_a_row_that_cannot_be_read_stops_the_import_with_the_row_number():
    lines = read().strip().splitlines()
    lines[1] = lines[1].replace("1.0000", "one lot", 1)   # volume
    with pytest.raises(ValueError) as exc:
        csv_parser.parse_mt5_csv("\n".join(lines), 1, None, tz_name=ATHENS)
    assert "volume" in str(exc.value) and "row" in str(exc.value)


# ── Two exporter generations ──────────────────────────────────────────────────
# The script that ships in scripts/mt5/ExportDealsCSV.mq5 writes `time`; a later
# revision renames it `time_server` and appends `time_utc`/`utc_offset_sec`.
# Only the latter made it into the test fixture, so the shipped format had no
# coverage at all — a parser that demanded `time_server` would reject every file
# a user actually produces. The fixture is rewritten here into the shipped
# column set (the file has no quoted fields, so this is positional and safe).

def shipped_header():
    keep = ["deal_ticket", "position_id", "time", "symbol", "type", "entry",
            "volume", "price", "commission", "fee", "swap", "profit", "magic",
            "comment", "account_currency", "server", "symbol_path", "digits",
            "point", "contract_size"]
    rows = [ln.split(",") for ln in read().strip().splitlines()]
    out = [",".join(keep)]
    for r in rows[1:]:
        out.append(",".join([r[0], r[1], r[2]] + r[5:]))
    return "\n".join(out) + "\n"


def test_the_shipped_exporter_header_is_accepted():
    """ExportDealsCSV.mq5 writes `time` and no UTC columns at all."""
    assert "time_server" not in shipped_header().splitlines()[0]
    assert "time_utc" not in shipped_header().splitlines()[0]
    assert csv_parser.detect_broker(shipped_header()) == "mt5"
    _, report = csv_parser.parse_mt5_rows(shipped_header(), ATHENS)
    assert report["time_column"] == "time"


def test_the_shipped_header_converts_exactly_the_same_as_the_later_one():
    """Same deals, two column layouts, identical trades — including the winter
    hour, which is where a naive fallback to the ignored time_utc would show."""
    from_shipped = by_position(csv_parser.parse_mt5_csv(
        shipped_header(), 1, None, tz_name=ATHENS))
    from_revised = by_position(parse())
    assert set(from_shipped) == set(from_revised)
    for pid, trade in from_shipped.items():
        other = from_revised[pid]
        assert json.loads(trade["executions"]) == json.loads(other["executions"]), pid
        assert trade["date"] == other["date"]
        assert trade["net_pnl"] == pytest.approx(other["net_pnl"])
    assert json.loads(from_shipped["200"]["executions"])[0]["time"] == "03:07:14"


def test_a_file_with_only_the_shipped_columns_is_enough_to_detect_mt5():
    """Detection must not require columns the shipped script never writes."""
    header = "deal_ticket,position_id,time,symbol,type,entry,volume,price\n"
    assert csv_parser.detect_broker(header) == "mt5"


def test_a_file_with_no_time_column_at_all_is_refused():
    """`time_utc` alone is a broken export's residue; reading it would re-inherit
    the flat three-hour bug this importer exists to avoid."""
    header = HEADER.replace("time_server,", "time_utc,")
    assert csv_parser.detect_broker(header + "\n") is None
    with pytest.raises(ValueError) as exc:
        csv_parser.parse_mt5_csv(header, 1, None, tz_name=ATHENS)
    assert "deal_ticket" in str(exc.value)


# ── Server time -> UTC, per season ────────────────────────────────────────────

def test_a_winter_deal_converts_on_the_two_hour_rule():
    """05:07:14 Athens in January is 03:07:14 UTC.

    The exporter's own time_utc column says 02:07:14 because it subtracts a
    flat three hours, and the import must not inherit that.
    """
    trade = by_position(parse())["200"]
    first = json.loads(trade["executions"])[0]
    assert first["server_time"] == "2026.01.07 05:07:14"
    assert first["date"] == "2026-01-07"
    assert first["time"] == "03:07:14"
    assert first["time"] != "02:07:14"


def test_a_summer_deal_converts_on_the_three_hour_rule():
    trade = by_position(parse())["300"]
    first = json.loads(trade["executions"])[0]
    assert first["time"] == "07:00:00"    # 10:00 Athens, +3 in July
    assert trade["date"] == "2026-07-15"


def test_the_close_date_governs_the_trade_date_not_the_entry_date():
    """A position open across the winter/summer change is dated by its exit."""
    positions, _ = csv_parser.parse_mt5_rows(read(), ATHENS)
    trade = csv_parser.mt5_trade_from_position(positions[0], 1)
    assert trade["date"] == "2026-01-07"


# ── P&L: broker-reported, commission and swap as one cost ────────────────────

def test_gross_commissions_and_net_reconcile():
    """Position 200: a 0.75 partial close then a 0.25 stop-out."""
    trade = by_position(parse())["200"]
    assert trade["side"] == "LONG"
    # Reported profit on the two exits only: 7.50 + 5.00.
    assert trade["gross_pnl"] == pytest.approx(12.50)
    # 3.25 + 2.44 + 0.81 charged across the three deals.
    assert trade["commissions"] == pytest.approx(6.50)
    assert trade["net_pnl"] == pytest.approx(6.00)
    assert trade["net_pnl"] == pytest.approx(trade["gross_pnl"] - trade["commissions"])


def test_a_credited_swap_reduces_the_cost_instead_of_adding_to_it():
    """Position 400 enters with commission -0.33 and swap +0.88.

    Absolutising each column would charge 1.21 for a trade that netted a
    0.55 credit; the sign of the sum is what the broker actually applied.
    """
    trade = by_position(parse())["400"]
    assert trade["instrument_type"] == "METAL"
    assert trade["commissions"] == pytest.approx(-0.12)
    assert trade["gross_pnl"] == pytest.approx(50.00)
    assert trade["net_pnl"] == pytest.approx(50.12)


def test_a_hedge_pair_reports_the_profit_once_across_both_trades():
    """out_by closes one position with another; the leg reporting 0 is not a
    missing number, and adding the two trades must not duplicate the 23.32."""
    trades = by_position(parse())
    assert trades["300"]["gross_pnl"] == pytest.approx(23.32)
    assert trades["301"]["gross_pnl"] == pytest.approx(0.00)
    assert trades["301"]["side"] == "LONG"
    assert trades["300"]["side"] == "SHORT"
    assert sum(t["gross_pnl"] for t in trades.values()) == pytest.approx(85.82)


def test_an_open_position_has_no_realised_pnl_but_keeps_its_entry_cost():
    trade = by_position(parse())["500"]
    assert trade["gross_pnl"] == 0.0
    assert trade["net_pnl"] == pytest.approx(-0.33)
    assert trade["commissions"] == pytest.approx(0.33)


def test_instrument_type_comes_from_the_symbol_path():
    trades = by_position(parse())
    assert trades["200"]["instrument_type"] == "FX"
    assert trades["400"]["instrument_type"] == "METAL"
    assert trades["200"]["ticker"] == "EURUSD"


def test_the_stop_out_comment_is_kept_with_the_trade():
    assert by_position(parse())["200"]["comment"] == "[sl 1.10200]"


def test_balance_rows_are_counted_and_not_imported():
    trades, report = parse()
    assert report["balance_rows"] == 1
    assert all(t["position_id"] != "0" for t in trades)
    assert "9001" not in {e["deal_ticket"] for t in trades
                          for e in json.loads(t["executions"])}


def test_group_keys_are_unique_and_do_not_carry_the_date():
    """A position still open now must keep its key after it closes later, or
    the closing import leaves a stale duplicate row behind."""
    trades = [t["trade_group"] for t in parse()[0]]
    assert len(trades) == len(set(trades))
    assert "MT5_EURUSD_FX_200" in trades
    # The key must not start with a close date, or an open position would be
    # re-keyed when it closes and leave its old row behind.
    assert all(t.startswith("MT5_") for t in trades)


def test_an_out_by_hedge_counts_as_an_exit_under_the_existing_open_rule():
    """MT5's paired out_by leg has the action opposite to its own position's
    entry, so the existing BOT/SOLD balance test already closes it correctly.
    Keep that fact locked down: no MT5-only interpretation is needed here.
    """
    import main

    closed = by_position(parse()[0])
    for pid in ("300", "301"):
        trade = dict(closed[pid])
        trade["executions"] = json.loads(trade["executions"])
        assert main._is_open_position(trade) is False

    still_open = dict(closed["500"])
    still_open["executions"] = json.loads(still_open["executions"])
    assert main._is_open_position(still_open) is True

    # Ordinary broker executions without MT5's extra flags are unchanged.
    stock = {"side": "LONG", "executions": [
        {"action": "BOT", "qty": 10}, {"action": "SOLD", "qty": 10}]}
    assert main._is_open_position(stock) is False


# ── Refusals: shapes the grouping cannot represent ────────────────────────────

def test_a_position_id_shared_by_two_symbols_is_refused():
    """position_id is only unique within one symbol. Grouping across two would
    price the trade off whichever row came first and merge two instruments into
    one trade with one ticker — a wrong answer that looks plausible.
    """
    lines = read().strip().splitlines()
    header = lines[0]
    entry = next(ln for ln in lines if ",200,2026.01.07 05:07:14," in ln)
    crossed = entry.replace("EURUSD", "GBPUSD", 1)   # same position id, other symbol
    with pytest.raises(ValueError) as exc:
        csv_parser.parse_mt5_csv(header + "\n" + entry + "\n" + crossed + "\n",
                                 1, None, tz_name=ATHENS)
    assert "200" in str(exc.value) and "EURUSD" in str(exc.value) \
        and "GBPUSD" in str(exc.value)
    assert "no trades were imported" in str(exc.value)


def test_a_netting_reversal_is_refused_rather_than_guessed_at():
    """`inout` means one deal closed the old position and opened the reverse on
    the same id. Splitting that correctly needs the broker's position book, so
    the import stops instead of filing it as a single lopsided trade.
    """
    lines = read().strip().splitlines()
    header = lines[0]
    row = next(ln for ln in lines if ",500,2026.07.17 09:00:00," in ln)
    reversed_row = row.replace(",in,", ",inout,", 1)
    with pytest.raises(ValueError) as exc:
        csv_parser.parse_mt5_csv(header + "\n" + reversed_row + "\n",
                                 1, None, tz_name=ATHENS)
    assert "inout" in str(exc.value) and "netting reversal" in str(exc.value)


def test_a_daylight_saving_edge_case_is_counted_and_told_to_the_user():
    """The spring-forward gap and the autumn repeat are converted, but the
    answer is not certainly right — the note is the only place saying so."""
    lines = read().strip().splitlines()
    header = lines[0]
    entry = next(ln for ln in lines if ",500,2026.07.17 09:00:00," in ln)
    cells = entry.split(",")
    # Europe/Athens: 03:30 on 2026-03-29 does not exist, 03:30 on 2026-10-25 does
    # twice. Same row, both transitions.
    cells[2] = "2026.03.29 03:30:00"
    gap = ",".join(cells)
    cells[2] = "2026.10.25 03:30:00"
    repeat = ",".join(cells)

    _, gap_report = csv_parser.parse_mt5_rows(header + "\n" + gap + "\n", ATHENS)
    assert gap_report["dst_nonexistent"] == 1 and gap_report["dst_ambiguous"] == 0

    _, repeat_report = csv_parser.parse_mt5_rows(header + "\n" + repeat + "\n", ATHENS)
    assert repeat_report["dst_ambiguous"] == 1 and repeat_report["dst_nonexistent"] == 0

    _, clean = csv_parser.parse_mt5_rows(header + "\n" + entry + "\n", ATHENS)
    assert clean["dst_ambiguous"] == 0 and clean["dst_nonexistent"] == 0
    assert main._dst_note(clean) == ""
    assert "does not exist" in main._dst_note(gap_report) or \
        "skipped clock hour" in main._dst_note(gap_report)
    assert "Europe/Athens" in main._dst_note(repeat_report)


# ── Deduplication by deal ticket ──────────────────────────────────────────────

def test_a_repeat_import_skips_every_deal_already_stored(conn):
    trades, first = parse(conn=conn)
    store(trades, conn)
    assert first["skipped_deals"] == 0

    # Dedup reads only source='mt5' rows, so the stored trades must be tagged
    # as such or a repeat import would re-file every position.
    assert {r[0] for r in conn.execute("SELECT source FROM trades")} == {"mt5"}

    again, second = parse(conn=conn)
    assert second["skipped_deals"] == first["deals"]
    assert again == []          # every position came back fully known


def test_skips_are_scoped_to_the_account(conn):
    """Another account importing the same file must not be told it is a dupe."""
    store(parse()[0], conn)
    conn.execute("INSERT INTO trades (account_id, trade_group, date, ticker, "
                 "instrument_type, side, executions, source) VALUES (2,'x','2026-01-01',"
                 "'EURUSD','FX','LONG','[]','imported')")
    conn.commit()
    _, report = parse(conn=conn, account_id=2)
    assert report["skipped_deals"] == 0
    assert report["positions"] > 0


def test_a_position_imported_open_imports_again_once_it_closes(conn):
    """The whole point of regular exports: an open position gains an exit.

    Its entry ticket is already stored, so a naive "skip this position if I've
    seen any of its deals" rule would never let it close. The position is
    refreshed instead — nothing is skipped — and the ticket dedup stops the
    stored entry from turning into a second fill.
    """
    lines = read().strip().splitlines()
    header = lines[0]
    opening = next(ln for ln in lines if ",500,2026.07.17 09:00:00," in ln)
    opening_csv = header + "\n" + opening + "\n"
    _, opening_report = csv_parser.parse_mt5_csv(opening_csv, 1, conn, tz_name=ATHENS)
    store(csv_parser.parse_mt5_csv(opening_csv, 1, conn, tz_name=ATHENS)[0], conn)
    assert opening_report["open_positions"] == 1
    assert opening_report["skipped_deals"] == 0

    # A later export where the same position has closed.
    closing = ("2008,500,2026.07.17 12:00:00,2026-07-17T09:00:00Z,10800,EURUSD,sell,"
               "out,0.1000,1.13000,-0.33,0.00,0.00,10.00,0,,EUR,ICMarketsSC-Demo,"
               "Forex\\Majors\\EURUSD,5,0.0000100000,100000.00")
    trades, report = csv_parser.parse_mt5_csv(
        header + "\n" + opening + "\n" + closing + "\n", 1, conn, tz_name=ATHENS)
    assert report["skipped_deals"] == 0
    assert report["open_positions"] == 0
    assert [t["trade_group"] for t in trades] == ["MT5_EURUSD_FX_500"]
    assert {e["deal_ticket"] for e in json.loads(trades[0]["executions"])} == {"2007", "2008"}

    # The upsert lands on the stored row rather than adding a second one.
    store(trades, conn)
    rows = conn.execute("SELECT gross_pnl, net_pnl, date FROM trades "
                        "WHERE trade_group='MT5_EURUSD_FX_500'").fetchall()
    assert len(rows) == 1
    assert rows[0]["gross_pnl"] == pytest.approx(10.00)
    assert rows[0]["net_pnl"] == pytest.approx(9.34)   # 10.00 - entry - exit commission

    assert rows[0]["date"] == "2026-07-17"


def test_two_accounts_can_store_the_same_position(conn):
    trades = parse()[0]
    store(trades, conn)
    store([{**t, "account_id": 2} for t in trades], conn)
    n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert n == 2 * len(trades)
