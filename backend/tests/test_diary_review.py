"""Confirming and editing a diary match through the API.

    cd backend && python -m pytest tests/test_diary_review.py -q

Two properties:
1. A confirmation writes `manual` into trade_analysis and takes the match out of
   the review queue — without calling the AI again.
2. The diary list reads live values, so an edit made in TradeDetail shows up
   here and the model's own verdict stays available as model_match_confidence.
"""
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

DATE = "2026-07-01"


def _payload():
    return {
        "diary_date": DATE,
        "overall_summary": "Steady day.",
        "patterns_identified": ["waited for the level"],
        "improvement_areas": ["size down"],
        "trade_analyses": [
            {"trade_group": "NVDA_1", "ticker": "NVDA",
             "match_confidence": "high", "match_notes": "price within $0.50",
             "model_match_confidence": "low", "model_match_notes": "only trade that day",
             "stop_loss": 106.50, "r_multiple": 0.80, "emotional_state": "calm",
             "mistakes": None, "strategy": "VWAP Reclaim",
             "entry_reason": "reclaim", "exit_reason": "target",
             "notes": None, "idea_source": "Watchlist", "target_price": 109.5,
             "risk_per_trade": None, "risk_reward": None, "tags": [],
             "ai_feedback": "Good patience."},
            {"trade_group": None, "ticker": "AMD",
             "match_confidence": "unmatched", "match_notes": "no trade that day",
             "model_match_confidence": "unmatched", "model_match_notes": "no trade that day",
             "stop_loss": None, "r_multiple": None, "emotional_state": None,
             "mistakes": None, "strategy": None, "entry_reason": None,
             "exit_reason": None, "notes": None, "idea_source": None,
             "target_price": None, "risk_per_trade": None, "risk_reward": None,
             "tags": [], "ai_feedback": ""},
        ],
    }


@pytest.fixture
def app(tmp_path, monkeypatch):
    import os
    db = tmp_path / "journal.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    for name in ("database", "main", "ai_settings", "ai_analysis", "daily_summary"):
        if name in sys.modules:
            sys.modules.pop(name, None)
    import database
    database.DB_PATH = str(db)
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.db = str(db)
        yield c


def _seed(app, with_diary=True):
    """Create the account, one trade and (unless asked otherwise) one diary
    analysis written through the real save path."""
    import sqlite3
    r = app.post("/api/accounts", json={"name": "Test", "type": "day_trading"})
    assert r.status_code == 201, r.text
    payload = _payload()
    conn = sqlite3.connect(app.db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, "
        "side, gross_pnl, net_pnl, commissions, executions) "
        "VALUES (1, 'NVDA_1', ?, 'NVDA', 'STOCK', 'LONG', 73.0, 73.0, 0.0, '[]')",
        (DATE,),
    )
    entry_id = None
    if with_diary:
        conn.execute(
            "INSERT INTO diary_entries (account_id, entry_date, image_path, ai_analysis) "
            "VALUES (1, ?, ?, ?)",
            (DATE, "2026-07-01_notes.jpg", json.dumps(payload)),
        )
        entry_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        from ai_analysis import save_analysis_to_db
        save_analysis_to_db(conn, entry_id, payload)
    conn.commit()
    conn.close()
    return entry_id


def test_a_confirmed_match_leaves_the_queue(app):
    _seed(app)
    listed = app.get("/api/diary").json()[0]
    assert listed["needs_review"] == 2, "unmatched + unconfirmed high"
    assert [q["match_confidence"] for q in listed["review_queue"]] == ["unmatched", "high"]

    r = app.patch("/api/trades/NVDA_1/analysis",
                  json={"match_confidence": "manual", "match_notes": "confirmed by you"})
    assert r.status_code == 200, r.text
    assert r.json()["match_confidence"] == "manual"

    listed = app.get("/api/diary").json()[0]
    assert listed["needs_review"] == 1, "only the unmatched line remains"
    assert listed["ai_analysis"]["trade_analyses"][0]["match_confidence"] == "manual"
    assert listed["ai_analysis"]["trade_analyses"][0]["model_match_confidence"] == "low"


def test_editing_a_field_in_trade_detail_shows_up_in_the_diary_list(app):
    _seed(app)
    r = app.patch("/api/trades/NVDA_1/analysis", json={"r_multiple": -1.5,
                                                       "emotional_state": "revenge"})
    assert r.status_code == 200, r.text
    ta = app.get("/api/diary").json()[0]["ai_analysis"]["trade_analyses"][0]
    assert ta["r_multiple"] == -1.5, "a value the user set must be the one shown"
    assert ta["emotional_state"] == "revenge"
    assert ta["stop_loss"] == 106.50, "an untouched field keeps what the model said"


def test_r_multiple_is_editable_through_the_existing_endpoint(app):
    _seed(app)
    assert app.patch("/api/trades/NVDA_1/analysis",
                     json={"r_multiple": 2.25}).json()["r_multiple"] == 2.25
    assert app.patch("/api/trades/NVDA_1/analysis",
                     json={"r_multiple": None}).json()["r_multiple"] is None


def test_an_unknown_match_confidence_is_refused(app):
    _seed(app)
    r = app.patch("/api/trades/NVDA_1/analysis", json={"match_confidence": "probably"})
    assert r.status_code == 422
    assert "match confidence" in r.json()["detail"]


def test_an_unmatched_diary_line_has_nothing_to_confirm(app):
    _seed(app)
    ta = app.get("/api/diary").json()[0]["ai_analysis"]["trade_analyses"][1]
    assert ta["trade_group"] is None
    assert ta["match_confidence"] == "unmatched"
    queue = app.get("/api/diary").json()[0]["review_queue"]
    assert [q["ticker"] for q in queue] == ["AMD", "NVDA"]


def test_an_upload_runs_the_match_check_before_it_saves(app, monkeypatch):
    """The full path: a mocked model answer comes back recomputed.

    Direct assertions on analyse_confidence wouldn't notice if the upload forgot
    to call it — this pins the endpoint as well.
    """
    import main
    _seed(app, with_diary=False)   # so the upload below is the only entry
    raw = {
        "diary_date": DATE, "overall_summary": "ok",
        "patterns_identified": [], "improvement_areas": [],
        "trade_analyses": [
            {"trade_group": "NVDA_1", "ticker": "NVDA",
             "match_confidence": "low", "match_notes": "guessed",
             "entry_price_diary": 107.50, "diary_time": None,
             "stop_loss": 106.5, "r_multiple": 0.8,
             "emotional_state": "calm", "mistakes": None, "strategy": "VWAP Reclaim",
             "entry_reason": None, "exit_reason": None, "notes": None,
             "idea_source": None, "target_price": None, "risk_per_trade": None,
             "risk_reward": None, "tags": [], "ai_feedback": "good"},
        ],
    }
    # `main` imports this name directly, so patch what the endpoint resolves.
    monkeypatch.setattr(main, "analyze_diary_text",
                        lambda text, date, ctx: json.loads(json.dumps(raw)))
    import sqlite3
    conn = sqlite3.connect(app.db)
    conn.execute("UPDATE trades SET executions=? WHERE trade_group='NVDA_1'", (
        json.dumps([{"action": "BOT", "qty": 100, "price": 107.67,
                     "date": DATE, "time": "09:46:00"}]),))
    conn.commit()
    conn.close()

    r = app.post("/api/upload-diary", files={"file": ("notes.txt", b"NVDA 107.50", "text/plain")},
                 data={"date": DATE, "account_id": "1"})
    assert r.status_code == 200, r.text
    assert "analysis_error" not in r.json(), r.text
    listed = app.get("/api/diary").json()[0]
    ta = listed["ai_analysis"]["trade_analyses"][0]
    assert ta["match_confidence"] == "high"
    assert ta["model_match_confidence"] == "low"
    assert ta["stop_loss"] == 106.5
    assert ta["r_multiple"] == 0.8


def test_a_diary_entry_with_unreadable_json_is_still_listable(app):
    import sqlite3
    _seed(app)
    conn = sqlite3.connect(app.db)
    conn.execute("UPDATE diary_entries SET ai_analysis = ? WHERE entry_date = ?",
                 ("{not json", DATE))
    conn.commit()
    conn.close()
    rows = app.get("/api/diary").json()
    assert rows[0]["ai_analysis"] is None
    assert rows[0]["needs_review"] == 0
