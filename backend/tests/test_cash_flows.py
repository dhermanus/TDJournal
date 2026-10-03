"""Cash flows and the equity ratios end to end, item 13a.

    cd backend && python -m pytest tests/test_cash_flows.py -q

Covers the whole path the pure test in test_equity.py deliberately avoids: the
schema column, the CRUD endpoints, the validation that keeps the denominator
meaningful, and the KPI response that reports ratios as null when nobody has
said what the account is worth.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

STMT = ("Account Statement\n\n"
        "Cash Balance\nDATE,TIME,TYPE,REF #,DESCRIPTION,Misc Fees,Commissions & Fees,AMOUNT,BALANCE\n"
        '9/15/26,09:46:16,TRD,="1",BOT +400 TSLA @250.00,,,"-100,000.00","1"\n'
        '9/15/26,09:50:48,TRD,="2",SOLD -400 TSLA @250.50,-0.75,,"100,200.00","1"\n')


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "journal.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    for name in ("database", "main"):
        sys.modules.pop(name, None)
    import database
    database.DB_PATH = str(db)
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.db = str(db)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO accounts (id,name,type,color,broker) "
                     "VALUES (1,'Day','day_trading','#6366f1','Thinkorswim')")
        conn.commit()
        conn.close()
        yield c


def test_schema_gives_accounts_a_capital_column_and_a_flows_table(client):
    conn = sqlite3.connect(client.db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(accounts)")]
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    conn.close()
    assert "starting_capital" in cols, cols
    assert "account_cash_flows" in tables, tables


def test_capital_is_null_until_somebody_sets_it(client):
    r = client.get("/api/accounts")
    assert r.status_code == 200, r.text
    assert r.json()[0]["starting_capital"] is None, "unknown, not 0"


def test_setting_and_clearing_starting_capital(client):
    r = client.put("/api/accounts/1", json={"starting_capital": 10000})
    assert r.status_code == 200, r.text
    assert r.json()["starting_capital"] == 10000

    # Null is expressible: clearing it back to unknown is the point of null, so
    # it must not be filtered out as "no value given".
    r = client.put("/api/accounts/1", json={"starting_capital": None})
    assert r.status_code == 200, r.text
    assert r.json()["starting_capital"] is None

    # A negative balance has no meaning against these ratios.
    r = client.put("/api/accounts/1", json={"starting_capital": -5})
    assert r.status_code == 400, r.text
    assert "0 or more" in r.json()["error"], r.text


def test_negative_capital_on_create_is_refused(client):
    r = client.post("/api/accounts", json={"name": "Bad", "type": "swing_trading",
                                           "starting_capital": -1})
    assert r.status_code == 400, r.text


def test_cash_flow_crud(client):
    r = client.post("/api/accounts/1/cash-flows",
                    json={"kind": "deposit", "amount": 5000, "flow_date": "2026-01-15"})
    assert r.status_code == 201, r.text
    flow_id = r.json()["id"]
    assert r.json()["kind"] == "deposit"

    r = client.post("/api/accounts/1/cash-flows",
                    json={"kind": "withdrawal", "amount": 1000, "flow_date": "2026-02-01"})
    assert r.status_code == 201, r.text

    listed = client.get("/api/accounts/1/cash-flows").json()
    assert [f["kind"] for f in listed["flows"]] == ["deposit", "withdrawal"]
    assert listed["net_flows"] == 4000

    r = client.put(f"/api/accounts/1/cash-flows/{flow_id}", json={"amount": 6000})
    assert r.status_code == 200 and r.json()["amount"] == 6000
    assert client.get("/api/accounts/1/cash-flows").json()["net_flows"] == 5000

    r = client.delete(f"/api/accounts/1/cash-flows/{flow_id}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get("/api/accounts/1/cash-flows").json()["net_flows"] == -1000

    assert client.delete(f"/api/accounts/1/cash-flows/{flow_id}").status_code == 404


def test_cash_flow_validation(client):
    cases = [
        ({"kind": "shrinkage", "amount": 10, "flow_date": "2026-01-01"}, "deposit"),
        ({"kind": "deposit", "amount": 0, "flow_date": "2026-01-01"}, "greater than 0"),
        ({"kind": "deposit", "amount": -5, "flow_date": "2026-01-01"}, "greater than 0"),
        ({"kind": "deposit", "amount": 5, "flow_date": "2026-02-30"}, "real calendar"),
        ({"kind": "deposit", "amount": 5, "flow_date": "15/01/2026"}, "YYYY-MM-DD"),
        ({"kind": "deposit", "amount": 5, "flow_date": "../x"}, "YYYY-MM-DD"),
    ]
    for body, fragment in cases:
        r = client.post("/api/accounts/1/cash-flows", json=body)
        assert r.status_code == 400, f"{body} -> {r.status_code} {r.text}"
        assert fragment in r.json()["error"], f"{body} -> {r.json()}"


def test_cash_flows_are_scoped_to_their_account(client):
    conn = sqlite3.connect(client.db)
    conn.execute("INSERT INTO accounts (id,name,type) VALUES (2,'Swing','swing_trading')")
    conn.commit()
    conn.close()
    client.post("/api/accounts/1/cash-flows",
                json={"kind": "deposit", "amount": 100, "flow_date": "2026-01-01"})
    # Account 2 sees its own (empty) ledger, and cannot touch account 1's row.
    assert client.get("/api/accounts/2/cash-flows").json()["flows"] == []
    assert client.get("/api/accounts/999/cash-flows").status_code == 404
    assert client.post("/api/accounts/999/cash-flows",
                       json={"kind": "deposit", "amount": 1,
                             "flow_date": "2026-01-01"}).status_code == 404


def _import(client):
    r = client.post("/api/import-csv", data={"account_id": "1", "broker": "thinkorswim"},
                    files={"file": ("s.csv", STMT.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def test_kpis_report_equity_ratios_once_capital_is_known(client):
    _import(client)                                   # +199.25, no drawdown
    before = client.get("/api/kpis").json()
    assert before["total_net_pnl"] == pytest.approx(199.25)
    assert before["return_pct"] is None, "no capital given -> no percentage"
    assert before["max_drawdown_pct"] is None
    assert before["starting_capital"] is None
    assert before["equity_base"] is None

    client.put("/api/accounts/1", json={"starting_capital": 10000})
    client.post("/api/accounts/1/cash-flows",
                json={"kind": "deposit", "amount": 2000, "flow_date": "2026-01-01"})
    after = client.get("/api/kpis").json()
    assert after["starting_capital"] == 10000
    assert after["net_flows"] == 2000
    assert after["equity_base"] == 12000
    assert after["return_pct"] == pytest.approx(round(199.25 / 12000 * 100, 2))
    assert after["max_drawdown_pct"] == pytest.approx(0.0)


def test_all_accounts_view_totals_the_capital_of_every_account(client):
    """The consolidated view needs a total across accounts, or none at all."""
    _import(client)
    conn = sqlite3.connect(client.db)
    conn.execute("INSERT INTO accounts (id,name,type) VALUES (2,'Swing','swing_trading')")
    conn.commit()
    conn.close()
    client.put("/api/accounts/1", json={"starting_capital": 5000})

    # account 2 has no figure yet, so the total is unknown rather than 5000 —
    # a percentage of a total that silently omits an account understates risk.
    k = client.get("/api/kpis").json()                 # account_id omitted
    assert k["total_net_pnl"] == pytest.approx(199.25)
    assert k["equity_base"] is None, k["equity_base"]
    assert k["return_pct"] is None
    assert k["unknown_accounts"] == 1, "the reason, so the UI can say it"

    # Price the second account and the consolidated ratios appear.
    client.put("/api/accounts/2", json={"starting_capital": 15000})
    client.post("/api/accounts/2/cash-flows",
                json={"kind": "deposit", "amount": 5000, "flow_date": "2026-01-01"})
    k = client.get("/api/kpis").json()
    assert k["unknown_accounts"] == 0
    assert k["equity_base"] == 25000, "5000 + 15000 + 5000"
    assert k["return_pct"] == pytest.approx(round(199.25 / 25000 * 100, 2))
