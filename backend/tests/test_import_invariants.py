"""Import invariants, item 3 — proven first, written as red tests.

    cd backend && python -m pytest tests/test_import_invariants.py -q

Item 3 asks for duplicate detection that survives two files, positions that stay
one trade across statement boundaries, "position returns to zero at close" and
"trade P&L equals the sum of its own fills", timezone consistency, and the
instrument classes where the math differs.

The first group reproduces two defects documented in docs/dormant-defects.md by
running them, and one the doc does not carry. None can fire on this journal —
every one of its 1,346 trades is `source='mt5'`, `instrument_type='FX'` — but
they bite the first time a US-broker statement is imported, which is what these
tests do. Expected figures are worked out by hand from the rows, not read back
from the parser.
"""
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

CB = ("Cash Balance\n"
      "DATE,TIME,TYPE,REF #,DESCRIPTION,Misc Fees,Commissions & Fees,AMOUNT,BALANCE\n")
TH = ("Account Trade History\n"
      "Exec Time,Spread,Side,Qty,Pos Effect,Symbol,Exp,Strike,Type,Price,Net Price,Order Type\n")


def statement(cash=None, history=None):
    return "Account Statement\n\n" + (cash or "") + (history or "")


def fills_of(trades):
    out = []
    for t in trades:
        e = t["executions"]
        out += json.loads(e) if isinstance(e, str) else e
    return out


def flat(ticks):
    """qty bought / qty sold across a list of executions."""
    return (sum(e["qty"] for e in ticks if e["action"] == "BOT"),
            sum(e["qty"] for e in ticks if e["action"] == "SOLD"))


@pytest.fixture
def client(tmp_path, monkeypatch):
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
        c.db = str(db)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO accounts (id,name,type) VALUES (1,'Day','day_trading')")
        conn.commit()
        conn.close()
        yield c


def import_csv(client, content, broker="thinkorswim"):
    r = client.post("/api/import-csv", data={"account_id": "1", "broker": broker},
                    files={"file": ("s.csv", content.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def stored(client):
    import sqlite3
    conn = sqlite3.connect(client.db)
    rows = conn.execute("SELECT trade_group, executions, net_pnl, instrument_type "
                        "FROM trades WHERE account_id=1").fetchall()
    conn.close()
    return [{"trade_group": g, "executions": json.loads(e), "net_pnl": p,
             "instrument_type": i} for g, e, p, i in rows]


# ── 1. an identical fill in a later statement is a different fill ───────────

def test_a_repeated_fill_does_not_leave_the_position_unbalanced(client):
    """`execution_fingerprint` has no multiplicity and the lookup is a set.

    Buy 80, then a statement carrying that same row again plus a sell of 120.
    Correct: 160 bought, 120 sold, 40 still open, no realized P&L. Dropping the
    repeated buy — which is what a set match does — books bot=80 against sold=120
    and the position cannot ever be reconciled.
    """
    import_csv(client, statement(CB + '9/15/26,09:46:16,TRD,="1",BOT +80 XYZ @100.00,,,,"1"\n'))
    result = import_csv(client, statement(CB + (
        '9/15/26,09:46:16,TRD,="1",BOT +80 XYZ @100.00,,,,"1"\n'      # same fingerprint as the stored fill
        '9/15/26,09:46:16,TRD,="1a",BOT +80 XYZ @100.00,,,,"1"\n'     # ...and the same again: a second real fill
        '9/15/26,10:00:00,TRD,="2",SOLD -120 XYZ @105.00,-1.00,,"12,600.00","1"\n')))
    # This later file carries the same fingerprint twice while the database
    # already holds one occurrence: exactly one of them is a duplicate. A set
    # cannot tell "matches a stored fill" from "matches twice", so it drops both
    # and leaves 80 bought against 120 sold.
    assert result["skipped"] == 1, result
    bot, sold = flat(fills_of(stored(client)))
    assert (bot, sold) == (160, 120), f"bot={bot} sold={sold}"
    assert sum(t["net_pnl"] for t in stored(client)) == 0, "open positions book no P&L"


def test_reimporting_the_very_same_statement_is_a_no_op(client):
    """The legitimate case dedup exists for: a file imported twice changes nothing."""
    rows = ('9/15/26,09:46:16,TRD,="1",BOT +400 TSLA @250.00,,,"-100,000.00","1"\n'
            '9/15/26,09:50:48,TRD,="2",SOLD -400 TSLA @250.50,-0.75,,"100,200.00","1"\n')
    import_csv(client, statement(CB + rows))
    before = stored(client)
    assert import_csv(client, statement(CB + rows))["imported"] == 0
    assert stored(client) == before
    bot, sold = flat(fills_of(before))
    assert (bot, sold) == (400, 400)
    assert before[0]["net_pnl"] == pytest.approx(199.25)


def test_a_position_opened_in_one_statement_and_closed_in_the_next_is_one_trade(client):
    import_csv(client, statement(CB + '9/15/26,09:46:16,TRD,="1",BOT +400 TSLA @250.00,,,"-100,000.00","1"\n'))
    import_csv(client, statement(CB + (
        '9/15/26,09:46:16,TRD,="1",BOT +400 TSLA @250.00,,,"-100,000.00","1"\n'
        '9/15/26,09:50:48,TRD,="2",SOLD -400 TSLA @250.50,-0.75,,"100,200.00","1"\n')))
    trades = stored(client)
    assert len(trades) == 1, [t["trade_group"] for t in trades]
    assert flat(fills_of(trades)) == (400, 400)
    assert trades[0]["net_pnl"] == pytest.approx(199.25)


# ── 2. a futures row must not be priced as one share ───────────────────────

def test_a_futures_row_in_trade_history_is_not_priced_as_stock(client):
    """`else: instrument_type='STOCK'; multiplier=1` in the Trade History parser.

    One /ES contract, 5 index points. The E-mini point value is 50
    (FUTURES_MULTIPLIERS), so the trip is worth 250. The stock branch books 5 —
    50x too small — and names the trade `..._/ES_STOCK_1`.
    """
    from csv_parser import parse_broker_csv
    content = statement(history=TH + (
        "9/15/26 09:46:16,NQ,BUY,1,BOT,/ES,,,_FUT,5000.00,5000.00,MARKET\n"
        "9/15/26 09:50:48,NQ,SELL,1,SELLTOCLOSE,/ES,,,_FUT,5005.00,5005.00,MARKET\n"))
    trades, _ = parse_broker_csv(content, "thinkorswim", account_id=1, conn=None)
    assert len(trades) == 1, [t["trade_group"] for t in trades]
    trade = trades[0]
    assert trade["instrument_type"] == "FUTURE", trade["trade_group"]
    assert trade["gross_pnl"] == pytest.approx(250.0), trade["gross_pnl"]


# ── 3. a futures description in Cash Balance must not vanish ───────────────

def test_a_futures_fill_in_cash_balance_is_parsed_not_skipped(client):
    """`parse_cash_description` tried option, then stock — never futures.

    An unknown description hits `continue`, so the fill disappears with no count
    and no error. Both legs of one round trip fail to parse and the endpoint
    still answers `imported: 0, errors: []` — a successful import of nothing.
    The AMOUNT column here is broker cash truth (already multiplied), so the
    figures below are what a real /ES trip moves: 5 index points x $50 = $250.
    """
    from csv_parser import parse_broker_csv
    content = statement(CB + (
        '9/15/26,09:46:16,TRD,="1",BOT +1 /ES @5000.00,,,"-250,000.00","1"\n'
        '9/15/26,09:50:48,TRD,="2",SOLD -1 /ES @5005.00,-1.00,,"250,250.00","1"\n'))
    trades, _ = parse_broker_csv(content, "thinkorswim", account_id=1, conn=None)
    assert len(trades) == 1, f"both futures fills were dropped: {len(trades)} trades"
    assert flat(fills_of(trades)) == (1, 1)
    assert trades[0]["instrument_type"] == "FUTURE"
    assert trades[0]["gross_pnl"] == pytest.approx(250.0)


# ── 4. the two invariants over generated fills ─────────────────────────────

def _generated(seed=7, count=30):
    import random
    rng = random.Random(seed)

    def ticker(i):
        # Letters only: the stock description parser accepts [A-Z]+ and a
        # digit drops the row silently (pinned separately below).
        n, out = i, ""
        while True:
            out = chr(ord("A") + n % 26) + out
            n //= 26
            if n == 0:
                break
        return "STK" + out

    rows, expected = [], []
    for i in range(count):
        qty = rng.choice([10, 25, 40, 100])
        p1 = round(rng.uniform(20, 300), 2)
        p2 = round(p1 + rng.uniform(-3, 3), 2)
        day = rng.randint(1, 4)
        t1 = f"09:{rng.randint(10, 59):02d}:00"
        t2 = f"10:{rng.randint(10, 59):02d}:00"
        sym = ticker(i)
        buy_amount = round(-qty * p1, 2)
        sell_amount = round(qty * p2, 2)
        rows.append(f'9/{day:02d}/26,{t1},TRD,="a{i}",BOT +{qty} {sym} @{p1},,,{buy_amount},"1"\n')
        rows.append(f'9/{day:02d}/26,{t2},TRD,="b{i}",SOLD -{qty} {sym} @{p2},,,{sell_amount},"1"\n')
        expected.append((qty, p1, p2))
    return rows, expected


def test_generated_round_trips_close_flat(client):
    """Position returns to zero: total bought equals total sold."""
    rows, _ = _generated()
    result = import_csv(client, statement(CB + "".join(rows)))
    ticks = fills_of(stored(client))
    assert result["skipped"] == 0
    assert len(ticks) == len(rows), f"{len(ticks)} of {len(rows)} fills survived"
    assert flat(ticks)[0] == flat(ticks)[1], "the position did not return to zero"


def test_each_generated_trade_equals_the_sum_of_its_own_fills(client):
    """Trade P&L equals net cash flow of that trade's fills — by hand, per row."""
    rows, expected = _generated(seed=11, count=20)
    import_csv(client, statement(CB + "".join(rows)))
    trades = stored(client)
    assert len(trades) == len(expected), "one trade per round trip"
    by_group = {t["trade_group"]: t for t in trades}
    for qty, p1, p2 in expected:
        # the statement's own signed AMOUNT column, which is what aggregate reads
        want = round(qty * p2 - qty * p1, 2)
        match = [t for t in trades
                 if abs(t["net_pnl"] - want) < 0.01]
        assert match, (f"no trade booked {want} (qty={qty} @{p1}->{p2}); "
                       f"seen {sorted(round(t['net_pnl'], 2) for t in trades)}")
    assert by_group
