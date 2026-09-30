"""Run Alembic migrations at startup, with a copy of the database first.

This is the versioning layer the journal never had: `init_db()` used
`CREATE TABLE IF NOT EXISTS` plus guarded `ALTER`s, so it could create what was
missing but never record what had been applied. A pre-Alembic database therefore
has no version to read, which is the whole problem this module solves.

Adoption policy — three states, decided before anything writes:

  * no application tables   → `upgrade` : the migration builds the schema.
  * tables, no version table → `stamp`   : adopt the existing schema as the
    baseline rather than replaying DDL against a live journal. Replaying is
    what produced the stale `trade_attachments.account_id` table earlier; the
    idempotent DDL in `schema.ensure_schema()` still converges anything the
    stamped baseline does not cover, so nothing is left uncreated.
  * a version table         → `upgrade` : the normal path.

Either way, when the version is about to change, a copy of the database is
written first. That copy is database-only on purpose: a pre-migration snapshot
protects against a schema change, and the full backup (which also carries the
uploads folder) is the user-facing one-button feature.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

import schema
from backup import unique_path

BACKEND_DIR = Path(__file__).resolve().parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


class MigrationError(RuntimeError):
    """Startup could not bring the database to the current schema."""


def _config(db_path: str | Path) -> Config:
    if not ALEMBIC_INI.is_file():
        raise MigrationError(f"Missing {ALEMBIC_INI.name}; cannot run migrations.")
    cfg = Config(str(ALEMBIC_INI))
    # Set explicitly rather than relying on env.py's fallback: callers pass the
    # live path (tests point DB_PATH at a scratch file without setting env), and
    # env.py must not overwrite what it is given.
    cfg.set_main_option("sqlalchemy.url", schema.sqlite_url(db_path))
    cfg.attributes["db_path"] = str(Path(db_path).absolute())
    return cfg


def current_version(db_path: str | Path) -> str | None:
    """The recorded Alembic revision, or None before the database is versioned."""
    import sqlite3
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def head_revision(cfg: Config) -> str:
    """The single head revision of this migration history.

    Read through ScriptDirectory rather than `command.heads`, which prints to
    the console and returns nothing — this is called at startup, where the
    answer has to be a value, not a line of output.
    """
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if not head:
        raise MigrationError("Migration history has no head revision.")
    return head


def pending_revisions(cfg: Config, recorded: str) -> list[str]:
    """Revisions between `recorded` and head, newest first. Empty when caught up."""
    scripts = ScriptDirectory.from_config(cfg)
    return [rev.revision for rev in scripts.iterate_revisions(head_revision(cfg), recorded)]


def _snapshot_before_migration(db_path: str | Path, backups_dir: Path | None) -> Path | None:
    """Copy the database before the schema changes. Database only, by design."""
    if backups_dir is None:
        return None
    if not Path(db_path).is_file():
        # An empty database has no state to protect. Skipping this also avoids
        # creating a ZIP that would look like a real, restorable journal while
        # containing no application tables.
        return None
    import sqlite3
    backups_dir.mkdir(parents=True, exist_ok=True)
    # A raw SQLite copy, not a zip — naming it `.zip` would be a lie, and a file
    # that cannot be opened as an archive is exactly the kind of thing that
    # gets discovered during a rollback. Kept in its own subfolder so the
    # user's one-button backup folder stays a list of restorable archives.
    folder = backups_dir / "pre-migration"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("pre-migration-%Y%m%d-%H%M%S-%f.db")
    destination = unique_path(folder, stamp)
    with sqlite3.connect(str(db_path)) as source:
        with sqlite3.connect(str(destination)) as snapshot:
            source.backup(snapshot)
    return destination


def run_migrations(
    db_path: str | Path,
    *,
    backups_dir: str | Path | None = None,
) -> dict:
    """Bring `db_path` to the head revision, copying it first if anything changes.

    Returns a small report: which path was taken, the revision reached, and the
    snapshot path when one was written. Raises MigrationError rather than
    starting with a database the caller believes is current.
    """
    db_path = Path(db_path)
    cfg = _config(db_path)
    target = head_revision(cfg)

    # The connection below is the only place the pre-existing shape is inspected,
    # and it happens before Alembic touches anything.
    import sqlite3
    with sqlite3.connect(str(db_path)) as conn:
        has_tables = schema.has_user_tables(conn)

    recorded = current_version(db_path)
    mode: str
    snapshot: Path | None = None

    if recorded is None:
        # Nothing recorded: either an empty file or a pre-versioning journal.
        if not has_tables:
            mode = "create"
            snapshot = _snapshot_before_migration(db_path, Path(backups_dir) if backups_dir else None)
            command.upgrade(cfg, "head")
        else:
            # Adopt as-is. The snapshot matters most here: this is the first
            # write the new code makes to a journal that already holds years of
            # trades, and it happens on a database we have never measured.
            mode = "baseline"
            snapshot = _snapshot_before_migration(db_path, Path(backups_dir) if backups_dir else None)
            command.stamp(cfg, target)
    elif recorded == target:
        mode = "current"
    else:
        # Guard against a *reverse* stamp or an unknown revision rather than
        # silently downgrading: down migrations are not written for this app.
        revisions = pending_revisions(cfg, recorded)
        if not revisions:
            raise MigrationError(
                f"Database is stamped {recorded!r} but no migration path to "
                f"{target!r} was found. Refusing to start."
            )
        mode = "upgrade"
        snapshot = _snapshot_before_migration(db_path, Path(backups_dir) if backups_dir else None)
        command.upgrade(cfg, "head")

    reached = current_version(db_path)
    if reached != target:
        raise MigrationError(
            f"Migration finished at {reached!r}, expected {target!r}. "
            f"Restore from {snapshot} if a copy was written."
        )

    return {"mode": mode, "revision": reached, "snapshot": str(snapshot) if snapshot else None}
