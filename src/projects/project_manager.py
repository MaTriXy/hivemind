"""ProjectManager — async service layer for multi-project isolation.

This module is the single authoritative service for all project lifecycle
operations: create, read, list, update, and cascade-delete.

Isolation modes
---------------
Default (row-level):
    All data lives in a single ``platform.db`` SQLite file.  Every table that
    is project-scoped (conversations, messages, agent_actions, memory) has a
    ``project_id`` foreign key, so isolation is enforced by FK filtering.
    Cascade DELETE at both the ORM and DB levels ensures that deleting a
    project removes all descendant rows atomically.

per_db mode (``ISOLATION_MODE=per_db``):
    Enabled by setting the ``ISOLATION_MODE=per_db`` environment variable.
    The ``projects`` table (the registry) remains in ``platform.db``.
    Each project also gets its own SQLite file at::

        <DATA_DIR>/projects/<project_id>.db

    That file receives a full schema for the project-scoped tables
    (conversations, messages, agent_actions, memory).  Deleting a project
    removes its registry record *and* its dedicated database file.

    ``get_project_db_url(project_id)`` is the hook for other services
    (ConversationStore, MemoryStore) to open the correct database.

Public API
----------
    project_manager.create_project(name, config)   -> ProjectRow
    project_manager.get_project(project_id)         -> ProjectRow | None
    project_manager.list_projects(limit, offset)    -> list[ProjectRow]
    project_manager.update_project(project_id, ...) -> ProjectRow
    project_manager.delete_project(project_id)      -> bool
    project_manager.get_project_db_url(project_id)  -> str (per_db mode only)
    project_manager.is_per_db_mode                  -> bool

``ProjectRow`` is a plain ``dict`` (not an ORM model) so it crosses
module/serialization boundaries without impedance mismatch.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ISOLATION_MODE default is documented in src/config.AppSettings.ISOLATION_MODE.
# We read directly from os.getenv at __init__ time so that test code can use
# patch.dict(os.environ) to override it per-test without reloading the module.
from src.config import settings as _settings
from src.db.models import Project

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DATA_DIR = _PROJECT_ROOT / "data"

#: Isolation mode sentinel — matches the env var value.
_PER_DB_MODE = "per_db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from _shared_utils import utcnow as _utcnow


def _new_project_id() -> str:
    """Generate a UUID4 string for use as a project primary key."""
    return str(uuid.uuid4())


def _iso(dt: datetime | None) -> str | None:
    """Convert a datetime to ISO-8601 string, or None if dt is None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _project_to_dict(project: Project) -> dict:
    """Serialise an ORM Project row to a plain dict (ProjectRow)."""
    return {
        "id": project.id,
        "name": project.name,
        "project_dir": project.project_dir or "",
        "config": project.config_json or {},
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
    }


# ---------------------------------------------------------------------------
# ProjectManager
# ---------------------------------------------------------------------------


class ProjectManager:
    """Async service for creating, reading, updating, and deleting projects.

    One instance should be created per process and reused (thread-safe via
    SQLAlchemy's async session factory which is already a singleton).

    Args:
        session_factory: An ``async_sessionmaker`` bound to the platform DB.
        data_dir:        Root directory for per-project SQLite files.
                         Defaults to ``<project_root>/data``.  Only used when
                         ``ISOLATION_MODE=per_db``.

    Example::

        from src.db.database import get_session_factory
        from src.projects.project_manager import ProjectManager

        mgr = ProjectManager(get_session_factory())
        project = await mgr.create_project("My API", config={"budget_usd": 50})
        print(project["id"])  # UUID string
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        data_dir: Path | str | None = None,
    ) -> None:
        self._factory = session_factory
        self._data_dir = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        # Read ISOLATION_MODE from env at construction time (not from the singleton)
        # so tests can override it with patch.dict(os.environ).  The env var name
        # and its default ("") are documented in src.config.AppSettings.ISOLATION_MODE.
        self._isolation_mode = os.getenv("ISOLATION_MODE", _settings.ISOLATION_MODE).strip().lower()

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def is_per_db_mode(self) -> bool:
        """True when ``ISOLATION_MODE=per_db`` is set."""
        return self._isolation_mode == _PER_DB_MODE

    # ── Per-DB mode helpers ───────────────────────────────────────────────

    def _per_db_dir(self) -> Path:
        """Return (and create) the directory that stores per-project DBs."""
        d = self._data_dir / "projects"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_project_db_url(self, project_id: str) -> str:
        """Return the async SQLite URL for a project's dedicated DB.

        In ``row_level`` (default) mode this returns the main platform DB URL
        so callers don't need to branch — they always call this method and get
        the right connection string.

        In ``per_db`` mode, each project gets its own file at::

            <data_dir>/projects/<project_id>.db

        Args:
            project_id: UUID of the project.

        Returns:
            SQLite+aiosqlite URL string.
        """
        if not self.is_per_db_mode:
            # Row-level mode: single DB for everything.
            from src.db.database import _resolve_database_url

            return _resolve_database_url()

        db_path = self._per_db_dir() / f"{project_id}.db"
        return f"sqlite+aiosqlite:///{db_path}"

    async def _init_per_project_db(self, project_id: str) -> None:
        """Create schema in a new per-project SQLite file.

        Only called in ``per_db`` mode during project creation.  Creates the
        project-scoped tables (conversations, messages, agent_actions, memory)
        in the dedicated DB.  The ``projects`` table is intentionally excluded
        from the per-project DB — it only lives in the registry (platform.db).
        """
        from sqlalchemy import MetaData

        from src.db.database import _configure_sqlite, get_engine
        from src.db.models import Base

        url = self.get_project_db_url(project_id)
        engine = get_engine(database_url=url)
        await _configure_sqlite(engine)

        # Only create the project-scoped tables, not ``projects`` itself.
        project_scoped_tables = [
            t for name, t in Base.metadata.tables.items() if name != "projects"
        ]

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: MetaData().create_all(
                    sync_conn,
                    tables=project_scoped_tables,
                )
            )

        # Use a separate metadata object that only holds the project tables
        # so we don't accidentally create the ``projects`` table in the per-project DB.
        scoped_meta = MetaData()
        for t in project_scoped_tables:
            t.tometadata(scoped_meta)

        async with engine.begin() as conn:
            await conn.run_sync(scoped_meta.create_all)

        await engine.dispose()
        logger.info("Per-project DB initialised for project %s at %s", project_id, url)

    async def _delete_per_project_db(self, project_id: str) -> None:
        """Remove the per-project SQLite file (``per_db`` mode only)."""
        db_path = self._per_db_dir() / f"{project_id}.db"
        if db_path.exists():
            db_path.unlink()
            logger.info("Deleted per-project DB file: %s", db_path)
        # Also clean up WAL / SHM sidecar files if present.
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_suffix(db_path.suffix + suffix)
            if sidecar.exists():
                sidecar.unlink()

    # ── CRUD ──────────────────────────────────────────────────────────────

    async def create_project(
        self,
        name: str,
        *,
        config: dict[str, Any] | None = None,
        project_id: str | None = None,
        project_dir: str = "",
    ) -> dict:
        """Create a new project and return its serialised row.

        Args:
            name:       Display name for the project (max 255 chars).
            config:     Optional JSON-serialisable configuration dict.
                        Suggested keys: ``budget_usd``, ``default_model``,
                        ``tags``, ``description``.
            project_id: Optional explicit UUID.  Auto-generated if omitted.
                        Must be a valid UUID4 string.

        Returns:
            ``ProjectRow`` dict with keys:
            ``id``, ``name``, ``config``, ``created_at``, ``updated_at``.

        Raises:
            ValueError: If ``name`` is empty or ``project_id`` is not a valid UUID.
        """
        name = name.strip()
        if not name:
            raise ValueError("Project name must not be empty.")
        if len(name) > 255:
            raise ValueError("Project name must be at most 255 characters.")

        pid = project_id or _new_project_id()
        # Validate UUID format to prevent injection / enumeration.
        try:
            uuid.UUID(pid)
        except ValueError:
            raise ValueError(f"project_id must be a valid UUID; got {pid!r}.")

        now = _utcnow()
        project = Project(
            id=pid,
            name=name,
            project_dir=project_dir,
            config_json=config or {},
            created_at=now,
            updated_at=now,
        )

        async with self._factory() as session:
            session.add(project)
            await session.commit()
            await session.refresh(project)
            result = _project_to_dict(project)

        if self.is_per_db_mode:
            await self._init_per_project_db(pid)

        logger.info(
            "Project created: id=%s name=%r isolation=%s",
            pid,
            name,
            self._isolation_mode or "row_level",
        )
        return result

    async def get_project(self, project_id: str) -> dict | None:
        """Return a project by its UUID, or ``None`` if not found.

        Args:
            project_id: UUID string of the project.

        Returns:
            ``ProjectRow`` dict, or ``None`` if no such project exists.
        """
        async with self._factory() as session:
            result = await session.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if project is None:
                return None
            return _project_to_dict(project)

    async def list_projects(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Return all projects, ordered by creation time (newest first).

        Args:
            limit:  Maximum number of projects to return (1–500).
            offset: Pagination offset.

        Returns:
            List of ``ProjectRow`` dicts.
        """
        limit = max(1, min(limit, 500))
        offset = max(0, offset)

        async with self._factory() as session:
            result = await session.execute(
                select(Project).order_by(Project.created_at.desc()).limit(limit).offset(offset)
            )
            projects = result.scalars().all()
            return [_project_to_dict(p) for p in projects]

    async def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict | None:
        """Update a project's name and/or configuration.

        This is a partial update — only the supplied fields are changed.
        ``updated_at`` is always refreshed to the current UTC time.

        Args:
            project_id: UUID of the project to update.
            name:       New display name, or ``None`` to leave unchanged.
            config:     New configuration dict, or ``None`` to leave unchanged.
                        Replaces the entire ``config_json`` blob (not a merge).

        Returns:
            Updated ``ProjectRow`` dict, or ``None`` if project not found.

        Raises:
            ValueError: If the new name is empty or exceeds 255 characters.
        """
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("Project name must not be empty.")
            if len(name) > 255:
                raise ValueError("Project name must be at most 255 characters.")

        async with self._factory() as session:
            result = await session.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if project is None:
                return None

            if name is not None:
                project.name = name
            if config is not None:
                project.config_json = config
            project.updated_at = _utcnow()

            await session.commit()
            await session.refresh(project)
            updated = _project_to_dict(project)

        logger.info("Project updated: id=%s", project_id)
        return updated

    async def delete_project(self, project_id: str) -> bool:
        """Delete a project and all its child records (cascade).

        In ``row_level`` mode the cascade is handled by the DB-level
        ``ON DELETE CASCADE`` foreign keys on conversations → messages,
        agent_actions, and memory.

        In ``per_db`` mode, the per-project SQLite file is also deleted after
        the registry record is removed.

        Args:
            project_id: UUID of the project to delete.

        Returns:
            ``True`` if a project was deleted; ``False`` if not found.
        """
        async with self._factory() as session:
            result = await session.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if project is None:
                return False

            await session.delete(project)
            await session.commit()

        if self.is_per_db_mode:
            await self._delete_per_project_db(project_id)

        logger.info("Project deleted (cascade): id=%s", project_id)
        return True

    async def project_exists(self, project_id: str) -> bool:
        """Return True if a project with the given UUID exists.

        Lighter than ``get_project`` — only checks existence, no row fetch.
        """
        async with self._factory() as session:
            result = await session.execute(select(Project.id).where(Project.id == project_id))
            return result.scalar_one_or_none() is not None
