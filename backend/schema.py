"""TDJournal's SQLite schema: one definition, two callers.

`BASE_STATEMENTS` and `COLUMN_ADDITIONS` are the schema. They are consumed by
both paths that can create it:

  * `ensure_schema()` — the application, at every startup; and
  * migration `0001_initial_schema` — Alembic, so the schema is owned by a
    revision rather than only by application code.

Keeping them as a list of *single* statements is deliberate. `sqlite3.execute`
and Alembic's `op.execute` both reject multi-statement strings, so a
`CREATE TABLE ...; CREATE INDEX ...` blob works in `executescript()` but would
explode in a migration. One statement per element runs identically on both.

Every statement is `IF NOT EXISTS` (or a guarded `ADD COLUMN`), because a
pre-Alembic database may already contain parts of it: the same row of code has
to converge a brand-new file and a five-year-old one.

The two conversions SQLite cannot express with `ALTER` — widening
`trades.instrument_type`'s CHECK, and the attachment-table repair — live in
`ensure_schema()`, which runs on *every* boot, not in a migration. They are
invariants checked repeatedly rather than one-shot events, and the attachment
repair must run against databases that Alembic believes are already current.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import instruments


def sqlite_url(path: str | Path) -> str:
    """A SQLAlchemy URL that opens exactly this SQLite file.

    The slash count is load-bearing and was measured rather than assumed: an
    absolute POSIX path needs *four* slashes (`sqlite:////tmp/x`, parsed as
    `/tmp/x`), while a Windows drive path needs three plus the drive letter
    (`sqlite:///C:/x`). Three slashes before `/tmp/x` parses as a relative path
    and fails with `unable to open database file` — which, if it had gone
    unnoticed, would have let migrations write to a file nobody intended.

    Lives here rather than next to Alembic so `migrations/env.py` and
    `dbmigration.py` share one definition without importing each other.
    """
    return f"sqlite:///{Path(path).absolute().as_posix()}"


# ── Base schema ───────────────────────────────────────────────────────────────
# Order matters only for readability; there are no cross-table CHECKs that
# depend on creation order (FOREIGN KEY targets resolve at DML time in SQLite).
BASE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('day_trading','swing_trading','investment')),
        color TEXT NOT NULL DEFAULT '#6366f1',
        broker TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        trade_group TEXT NOT NULL,
        date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        instrument_type TEXT NOT NULL CHECK(instrument_type IN ('STOCK','OPTION','FUTURE','FX','METAL','INDEX')),
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS diary_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        entry_date TEXT NOT NULL,
        image_path TEXT,
        raw_text TEXT,
        ai_analysis TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_group TEXT NOT NULL,
        tag_type TEXT NOT NULL,
        tag_value TEXT NOT NULL,
        source TEXT NOT NULL CHECK(source IN ('ai','manual'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER REFERENCES accounts(id),
        summary_date TEXT NOT NULL,
        ai_content TEXT NOT NULL,
        generated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(summary_date, account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL DEFAULT 0,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        UNIQUE(account_id, key)
    )
    """,
    # M1 bars imported from ExportBarsCSV.mq5. Market data rather than account
    # data, so it is keyed by symbol+time with no account scoping: two accounts
    # trading EURUSD read the same candles, and a re-import refreshes a bar
    # instead of adding a second copy of the same minute. `time` is naive UTC,
    # converted from broker-server time on import the same way deal timestamps
    # are, so bars and executions share a timeline.
    """
    CREATE TABLE IF NOT EXISTS bars (
        symbol TEXT NOT NULL,
        time TEXT NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        tick_volume INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (symbol, time)
    )
    """,
    # The trader's playbook: named setups used to tag trades.
    """
    CREATE TABLE IF NOT EXISTS custom_setups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        side TEXT,
        notes TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # Files attached to a trade review (screenshots, statements, notes). Bytes
    # live under the local uploads directory; only metadata lives here. Keyed by
    # trade_group alone to match trade_tags and trade_analysis, both of which
    # treat the group as globally unique (trade_analysis declares it UNIQUE), so
    # a re-import that keeps the group keeps the attachments with it.
    """
    CREATE TABLE IF NOT EXISTS trade_attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_group TEXT NOT NULL,
        original_name TEXT NOT NULL,
        stored_name TEXT NOT NULL UNIQUE,
        content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
        size_bytes INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trades_account_date ON trades(account_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_trades_group ON trades(trade_group)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_group ON trade_analysis(trade_group)",
    "CREATE INDEX IF NOT EXISTS idx_tags_group ON trade_tags(trade_group)",
    "CREATE INDEX IF NOT EXISTS idx_attachments_trade ON trade_attachments(trade_group, created_at)",
]


# Columns added after the original tables shipped. Each is guarded rather than
# declared inline, because a table created before the column existed must gain
# it on the next boot, and `ALTER TABLE ... ADD COLUMN` has no `IF NOT EXISTS`.
COLUMN_ADDITIONS = [
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
]


def apply_base(conn: sqlite3.Connection) -> None:
    """Create whatever does not exist yet. Safe on any database, any number of times."""
    for statement in BASE_STATEMENTS:
        conn.execute(statement)
    conn.commit()


def apply_columns(conn: sqlite3.Connection) -> None:
    """Add missing columns, ignoring the ones that are already there."""
    for statement in COLUMN_ADDITIONS:
        try:
            conn.execute(statement)
            conn.commit()
        except sqlite3.OperationalError as exc:
            # Already exists. SQLite has no ADD COLUMN IF NOT EXISTS, so the
            # failure is the test — but only this one failure is ignorable.
            if "duplicate column" not in str(exc).lower():
                raise


# ── Conversions SQLite cannot express with ALTER ──────────────────────────────

_INSTRUMENT_CHECK = re.compile(
    r"CHECK\s*\(\s*instrument_type\s+IN\s*\([^)]*\)\s*\)", re.IGNORECASE
)


def instrument_check_values(sql: str) -> set[str] | None:
    """The types the trades table's CHECK accepts, or None if it has no such CHECK.

    None means the column is unconstrained, which accepts everything we could
    ever ask for — the caller then has nothing to widen.
    """
    match = _INSTRUMENT_CHECK.search(sql or "")
    if not match:
        return None
    inner = match.group(0)[match.group(0).find("(") + 1:]
    inner = inner[:inner.rfind(")")]
    inner = inner[inner.find("(") + 1:inner.rfind(")")]
    return {v.strip().strip("'\"") for v in inner.split(",") if v.strip()}


def _new_trades_sql(sql: str) -> str:
    """The trades table's DDL with its CHECK widened to every known type.

    Taken from the live schema rather than regenerated from column metadata so
    that UNIQUE, DEFAULTs and column order survive untouched — a regeneration
    would silently drop the UNIQUE(trade_group, account_id) conflict target
    that import deduplication depends on.

    Only called once a CHECK has been found, so it can raise on a schema that
    has none instead of quietly writing a no-op rewrite.
    """
    if not _INSTRUMENT_CHECK.search(sql):
        raise ValueError("trades DDL has no instrument_type CHECK to widen")
    wanted = f"CHECK(instrument_type IN ({instruments.CHECK_EXPR}))"
    return _INSTRUMENT_CHECK.sub(wanted, sql, count=1)


def widen_instrument_type_check(conn) -> bool:
    """Rebuild trades if its CHECK is narrower than instruments.INSTRUMENT_TYPES.

    SQLite has no ALTER for a table constraint, so the documented rebuild is
    the only route: create a copy with the widened DDL, copy the rows across,
    drop the original, rename the copy, put the indexes back. Returns True when
    a rebuild happened.

    Anything unexpected raises rather than passing quietly — the last thing
    this needs is an app that starts with a constraint nobody noticed kept
    blocking MT5 rows.
    """
    row = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if row is None:
        return False
    original_sql = row[1]   # SELECT name, sql
    current = instrument_check_values(original_sql)
    wanted = set(instruments.INSTRUMENT_TYPES)

    # No CHECK, or already wide enough — nothing to do.
    if current is None or wanted.issubset(current):
        return False

    indexes = [
        r[0]
        for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='trades' AND sql IS NOT NULL"
        )
    ]
    # Positional (not r["name"]): this module is shared with callers that use a
    # plain sqlite3.Connection, whose rows are tuples and cannot be indexed by key.
    columns = [r[1] for r in conn.execute("PRAGMA table_info(trades)")]
    if not columns:
        raise RuntimeError("trades table reported no columns; refusing to rebuild")

    # PRAGMA foreign_keys is a no-op inside a transaction, so close one first.
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")

    temp = "trades_migrated"
    conn.execute(f"DROP TABLE IF EXISTS {temp}")
    try:
        conn.execute(_new_trades_sql(original_sql).replace(
            "CREATE TABLE trades", f"CREATE TABLE {temp}", 1
        ))
        col_list = ", ".join(columns)
        conn.execute(
            f"INSERT INTO {temp} ({col_list}) SELECT {col_list} FROM trades"
        )
        # Rename instead of drop-then-rename so a failure mid-way leaves the
        # original data intact rather than a dangling copy.
        conn.execute("DROP TABLE trades")
        conn.execute(f"ALTER TABLE {temp} RENAME TO trades")
        for ddl in indexes:
            conn.execute(ddl)
    except Exception:
        conn.execute(f"DROP TABLE IF EXISTS {temp}")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")

    conn.commit()
    return True


def instrument_check_probe(conn) -> None:
    """Read the CHECK back and assert it took the widened value.

    The rebuild can run without error and still leave the old constraint if
    something rewrote the DDL unexpectedly, so confirm against the stored SQL.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if row is None:
        return
    accepted = instrument_check_values(row[0])
    if accepted is None:
        return
    missing = set(instruments.INSTRUMENT_TYPES) - accepted
    if missing:
        raise RuntimeError(
            f"trades.instrument_type still rejects {sorted(missing)} after migration"
        )


def repair_trade_attachments(conn) -> bool:
    """Converge the early account-scoped attachment draft to the shipped schema.

    An earlier local draft created this table with `account_id NOT NULL`. The
    released design keys attachments by trade_group alone. SQLite cannot drop a
    required column in older supported versions, so rebuild the table while
    preserving every metadata row; the stored file names do not change. Returns
    True only when a rebuild was needed.

    This runs at every startup rather than as a migration because it must also
    fix databases that Alembic already considers current — a migration is
    stamped once and never revisits them.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trade_attachments'"
    ).fetchone()
    if row is None or "account_id" not in (row[0] or ""):
        return False

    conn.execute("BEGIN IMMEDIATE")
    seq_row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone()
    attachment_seq = None
    if seq_row is not None:
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='trade_attachments'"
        ).fetchone()
        attachment_seq = seq[0] if seq else None

    conn.execute("""
        CREATE TABLE trade_attachments_repair (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_group TEXT NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL UNIQUE,
            content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        INSERT INTO trade_attachments_repair
            (id, trade_group, original_name, stored_name, content_type, size_bytes, created_at)
        SELECT id, trade_group, original_name, stored_name, content_type, size_bytes, created_at
        FROM trade_attachments
    """)
    conn.execute("DROP TABLE trade_attachments")
    conn.execute("ALTER TABLE trade_attachments_repair RENAME TO trade_attachments")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attachments_trade "
        "ON trade_attachments(trade_group, created_at)"
    )
    # Dropping the original drops its AUTOINCREMENT counter with it; put it back
    # so ids handed out before the repair are never reissued to a new file.
    if attachment_seq is not None:
        conn.execute(
            "INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES (?, ?)",
            ("trade_attachments", attachment_seq),
        )
    conn.commit()
    return True


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Bring a database to the current shape. Idempotent on every known state.

    Runs at every startup, after Alembic has had its say: Alembic records which
    revisions ran, while this re-asserts the invariants that a version stamp
    cannot express (columns a pre-versioning database may be missing, a CHECK
    that needs rebuilding, an attachment table from an abandoned draft).
    """
    apply_base(conn)
    apply_columns(conn)
    repair_trade_attachments(conn)
    widen_instrument_type_check(conn)
    instrument_check_probe(conn)
    conn.commit()


def has_user_tables(conn: sqlite3.Connection) -> bool:
    """True when a database already contains application tables (not just alembic's)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name != 'alembic_version' LIMIT 1"
    ).fetchone()
    return row is not None
