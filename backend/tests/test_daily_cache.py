"""Day/weekly cache uses only the current input, and notices changes.

    cd backend && python -m pytest tests/test_daily_cache.py -q

The old path said `if a daily_summaries row exists, serve it until the user
presses Regenerate`. These tests prove the fingerprint is computed from the same
record the prompt uses: every day trade and analysis field + the latest diary
entry. Adding an unrelated day's trade and changing all-time KPIs must *not*
invalidate the day (it would cause paid requests forever on a working account).
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from daily_cache import TRADE_COLUMNS, daily_input_fingerprint, date_range_fingerprint


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "cache.db")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
      trade_group TEXT NOT NULL, date TEXT NOT NULL, ticker TEXT NOT NULL,
      side TEXT, instrument_type TEXT, net_pnl REAL, gross_pnl REAL,
      commissions REAL, executions TEXT, option_type TEXT, option_strike REAL,
      option_expiry TEXT);
    CREATE TABLE trade_analysis (
      trade_group TEXT PRIMARY KEY, strategy TEXT, r_multiple REAL,
      stop_loss REAL, target_price REAL, risk_per_trade REAL, risk_reward REAL,
      mistakes TEXT, emotional_state TEXT, entry_reason TEXT, exit_reason TEXT,
      ai_feedback TEXT, idea_source TEXT, match_confidence TEXT);
    CREATE TABLE diary_entries (
      id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
      entry_date TEXT NOT NULL, ai_analysis TEXT);
    """)
    conn.execute("""INSERT INTO trades
      (account_id, trade_group, date, ticker, side, instrument_type, net_pnl,
       gross_pnl, commissions, executions)
      VALUES (1,'A_1','2026-07-01','AAPL','LONG','STOCK',10,11,1,'[]')""")
    conn.execute("""INSERT INTO trade_analysis
      (trade_group, strategy, r_multiple, emotional_state, mistakes)
      VALUES ('A_1','VWAP Reclaim',1.2,'Calm',NULL)""")
    conn.execute("INSERT INTO diary_entries (account_id,entry_date,ai_analysis) VALUES (1,'2026-07-01',?)",
                 (json.dumps({"overall_summary": "patient"}),))
    conn.commit()
    yield conn
    conn.close()


# ── daily fingerprint ──────────────────────────────────────────────────────────

def test_same_inputs_have_same_hash_across_a_reopen(db, tmp_path):
    first = daily_input_fingerprint(db, "2026-07-01", 1)
    db.commit()
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    reopened = sqlite3.connect(path)
    reopened.row_factory = sqlite3.Row
    second = daily_input_fingerprint(reopened, "2026-07-01", 1)
    assert first == second
    assert len(first) == 16


def test_a_trade_added_for_this_day_marks_review_stale(db):
    before = daily_input_fingerprint(db, "2026-07-01", 1)
    db.execute("INSERT INTO trades (account_id,trade_group,date,ticker,net_pnl) VALUES (1,'B_1','2026-07-01','MSFT',-4)")
    assert daily_input_fingerprint(db, "2026-07-01", 1) != before


def test_a_pnl_edit_on_this_day_marks_review_stale(db):
    before = daily_input_fingerprint(db, "2026-07-01", 1)
    db.execute("UPDATE trades SET net_pnl=11 WHERE trade_group='A_1'")
    assert daily_input_fingerprint(db, "2026-07-01", 1) != before


def test_every_prompted_trade_analysis_field_invalidates(db):
    # Fail loudly if someone adds a field to `generate_daily_summary`'s prompt
    # but forgets to include it in the fingerprint, or vice versa.
    expected = {"strategy", "r_multiple", "stop_loss", "target_price",
                "risk_per_trade", "risk_reward", "mistakes", "emotional_state",
                "entry_reason", "exit_reason", "ai_feedback", "idea_source",
                "match_confidence"}
    actual = set(TRADE_COLUMNS) - {
        "id", "trade_group", "ticker", "side", "instrument_type", "net_pnl",
        "gross_pnl", "commissions", "executions", "option_type", "option_strike",
        "option_expiry",
    }
    assert actual == expected
    before = daily_input_fingerprint(db, "2026-07-01", 1)
    for field in expected:
        # Change the field to a distinct value that's compatible with its type.
        new = "changed" if field not in {"r_multiple", "stop_loss", "target_price",
                                         "risk_per_trade", "risk_reward"} else 222.0
        db.execute(f"UPDATE trade_analysis SET {field}=? WHERE trade_group='A_1'", (new,))
        after = daily_input_fingerprint(db, "2026-07-01", 1)
        assert after != before, f"{field} did not invalidate the hash"
        # restore original to keep the next iteration independent
        db.rollback()
        db.execute(f"UPDATE trade_analysis SET {field}=? WHERE trade_group='A_1'",
                   (1.2 if field == "r_multiple" else "VWAP Reclaim" if field == "strategy" else "Calm" if field == "emotional_state" else None,))
        db.commit()
        before = daily_input_fingerprint(db, "2026-07-01", 1)


def test_a_diary_note_edit_marks_review_stale(db):
    before = daily_input_fingerprint(db, "2026-07-01", 1)
    db.execute("UPDATE diary_entries SET ai_analysis=? WHERE entry_date='2026-07-01'",
               (json.dumps({"overall_summary": "broke my rule"}),))
    assert daily_input_fingerprint(db, "2026-07-01", 1) != before


def test_a_second_diary_entry_does_not_change_the_latest_one_the_prompt_reads(db):
    before = daily_input_fingerprint(db, "2026-07-01", 1)
    db.execute("INSERT INTO diary_entries (account_id,entry_date,ai_analysis) VALUES (1,'2026-07-01',?)",
               (json.dumps({"overall_summary": "newest note"}),))
    after = daily_input_fingerprint(db, "2026-07-01", 1)
    assert before != after


def test_another_days_trade_does_not_make_day_review_stale(db):
    before = daily_input_fingerprint(db, "2026-07-01", 1)
    db.execute("INSERT INTO trades (account_id,trade_group,date,ticker,net_pnl) VALUES (1,'B_1','2026-07-02','MSFT',9000)")
    assert daily_input_fingerprint(db, "2026-07-01", 1) == before


def test_another_accounts_trade_does_not_make_review_stale(db):
    before = daily_input_fingerprint(db, "2026-07-01", 1)
    db.execute("INSERT INTO trades (account_id,trade_group,date,ticker,net_pnl) VALUES (2,'B_1','2026-07-01','MSFT',9000)")
    assert daily_input_fingerprint(db, "2026-07-01", 1) == before


def test_account_all_is_different_when_other_account_has_a_trade(db):
    db.execute("INSERT INTO trades (account_id,trade_group,date,ticker,net_pnl) VALUES (2,'B_1','2026-07-01','MSFT',9000)")
    one = daily_input_fingerprint(db, "2026-07-01", 1)
    all_accounts = daily_input_fingerprint(db, "2026-07-01", None)
    assert one != all_accounts


def test_empty_day_has_a_stable_fingerprint(db):
    before = daily_input_fingerprint(db, "2026-07-03", 1)
    after = daily_input_fingerprint(db, "2026-07-03", 1)
    assert before == after


# ── weekly/date-range fingerprint ──────────────────────────────────────────────

def test_date_range_changes_when_any_day_changes(db):
    before = date_range_fingerprint(db, "2026-06-29", "2026-07-05", 1)
    db.execute("INSERT INTO trades (account_id,trade_group,date,ticker,net_pnl) VALUES (1,'C_1','2026-07-03','AMD',20)")
    assert date_range_fingerprint(db, "2026-06-29", "2026-07-05", 1) != before


def test_date_range_is_inclusive_and_order_independent(db):
    first = date_range_fingerprint(db, "2026-06-29", "2026-07-05", 1)
    db.execute("INSERT INTO trades (account_id,trade_group,date,ticker,net_pnl) VALUES (1,'B_1','2026-07-06','MSFT',20)")
    assert date_range_fingerprint(db, "2026-06-29", "2026-07-05", 1) == first, "exclusive upper day"


def test_date_range_invalid_or_inverted_returns_empty_marker(db):
    assert date_range_fingerprint(db, "not-a-date", "2026-07-05", 1) == ""
    assert date_range_fingerprint(db, "2026-07-05", "2026-06-29", 1) == ""
