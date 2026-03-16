"""Database URL helper utilities — single source of truth.

This module is the *only* place that defines driver-upgrade logic for
database connection strings.  Both ``src/db/database.py`` and
``src/db/migrations/env.py`` import from here, eliminating the duplication
identified in audit finding H-1.

Resolution order for the database URL (used by ``resolve_database_url``):
  1. ``DATABASE_URL`` env var — if set and non-empty, used verbatim (with
     async driver prefix applied).
  2. ``PLATFORM_DB_PATH`` env var — path to an SQLite file (wrapped in the
     ``sqlite+aiosqlite:///`` scheme).
  3. Caller-supplied default path (typically ``data/platform.db``).
"""

from __future__ import annotations

import os
from pathlib import Path


def upgrade_driver(url: str) -> str:
    """Replace sync driver prefixes with their async equivalents.

    Idempotent — already-async URLs are returned unchanged.

    Supported upgrades:
        ``sqlite://``      → ``sqlite+aiosqlite://``
        ``postgresql://``  → ``postgresql+asyncpg://``
        ``postgres://``    → ``postgresql+asyncpg://`` (Heroku-style)

    Args:
        url: Raw database URL string.

    Returns:
        URL with an async-compatible driver prefix.
    """
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        # Heroku-style shorthand — also upgrade to asyncpg
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


def resolve_database_url(default_sqlite_path: str | Path) -> str:
    """Return the effective async database URL.

    Priority:
      1. ``DATABASE_URL`` env var (if non-empty).
      2. ``PLATFORM_DB_PATH`` env var → ``sqlite+aiosqlite:///`` URL.
      3. *default_sqlite_path* argument → ``sqlite+aiosqlite:///`` URL.

    Parent directories for SQLite paths are created automatically.

    Args:
        default_sqlite_path: Fallback SQLite file path when neither
            ``DATABASE_URL`` nor ``PLATFORM_DB_PATH`` is set.

    Returns:
        Fully resolved async database URL string.
    """
    raw = os.getenv("DATABASE_URL", "").strip()
    if raw:
        return upgrade_driver(raw)

    sqlite_path = os.getenv("PLATFORM_DB_PATH", str(default_sqlite_path))
    Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{sqlite_path}"


def is_sqlite(url: str) -> bool:
    """Return True if *url* refers to an SQLite database."""
    return url.startswith("sqlite")
