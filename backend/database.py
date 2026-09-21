import sqlite3
import os
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DATABASE_PATH", "trading_journal.db")

# STOCK / OPTION / FUTURE come from the Thinkorswim, IBKR and generic importers.
# The rest come from the MetaTrader 5 importer, which sorts each symbol into a class
# from its MT5 category path (see csv_parser.classify_mt5_instrument).
INSTRUMENT_TYPES = (
    'STOCK', 'OPTION', 'FUTURE',
    'FOREX', 'METAL', 'INDEX', 'CRYPTO', 'COMMODITY', 'SHARE_CFD', 'OTHER',
)


def trades_ddl(name: str = "trades", if_not_exists: bool = False) -> str:
    """CREATE TABLE statement for the trades table.

    Kept in one place so a fresh install and the one-time rebuild that widens the
    instrument_type CHECK (SQLite cannot alter a CHECK in place) build the same table.
    """
    types = ",".join(f"'{t}'" for t in INSTRUMENT_TYPES)
    guard = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
        CREATE TABLE {guard}{name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            trade_group TEXT NOT NULL,
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            instrument_type TEXT NOT NULL CHECK(instrument_type IN ({types})),
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
            setup TEXT,
            setup_grade TEXT,
            setup_notes TEXT,
            setup_features TEXT,
            setup_source TEXT DEFAULT 'auto',
            mfe_pct REAL,
            mae_pct REAL,
            exit_efficiency REAL,
            swap REAL DEFAULT 0,
            position_id TEXT,
            pips REAL,
            mfe_pips REAL,
            mae_pips REAL,
            UNIQUE(trade_group, account_id)
        )
    """


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('day_trading','swing_trading','investment')),
            color TEXT NOT NULL DEFAULT '#6366f1',
            broker TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            currency TEXT NOT NULL DEFAULT 'USD'
        );

        CREATE TABLE IF NOT EXISTS diary_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            entry_date TEXT NOT NULL,
            image_path TEXT,
            raw_text TEXT,
            ai_analysis TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS trade_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_group TEXT NOT NULL UNIQUE,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            strategy TEXT,
            stop_loss REAL,
            risk_per_trade REAL,
            risk_reward REAL,
            r_multiple REAL,
            entry_reason TEXT,
            exit_reason TEXT,
            mistakes TEXT,
            emotional_state TEXT,
            notes TEXT,
            ai_feedback TEXT,
            match_confidence TEXT CHECK(match_confidence IN ('high','medium','low','ambiguous','unmatched','manual')),
            match_notes TEXT,
            diary_entry_id INTEGER REFERENCES diary_entries(id)
        );

        CREATE TABLE IF NOT EXISTS trade_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_group TEXT NOT NULL,
            tag_type TEXT NOT NULL,
            tag_value TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('ai','manual'))
        );

        CREATE TABLE IF NOT EXISTS daily_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER REFERENCES accounts(id),
            summary_date TEXT NOT NULL,
            ai_content TEXT NOT NULL,
            generated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(summary_date, account_id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL DEFAULT 0,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE(account_id, key)
        );

        -- The trader's playbook: named setups used to tag trades.
        CREATE TABLE IF NOT EXISTS custom_setups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            side TEXT,                      -- LONG | SHORT | NULL (either)
            notes TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Per-account symbol details written by the MetaTrader 5 importer: the class the
        -- symbol was sorted into, its MT5 category path, digits, point size and contract size.
        CREATE TABLE IF NOT EXISTS symbol_specs (
            account_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            instrument_type TEXT NOT NULL,
            category_path TEXT,
            digits INTEGER,
            point REAL,
            contract_size REAL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (account_id, symbol)
        );

        CREATE INDEX IF NOT EXISTS idx_analysis_group ON trade_analysis(trade_group);
        CREATE INDEX IF NOT EXISTS idx_tags_group ON trade_tags(trade_group);
    """)

    cursor.execute(trades_ddl("trades", if_not_exists=True))
    _create_trades_indexes(conn)

    conn.commit()

    # Safe migrations — ignored if column already exists
    for ddl in [
        "ALTER TABLE trade_analysis ADD COLUMN target_price REAL",
        "ALTER TABLE trade_analysis ADD COLUMN trade_rating INTEGER",
        "ALTER TABLE trade_analysis ADD COLUMN idea_source TEXT",
        # Playbook setup tag + optional execution-quality grade
        "ALTER TABLE trades ADD COLUMN setup TEXT",           # playbook setup name, or NONE
        "ALTER TABLE trades ADD COLUMN setup_grade TEXT",     # A++ | A+ | A | B | C | D | F
        "ALTER TABLE trades ADD COLUMN setup_notes TEXT",     # JSON: free-form notes
        "ALTER TABLE trades ADD COLUMN setup_features TEXT",  # JSON: market state at entry
        # how the tag was set ('manual' in this demo; kept for compatibility)
        "ALTER TABLE trades ADD COLUMN setup_source TEXT DEFAULT 'auto'",
        # Trade-quality metrics measured from 1-min bars over the hold window.
        # MFE = best unrealised gain reached, MAE = worst unrealised loss reached,
        # exit_efficiency = realised / MFE, i.e. share of the move captured.
        "ALTER TABLE trades ADD COLUMN mfe_pct REAL",
        "ALTER TABLE trades ADD COLUMN mae_pct REAL",
        "ALTER TABLE trades ADD COLUMN exit_efficiency REAL",
        # MetaTrader 5 / FX support. swap is signed (negative = cost); pips holds pips for
        # FOREX and points for the other MT5 classes; position_id is the MT5 position ticket.
        "ALTER TABLE trades ADD COLUMN swap REAL DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN position_id TEXT",
        "ALTER TABLE trades ADD COLUMN pips REAL",
        "ALTER TABLE trades ADD COLUMN mfe_pips REAL",
        "ALTER TABLE trades ADD COLUMN mae_pips REAL",
        # Every MT5 account has one deposit currency; all its P&L is reported in it.
        "ALTER TABLE accounts ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'",
    ]:
        try:
            conn.execute(ddl)
            conn.commit()
        except Exception:
            pass

    # SQLite cannot widen a CHECK constraint, so a database created before FX support
    # gets its trades table rebuilt once (after a backup) to allow the new types.
    _widen_trades_instrument_check(conn)

    conn.close()


def _create_trades_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_account_date ON trades(account_id, date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_group ON trades(trade_group)")


def _backup_database(conn: sqlite3.Connection) -> str | None:
    """Copy the live database next to itself before a structural change."""
    if DB_PATH in ("", ":memory:"):
        return None
    path = f"{DB_PATH}.pre-fx-{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak"
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return path


def _widen_trades_instrument_check(conn: sqlite3.Connection) -> bool:
    """Rebuild trades with the current instrument_type CHECK. Returns True if it ran.

    Skipped when the table already allows FOREX (fresh installs, or a database that was
    already rebuilt). Every existing column is copied across; nothing else references
    trades(id) (analysis and tags join on the trade_group text), so no other table changes.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not row or "'FOREX'" in row[0]:
        return False

    conn.commit()
    backup = _backup_database(conn)
    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)")]
    try:
        conn.execute("BEGIN")
        conn.execute(trades_ddl("trades_new"))
        new_cols = [r[1] for r in conn.execute("PRAGMA table_info(trades_new)")]
        shared = ", ".join(c for c in old_cols if c in new_cols)
        conn.execute(f"INSERT INTO trades_new ({shared}) SELECT {shared} FROM trades")
        conn.execute("DROP TABLE trades")
        conn.execute("ALTER TABLE trades_new RENAME TO trades")
        _create_trades_indexes(conn)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(
            "Could not upgrade the trades table for FX support; nothing was changed."
            + (f" A backup of your database is at {backup}." if backup else "")
        ) from exc
    return True


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)
