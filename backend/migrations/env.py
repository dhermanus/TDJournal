"""Alembic environment for TDJournal's SQLite schema.

The app's tables use plain sqlite3, not ORM metadata, so revisions issue SQL
through Alembic's migration connection. That also lets Alembic own its version
stamp while SQLite retains its normal constraints and indexes.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

import schema
from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

# Keep this in step with database.DB_PATH; the application sets it before
# `command.upgrade()`, and this fallback is convenient for `alembic history`.
database_path = config.attributes.get("db_path") or os.getenv("TDJOURNAL_DATABASE_PATH") or os.getenv(
    "DATABASE_PATH", str(backend_dir / "trading_journal.db")
)
config.set_main_option("sqlalchemy.url", schema.sqlite_url(database_path))

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
