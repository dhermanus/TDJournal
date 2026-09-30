import sqlite3
import os
from pathlib import Path
from dotenv import load_dotenv

import schema

load_dotenv()

DB_PATH = os.getenv("DATABASE_PATH", "trading_journal.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Bring the journal to the current schema, then start it.

    Order matters: Alembic decides what has been applied and takes its
    pre-change snapshot first, then the idempotent pass in `schema` re-asserts
    the invariants a version stamp cannot express (columns a pre-versioning
    database may lack, a CHECK that needs rebuilding, an abandoned attachment
    draft). Either layer alone would leave a gap; together they converge every
    state the app has ever started from.
    """
    import dbmigration

    backups_dir = os.getenv("BACKUP_DIR") or str(Path(DB_PATH).resolve().parent / "backups")
    try:
        dbmigration.run_migrations(DB_PATH, backups_dir=backups_dir)
    except Exception as exc:  # noqa: BLE001 - any migration failure is fatal at startup
        # Refuse to boot with a schema we cannot vouch for, rather than start
        # and fail on the first query against a half-migrated table.
        if isinstance(exc, dbmigration.MigrationError):
            raise
        raise dbmigration.MigrationError(
            f"Could not apply schema migrations to {DB_PATH}: {exc}"
        ) from exc

    conn = get_db()
    schema.ensure_schema(conn)
    conn.close()



# Public names kept for existing tests and internal callers. The canonical
# definitions now live in schema.py, which startup and Alembic both import, so
# the application and its migrations cannot hold two versions of the schema.
instrument_check_values = schema.instrument_check_values
widen_instrument_type_check = schema.widen_instrument_type_check
_instrument_check_probe = schema.instrument_check_probe
_new_trades_sql = schema._new_trades_sql


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)
