"""Line-numbered import errors, item 4a.

    cd backend && python -m pytest tests/test_import_line_errors.py -q

The property: a row that could not be imported says so, with the line number a
user will find in their editor. Before this, the Thinkorswim section parsers and
the IBKR trades loop both `continue`d past such rows and the import still
reported `imported: N, errors: []` — a statement could lose a fill and nothing
in the app ever said so.

The other half of the property is just as important: rows skipped *by design*
must not be reported. A statement carries fees, deposits and balance movements
in Cash Balance, and the sample IBKR file carries 56 forex fills this parser
does not handle — reporting those as errors would bury the ones that matter.
"""
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from csv_parser import parse_broker_csv  # noqa: E402

CB = ("Cash Balance\n"
      "DATE,TIME,TYPE,REF #,DESCRIPTION,Misc Fees,Commissions & Fees,AMOUNT,BALANCE\n")
TH = ("Account Trade History\n"
      "Exec Time,Spread,Side,Qty,Pos Effect,Symbol,Exp,Strike,Type,Price,Net Price,Order Type\n")


def statement(cash=None, history=None):
    return "Account Statement\n\n" + (cash or "") + (history or "")


def read(content, broker="thinkorswim"):
    problems = []
    trades, skipped = parse_broker_csv(content, broker, 1, None, problems=problems)
    return trades, skipped, problems


def test_an_unreadable_thinkorswim_trade_row_names_its_line():
    """A TRD row whose description no parser understands was lost silently."""
    content = statement(CB + (
        '9/15/26,09:46:16,TRD,="1",BOT +400 TSLA @250.00,,,"-100,000.00","1"\n'
        '9/15/26,09:47:00,TRD,="2",????WATTHELL,,,,,"0.00","1"\n'
        '9/15/26,09:48:00,TRD,="3",SOLD -400 TSLA @250.50,-0.75,,"100,200.00","1"\n'))
    trades, skipped, problems = read(content)
    # The number has to be the line in the file, not the index within a section
    # that had its own headers and blank lines stripped out.
    bad_line = content.splitlines().index(
        '9/15/26,09:47:00,TRD,="2",????WATTHELL,,,,,"0.00","1"') + 1
    assert problems == [f"line {bad_line}: trade description not understood: '????WATTHELL'"]
    assert len(trades) == 1, "the readable rows still import"
    assert skipped == 0


def test_non_trade_rows_are_not_reported():
    """Cash Balance carries fees and deposits; skipping them is not losing them."""
    content = statement(CB + (
        '9/15/26,09:46:16,TRD,="1",BOT +400 TSLA @250.00,,,"-100,000.00","1"\n'
        '9/15/26,09:50:48,TRD,="2",SOLD -400 TSLA @250.50,-0.75,,"100,200.00","1"\n'
        '9/15/26,16:00:00,MISC,,"Wire transfer",,,"50,000.00","1"\n'
        '9/15/26,16:00:01,FEE,,"Market data fee",-1.50,,"-1.50","1"\n'))
    trades, skipped, problems = read(content)
    assert problems == [], problems
    assert len(trades) == 1


def test_a_trade_history_row_with_a_bad_price_names_its_line():
    content = statement(history=TH + (
        "9/15/26 09:46:16,NQ,BUY,1,BOT,TSLA,,,_FUT,abc,abc,MARKET\n"
        "9/15/26 09:50:48,NQ,SELL,1,SELLTOCLOSE,TSLA,,,_FUT,abc,abc,MARKET\n"))
    _, _, problems = read(content)
    assert len(problems) == 2, problems
    assert problems[0].startswith("line "), problems[0]
    assert "Price is not a number" in problems[0], problems[0]


def test_an_unknown_futures_root_is_reported_not_priced():
    """The refusal from item 3 has to reach the user, or the row just vanishes."""
    content = statement(history=TH +
                        "9/15/26 09:46:16,NQ,BUY,1,BOT,/ZZZ,,,_FUT,5000.00,5000.00,MARKET\n")
    trades, _, problems = read(content)
    assert trades == []
    assert len(problems) == 1
    assert "no known point value" in problems[0], problems[0]
    assert problems[0].startswith("line "), problems[0]


def test_an_ibkr_fill_with_a_bad_date_names_its_line():
    content = ("Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,"
               "Date/Time,Quantity,T. Price,Closing Price,Proceeds,Comm/Fee\n"
               "Trades,Data,Order,Stocks,USD,AAPL,\"not a date\",-100,239.78,,23978.00,-1.00\n")
    trades, _, problems = read(content, broker="ibkr")
    assert trades == []
    assert len(problems) == 1, problems
    assert problems[0].startswith("line "), problems[0]
    assert "not YYYY-MM-DD" in problems[0], problems[0]


def test_unsupported_ibkr_asset_classes_group_into_one_note():
    """56 forex rows must not become 56 identical errors."""
    rows = []
    for i in range(3):
        rows.append(f'Trades,Data,Order,Forex,USD,EUR.USD,"2026-09-15, 09:46:{i:02d}",'
                    f'-10000,1.1,,-11000.00,-2.00')
    content = ("Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,"
               "Date/Time,Quantity,T. Price,Closing Price,Proceeds,Comm/Fee\n"
               + "\n".join(rows) + "\n")
    trades, _, problems = read(content, broker="ibkr")
    assert trades == []
    assert len(problems) == 1, problems
    assert "3 rows" in problems[0] and "Forex" in problems[0], problems[0]


def test_cash_rows_are_never_reported():
    """Cash rows are balance movements, not fills this parser dropped."""
    content = ("Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity\n"
               "Cash,Header,Type,Date,Description,Amount\n"
               "Cash,Data,Deposits & Withdrawals,2026-09-15,Wire,50000.00\n"
               "Trades,Data,Order,Stocks,USD,AAPL,\"2026-09-15, 09:52:00\",-100\n")
    trades, _, problems = read(content, broker="ibkr")
    assert problems == [], problems
    assert len(trades) == 1, "the fill in the same file still imports"


def test_the_sample_statements_report_almost_nothing():
    """A clean export must not light up the error list — or nobody reads it."""
    root = BACKEND.parent / "scripts"
    for name in ("sample_import.csv", "sample_import_ibkr.csv"):
        content = (root / name).read_text(encoding="utf-8-sig")
        _, _, problems = read(content, broker="auto")
        # The IBKR sample's 56 forex fills are genuinely skipped and worth one
        # grouped note; the Thinkorswim sample drops nothing at all.
        assert len(problems) <= 1, f"{name}: {problems}"


def test_generic_and_mt5_still_refuse_whole_file_instead():
    """They raise with the line; handing them a collector must not change that."""
    generic = "date,time,symbol,side,quantity,price\n2026-09-15,09:46:16,TSLA,BUY,nope,250.00\n"
    with pytest.raises(ValueError) as exc:
        parse_broker_csv(generic, "generic", 1, None, problems=[])
    assert "line 2" in str(exc.value), str(exc.value)


def test_the_import_response_carries_line_errors(tmp_path, monkeypatch):
    """And they surface to the UI, with a count in the summary line."""
    import sqlite3
    db = tmp_path / "journal.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    for name in ("database", "main"):
        sys.modules.pop(name, None)
    import database
    database.DB_PATH = str(db)
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO accounts (id,name,type) VALUES (1,'Day','day_trading')")
        conn.commit()
        conn.close()
        content = statement(CB + (
            '9/15/26,09:46:16,TRD,="1",BOT +400 TSLA @250.00,,,"-100,000.00","1"\n'
            '9/15/26,09:47:00,TRD,="2",????WATTHELL,,,,,"0.00","1"\n'))
        r = c.post("/api/import-csv",
                   data={"account_id": "1", "broker": "thinkorswim"},
                   files={"file": ("s.csv", content.encode(), "text/csv")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["line_errors"]) == 1, body
        assert body["line_errors"][0].startswith("line "), body["line_errors"]
        assert "1 row(s) in the file were not imported" in body["message"], body["message"]
        assert body["imported"] == 1
