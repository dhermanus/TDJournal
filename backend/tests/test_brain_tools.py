"""Brain's read-only query tools.

    cd backend && python -m pytest tests/test_brain_tools.py -q

The three properties worth proving, because each fails silently if broken:

1. numbers come from SQL/Python, not the model — `get_performance_summary`
   over the real journal returns the same totals the reports endpoint prints;
2. the model cannot reach data it should not: unknown tool names, SQL in
   parameters, non-SELECT statements and out-of-account rows all refuse;
3. filters are validated enums, so a wrong value is an error the model can
   report rather than a silently empty result.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from brain_tools import TOOL_NAMES, TOOL_SCHEMAS, run_tool, GROUPABLE, _select


@pytest.fixture
def conn(tmp_path):
    """A journal with two accounts and a diary row."""
    db = tmp_path / "j.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, type TEXT,
            color TEXT, broker TEXT, created_at TEXT);
        CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL, trade_group TEXT NOT NULL, date TEXT NOT NULL,
            ticker TEXT NOT NULL, instrument_type TEXT, side TEXT, gross_pnl REAL,
            net_pnl REAL, commissions REAL DEFAULT 0, executions TEXT DEFAULT '[]',
            setup TEXT);
        CREATE TABLE trade_analysis (id INTEGER PRIMARY KEY,
            trade_group TEXT UNIQUE, strategy TEXT, r_multiple REAL,
            emotional_state TEXT, mistakes TEXT, notes TEXT);
        CREATE TABLE diary_entries (id INTEGER PRIMARY KEY, account_id INTEGER,
            entry_date TEXT, image_path TEXT, raw_text TEXT, ai_analysis TEXT);
        """
    )
    rows = [
        # account 1 — six trades, deliberately mixed
        (1, "g1", "2026-07-01", "EURUSD", "FX", "LONG", 100.0, 98.0, 2.0, "Trend"),
        (1, "g2", "2026-07-02", "EURUSD", "FX", "LONG", -50.0, -52.0, 2.0, "Trend"),
        (1, "g3", "2026-07-06", "USDJPY", "FX", "SHORT", 30.0, 28.0, 2.0, "Range"),
        (1, "g4", "2026-07-07", "USDJPY", "FX", "SHORT", 0.0, 0.0, 0.0, "Range"),
        (1, "g5", "2026-07-08", "EURUSD", "FX", "LONG", -20.0, -22.0, 2.0, None),
        (1, "g6", "2026-06-30", "AAPL", "STOCK", "LONG", 10.0, 9.0, 1.0, "Gap"),
        # account 2 — must never be visible to account 1
        (2, "o1", "2026-07-01", "TSLA", "STOCK", "LONG", 9999.0, 9999.0, 0.0, "Other"),
    ]
    for (acct, g, date, ticker, instr, side, gross, net, comm, strat) in rows:
        c.execute(
            "INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type,"
            " side, gross_pnl, net_pnl, commissions) VALUES (?,?,?,?,?,?,?,?,?)",
            (acct, g, date, ticker, instr, side, gross, net, comm))
        c.execute(
            "INSERT INTO trade_analysis (trade_group, strategy) VALUES (?,?)", (g, strat))
    c.execute(
        "INSERT INTO diary_entries (account_id, entry_date, ai_analysis)"
        " VALUES (1, '2026-07-01', ?)",
        (json.dumps({"overall_summary": "Stayed patient",
                     "patterns_identified": ["waited for setup"]}),))
    c.commit()
    yield c
    c.close()


def tool(conn, name, body=None, account_id=1):
    return json.loads(run_tool(name, body or {}, conn, account_id))


def test_every_declared_tool_runs(conn):
    """A schema whose name is not dispatchable would fail only at runtime."""
    bodies = {
        "get_performance_summary": {},
        "list_trades": {"limit": 5},
        "breakdown": {"group_by": "ticker"},
        "get_diary": {"limit": 5},
    }
    assert TOOL_NAMES == [t["name"] for t in TOOL_SCHEMAS]
    for t in TOOL_SCHEMAS:
        assert t["input_schema"]["type"] == "object"
        assert t["description"].strip()
        out = tool(conn, t["name"], bodies[t["name"]])
        assert isinstance(out, (dict, list))


def test_summary_totals_match_the_rows(conn):
    out = tool(conn, "get_performance_summary")
    # net_pnl: 98, -52, 28, 0, -22, 9 → 3 wins, 2 losses, 1 scratch
    assert out["trades"] == 6
    assert out["wins"] == 3 and out["losses"] == 2 and out["scratch_trades"] == 1
    assert out["win_rate_pct"] == pytest.approx(50.0)
    assert out["net_pnl"] == pytest.approx(98 - 52 + 28 + 0 - 22 + 9)
    assert out["commissions"] == pytest.approx(9.0)
    assert out["gross_pnl"] == pytest.approx(100 - 50 + 30 + 0 - 20 + 10)
    # PF = sum of wins / |sum of losses| = 135 / 74
    assert out["profit_factor"] == pytest.approx(135 / 74, abs=0.01)
    assert out["first_date"] == "2026-06-30" and out["last_date"] == "2026-07-08"


def test_filters_narrow_the_selection(conn):
    out = tool(conn, "get_performance_summary",
               {"ticker": "EURUSD", "date_from": "2026-07-01", "date_to": "2026-07-31"})
    assert out["trades"] == 3, out
    assert out["net_pnl"] == pytest.approx(98 - 52 - 22)

    wide = tool(conn, "get_performance_summary", {"ticker": "EURUSD"})
    assert wide["trades"] == 3, "date_from/date_to alone must not change ticker filter"


def test_an_empty_result_set_is_an_answer_not_an_error(conn):
    out = tool(conn, "get_performance_summary",
               {"instrument_type": "FUTURE", "date_from": "2026-07-01"})
    assert out["trades"] == 0
    assert out["net_pnl"] == 0
    assert out["best_day"] is None and out["worst_day"] is None
    assert out["profit_factor"] is None


def test_a_broken_filter_raises_instead_of_returning_nothing(conn):
    """A rejected value must reach the model as an error, not as 'you had no trades'."""
    for body in ({"date_from": "01/02/2026"}, {"date_from": "2026-13-01"},
                 {"side": "MAYBE"}, {"instrument_type": "OPTIONX"}):
        with pytest.raises(ValueError):
            tool(conn, "get_performance_summary", body)


def test_list_trades_returns_rows_and_respects_limit(conn):
    out = tool(conn, "list_trades", {"sort_by": "pnl_desc", "limit": 2})
    assert out["limit"] == 2
    assert [t["net_pnl"] for t in out["trades"]] == [98, 28]
    assert all("trade_group" in t for t in out["trades"])

    huge = tool(conn, "list_trades", {"limit": 10_000})
    assert huge["limit"] == 50, "limit must be capped, not passed through"


def test_breakdown_groups_by_a_whitelisted_dimension_only(conn):
    out = tool(conn, "breakdown", {"group_by": "strategy"})
    by_group = {g["group"]: g for g in out["groups"]}
    # A trade with no strategy is labelled, not dropped or shown as null.
    assert set(by_group) == {"Trend", "Range", "Gap", "No strategy"}
    assert by_group["Trend"]["trades"] == 2
    assert by_group["Trend"]["net_pnl"] == pytest.approx(98 - 52)
    assert by_group["Trend"]["wins"] == 1
    assert by_group["Range"]["trades"] == 2
    assert by_group["Range"]["wins"] == 1, "the zero-P&L trade is a scratch, not a win"
    assert by_group["Range"]["net_pnl"] == pytest.approx(28)
    assert by_group["No strategy"]["trades"] == 1

    for bad in ("net_pnl", "trade_group", "1; DROP TABLE trades", "t.date || ''"):
        with pytest.raises(ValueError):
            tool(conn, "breakdown", {"group_by": bad})

    # Every grouping is a fixed column or a fixed CASE — never interpolated.
    assert all(isinstance(v, str) and v.strip() for v in GROUPABLE.values())


def test_grouping_dates_and_weekdays(conn):
    by_date = tool(conn, "breakdown", {"group_by": "date"})
    assert {g["group"] for g in by_date["groups"]} == {
        "2026-06-30", "2026-07-01", "2026-07-02", "2026-07-06",
        "2026-07-07", "2026-07-08"}

    by_week = tool(conn, "breakdown", {"group_by": "weekday"})
    names = {g["group"] for g in by_week["groups"]}
    assert names <= {"Monday", "Tuesday", "Wednesday", "Thursday",
                     "Friday", "Saturday", "Sunday"}, names
    assert len(by_week["groups"]) == len({g["group"] for g in by_week["groups"]})


def test_diary_is_summarised_not_dumped(conn):
    out = tool(conn, "get_diary")
    assert out["entries"][0]["date"] == "2026-07-01"
    assert out["entries"][0]["summary"] == "Stayed patient"
    assert out["entries"][0]["patterns"] == ["waited for setup"]
    # raw_text / ai_analysis blob must not be echoed wholesale
    assert "raw_text" not in out["entries"][0]


# ── the read-only contract ─────────────────────────────────────────────────────

def test_unknown_tool_names_are_refused(conn):
    for name in ("drop_table_trades", "get_performance_summary ", "GET_PERFORMANCE_SUMMARY",
                 "__import__", "exec", ""):
        with pytest.raises(ValueError, match="unknown tool"):
            run_tool(name, {}, conn, 1)


def test_parameters_cannot_smuggle_sql(conn):
    """A ticker is a ticker: quotes, dashes and comment markers are data."""
    before = tool(conn, "get_performance_summary")["trades"]
    # SQL-shaped text is treated as a symbol nobody trades, so it matches nothing.
    for ticker in ("x'; DROP TABLE trades; --", "EUR' OR '1'='1",
                   "1 UNION SELECT * FROM trades", 'a"; DELETE FROM trades;--'):
        out = tool(conn, "get_performance_summary", {"ticker": ticker})
        assert out["trades"] == 0, ticker
    # A NUL byte is rejected outright — SQLite would otherwise truncate the
    # comparison silently at the terminator.
    with pytest.raises(ValueError):
        tool(conn, "get_performance_summary", {"ticker": "EUR\x00USD"})
    with pytest.raises(ValueError):
        tool(conn, "get_performance_summary", {"ticker": "X" * 40})
    assert tool(conn, "get_performance_summary")["trades"] == before
    # table still there
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'").fetchone()
    assert row is not None


def test_only_select_statements_can_run(conn):
    for sql in ("DELETE FROM trades", "UPDATE trades SET net_pnl = 0",
                "DROP TABLE trades", "PRAGMA writable_schema = 1",
                "SELECT 1; DELETE FROM trades",
                "  insert into trades values (1)"):
        with pytest.raises(ValueError):
            _select(conn, sql, ())
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 7


def test_account_scope_is_applied_and_cannot_be_widened(conn):
    """account_id is bound by the tool itself; the body cannot override it."""
    mine = tool(conn, "get_performance_summary", {}, account_id=1)
    theirs = tool(conn, "get_performance_summary", {}, account_id=2)
    assert mine["trades"] == 6 and theirs["trades"] == 1
    assert theirs["net_pnl"] == pytest.approx(9999)
    # A body that *tries* to pick an account is ignored.
    spoofed = tool(conn, "get_performance_summary", {"account_id": 999}, account_id=1)
    assert spoofed["trades"] == 6

    assert len(tool(conn, "list_trades", {}, account_id=2)["trades"]) == 1
    assert all(g["group"] == "TSLA" for g in tool(conn, "breakdown",
               {"group_by": "ticker"}, account_id=2)["groups"])
    assert tool(conn, "list_trades", {}, account_id=999)["trades"] == []


def test_limits_and_types_are_enforced(conn):
    with pytest.raises(ValueError):
        tool(conn, "list_trades", {"limit": 0})
    with pytest.raises(ValueError):
        tool(conn, "list_trades", {"limit": -5})
    with pytest.raises(ValueError):
        tool(conn, "list_trades", {"limit": "5"})
    with pytest.raises(ValueError):
        tool(conn, "list_trades", {"limit": True})
    with pytest.raises(ValueError):
        tool(conn, "get_performance_summary", {"date_from": {"x": 1}})
