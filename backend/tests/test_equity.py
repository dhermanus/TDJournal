"""Equity ratios: capital, cash flows, return, drawdown and risk.

    cd backend && python -m pytest tests/test_equity.py -q

Pure arithmetic against hand-worked cases — $1000 starting, +$500 deposit,
+$200 P&L, $100 drawdown: return is 200/1500 = 13.33%, drawdown 100/1500 = 6.67%.

The two properties worth more than the numbers:

* an account with no starting capital reports *no* percentages rather than a
  division by zero or a misleading -100%; and
* a cash flow before a date range funds the period, so `date_from` is exclusive
  and a mid-period deposit does not inflate the base of the period it arrived in.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import equity  # noqa: E402


def _conn(tmp_path):
    c = sqlite3.connect(tmp_path / "eq.db")
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            starting_capital REAL
        )""")
    c.execute("""
        CREATE TABLE account_cash_flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('deposit','withdrawal')),
            amount REAL NOT NULL CHECK(amount > 0),
            flow_date TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
    c.execute("INSERT INTO accounts (id, name, starting_capital) VALUES (1, 'A', 1000.0)")
    c.commit()
    return c


def _flow(c, kind, amount, date):
    c.execute("INSERT INTO account_cash_flows (account_id, kind, amount, flow_date) "
              "VALUES (1, ?, ?, ?)", (kind, amount, date))
    c.commit()


# ── the denominators ─────────────────────────────────────────────────────────

def test_starting_capital_and_flows_combine(tmp_path):
    c = _conn(tmp_path)
    _flow(c, "deposit", 500, "2026-01-15")
    _flow(c, "withdrawal", 200, "2026-02-01")
    starting, net, rows, unknown = equity.cash_for(c, 1)
    assert starting == 1000.0
    assert net == 300.0
    assert unknown == 0
    assert [r["kind"] for r in rows] == ["deposit", "withdrawal"]
    assert equity.base_equity(starting, net) == 1300.0
    c.close()


def test_flows_before_the_range_form_the_base_those_trades_were_funded_by(tmp_path):
    """date_from is exclusive: a deposit that day already funded that day."""
    c = _conn(tmp_path)
    _flow(c, "deposit", 500, "2026-01-15")   # before the range
    _flow(c, "deposit", 900, "2026-03-01")   # inside the range
    starting, net, _, _ = equity.cash_for(c, 1, date_from="2026-03-01")
    assert net == 500.0, "the March deposit funds March, not the base"
    starting2, net2, _, _ = equity.cash_for(c, 1)  # no range: everything counts
    assert net2 == 1400.0
    assert (starting, starting2) == (1000.0, 1000.0)
    c.close()


def test_no_starting_capital_is_unknown_not_zero(tmp_path):
    """NULL means unknown: dividing by it would report -100% for a flat account."""
    c = sqlite3.connect(tmp_path / "eq.db")
    c.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, starting_capital REAL)")
    c.execute("""CREATE TABLE account_cash_flows (
        id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
        kind TEXT NOT NULL, amount REAL NOT NULL, flow_date TEXT NOT NULL,
        note TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    c.execute("INSERT INTO accounts (id, name) VALUES (2, 'Unset')")
    c.commit()
    starting, net, rows, unknown = equity.cash_for(c, 2)
    assert starting is None
    assert unknown == 0, "a single unknown account is not a count of unknowns"
    assert rows == []
    assert equity.base_equity(starting, net) is None

    r = equity.ratios(total_net_pnl=50.0, max_drawdown=-10.0, daily_pnl=[],
                      starting_capital=None, net_flows=0.0)
    assert r["equity_base"] is None
    assert r["return_pct"] is None, "no denominator -> no percentage"
    assert r["max_drawdown_pct"] is None
    c.close()


def test_all_accounts_sums_the_capital_across_accounts(tmp_path):
    """The consolidated view needs a total, and a partial one is not offered."""
    c = _conn(tmp_path)
    c.execute("INSERT INTO accounts (id, name, starting_capital) VALUES (2, 'B', 4000.0)")
    c.commit()
    starting, net, rows, unknown = equity.cash_for(c, None)
    assert starting == 5000.0, "1000 + 4000"
    assert unknown == 0
    assert net == 0.0

    # An account nobody has priced makes the whole total unknowable — including
    # it would make every percentage an understatement of the real exposure.
    c.execute("INSERT INTO accounts (id, name) VALUES (3, 'Unset')")
    c.commit()
    starting, net, rows, unknown = equity.cash_for(c, None)
    assert starting is None, "a partial total is not the total"
    assert unknown == 1
    c.close()


def test_all_accounts_totals_its_flows_too(tmp_path):
    c = _conn(tmp_path)
    c.execute("INSERT INTO accounts (id, name, starting_capital) VALUES (2, 'B', 4000.0)")
    c.commit()
    _flow(c, "deposit", 500, "2026-01-15")
    c.execute("INSERT INTO account_cash_flows (account_id, kind, amount, flow_date) "
              "VALUES (2, 'withdrawal', 200, '2026-01-20')")
    c.commit()
    starting, net, rows, unknown = equity.cash_for(c, None)
    assert (starting, net, unknown) == (5000.0, 300.0, 0)
    assert len(rows) == 2, "both accounts' flows, in date order"
    assert [r["flow_date"] for r in rows] == ["2026-01-15", "2026-01-20"]
    c.close()


# ── the ratios ───────────────────────────────────────────────────────────────

def test_return_and_drawdown_are_percentages_of_equity(tmp_path):
    daily = [{"date": "2026-01-01", "net_pnl": 200.0, "cumulative": 200.0},
             {"date": "2026-01-02", "net_pnl": -100.0, "cumulative": 100.0},
             {"date": "2026-01-03", "net_pnl": 100.0, "cumulative": 200.0}]
    r = equity.ratios(total_net_pnl=200.0, max_drawdown=-100.0, daily_pnl=daily,
                      starting_capital=1000.0, net_flows=500.0)
    # base = 1500
    assert r["equity_base"] == 1500.0
    assert r["return_pct"] == pytest.approx(13.33)      # 200 / 1500
    assert r["max_drawdown_pct"] == pytest.approx(6.67)  # 100 / 1500
    assert r["peak_equity"] == pytest.approx(1700.0)     # 1500 + best cumulative
    # every percentage is rounded to 2dp on the way out, so the expectations are
    assert r["avg_loss_pct"] == round(100 / 1500 * 100, 2)
    assert r["largest_loss_pct"] == round(100 / 1500 * 100, 2)


def test_zero_capital_gives_no_percentages(tmp_path):
    """Base 0 is a real state (spent everything) and must not be divided into."""
    r = equity.ratios(total_net_pnl=-50.0, max_drawdown=-50.0, daily_pnl=[],
                      starting_capital=0.0, net_flows=0.0)
    assert r["equity_base"] == 0.0
    assert r["return_pct"] is None
    assert r["max_drawdown_pct"] is None


def test_lossy_days_produce_the_two_risk_measures(tmp_path):
    daily = [{"date": "a", "net_pnl": -50.0, "cumulative": -50.0},
             {"date": "b", "net_pnl": -150.0, "cumulative": -200.0},
             {"date": "c", "net_pnl": 40.0, "cumulative": -160.0}]
    r = equity.ratios(total_net_pnl=-160.0, max_drawdown=-200.0, daily_pnl=daily,
                      starting_capital=2000.0, net_flows=0.0)
    assert r["avg_loss_pct"] == round(100 / 2000 * 100, 2)   # (50+150)/2
    assert r["largest_loss_pct"] == round(150 / 2000 * 100, 2)


def test_a_winning_period_keeps_peak_equity_above_the_base(tmp_path):
    daily = [{"date": "a", "net_pnl": 300.0, "cumulative": 300.0}]
    r = equity.ratios(total_net_pnl=300.0, max_drawdown=0.0, daily_pnl=daily,
                      starting_capital=1000.0, net_flows=0.0)
    assert r["peak_equity"] == pytest.approx(1300.0)
    assert r["max_drawdown_pct"] == 0.0


# ── the KPI-facing wrapper ───────────────────────────────────────────────────

def test_summarize_carries_the_flows_along_with_the_ratios(tmp_path):
    c = _conn(tmp_path)
    _flow(c, "deposit", 500, "2026-01-15")
    daily = [{"date": "2026-01-02", "net_pnl": 200.0, "cumulative": 200.0}]
    out = equity.summarize(c, account_id=1, date_from=None, total_net_pnl=200.0,
                           max_drawdown=-50.0, daily_pnl=daily)
    assert out["starting_capital"] == 1000.0
    assert out["net_flows"] == 500.0
    assert out["flow_count"] == 1
    assert out["return_pct"] == pytest.approx(13.33)   # 200 / 1500
    c.close()


def test_summarize_for_an_account_without_capital_returns_null_ratios(tmp_path):
    c = sqlite3.connect(tmp_path / "eq.db")
    c.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, starting_capital REAL)")
    c.execute("""CREATE TABLE account_cash_flows (
        id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
        kind TEXT NOT NULL, amount REAL NOT NULL, flow_date TEXT NOT NULL,
        note TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    c.execute("INSERT INTO accounts (id, name) VALUES (9, 'Fresh')")
    c.commit()
    out = equity.summarize(c, account_id=9, date_from=None, total_net_pnl=10.0,
                           max_drawdown=-1.0, daily_pnl=[])
    assert out["starting_capital"] is None
    assert out["return_pct"] is None
    assert out["max_drawdown_pct"] is None
    assert out["net_flows"] == 0.0
    c.close()
