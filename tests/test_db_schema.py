"""Tests for the platform persistence layer (src/db/).

Tests cover:
- ORM model creation and relationships (all 5 tables)
- Cascade DELETE referential integrity
- Alembic migration can run on a fresh DB
- DATABASE_URL driver auto-upgrade logic
- UNIQUE constraint on memory(project_id, key)

All tests use an in-memory SQLite database via init_db() to avoid
file-system side effects.  Each test gets a fresh engine + session.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Engine / session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def event_loop():
    """Create a new event loop for each test to avoid state leakage."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def engine():
    """Create a fresh in-memory SQLite engine for each test.

    The ``event.listens_for`` hook enables FK enforcement per-connection
    (SQLite requires ``PRAGMA foreign_keys=ON`` — it is off by default).
    """
    from sqlalchemy import event

    from src.db.models import Base

    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _enforce_fks(dbapi_conn, _rec):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture(scope="function")
async def session(engine):
    """Provide an AsyncSession bound to the in-memory engine."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Test: Project model
# ---------------------------------------------------------------------------


class TestProjectModel:
    async def test_create_project(self, session: AsyncSession) -> None:
        from src.db.models import Project

        proj = Project(name="My Project", config_json={"budget_usd": 50.0})
        session.add(proj)
        await session.flush()

        assert proj.id is not None
        assert len(proj.id) == 36  # UUID string
        assert proj.name == "My Project"
        assert proj.config_json == {"budget_usd": 50.0}
        assert proj.created_at is not None
        assert proj.updated_at is not None

    async def test_project_uuid_uniqueness(self, session: AsyncSession) -> None:
        from src.db.models import Project

        p1 = Project(name="A")
        p2 = Project(name="B")
        session.add_all([p1, p2])
        await session.flush()

        assert p1.id != p2.id

    async def test_project_query(self, session: AsyncSession) -> None:
        from src.db.models import Project

        session.add_all(
            [
                Project(name="Alpha"),
                Project(name="Beta"),
                Project(name="Gamma"),
            ]
        )
        await session.commit()

        result = await session.execute(select(Project).order_by(Project.name))
        projects = result.scalars().all()
        assert len(projects) == 3
        assert [p.name for p in projects] == ["Alpha", "Beta", "Gamma"]


# ---------------------------------------------------------------------------
# Test: Conversation model
# ---------------------------------------------------------------------------


class TestConversationModel:
    async def test_create_conversation(self, session: AsyncSession) -> None:
        from src.db.models import Conversation, Project

        proj = Project(name="Test")
        session.add(proj)
        await session.flush()

        conv = Conversation(project_id=proj.id, title="First chat")
        session.add(conv)
        await session.flush()

        assert conv.id is not None
        assert conv.project_id == proj.id
        assert conv.title == "First chat"
        assert conv.last_active_at is not None

    async def test_conversation_requires_project(self, session: AsyncSession) -> None:
        from src.db.models import Conversation

        conv = Conversation(project_id=str(uuid.uuid4()), title="Orphan")
        session.add(conv)
        with pytest.raises(Exception):
            await session.flush()


# ---------------------------------------------------------------------------
# Test: Message model
# ---------------------------------------------------------------------------


class TestMessageModel:
    async def test_create_messages(self, session: AsyncSession) -> None:
        from src.db.models import Conversation, Message, Project

        proj = Project(name="P")
        session.add(proj)
        await session.flush()

        conv = Conversation(project_id=proj.id)
        session.add(conv)
        await session.flush()

        msgs = [
            Message(conversation_id=conv.id, role="user", content="Hello"),
            Message(
                conversation_id=conv.id,
                role="assistant",
                content="Hi!",
                metadata_json={"model": "claude-3-5-sonnet", "cost_usd": 0.001},
            ),
        ]
        session.add_all(msgs)
        await session.commit()

        result = await session.execute(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.timestamp)
        )
        fetched = result.scalars().all()
        assert len(fetched) == 2
        assert fetched[0].role == "user"
        assert fetched[1].role == "assistant"
        assert fetched[1].metadata_json["cost_usd"] == 0.001

    async def test_message_roles(self, session: AsyncSession) -> None:
        """All four roles must be storable."""
        from src.db.models import Conversation, Message, Project

        proj = Project(name="P")
        session.add(proj)
        await session.flush()
        conv = Conversation(project_id=proj.id)
        session.add(conv)
        await session.flush()

        for role in ("user", "assistant", "system", "tool"):
            session.add(Message(conversation_id=conv.id, role=role, content="test"))
        await session.commit()

        result = await session.execute(select(Message))
        roles = {m.role for m in result.scalars().all()}
        assert roles == {"user", "assistant", "system", "tool"}


# ---------------------------------------------------------------------------
# Test: AgentAction model
# ---------------------------------------------------------------------------


class TestAgentActionModel:
    async def test_create_agent_action(self, session: AsyncSession) -> None:
        from src.db.models import AgentAction, Conversation, Project

        proj = Project(name="P")
        session.add(proj)
        await session.flush()
        conv = Conversation(project_id=proj.id)
        session.add(conv)
        await session.flush()

        action = AgentAction(
            conversation_id=conv.id,
            agent_role="orchestrator",
            action_type="tool_call",
            payload_json={"tool": "bash", "command": "ls -la"},
            result_json={"exit_code": 0, "output": "..."},
        )
        session.add(action)
        await session.commit()

        result = await session.execute(select(AgentAction))
        fetched = result.scalars().first()
        assert fetched.agent_role == "orchestrator"
        assert fetched.action_type == "tool_call"
        assert fetched.payload_json["tool"] == "bash"
        assert fetched.result_json["exit_code"] == 0

    async def test_in_progress_action_has_null_result(self, session: AsyncSession) -> None:
        from src.db.models import AgentAction, Conversation, Project

        proj = Project(name="P")
        session.add(proj)
        await session.flush()
        conv = Conversation(project_id=proj.id)
        session.add(conv)
        await session.flush()

        action = AgentAction(
            conversation_id=conv.id,
            agent_role="pm",
            action_type="decision",
            payload_json={"plan": "..."},
            result_json=None,  # still in-progress
        )
        session.add(action)
        await session.commit()

        result = await session.execute(select(AgentAction))
        fetched = result.scalars().first()
        assert fetched.result_json is None


# ---------------------------------------------------------------------------
# Test: Memory model
# ---------------------------------------------------------------------------


class TestMemoryModel:
    async def test_create_memory(self, session: AsyncSession) -> None:
        from src.db.models import Memory, Project

        proj = Project(name="P")
        session.add(proj)
        await session.flush()

        mem = Memory(project_id=proj.id, key="user.name", value_json="Alice")
        session.add(mem)
        await session.commit()

        result = await session.execute(select(Memory).where(Memory.project_id == proj.id))
        fetched = result.scalars().first()
        assert fetched.key == "user.name"
        assert fetched.value_json == "Alice"

    async def test_memory_unique_key_per_project(self, session: AsyncSession) -> None:
        """Duplicate (project_id, key) must raise IntegrityError."""
        from src.db.models import Memory, Project

        proj = Project(name="P")
        session.add(proj)
        await session.flush()

        session.add(Memory(project_id=proj.id, key="foo", value_json=1))
        await session.flush()

        session.add(Memory(project_id=proj.id, key="foo", value_json=2))
        with pytest.raises((IntegrityError, Exception)):
            await session.flush()

    async def test_memory_same_key_different_projects(self, session: AsyncSession) -> None:
        """Same key in different projects must be allowed."""
        from src.db.models import Memory, Project

        p1 = Project(name="P1")
        p2 = Project(name="P2")
        session.add_all([p1, p2])
        await session.flush()

        session.add(Memory(project_id=p1.id, key="config", value_json={"a": 1}))
        session.add(Memory(project_id=p2.id, key="config", value_json={"b": 2}))
        await session.commit()

        result = await session.execute(select(Memory).order_by(Memory.key))
        entries = result.scalars().all()
        assert len(entries) == 2

    async def test_memory_json_types(self, session: AsyncSession) -> None:
        """value_json should accept str, int, list, dict, bool, None."""
        from src.db.models import Memory, Project

        proj = Project(name="P")
        session.add(proj)
        await session.flush()

        test_cases = [
            ("k1", "string value"),
            ("k2", 42),
            ("k3", 3.14),
            ("k4", True),
            ("k5", [1, 2, 3]),
            ("k6", {"nested": {"key": "val"}}),
            ("k7", None),
        ]
        for key, value in test_cases:
            session.add(Memory(project_id=proj.id, key=key, value_json=value))
        await session.commit()

        result = await session.execute(select(Memory).where(Memory.project_id == proj.id))
        entries = {m.key: m.value_json for m in result.scalars().all()}
        for key, expected in test_cases:
            assert entries[key] == expected


# ---------------------------------------------------------------------------
# Test: Cascade DELETE
# ---------------------------------------------------------------------------


class TestCascadeDelete:
    async def test_delete_project_cascades_all(self, session: AsyncSession) -> None:
        """Deleting a project must cascade to conversations, messages, actions, memory."""
        from src.db.models import (
            AgentAction,
            Conversation,
            Memory,
            Message,
            Project,
        )

        proj = Project(name="ToDelete")
        session.add(proj)
        await session.flush()

        conv = Conversation(project_id=proj.id, title="c1")
        session.add(conv)
        await session.flush()

        session.add(Message(conversation_id=conv.id, role="user", content="hi"))
        session.add(AgentAction(conversation_id=conv.id, agent_role="pm", action_type="decision"))
        session.add(Memory(project_id=proj.id, key="x", value_json=1))
        await session.commit()

        # Delete the project
        proj_to_delete = await session.get(Project, proj.id)
        await session.delete(proj_to_delete)
        await session.commit()

        # All children must be gone
        assert len((await session.execute(select(Conversation))).scalars().all()) == 0
        assert len((await session.execute(select(Message))).scalars().all()) == 0
        assert len((await session.execute(select(AgentAction))).scalars().all()) == 0
        assert len((await session.execute(select(Memory))).scalars().all()) == 0

    async def test_delete_conversation_cascades_messages_and_actions(
        self, session: AsyncSession
    ) -> None:
        from src.db.models import AgentAction, Conversation, Message, Project

        proj = Project(name="P")
        session.add(proj)
        await session.flush()

        conv = Conversation(project_id=proj.id)
        session.add(conv)
        await session.flush()

        session.add(Message(conversation_id=conv.id, role="user", content="hi"))
        session.add(
            AgentAction(conversation_id=conv.id, agent_role="orchestrator", action_type="tool_call")
        )
        await session.commit()

        conv_to_delete = await session.get(Conversation, conv.id)
        await session.delete(conv_to_delete)
        await session.commit()

        assert len((await session.execute(select(Message))).scalars().all()) == 0
        assert len((await session.execute(select(AgentAction))).scalars().all()) == 0
        # Project must still exist
        assert await session.get(Project, proj.id) is not None


# ---------------------------------------------------------------------------
# Test: DATABASE_URL driver upgrade
# ---------------------------------------------------------------------------


class TestDatabaseURLUpgrade:
    def test_sqlite_upgrade(self) -> None:
        from src.db.database import _upgrade_driver

        assert _upgrade_driver("sqlite:///data/db.db") == "sqlite+aiosqlite:///data/db.db"
        assert _upgrade_driver("sqlite+aiosqlite:///data/db.db") == "sqlite+aiosqlite:///data/db.db"

    def test_postgresql_upgrade(self) -> None:
        from src.db.database import _upgrade_driver

        assert (
            _upgrade_driver("postgresql://user:pw@host/db")
            == "postgresql+asyncpg://user:pw@host/db"
        )
        assert (
            _upgrade_driver("postgres://user:pw@host/db") == "postgresql+asyncpg://user:pw@host/db"
        )
        assert (
            _upgrade_driver("postgresql+asyncpg://user:pw@host/db")
            == "postgresql+asyncpg://user:pw@host/db"
        )

    def test_unknown_url_unchanged(self) -> None:
        from src.db.database import _upgrade_driver

        assert _upgrade_driver("mysql://user:pw@host/db") == "mysql://user:pw@host/db"


# ---------------------------------------------------------------------------
# Test: Alembic migration on fresh DB
# ---------------------------------------------------------------------------


class TestAlembicMigration:
    def test_migration_runs_on_fresh_db(self, tmp_path) -> None:
        """Verify alembic upgrade head runs without error on a fresh SQLite file."""
        import subprocess
        import sys

        db_path = str(tmp_path / "fresh.db")
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            env={**os.environ, "PLATFORM_DB_PATH": db_path},
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0, (
            f"alembic upgrade failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
        assert (
            "Running upgrade" in result.stderr
            or "Running upgrade" in result.stdout
            or result.returncode == 0
        )

    def test_migration_is_idempotent(self, tmp_path) -> None:
        """Running upgrade head twice must not error (already-at-head is a no-op)."""
        import subprocess
        import sys

        db_path = str(tmp_path / "idem.db")
        env = {**os.environ, "PLATFORM_DB_PATH": db_path}
        cwd = str(Path(__file__).resolve().parent.parent)

        r1 = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
        )
        assert r1.returncode == 0

        r2 = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
        )
        assert r2.returncode == 0, f"Second upgrade failed: {r2.stderr}"

    def test_downgrade_and_upgrade(self, tmp_path) -> None:
        """downgrade base → upgrade head must work cleanly."""
        import subprocess
        import sys

        db_path = str(tmp_path / "cycle.db")
        env = {**os.environ, "PLATFORM_DB_PATH": db_path}
        cwd = str(Path(__file__).resolve().parent.parent)

        for cmd in [
            ["upgrade", "head"],
            ["downgrade", "base"],
            ["upgrade", "head"],
        ]:
            r = subprocess.run(
                [sys.executable, "-m", "alembic", *cmd],
                capture_output=True,
                text=True,
                env=env,
                cwd=cwd,
            )
            assert r.returncode == 0, f"alembic {' '.join(cmd)} failed: {r.stderr}"
