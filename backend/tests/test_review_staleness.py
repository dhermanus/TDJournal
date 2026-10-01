"""The Day/Week Review regenerates only when its inputs changed.

    cd backend && python -m pytest tests/test_review_staleness.py -q

The old endpoint served any stored row until someone pressed Regenerate, so a
coaching note kept describing a day after its trades were corrected. These tests
drive the real endpoint with the model stubbed and assert three things: an
unchanged day is free to serve, an edited one is not, and `force` still bypasses
both.
"""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

DATE = "2026-07-01"


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

    # The endpoints resolve these names from main's globals at call time, so
    # patching the module attribute is what reaches them. Each stub appends to
    # `calls` so a test can count invocations without spying on the network.
    calls = []
    originals = (main.generate_daily_summary, main.generate_weekly_summary)

    def counting_daily(context):
        calls.append("daily")
        return {"narrative": f"generated #{len(calls)}", "grades": [], "date": DATE}

    def counting_weekly(context):
        calls.append("weekly")
        return {"narrative": f"week #{len(calls)}", "grades": []}

    main.generate_daily_summary = counting_daily
    main.generate_weekly_summary = counting_weekly

    with TestClient(main.app) as c:
        c.db = str(db)
        yield c, main, calls

    main.generate_daily_summary, main.generate_weekly_summary = originals


def _seed(client):
    """One account and one trade on DATE."""
    r = client.post("/api/accounts", json={"name": "Test", "type": "day_trading"})
    assert r.status_code == 201, r.text
    conn = sqlite3.connect(client.db)
    conn.execute(
        "INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, "
        "side, gross_pnl, net_pnl, commissions, executions) "
        "VALUES (1, 'NVDA_1', ?, 'NVDA', 'STOCK', 'LONG', 73.0, 73.0, 0.0, '[]')",
        (DATE,),
    )
    conn.commit()
    conn.close()


def daily_calls(calls):
    return sum(1 for kind in calls if kind == "daily")


def test_unchanged_day_serves_the_cached_review(app):
    client, main, calls = app
    _seed(client)

    first = client.get("/api/daily-summary", params={"date": DATE}).json()
    assert first["cached"] is False
    assert daily_calls(calls) == 1

    second = client.get("/api/daily-summary", params={"date": DATE}).json()
    assert second["cached"] is True, "an unchanged day must not be regenerated"
    assert daily_calls(calls) == 1, "no second call to the model"
    assert second["ai_usage"]["spent"] is False
    assert second["ai_usage"]["reason"] == "cached"


def test_editing_a_trade_regenerates_the_review(app):
    client, main, calls = app
    _seed(client)

    client.get("/api/daily-summary", params={"date": DATE})
    assert daily_calls(calls) == 1

    r = client.patch("/api/trades/NVDA_1/analysis", json={"mistakes": "moved my stop"})
    assert r.status_code == 200, r.text

    second = client.get("/api/daily-summary", params={"date": DATE}).json()
    assert second["cached"] is False, "the coaching text now describes a changed day"
    assert daily_calls(calls) == 2, "regeneration must reach the model"
    assert second["regenerated_reason"] and "changed" in second["regenerated_reason"]


def test_force_regenerates_even_when_things_match(app):
    client, main, calls = app
    _seed(client)

    client.get("/api/daily-summary", params={"date": DATE})
    again = client.get("/api/daily-summary",
                       params={"date": DATE, "force": "true"}).json()
    assert daily_calls(calls) == 2, "the Regenerate button must still work"
    assert again["cached"] is False

    # And the fresh one is what a later read serves. This is the regression the
    # write path used to have: with account_id NULL, REPLACE never matched the
    # existing row (SQLite treats NULLs as distinct in a UNIQUE index), so it
    # stacked a second row and the next read returned the *old* first row —
    # Regenerate looked like it had done nothing.
    assert "generated #2" in (again.get("narrative") or "")
    later = client.get("/api/daily-summary", params={"date": DATE})
    assert later.status_code == 200
    assert later.json()["cached"] is True
    assert "generated #2" in (later.json().get("narrative") or "")
    assert daily_calls(calls) == 2, "the served row is the regenerated one, not a new call"


def test_a_review_cached_before_tracking_says_so(app):
    client, main, calls = app
    _seed(client)

    client.get("/api/daily-summary", params={"date": DATE})
    conn = sqlite3.connect(client.db)
    # A row written by an older build: content but no input_hash.
    conn.execute("UPDATE daily_summaries SET input_hash = NULL WHERE summary_date = ?",
                 (DATE,))
    conn.commit()
    conn.close()

    again = client.get("/api/daily-summary", params={"date": DATE}).json()
    assert again["cached"] is False, "a hash-less row cannot be trusted"
    assert "tracking" in again["regenerated_reason"]
    assert daily_calls(calls) == 2


def test_cost_is_reported_on_a_real_generation(app):
    client, main, calls = app
    from ai_usage import _recorder, forget_usage

    def with_usage(context):
        _recorder.record(SimpleNamespace(
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                  model="claude-opus-5"),
            model="claude-opus-5", content=[]))
        return {"narrative": "generated", "grades": [], "date": DATE}

    main.generate_daily_summary = with_usage
    _seed(client)
    try:
        out = client.get("/api/daily-summary", params={"date": DATE}).json()
        usage = out["ai_usage"]
        assert usage["spent"] is True
        assert usage["input_tokens"] == 1000
        assert usage["output_tokens"] == 200
        assert usage["estimated_cost_usd"] > 0
        assert usage["estimate"] is True, "an estimate must not be shown as a bill"
        assert usage["cache_read_input_tokens"] == 0, "the proxy omits this field"
    finally:
        forget_usage()


def test_weekly_summary_uses_the_same_rule(app):
    client, main, calls = app
    _seed(client)

    first = client.get("/api/weekly-summary", params={"date": DATE}).json()
    assert first.get("cached") is not True

    second = client.get("/api/weekly-summary", params={"date": DATE}).json()
    assert second["cached"] is True, "an unchanged week must be served"
    assert second["ai_usage"]["reason"] == "cached"

    client.patch("/api/trades/NVDA_1/analysis", json={"mistakes": "chased it"})
    third = client.get("/api/weekly-summary", params={"date": DATE}).json()
    assert third.get("cached") is not True, "the week contains the changed day"
