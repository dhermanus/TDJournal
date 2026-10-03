"""Market-condition tags, item 13b: a trade can record its regime.

    cd backend && python -m pytest tests/test_market_tags.py -q

The point of the type is the question Reports now answers — "do I lose on
gap-down opens?" — so the last test is the one that matters: two trades, opposite
outcomes, one condition each, read back as a bucket.

Manual, not inferred. This journal's bars are FX-only and it has no configured
price feed, so nothing here can compute a gap, a trend or relative volume; the
trader records the condition and the report breaks P&L down by it. See
`market` in library.TAG_TYPES.
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
        '9/15/26,09:50:48,TRD,="2",SOLD -400 TSLA @250.50,-0.75,,"100,200.00","1"\n'
        '9/16/26,09:46:16,TRD,="3",BOT +200 NVDA @100.00,,,"-20,000.00","1"\n'
        '9/16/26,09:50:48,TRD,="4",SOLD -200 NVDA @99.50,-0.50,,"19,900.00","1"\n')

# Deliberately the fixed strings a trader would type; the report buckets on the
# value, so nothing normalises them.
GAP_DOWN = "gap-down open"
GAP_UP = "gap-up open"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "journal.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    for name in ("database", "main", "library"):
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


def _import(client):
    r = client.post("/api/import-csv", data={"account_id": "1", "broker": "thinkorswim"},
                    files={"file": ("s.csv", STMT.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def _groups(client):
    conn = sqlite3.connect(client.db)
    rows = conn.execute("SELECT trade_group FROM trades ORDER BY trade_group").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _tag(client, group, value):
    r = client.post(f"/api/trades/{group}/tags",
                    json={"tag_type": "market", "tag_value": value})
    assert r.status_code == 201, r.text
    return r.json()


def test_market_is_a_known_tag_type_end_to_end(client):
    """Creating, listing and reading a market tag — through the public API."""
    _import(client)
    groups = _groups(client)
    assert len(groups) == 2, groups

    created = _tag(client, groups[0], GAP_DOWN)
    assert created.get("tag_type") == "market" or created.get("tag", {}).get("tag_type") == "market", created

    listed = client.get(f"/api/trades/{groups[0]}/analysis").json()
    tags = listed.get("tags", [])
    assert any(t["tag_type"] == "market" and t["tag_value"] == GAP_DOWN for t in tags), tags


def test_library_lists_market_tags_and_accepts_a_new_one(client):
    """The Settings tag manager works for the new type like any other."""
    _import(client)
    groups = _groups(client)
    _tag(client, groups[0], GAP_DOWN)

    lib = client.get("/api/library").json()
    assert "market" in lib["tag_types"], lib["tag_types"]
    names = [i["name"] for i in lib["tags"]["market"]]
    assert GAP_DOWN in names, names
    # and creating a brand-new value in that type is allowed, not a 400
    r = client.post("/api/library",
                    json={"kind": "tag", "tag_type": "market", "name": "relative volume spike"})
    assert r.status_code in (200, 201), r.text
    lib2 = client.get("/api/library").json()
    assert "relative volume spike" in [i["name"] for i in lib2["tags"]["market"]]


def test_an_unknown_tag_type_is_still_refused(client):
    """Adding a type must not accidentally open the door to arbitrary strings."""
    _import(client)
    r = client.post(f"/api/trades/{_groups(client)[0]}/tags",
                    json={"tag_type": "astrology", "tag_value": "mercury"})
    assert r.status_code == 400, r.text


def test_reports_breaks_pnl_down_by_market_condition(client):
    """The question the type exists for: do I lose on gap-down opens?"""
    _import(client)
    groups = _groups(client)
    assert len(groups) == 2
    _tag(client, groups[0], GAP_DOWN)     # the winner
    _tag(client, groups[1], GAP_UP)       # the loser

    r = client.get("/api/reports")
    assert r.status_code == 200, r.text
    by_tag = r.json()["by_tag"]
    assert "market" in by_tag, sorted(by_tag)
    rows = {row["key"]: row for row in by_tag["market"]}
    assert set(rows) == {GAP_DOWN, GAP_UP}, sorted(rows)

    # The two trips: +199.25 and -100.50 — hand-worked from the statement above.
    assert rows[GAP_DOWN]["net_pnl"] == pytest.approx(199.25)
    assert rows[GAP_UP]["net_pnl"] == pytest.approx(-100.50)
    assert rows[GAP_DOWN]["trades"] == 1 and rows[GAP_UP]["trades"] == 1
    assert rows[GAP_DOWN]["win_rate"] == 100.0
    assert rows[GAP_UP]["win_rate"] == 0.0


def test_an_untagged_trade_is_absent_not_counted_as_neutral(client):
    """Bucketing must not invent a row for trades that recorded no condition."""
    _import(client)
    _tag(client, _groups(client)[0], GAP_DOWN)
    by_tag = client.get("/api/reports").json()["by_tag"]
    assert [row["key"] for row in by_tag["market"]] == [GAP_DOWN]


def test_market_tags_survive_a_second_reports_read(client):
    """No caching layer may drop a tag added after the first report."""
    _import(client)
    before = client.get("/api/reports").json()["by_tag"].get("market", [])
    assert before == []
    _tag(client, _groups(client)[0], GAP_DOWN)
    after = client.get("/api/reports").json()["by_tag"].get("market", [])
    assert [r["key"] for r in after] == [GAP_DOWN], after
