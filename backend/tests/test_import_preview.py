"""The dry-run preview, item 4b.

    cd backend && python -m pytest tests/test_import_preview.py -q

Two properties, and the second one is the whole point of the endpoint:

1. It reports what an import would do — create, update, replace, skip.
2. It writes nothing. Any of them.

(2) is hard here because parsing is not read-only: `build_trades_from_executions`
commits an UPDATE when new fills land on an open option position, measured on a
live copy as a row going from `net_pnl 0.0 / 1 fill` to `149.3 / 2 fills` before
the import returned a single trade to insert. A savepoint cannot contain that
(the parser calls `conn.commit()`, which leaves the savepoint), so the preview
parses against a copy of the database instead.

The classification is checked against a real import too, because the endpoint's
own counter cannot distinguish create from update: the insert is
`ON CONFLICT DO UPDATE` and counts both the same.
"""
import hashlib
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
# Just the entry of that trip, so the stored position is still open when the
# full statement arrives and parsing has to merge rather than skip.
PARTIAL = ("Account Statement\n\n" + CB
           + '9/15/26,09:46:16,TRD,="1",BOT +400 TSLA @250.00,,,"-100,000.00","1"\n')
# An option position opened and then closed by a later file. This is the shape
# that makes parsing write: the absorb-and-commit path only runs for options.
OPT_OPEN = ("Account Statement\n\n" + CB
            + '9/14/26,10:00:00,TRD,="1",BOT +1 AAPL 100 15 MAY 26 190 CALL @1.50,,,"-150.00","1"\n')
OPT_CLOSE = ("Account Statement\n\n" + CB
             + '9/14/26,10:00:00,TRD,="1",BOT +1 AAPL 100 15 MAY 26 190 CALL @1.50,,,"-150.00","1"\n'
             + '9/15/26,11:00:00,TRD,="2",SOLD -1 AAPL 100 15 MAY 26 190 CALL @3.00,,-0.70,"300.00","1"\n')
BAD_ROW = ("Account Statement\n\n" + CB
           + '9/15/26,09:46:16,TRD,="1",BOT +400 TSLA @250.00,,,"-100,000.00","1"\n'
           + '9/15/26,09:47:00,TRD,="2",????WATTHELL,,,,,"0.00","1"\n')


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


def post(client, content, path="/api/import-csv/preview", broker="thinkorswim"):
    r = client.post(path, data={"account_id": "1", "broker": broker},
                    files={"file": ("s.csv", content.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def do_import(client, content, broker="thinkorswim"):
    """The real, writing import. `post` previews by default, on purpose."""
    return post(client, content, path="/api/import-csv", broker=broker)


def md5(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def stored(client):
    conn = __import__("sqlite3").connect(client.db)
    rows = conn.execute(
        "SELECT trade_group, net_pnl, executions FROM trades ORDER BY trade_group"
    ).fetchall()
    conn.close()
    return [(r[0], r[1], len(json.loads(r[2]))) for r in rows]


# ── what it reports ─────────────────────────────────────────────────────────

def test_preview_of_an_empty_journal_reports_new_trades(client):
    body = post(client, STMT)
    assert body["create_count"] == 1, body
    assert body["update_count"] == 0
    assert body["create"] == ["9/15/26_TSLA_STOCK_1"]
    assert body["net_pnl"] == pytest.approx(199.25)
    assert body["nothing_to_do"] is False
    assert stored(client) == [], "preview must not have written anything"


def test_a_second_preview_of_an_open_position_says_update_not_create(client):
    do_import(client, PARTIAL)
    body = post(client, STMT)
    assert body["update"] == ["9/15/26_TSLA_STOCK_1"], body
    assert body["create_count"] == 0
    assert body["update_count"] == 1
    assert "1 to update" in body["message"], body["message"]


def test_the_prediction_matches_what_the_import_actually_does(client):
    """The counts are only useful if they survive being checked against reality."""
    do_import(client, PARTIAL)
    predicted = post(client, STMT)
    imported = do_import(client, STMT)
    assert imported["imported"] == 1
    assert predicted["update_count"] == 1
    assert predicted["create_count"] == 0
    assert stored(client) == [("9/15/26_TSLA_STOCK_1", 199.25, 2)], "one row, updated"


def test_previewing_an_already_imported_file_reports_nothing_to_do(client):
    """`skipped` counts fills, not trades: a re-import skips 2 and does nothing.

    Deciding `nothing_to_do` on the skips made a second preview report work it
    would not do, which is the exact question a preview exists to answer.
    """
    do_import(client, STMT)
    before = md5(client.db)
    body = post(client, STMT)
    assert body["nothing_to_do"] is True, body
    assert body["create_count"] == 0 and body["update_count"] == 0
    assert body["skipped"] == 2, body
    assert "already in the journal" in body["message"], body["message"]
    assert md5(client.db) == before


def test_line_errors_reach_the_preview_too(client):
    body = post(client, BAD_ROW)
    assert len(body["line_errors"]) == 1, body
    assert body["line_errors"][0].startswith("line "), body["line_errors"]
    assert "not imported" in body["message"], body["message"]
    assert stored(client) == []


def _letters(i: int) -> str:
    """A letters-only ticker: the stock description parser rejects a digit."""
    out = ""
    n = i
    while True:
        out = chr(ord("A") + n % 26) + out
        n //= 26
        if n == 0:
            break
    return out


def test_the_preview_caps_the_lists_but_not_the_counts(client):
    """A long statement must not ship every trade_group name to render."""
    rows = ["Account Statement\n\n" + CB]
    for i in range(120):
        day = 1 + (i // 40)
        sym = _letters(i)
        rows.append(f'9/{day:02d}/26,09:{i % 60:02d}:00,TRD,="b{i}",'
                    f'BUY +10 {sym} @10.00,,,,"-100.00","1"\n')
        rows.append(f'9/{day:02d}/26,10:{i % 60:02d}:00,TRD,="s{i}",'
                    f'SOLD -10 {sym} @11.00,,-1.00,"110.00","1"\n')
    body = post(client, "".join(rows))
    assert body["create_count"] > 50, f"fixture must exceed the cap, got {body['create_count']}"
    assert len(body["create"]) == 50, len(body["create"])
    assert body["create_count"] > len(body["create"]), "counts must stay uncapped"


# ── it writes nothing ───────────────────────────────────────────────────────

def test_preview_writes_nothing_to_the_live_journal(client):
    before = md5(client.db)
    post(client, STMT)
    assert md5(client.db) == before, "the preview changed the journal"
    assert stored(client) == []


def test_the_parse_mutation_does_not_reach_the_live_journal(client):
    """Parse commits on its own — the copy is what absorbs that.

    This is the case a transaction-level dry run gets wrong: the parse updates a
    stored row *before* any trade is returned, so rolling back the endpoint's own
    work would be too late.
    """
    do_import(client, OPT_OPEN)
    assert stored(client)[0][2] == 1, stored(client)

    before = md5(client.db)
    post(client, OPT_CLOSE)        # closes it -> absorb-and-commit path in parse
    assert md5(client.db) == before, "the parse's UPDATE escaped to the journal"
    assert stored(client)[0][2] == 1, (
        "the live position must still be the one the import stored, "
        f"not the parse's merged copy: {stored(client)}"
    )


def test_preview_still_writes_nothing_after_an_update_is_predicted(client):
    """The prediction path and the no-write path, together."""
    do_import(client, OPT_OPEN)
    before = md5(client.db)
    body = post(client, OPT_CLOSE)
    assert body["update_count"] == 1, body
    assert md5(client.db) == before
    assert stored(client)[0][2] == 1


# ── hygiene ─────────────────────────────────────────────────────────────────

def test_preview_needs_a_real_account(client):
    r = client.post("/api/import-csv/preview", data={"account_id": "999", "broker": "thinkorswim"},
                    files={"file": ("s.csv", STMT.encode(), "text/csv")})
    assert r.status_code in (400, 404), r.text


def test_preview_rejects_a_non_csv(client):
    r = client.post("/api/import-csv/preview", data={"account_id": "1"},
                    files={"file": ("s.txt", b"hello", "text/plain")})
    assert r.status_code == 400, r.text
    assert "csv" in r.json()["error"].lower(), r.text


def test_no_preview_temp_files_are_left_behind(client, tmp_path, monkeypatch):
    """The copy is thrown away on the success path *and* after a raise.

    The second half matters on Windows: an open handle keeps the file locked, so
    a connection left open would both leak the file and make the unlink fail.
    """
    import tempfile
    import main
    store = tmp_path / "preview-tmp"
    store.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(store))

    post(client, STMT)
    assert list(store.iterdir()) == [], list(store.iterdir())

    # A failure raised from inside the copy, i.e. after mkstemp and backup.
    real = main.parse_broker_csv
    monkeypatch.setattr(main, "parse_broker_csv",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("parser exploded")))
    with pytest.raises(RuntimeError, match="parser exploded"):
        post(client, STMT)
    monkeypatch.setattr(main, "parse_broker_csv", real)

    leftovers = [p for p in store.iterdir() if p.name.startswith("tdjournal-preview")]
    assert leftovers == [], leftovers
