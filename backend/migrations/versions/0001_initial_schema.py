"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-30

The baseline. Every table and index the journal ships with, taken from the
schema that `database.init_db()` created before Alembic was adopted.

Two deliberate properties:

  * every statement is `IF NOT EXISTS`, and each column addition is its own
    guarded `ALTER` — the first run against a pre-Alembic database may find
    parts of this already present, and this revision has to converge on both
    states rather than assume either; and

  * the statements come from `schema.BASE_STATEMENTS` / `schema.COLUMN_ADDITIONS`
    rather than being retyped here, so the application and Alembic cannot drift
    into two definitions of the same table. The two conversions SQLite cannot
    do with ALTER (the trades CHECK widen and the attachment-table repair) are
    intentionally *not* here: they stay in `schema.ensure_schema()`, which runs
    on every boot against databases Alembic already considers current.
"""
from alembic import op
from sqlalchemy import text

from schema import COLUMN_ADDITIONS, BASE_STATEMENTS

# Identifiers created by these statements, used to keep upgrade idempotent for
# a stamped legacy database.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for statement in BASE_STATEMENTS:
        bind.execute(text(statement))

    # ADD COLUMN has no IF NOT EXISTS in SQLite, so each addition is skipped
    # when the column is already present.
    for statement in COLUMN_ADDITIONS:
        table = statement.split()[2]
        column = statement.split("ADD COLUMN")[1].split()[0]
        columns = bind.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()
        names = [row[1] for row in columns]
        if names and column not in names:
            try:
                bind.execute(text(statement))
            except Exception as exc:  # noqa: BLE001 - sqlite reports this as OperationalError
                if "duplicate column" not in str(exc).lower():
                    raise


def downgrade() -> None:
    """Baseline only: downgrading would drop a live journal.

    Deliberately does nothing. Dropping tables on downgrade is not a route
    anyone should be able to trigger against real data; a rollback is a restore
    from backup, which is a separate, explicit operation.
    """
