"""Alembic environment configuration for async SQLAlchemy.

This env.py handles:
- Loading DATABASE_URL from the environment (with async driver upgrade).
- Running migrations in both "offline" mode (generate SQL scripts) and
  "online" mode (apply migrations to a live database).
- Full async support via ``run_sync`` (required for asyncio-based engines).
- Dual SQLite/PostgreSQL support.

Usage:
    alembic upgrade head
    alembic downgrade -1
    alembic revision --autogenerate -m "add indexes"
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so we can import src.*
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Import target metadata from our models (drives --autogenerate).
from src.db.models import Base
from src.db.url_helpers import resolve_database_url, upgrade_driver

# ---------------------------------------------------------------------------
# Alembic Config object — provides access to alembic.ini values
# ---------------------------------------------------------------------------
config = context.config

# Honour the [loggers] section in alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# URL helpers — delegate to src.db.url_helpers (single source of truth, H-1 fix)
# ---------------------------------------------------------------------------


def _upgrade_driver(url: str) -> str:
    """Deprecated shim: delegates to src.db.url_helpers.upgrade_driver."""
    return upgrade_driver(url)


def _get_database_url() -> str:
    """Resolve the effective database URL.

    Delegates to ``src.db.url_helpers.resolve_database_url`` so there is
    exactly one implementation of driver-upgrade logic (audit finding H-1).
    """
    default_path = _PROJECT_ROOT / "data" / "platform.db"
    return resolve_database_url(default_path)


# Override the ini URL with the dynamically resolved one.
config.set_main_option("sqlalchemy.url", _get_database_url())


# ---------------------------------------------------------------------------
# Offline migrations — generate SQL script without connecting to DB
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    In offline mode, Alembic emits SQL to stdout/a file rather than
    executing it. Useful for generating deployment scripts for DBAs.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Render ANSI SQL that works for both SQLite and PostgreSQL
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migrations — connect and apply changes
# ---------------------------------------------------------------------------


def do_run_migrations(connection: Connection) -> None:
    """Execute pending migrations using the provided sync connection.

    ``render_as_batch=True`` is required for SQLite, which does not support
    ALTER TABLE natively. Batch mode rewrites the table via CREATE/INSERT/DROP.
    It is harmless for PostgreSQL.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,  # detect column type changes
        compare_server_default=True,  # detect server_default changes
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations inside a sync context.

    SQLAlchemy's async engine cannot drive Alembic directly — we connect
    asynchronously but then hand off to ``run_sync`` which executes
    ``do_run_migrations`` in the synchronous connection context that
    Alembic expects.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # no pooling during migrations
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migration mode."""
    asyncio.run(run_async_migrations())


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
