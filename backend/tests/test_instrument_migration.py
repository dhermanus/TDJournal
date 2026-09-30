"""Rebuilding trades so its CHECK accepts every instrument type.

    cd backend && python -m pytest tests -q

SQLite cannot ALTER a table constraint, so widening it means copying the table.
That rewrite can lose rows, indexes, or the UNIQUE conflict target imports rely
on without raising — hence the live checks below rather than a dry read of the
DDL builder.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import database  # noqa: E402
import instruments  # noqa: E402


# The trades table exactly as the project shipped it, before FX existed.
LEGACY_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    trade_group TEXT NOT NULL,
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    instrument_type TEXT NOT NULL CHECK(instrument_type IN ('STOCK','OPTION','FUTURE')),
    side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
    gross_pnl REAL,
    net_pnl REAL,
    commissions REAL DEFAULT 0,
    executions TEXT NOT NULL DEFAULT '[]',
    option_expiry TEXT,
    option_strike REAL,
    option_type TEXT CHECK(option_type IN ('CALL','PUT',NULL)),
    source TEXT NOT NULL DEFAULT 'imported',
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(trade_group, account_id)
);
CREATE INDEX idx_trades_account_date ON trades(account_id, date);
CREATE INDEX idx_trades_group ON trades(trade_group);
"""

# Columns added by the plain ALTER migrations over time — a rebuild must carry
# these across too, they are not part of the base CREATE.
ALTERED_COLUMNS = [
    "ALTER TABLE trades ADD COLUMN setup TEXT",
    "ALTER TABLE trades ADD COLUMN setup_grade TEXT",
    "ALTER TABLE trades ADD COLUMN mfe_pct REAL",
]

SEED_TRADES = [
    # (trade_group, ticker, instrument_type, setup, mfe)
    ("g1", "TSLA", "STOCK", "VWAP Reclaim", 0.85),
    ("g2", "SPY", "OPTION", None, None),
]


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A database carrying the old three-type constraint, some data, and an FK."""
    path = tmp_path / "journal.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('day_trading','swing_trading','investment')),
            color TEXT NOT NULL DEFAULT '#6366f1',
            broker TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.executescript(LEGACY_SCHEMA)
    for ddl in ALTERED_COLUMNS:
        conn.execute(ddl)
    conn.execute("INSERT INTO accounts (name, type) VALUES ('Day Trading', 'day_trading')")
    conn.executemany(
        "INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, "
        "side, gross_pnl, setup, mfe_pct) VALUES (1, ?, '2026-01-15', ?, ?, 'LONG', 42.5, ?, ?)",
        [(g, t, i, s, m) for g, t, i, s, m in SEED_TRADES],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_PATH", str(path))
    return path


def accepted_types(path):
    conn = sqlite3.connect(path)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
        return database.instrument_check_values(sql[0]) if sql else None
    finally:
        conn.close()


def test_the_legacy_schema_actually_rejected_an_mt5_asset(legacy_db):
    """Establish the precondition, so a green run cannot be a vacuous pass."""
    conn = sqlite3.connect(legacy_db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO trades (account_id, trade_group, date, ticker, "
            "instrument_type, side) VALUES (1, 'x', '2026-01-15', 'EURUSD', 'FX', 'LONG')"
        )
    conn.close()


def test_migrate_widens_the_check_to_every_canonical_type(legacy_db):
    database.init_db()
    assert accepted_types(legacy_db) == set(instruments.INSTRUMENT_TYPES)


def test_migrate_keeps_every_stored_row_and_its_values(legacy_db):
    database.init_db()
    conn = sqlite3.connect(legacy_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT trade_group, ticker, instrument_type, setup, mfe_pct FROM trades ORDER BY id"
    ).fetchall()
    conn.close()
    assert [tuple(r) for r in rows] == SEED_TRADES


def test_migrate_keeps_indexes_and_the_trade_group_conflict_target(legacy_db):
    database.init_db()
    conn = sqlite3.connect(legacy_db)
    indexes = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trades'"
        )
    }
    assert {"idx_trades_account_date", "idx_trades_group"} <= indexes

    # The import path upserts on this target; losing it duplicates on re-import.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO trades (account_id, trade_group, date, ticker, "
            "instrument_type, side) VALUES (1, 'g1', '2026-01-15', 'TSLA', 'STOCK', 'LONG')"
        )
    conn.close()


def test_every_mt5_type_inserts_after_migration_and_bogus_ones_still_fail(legacy_db):
    database.init_db()
    conn = sqlite3.connect(legacy_db)
    for n, t in enumerate(instruments.INSTRUMENT_TYPES):
        conn.execute(
            "INSERT INTO trades (account_id, trade_group, date, ticker, "
            "instrument_type, side) VALUES (1, ?, '2026-01-16', 'X', ?, 'LONG')",
            (f"t{n}", t),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO trades (account_id, trade_group, date, ticker, "
            "instrument_type, side) VALUES (1, 'bond', '2026-01-16', 'X', 'BOND', 'LONG')"
        )
    conn.close()


def test_migrate_leaves_no_temporary_table_behind(legacy_db):
    database.init_db()
    conn = sqlite3.connect(legacy_db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "trades" in tables
    assert not any(t.startswith("trades_migrated") for t in tables)


def test_migrate_is_idempotent_so_every_startup_is_not_a_rewrite(legacy_db):
    database.init_db()
    conn = sqlite3.connect(legacy_db)
    sql_first = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='trades'"
    ).fetchone()[0]
    rows_first = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    conn.close()

    database.init_db()

    conn = sqlite3.connect(legacy_db)
    sql_second = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='trades'"
    ).fetchone()[0]
    rows_second = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    conn.close()
    assert sql_first == sql_second
    assert rows_first == rows_second == len(SEED_TRADES)


def test_a_database_already_wide_enough_is_left_alone(tmp_path, monkeypatch):
    """A fresh install must not be rebuilt on its first run."""
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    database.init_db()
    assert accepted_types(path) == set(instruments.INSTRUMENT_TYPES)


def test_a_table_without_an_instrument_check_is_untouched(tmp_path, monkeypatch):
    """unconstrained accepts everything, so there is nothing to widen."""
    path = tmp_path / "nocheck.db"
    conn = sqlite3.connect(path)
    # Same columns as the real table, but the CHECK was never added.
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, "
                 "trade_group TEXT NOT NULL, date TEXT NOT NULL, ticker TEXT, "
                 "side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')), "
                 "instrument_type TEXT NOT NULL, UNIQUE(trade_group, account_id))")
    conn.execute("INSERT INTO trades (account_id, trade_group, date, ticker, instrument_type, side) "
                 "VALUES (1, 'g1', '2026-01-15', 'EURUSD', 'FX', 'LONG')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_PATH", str(path))
    database.init_db()

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT instrument_type FROM trades").fetchone()[0] == "FX"
    assert database.instrument_check_values(
        conn.execute("SELECT sql FROM sqlite_master WHERE name='trades'").fetchone()[0]
    ) is None
    conn.close()
