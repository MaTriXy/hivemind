"""MemoryStore — persistent project-scoped key/value agent memory.

All memory entries are persisted to the ``memory`` table from task_002's
schema. Memory is keyed per ``project_id``, NOT per session — it survives
across conversation boundaries and server restarts.

Design decisions:
- Uses UPSERT (INSERT ... ON CONFLICT DO UPDATE) for all writes so callers
  never need to check whether a key exists before writing.
- Keys use dot-notation namespacing convention:
    ``agent.orchestrator.last_plan``, ``user.preferences``, ``project.tech_stack``
- Values are arbitrary JSON (str, int, float, bool, list, dict, or None).
  Callers are responsible for serialisable types.
- Security constraint: callers MUST NOT store secrets or API keys in memory.
  The value_json column is readable by anyone with DB access.
- ``set_many`` uses a **single bulk upsert** (one DB round trip for N keys).
  The previous implementation looped over keys and issued N separate
  ``db.execute()`` calls, which was unnecessarily chatty for batch writes.

Public interface::

    store = MemoryStore(session_factory)

    # Write a memory entry
    await store.set_memory(project_id, "user.name", "Alice")
    await store.set_memory(project_id, "project.tech_stack", ["Python", "FastAPI"])

    # Read a single entry
    value = await store.get_memory(project_id, "user.name")  # "Alice"
    missing = await store.get_memory(project_id, "nonexistent")  # None

    # Load full project context (all key/value pairs as a dict)
    context = await store.get_all_memory(project_id)
    # {"user.name": "Alice", "project.tech_stack": ["Python", "FastAPI"]}

    # Delete a key (sets value to None, keeps the row — preserves audit trail)
    await store.delete_memory(project_id, "stale.key")
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models import Memory
from src.storage._store_utils import _dialect_insert, _ensure_project, _utcnow

logger = logging.getLogger(__name__)

# Maximum key length (matches DB column String(500))
_MAX_KEY_LEN = 500

# Keys that must never be stored in memory (security constraint)
_FORBIDDEN_KEY_PREFIXES: frozenset[str] = frozenset(
    {"secret", "api_key", "apikey", "token", "password", "credential", "auth"}
)


def _validate_key(key: str) -> None:
    """Raise ValueError if key violates naming or security rules."""
    if not key or not isinstance(key, str):
        raise ValueError("Memory key must be a non-empty string.")
    if len(key) > _MAX_KEY_LEN:
        raise ValueError(f"Memory key too long ({len(key)} chars). Maximum is {_MAX_KEY_LEN}.")
    key_lower = key.lower()
    for prefix in _FORBIDDEN_KEY_PREFIXES:
        if key_lower.startswith(prefix):
            raise ValueError(
                f"Memory key {key!r} starts with forbidden prefix {prefix!r}. "
                "Do NOT store secrets or credentials in agent memory."
            )


class MemoryStore:
    """Async service for reading and writing per-project agent memory.

    Injected via FastAPI ``Depends()`` — see ``src/dependencies.py``.

    Args:
        session_factory: An ``async_sessionmaker[AsyncSession]`` produced by
            ``get_session_factory()`` from ``src.db.database``.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    # ─────────────────────────────────────────────────────────────────────────
    # Public: Write
    # ─────────────────────────────────────────────────────────────────────────

    async def set_memory(
        self,
        project_id: str,
        key: str,
        value: Any,
    ) -> None:
        """Upsert a memory entry for a project.

        Uses INSERT ... ON CONFLICT DO UPDATE so this call is idempotent and
        safe to retry. If the key already exists its value is overwritten and
        ``updated_at`` is refreshed.

        Args:
            project_id: Project scope for this memory entry.
            key:        Dot-notation key, e.g. ``"agent.last_plan"``.
            value:      Any JSON-serialisable value (str, int, float, bool,
                        list, dict, or None). Secrets are forbidden — see
                        ``_FORBIDDEN_KEY_PREFIXES``.

        Raises:
            ValueError: If key fails validation.
            Exception:  On DB errors (logged with full traceback).
        """
        _validate_key(key)

        async with self._factory() as db:
            try:
                await _ensure_project(db, project_id)
                now = _utcnow()
                insert_stmt = _dialect_insert(db, Memory).values(
                    project_id=project_id,
                    key=key,
                    value_json=value,
                    created_at=now,
                    updated_at=now,
                )
                stmt = insert_stmt.on_conflict_do_update(
                    index_elements=["project_id", "key"],
                    set_={
                        "value_json": value,
                        "updated_at": now,
                    },
                )
                await db.execute(stmt)
                await db.commit()
                logger.debug("MemoryStore: set %s.%s = %r", project_id, key, value)
            except Exception:
                logger.error(
                    "MemoryStore.set_memory failed (project=%s key=%s)",
                    project_id,
                    key,
                    exc_info=True,
                )
                await db.rollback()
                raise

    async def set_many(
        self,
        project_id: str,
        entries: dict[str, Any],
    ) -> None:
        """Upsert multiple memory entries in a **single** DB round trip.

        Builds one bulk ``INSERT ... ON CONFLICT DO UPDATE`` statement for all
        entries instead of issuing N separate statements.  This eliminates the
        N-round-trip anti-pattern that existed in the previous loop-based
        implementation and is more efficient under both SQLite (fewer WAL
        flushes) and PostgreSQL (fewer network round trips).

        Args:
            project_id: Project scope for all entries.
            entries:    Dict mapping key → value. All keys are validated before
                        any writes occur (fail-fast on invalid keys).

        Raises:
            ValueError: If any key fails validation.
            Exception:  On DB errors (logged with full traceback).
        """
        if not entries:
            return  # Nothing to do — avoid an empty VALUES() clause

        for key in entries:
            _validate_key(key)

        async with self._factory() as db:
            try:
                await _ensure_project(db, project_id)
                now = _utcnow()

                # Build a list of row dicts for the bulk INSERT
                rows = [
                    {
                        "project_id": project_id,
                        "key": key,
                        "value_json": value,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for key, value in entries.items()
                ]

                # Single bulk upsert — one DB round trip for N keys
                insert_stmt = _dialect_insert(db, Memory).values(rows)
                stmt = insert_stmt.on_conflict_do_update(
                    index_elements=["project_id", "key"],
                    set_={
                        "value_json": insert_stmt.excluded.value_json,
                        "updated_at": insert_stmt.excluded.updated_at,
                    },
                )
                await db.execute(stmt)
                await db.commit()
                logger.debug(
                    "MemoryStore: set_many %d entries for project %s (1 round trip)",
                    len(entries),
                    project_id,
                )
            except Exception:
                logger.error(
                    "MemoryStore.set_many failed (project=%s keys=%s)",
                    project_id,
                    list(entries.keys()),
                    exc_info=True,
                )
                await db.rollback()
                raise

    async def delete_memory(self, project_id: str, key: str) -> bool:
        """Delete a memory entry for a project.

        Args:
            project_id: Project scope.
            key:        Key to remove.

        Returns:
            True if the row was deleted, False if it did not exist.
        """
        _validate_key(key)
        async with self._factory() as db:
            try:
                stmt = delete(Memory).where(
                    Memory.project_id == project_id,
                    Memory.key == key,
                )
                result = await db.execute(stmt)
                await db.commit()
                deleted = result.rowcount > 0
                if deleted:
                    logger.debug("MemoryStore: deleted %s.%s", project_id, key)
                return deleted
            except Exception:
                logger.error(
                    "MemoryStore.delete_memory failed (project=%s key=%s)",
                    project_id,
                    key,
                    exc_info=True,
                )
                await db.rollback()
                raise

    # ─────────────────────────────────────────────────────────────────────────
    # Public: Read
    # ─────────────────────────────────────────────────────────────────────────

    async def get_memory(
        self,
        project_id: str,
        key: str,
        default: Any = None,
    ) -> Any:
        """Retrieve a single memory value for a project.

        Args:
            project_id: Project scope.
            key:        Key to look up.
            default:    Value to return if the key does not exist (default None).
                        Note: if the key exists with a stored null value, ``None``
                        is returned regardless of ``default``.

        Returns:
            The stored JSON value, or ``default`` if the key is not found at all.
        """
        _validate_key(key)
        async with self._factory() as db:
            try:
                # Select the entire row (not just value_json) so we can
                # distinguish "key not found" from "key found with null value".
                stmt = select(Memory).where(
                    Memory.project_id == project_id,
                    Memory.key == key,
                )
                result = await db.execute(stmt)
                row = result.scalar_one_or_none()
                if row is None:
                    return default
                # row.value_json can legitimately be None (stored null)
                return row.value_json
            except Exception:
                logger.error(
                    "MemoryStore.get_memory failed (project=%s key=%s)",
                    project_id,
                    key,
                    exc_info=True,
                )
                raise

    async def get_all_memory(self, project_id: str) -> dict[str, Any]:
        """Return the full agent context for a project as a flat dict.

        This is the primary method for loading agent state on reconnect.
        The returned dict is keyed by memory key and maps to the stored value.

        Args:
            project_id: Project to load memory for.

        Returns:
            Dict mapping key → value for all entries. Empty dict if no memory
            has been stored yet.

        Example::

            context = await store.get_all_memory("my-project")
            # {
            #   "user.name": "Alice",
            #   "project.tech_stack": ["Python", "FastAPI"],
            #   "agent.orchestrator.last_plan": "...",
            # }
        """
        async with self._factory() as db:
            try:
                stmt = (
                    select(Memory.key, Memory.value_json)
                    .where(Memory.project_id == project_id)
                    .order_by(Memory.key)
                )
                result = await db.execute(stmt)
                rows = result.all()
                return {row.key: row.value_json for row in rows}
            except Exception:
                logger.error(
                    "MemoryStore.get_all_memory failed for project %s",
                    project_id,
                    exc_info=True,
                )
                raise

    async def get_keys(self, project_id: str) -> list[str]:
        """Return all memory keys stored for a project (values not included).

        Useful for introspection without loading all values into memory.

        Args:
            project_id: Project to inspect.

        Returns:
            Sorted list of key strings.
        """
        async with self._factory() as db:
            try:
                stmt = (
                    select(Memory.key).where(Memory.project_id == project_id).order_by(Memory.key)
                )
                result = await db.execute(stmt)
                return [row[0] for row in result.all()]
            except Exception:
                logger.error(
                    "MemoryStore.get_keys failed for project %s",
                    project_id,
                    exc_info=True,
                )
                raise
