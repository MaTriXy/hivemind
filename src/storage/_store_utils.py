"""Shared utilities for storage services (ConversationStore, MemoryStore).

This module centralises helpers that were previously duplicated between
``src/storage/conversation_store.py`` and ``src/storage/memory_store.py``:

- ``_utcnow()``       — canonical UTC timestamp factory
- ``_ensure_project`` — portable project-stub upsert (SQLite + PostgreSQL)
- ``_dialect_insert`` — dialect-aware INSERT constructor for upsert operations

Design decisions
----------------
- ``_ensure_project`` must be **dialect-agnostic** because the platform supports
  both SQLite (dev) and PostgreSQL (production).  The previous implementation used
  ``sqlalchemy.dialects.sqlite.insert`` which would raise a ``CompileError`` on
  PostgreSQL.  The new implementation uses a savepoint-guarded INSERT:

  1. Quick SELECT to short-circuit the common case (project already exists).
  2. If not found, attempt ``db.add()`` + ``db.flush()`` inside a savepoint
     (``begin_nested()``).  If a concurrent request created the row first, the
     savepoint rolls back without affecting the outer transaction — the outer
     transaction continues normally.

- ``_dialect_insert`` detects the database dialect at runtime and returns the
  correct dialect-specific ``insert()`` function.  This replaces the previous
  hard-coded ``from sqlalchemy.dialects.sqlite import insert`` which would
  fail on PostgreSQL with a ``CompileError``.

- ``_utcnow`` is kept as a module-level function rather than a class method so
  it can be imported by both stores without coupling them to each other.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from _shared_utils import utcnow as _utcnow
from src.db.models import Project


async def _ensure_project(db: AsyncSession, project_id: str) -> None:
    """Upsert a minimal project stub so FK constraints are satisfied.

    The canonical project record lives in the legacy session_mgr.  Here we just
    ensure the platform schema has a row that the FK in ``conversations`` and
    ``memory`` can reference.

    This implementation is **dialect-agnostic** (works on both SQLite and
    PostgreSQL) via the following strategy:

    1. SELECT the project by primary key — O(1) index lookup.
    2. If the row already exists, return immediately (fast path).
    3. Otherwise, open a savepoint and attempt an INSERT.
    4. If a concurrent transaction created the row between steps 1 and 3, the
       INSERT will raise ``IntegrityError``; we catch it and roll back only the
       savepoint (not the outer transaction).

    Args:
        db:         The current async session (must be within an active transaction).
        project_id: UUID of the project to ensure.
    """
    # Fast path — project already exists (most common case)
    result = await db.execute(select(Project.id).where(Project.id == project_id))
    if result.scalar_one_or_none() is not None:
        return

    # Slow path — project stub does not yet exist; create a minimal placeholder.
    # Wrap in a savepoint so that a concurrent-INSERT race does not roll back the
    # outer transaction (which may already have pending writes).
    now = _utcnow()
    async with db.begin_nested():  # creates a SAVEPOINT
        try:
            db.add(
                Project(
                    id=project_id,
                    name=project_id,  # placeholder; real record lives in session_mgr
                    config_json={},
                    created_at=now,
                    updated_at=now,
                )
            )
            await db.flush()
        except IntegrityError:
            # Another concurrent request created the row between our SELECT and
            # this INSERT.  The savepoint is automatically rolled back by the
            # context manager; the outer transaction is unaffected.
            pass


def _dialect_insert(db: AsyncSession, model: type[DeclarativeBase]) -> Any:
    """Return a dialect-specific ``insert()`` statement for the given model.

    Detects the database dialect at runtime and returns the correct
    dialect-specific insert function that supports ``on_conflict_do_update``
    (upsert semantics).

    Supported dialects:
        - **SQLite**: ``sqlalchemy.dialects.sqlite.insert``
        - **PostgreSQL**: ``sqlalchemy.dialects.postgresql.insert``

    Args:
        db:    The active async session (used to detect the dialect).
        model: The ORM model class to build the INSERT statement for.

    Returns:
        A dialect-specific ``Insert`` statement object that supports
        ``.on_conflict_do_update()`` and ``.on_conflict_do_nothing()``.

    Raises:
        ValueError: If the dialect is not SQLite or PostgreSQL.
    """
    dialect_name = db.bind.dialect.name  # type: ignore[union-attr]

    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert_fn
    elif dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert_fn
    else:
        raise ValueError(
            f"Unsupported database dialect {dialect_name!r}. "
            "Only 'sqlite' and 'postgresql' are supported."
        )

    return dialect_insert_fn(model)
