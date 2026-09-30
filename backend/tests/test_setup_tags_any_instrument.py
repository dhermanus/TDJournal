"""Setups are tagged on every instrument type, not just stocks.

    cd backend && python -m pytest tests -q

The list view used to grey out the Setup cell for anything that was not a stock,
and setup_stats filtered on instrument_type='STOCK'. With MT5 importing FX,
METAL and INDEX trades, that gate meant forex trades could be neither tagged
nor counted — so the Strategies breakdown and the row editor disagreed.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import instruments  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
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
        yield c


def seed(client, instrument_type, setup=None, pnl=100.0, ticker="EURUSD"):
    """One trade, identified by type + setup so a reseed in the same run replaces it."""
    group = f"{ticker}_{instrument_type}_{setup or 'none'}"
    conn = sqlite3.connect(client.db)
    if not conn.execute("SELECT 1 FROM accounts WHERE id=1").fetchone():
        conn.execute("INSERT INTO accounts (id, name, type) VALUES (1, 'Day', 'day_trading')")
    conn.execute("DELETE FROM trades WHERE trade_group=?", (group,))
    conn.execute(
        "INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, "
        "side, net_pnl, gross_pnl, setup) VALUES (1, ?, '2026-09-15', ?, ?, 'LONG', ?, ?, ?)",
        (group, ticker, instrument_type, pnl, pnl, setup),
    )
    conn.commit()
    conn.close()


def test_every_mt5_type_is_counted_by_setup_stats(client):
    """No instrument filter: an FX trade tagged with a setup must show up."""
    for t in instruments.INSTRUMENT_TYPES:
        seed(client, t, setup="London breakout", pnl=100.0, ticker=t[:3])

    body = client.get("/api/setups").json()
    by_setup = {row["k"]: row for row in body["by_setup"]}
    assert "London breakout" in by_setup
    # Six trades tagged, one per canonical type.
    assert by_setup["London breakout"]["n"] == len(instruments.INSTRUMENT_TYPES)


def test_an_untagged_fx_trade_is_still_ignored(client):
    """Filtering widened, not vanished — setups with no tag stay out."""
    seed(client, "FX", setup=None, pnl=250.0)
    body = client.get("/api/setups").json()
    assert body["by_setup"] == []
    assert body["by_grade"] == []


def test_setup_taking_accepts_an_fx_trade_id(client):
    """The same endpoint the list view calls to tag a row.

    Follows the real sequence: a setup has to exist in the playbook before a
    trade can be tagged with it.
    """
    seed(client, "FX", setup=None, pnl=250.0)
    created = client.post("/api/setups/custom", json={"name": "Liquidity sweep"})
    assert created.status_code in (200, 201), created.text

    conn = sqlite3.connect(client.db)
    trade_id = conn.execute(
        "SELECT id FROM trades WHERE instrument_type='FX'"
    ).fetchone()[0]
    conn.close()

    r = client.patch(f"/api/trades/{trade_id}/setup", json={"setup": "Liquidity sweep"})
    assert r.status_code == 200, r.text

    conn = sqlite3.connect(client.db)
    assert conn.execute(
        "SELECT setup FROM trades WHERE id=?", (trade_id,)
    ).fetchone()[0] == "Liquidity sweep"
    conn.close()
