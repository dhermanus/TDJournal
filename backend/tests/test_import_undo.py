"""Import batches and one-click undo, item 4c.

    cd backend && python -m pytest tests/test_import_undo.py -q

The property: undo puts the journal back to how it looked before *that* import,
and refuses rather than guess when it cannot.

Two refusals are deliberate. A batch already undone refuses (the snapshot has
been spent), and an older batch refuses while a newer one stands — restoring an
earlier picture of a trade over a later import would silently discard that
import, which is worse than saying no.

The snapshot ordering is the hard part and is what most of this file pins. The
parse writes before anyone knows which groups the file touches, so a snapshot
taken after the parse holds the merged row instead of the original; the groups
are therefore captured in two stages with the earlier picture winning.
"""
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

CB = ("Cash Balance\n"
      "DATE,TIME,TYPE,REF #,DESCRIPTION,Misc Fees,Commissions & Fees,AMOUNT,BALANCE\n")
STMT = ("Account Statement\n\n" + CB
        + '9/15/26,09:46:16,TRD,="1",BOT +400 TSLA @250.00,,,"-100,000.00","1"\n'
        + '9/15/26,09:50:48,TRD,="2",SOLD -400 TSLA @250.50,-0.75,,"100,200.00","1"\n')
# The entry only, so the second file has an open position to extend.
PARTIAL = ("Account Statement\n\n" + CB
           + '9/15/26,09:46:16,TRD,="1",BOT +400 TSLA @250.00,,,"-100,000.00","1"\n')
# An option, which is the shape whose rows the parse rewrites itself.
OPT_OPEN = ("Account Statement\n\n" + CB
            + '9/14/26,10:00:00,TRD,="1",BOT +1 AAPL 100 15 MAY 26 190 CALL @1.50,,,"-150.00","1"\n')
OPT_CLOSE = ("Account Statement\n\n" + CB
             + '9/14/26,10:00:00,TRD,="1",BOT +1 AAPL 100 15 MAY 26 190 CALL @1.50,,,"-150.00","1"\n'
             + '9/15/26,11:00:00,TRD,="2",SOLD -1 AAPL 100 15 MAY 26 190 CALL @3.00,,-0.70,"300.00","1"\n')
GROUP = "9/15/26_TSLA_STOCK_1"
OPT_GROUP = "9/14/26_AAPL_OPTION_2026-05-15_190_CALL_1"


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
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO accounts (id,name,type) VALUES (1,'Day','day_trading')")
        conn.commit()
        conn.close()
        c.db = str(db)
        yield c


def do_import(client, content, broker="thinkorswim"):
    r = client.post("/api/import-csv", data={"account_id": "1", "broker": broker},
                    files={"file": ("s.csv", content.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] >= 0
    return body


def row(client, group=GROUP):
    import sqlite3
    conn = sqlite3.connect(client.db)
    r = conn.execute(
        "SELECT id, net_pnl, executions FROM trades WHERE trade_group=?", (group,)
    ).fetchone()
    conn.close()
    if r is None:
        return None
    return {"id": r[0], "net_pnl": r[1],
            "fills": len(json.loads(r[2]))}


def batches(client):
    r = client.get("/api/import-batches", params={"account_id": "1"})
    assert r.status_code == 200, r.text
    return r.json()["batches"]


def undo(client, batch_id):
    r = client.post(f"/api/import-batches/{batch_id}/undo")
    return r


# ── recording ───────────────────────────────────────────────────────────────

def test_an_import_records_a_batch_with_a_snapshot(client):
    body = do_import(client, STMT)
    assert body["batch_id"] is not None, body
    listed = batches(client)
    assert listed[0]["id"] == body["batch_id"]
    assert listed[0]["imported"] == 1
    assert listed[0]["groups"] == 1, listed[0]
    assert listed[0]["undoable"] is True
    # The snapshot is not shipped in the list.
    assert "snapshot" not in listed[0]


def test_a_duplicate_import_records_no_batch(client):
    """A file imported twice creates nothing, so there is nothing to undo."""
    first = do_import(client, STMT)
    second = do_import(client, STMT)
    assert second["imported"] == 0
    assert second["batch_id"] is None, "a no-op must not add an undoable row"
    assert len(batches(client)) == 1, batches(client)
    assert first["batch_id"] is not None


def test_a_batch_snapshots_only_the_rows_the_import_touched(client):
    """A journal full of unrelated trades must not ride along in the snapshot."""
    # Unrelated MT5-style rows the import never touches.
    import sqlite3
    conn = sqlite3.connect(client.db)
    for i in range(25):
        conn.execute(
            "INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, "
            "side, gross_pnl, net_pnl, executions, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (1, f"MT5_EURUSD_{i}", "2026-09-10", "EURUSD", "FX", "LONG", 0.0, 0.0, "[]", "mt5"))
    conn.commit()
    conn.close()

    body = do_import(client, STMT)
    listed = batches(client)
    assert listed[0]["id"] == body["batch_id"]
    assert listed[0]["groups"] == 1, f"only the imported group: {listed[0]}"


# ── undo ────────────────────────────────────────────────────────────────────

def test_undo_removes_the_trade_it_created(client):
    do_import(client, STMT)
    assert row(client) is not None
    batch_id = batches(client)[0]["id"]

    r = undo(client, batch_id)
    assert r.status_code == 200, r.text
    assert row(client) is None, "the created trade should be gone"
    assert r.json()["groups_removed"] == 1


def test_undo_restores_the_row_it_changed(client):
    """The part a delete-only undo would get wrong: a row that was *updated*."""
    do_import(client, PARTIAL)
    before = row(client)
    assert before["fills"] == 1 and before["net_pnl"] == 0.0

    do_import(client, STMT)
    assert row(client)["fills"] == 2 and row(client)["net_pnl"] == 199.25
    batch_id = batches(client)[0]["id"]

    r = undo(client, batch_id)
    assert r.status_code == 200, r.text
    after = row(client)
    assert after is not None, "an updated row must be restored, not deleted"
    assert after["fills"] == 1, after
    assert after["net_pnl"] == 0.0, after
    assert after["id"] == before["id"], "and back on its original row"


def test_undo_is_refused_twice(client):
    """The snapshot has been spent; a second undo would delete without restoring."""
    do_import(client, STMT)
    batch_id = batches(client)[0]["id"]
    assert undo(client, batch_id).status_code == 200
    r = undo(client, batch_id)
    assert r.status_code == 400, r.text
    assert "already been undone" in r.json()["error"], r.text


def test_an_older_batch_refuses_while_a_newer_one_stands(client):
    """Restoring an older picture over a newer import would discard it."""
    first = do_import(client, PARTIAL)["batch_id"]
    second = do_import(client, STMT)["batch_id"]
    assert first != second

    r = undo(client, first)
    assert r.status_code == 400, r.text
    assert "Undo that one first" in r.json()["error"], r.text
    # The newer one is still fine, and once it goes the older is too.
    assert undo(client, second).status_code == 200
    r = undo(client, first)
    assert r.status_code == 200, r.text
    assert row(client) is None


def test_undo_removes_analysis_tags_and_attachments_written_after_the_import(client):
    """Restore what was there, remove what was not — both halves of one rule.

    Undo deletes every row keyed to the groups it touches, then re-inserts the
    snapshot. Anything added after the import (an AI analysis, a tag, an
    attachment) is therefore gone with the trade it describes: an analysis
    pointing at a trade_group that no longer exists is the orphan this journal
    has been bitten by before. Scope is journal data only, per the decision on
    this feature — diary entries and bars are separate workflows and untouched.
    """
    import sqlite3
    do_import(client, STMT)
    batch_id = batches(client)[0]["id"]

    conn = sqlite3.connect(client.db)
    conn.execute(
        "INSERT INTO trade_analysis (trade_group, ticker, date, strategy) VALUES (?,?,?,?)",
        (GROUP, "TSLA", "2026-09-15", "Reclaim"))
    conn.execute(
        "INSERT INTO trade_tags (trade_group, tag_type, tag_value, source) VALUES (?,?,?,?)",
        (GROUP, "setup", "VWAP", "manual"))
    conn.execute(
        "INSERT INTO trade_attachments (trade_group, original_name, stored_name, size_bytes) "
        "VALUES (?,?,?,?)",
        (GROUP, "chart.png", "abc123.png", 2048))
    conn.commit()
    conn.close()

    r = undo(client, batch_id)
    assert r.status_code == 200, r.text
    conn = sqlite3.connect(client.db)
    left = [conn.execute("SELECT COUNT(*) FROM trade_analysis WHERE trade_group=?", (GROUP,)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM trade_tags WHERE trade_group=?", (GROUP,)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM trade_attachments WHERE trade_group=?", (GROUP,)).fetchone()[0]]
    conn.close()
    assert left == [0, 0, 0], f"nothing may survive the trade it belongs to: {left}"
    assert row(client) is None


def test_undo_does_not_delete_rows_of_a_trade_it_left_alone(client):
    """The counterpart: a group the import never touched keeps everything."""
    import sqlite3
    # An unrelated row with its own analysis, present before the import.
    conn = sqlite3.connect(client.db)
    conn.execute(
        "INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, "
        "side, gross_pnl, net_pnl, executions, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1, "9/10/26_NVDA_STOCK_1", "2026-09-10", "NVDA", "STOCK", "LONG", 5.0, 5.0, "[]", "mt5"))
    conn.execute(
        "INSERT INTO trade_analysis (trade_group, ticker, date, strategy) VALUES (?,?,?,?)",
        ("9/10/26_NVDA_STOCK_1", "NVDA", "2026-09-10", "Keep"))
    conn.commit()
    conn.close()

    do_import(client, STMT)
    batch_id = batches(client)[0]["id"]
    assert undo(client, batch_id).status_code == 200

    conn = sqlite3.connect(client.db)
    other = conn.execute("SELECT COUNT(*) FROM trades WHERE trade_group='9/10/26_NVDA_STOCK_1'").fetchone()[0]
    note = conn.execute("SELECT COUNT(*) FROM trade_analysis WHERE trade_group='9/10/26_NVDA_STOCK_1'").fetchone()[0]
    conn.close()
    assert (other, note) == (1, 1), f"unrelated rows were caught in the undo: {other}, {note}"


def test_undoing_an_unknown_batch_is_a_400_not_a_500(client):
    r = undo(client, 4242)
    assert r.status_code == 400, r.text
    assert "not found" in r.json()["error"], r.text


def test_undo_leaves_other_accounts_alone(client):
    """The group key is account-scoped: an import for account 2 must be safe."""
    import sqlite3
    conn = sqlite3.connect(client.db)
    conn.execute("INSERT INTO accounts (id,name,type) VALUES (2,'Swing','swing_trading')")
    conn.execute(
        "INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, "
        "side, gross_pnl, net_pnl, executions, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (2, GROUP, "2026-09-15", "TSLA", "STOCK", "LONG", 1.0, 1.0, "[]", "mt5"))
    conn.commit()
    conn.close()

    do_import(client, STMT)
    batch_id = batches(client)[0]["id"]
    assert undo(client, batch_id).status_code == 200

    conn = sqlite3.connect(client.db)
    other = conn.execute("SELECT COUNT(*) FROM trades WHERE account_id=2").fetchone()[0]
    conn.close()
    assert other == 1, "account 2's identical trade_group must be untouched"


# ── the snapshot has to be the *original* row ───────────────────────────────

def test_undo_of_an_absorbed_option_restores_the_row_the_parse_rewrote(client):
    """Parse writes before anyone knows the group list — so capture it first.

    Without the pre-parse stage the snapshot holds the merged row (1 fill,
    149.30) instead of the original (1 fill, 0.00), and undo would restore the
    import's own work rather than undoing it.
    """
    do_import(client, OPT_OPEN)
    before = row(client, OPT_GROUP)
    assert before["fills"] == 1 and before["net_pnl"] == 0.0

    do_import(client, OPT_CLOSE)
    assert row(client, OPT_GROUP)["fills"] == 2, "the parse absorbed the close"
    batch_id = batches(client)[0]["id"]

    r = undo(client, batch_id)
    assert r.status_code == 200, r.text
    after = row(client, OPT_GROUP)
    assert after is not None, after
    assert after["fills"] == 1, f"restored the merged row, not the original: {after}"
    assert after["net_pnl"] == 0.0, after


def test_batch_listing_needs_an_account(client):
    r = client.get("/api/import-batches", params={"account_id": 999})
    assert r.status_code in (400, 404), r.text
