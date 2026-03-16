"""Tests for ProjectManager and /api/projects REST endpoints.

Covers:
- ProjectManager.create_project / get_project / list_projects / update_project / delete_project
- project_exists / project_id UUID generation
- Cascade delete: deleting project removes conversations, messages, memory
- ISOLATION_MODE=per_db gating (file creation/deletion)
- REST endpoints: POST, GET (list), GET (detail), PATCH, DELETE
- RFC 7807 error format
- UUID validation (enumeration prevention)
- Zero data leakage between two independent projects
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from src.db.models import Base, Conversation, Memory, Message
from src.projects.project_manager import ProjectManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def engine():
    """In-memory SQLite engine (shared connection for test isolation)."""
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )


@pytest_asyncio.fixture
async def mgr(session_factory):
    """ProjectManager backed by in-memory SQLite."""
    return ProjectManager(session_factory)


# ---------------------------------------------------------------------------
# Helper: seed a conversation + messages + memory under a project
# ---------------------------------------------------------------------------


async def _seed_children(session_factory, project_id: str):
    """Create one conversation with one message and one memory entry."""
    conv_id = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())
    mem_id = str(uuid.uuid4())

    from datetime import datetime

    now = datetime.now(UTC)

    async with session_factory() as session:
        session.add(
            Conversation(
                id=conv_id,
                project_id=project_id,
                title="seed conv",
                created_at=now,
                last_active_at=now,
            )
        )
        await session.flush()

        session.add(
            Message(
                id=msg_id,
                conversation_id=conv_id,
                role="user",
                content="hello",
                timestamp=now,
            )
        )
        session.add(
            Memory(
                id=mem_id,
                project_id=project_id,
                key="test.key",
                value_json="test-value",
                updated_at=now,
            )
        )
        await session.commit()

    return conv_id, msg_id, mem_id


async def _count(session_factory, model):
    from sqlalchemy import func, select

    async with session_factory() as session:
        result = await session.execute(select(func.count()).select_from(model))
        return result.scalar_one()


# ===========================================================================
# ProjectManager unit tests
# ===========================================================================


class TestProjectManagerCreate:
    async def test_create_returns_dict_with_uuid(self, mgr):
        p = await mgr.create_project("Test Project")
        assert isinstance(p, dict)
        assert "id" in p
        # Verify it's a valid UUID
        uuid.UUID(p["id"])  # raises ValueError if invalid
        assert p["name"] == "Test Project"

    async def test_create_with_config(self, mgr):
        cfg = {"budget_usd": 50, "default_model": "claude-opus-4-6"}
        p = await mgr.create_project("Cfg Project", config=cfg)
        assert p["config"] == cfg

    async def test_create_with_explicit_uuid(self, mgr):
        pid = str(uuid.uuid4())
        p = await mgr.create_project("Explicit UUID", project_id=pid)
        assert p["id"] == pid

    async def test_create_empty_name_raises(self, mgr):
        with pytest.raises(ValueError, match="not be empty"):
            await mgr.create_project("   ")

    async def test_create_name_too_long_raises(self, mgr):
        with pytest.raises(ValueError, match="255 characters"):
            await mgr.create_project("x" * 256)

    async def test_create_invalid_uuid_raises(self, mgr):
        with pytest.raises(ValueError, match="valid UUID"):
            await mgr.create_project("Name", project_id="not-a-uuid")

    async def test_create_persists_across_reads(self, mgr):
        p = await mgr.create_project("Persistent")
        fetched = await mgr.get_project(p["id"])
        assert fetched is not None
        assert fetched["name"] == "Persistent"

    async def test_created_at_and_updated_at_present(self, mgr):
        p = await mgr.create_project("Timestamp Test")
        assert p["created_at"] is not None
        assert p["updated_at"] is not None


class TestProjectManagerRead:
    async def test_get_nonexistent_returns_none(self, mgr):
        result = await mgr.get_project(str(uuid.uuid4()))
        assert result is None

    async def test_project_exists_true(self, mgr):
        p = await mgr.create_project("Exists Check")
        assert await mgr.project_exists(p["id"]) is True

    async def test_project_exists_false(self, mgr):
        assert await mgr.project_exists(str(uuid.uuid4())) is False

    async def test_list_empty(self, mgr):
        projects = await mgr.list_projects()
        assert projects == []

    async def test_list_returns_all(self, mgr):
        await mgr.create_project("Alpha")
        await mgr.create_project("Beta")
        await mgr.create_project("Gamma")
        projects = await mgr.list_projects()
        assert len(projects) == 3

    async def test_list_newest_first(self, mgr):
        a = await mgr.create_project("First")
        b = await mgr.create_project("Second")
        c = await mgr.create_project("Third")
        projects = await mgr.list_projects()
        ids = [p["id"] for p in projects]
        # Newest first: c > b > a
        assert ids.index(c["id"]) < ids.index(b["id"]) < ids.index(a["id"])

    async def test_list_pagination_limit(self, mgr):
        for i in range(5):
            await mgr.create_project(f"Project {i}")
        projects = await mgr.list_projects(limit=3)
        assert len(projects) == 3

    async def test_list_pagination_offset(self, mgr):
        for i in range(5):
            await mgr.create_project(f"Project {i}")
        all_projects = await mgr.list_projects()
        paged = await mgr.list_projects(limit=3, offset=3)
        assert len(paged) == 2
        assert {p["id"] for p in paged}.issubset({p["id"] for p in all_projects})


class TestProjectManagerUpdate:
    async def test_update_name(self, mgr):
        p = await mgr.create_project("Original")
        updated = await mgr.update_project(p["id"], name="Updated")
        assert updated["name"] == "Updated"
        assert updated["id"] == p["id"]

    async def test_update_config(self, mgr):
        p = await mgr.create_project("Config Update", config={"old": "val"})
        updated = await mgr.update_project(p["id"], config={"new": "val"})
        assert updated["config"] == {"new": "val"}

    async def test_update_both(self, mgr):
        p = await mgr.create_project("Both Update")
        updated = await mgr.update_project(p["id"], name="New Name", config={"x": 1})
        assert updated["name"] == "New Name"
        assert updated["config"] == {"x": 1}

    async def test_update_nonexistent_returns_none(self, mgr):
        result = await mgr.update_project(str(uuid.uuid4()), name="Ghost")
        assert result is None

    async def test_update_empty_name_raises(self, mgr):
        p = await mgr.create_project("Valid")
        with pytest.raises(ValueError, match="not be empty"):
            await mgr.update_project(p["id"], name="   ")

    async def test_update_refreshes_updated_at(self, mgr):
        p = await mgr.create_project("Timestamp")
        updated = await mgr.update_project(p["id"], name="New")
        # updated_at should be >= created_at
        assert updated["updated_at"] >= p["created_at"]


class TestProjectManagerDelete:
    async def test_delete_returns_true(self, mgr):
        p = await mgr.create_project("To Delete")
        result = await mgr.delete_project(p["id"])
        assert result is True

    async def test_delete_nonexistent_returns_false(self, mgr):
        result = await mgr.delete_project(str(uuid.uuid4()))
        assert result is False

    async def test_delete_removes_from_list(self, mgr):
        p = await mgr.create_project("Gone")
        await mgr.delete_project(p["id"])
        projects = await mgr.list_projects()
        assert not any(proj["id"] == p["id"] for proj in projects)

    async def test_delete_get_returns_none(self, mgr):
        p = await mgr.create_project("Delete Then Get")
        await mgr.delete_project(p["id"])
        assert await mgr.get_project(p["id"]) is None

    async def test_cascade_delete_conversations(self, mgr, session_factory):
        """Deleting a project must remove all child conversations."""
        p = await mgr.create_project("Cascade Conv")
        conv_id, _, _ = await _seed_children(session_factory, p["id"])

        # Verify conv exists
        assert await _count(session_factory, Conversation) == 1

        await mgr.delete_project(p["id"])

        # Conversation must be gone
        assert await _count(session_factory, Conversation) == 0

    async def test_cascade_delete_messages(self, mgr, session_factory):
        """Deleting a project must remove all descendant messages."""
        p = await mgr.create_project("Cascade Msg")
        _, msg_id, _ = await _seed_children(session_factory, p["id"])

        assert await _count(session_factory, Message) == 1
        await mgr.delete_project(p["id"])
        assert await _count(session_factory, Message) == 0

    async def test_cascade_delete_memory(self, mgr, session_factory):
        """Deleting a project must remove all memory entries."""
        p = await mgr.create_project("Cascade Mem")
        _, _, mem_id = await _seed_children(session_factory, p["id"])

        assert await _count(session_factory, Memory) == 1
        await mgr.delete_project(p["id"])
        assert await _count(session_factory, Memory) == 0

    async def test_cascade_only_affects_target_project(self, mgr, session_factory):
        """Deleting project A must NOT affect project B's data."""
        pa = await mgr.create_project("Project A")
        pb = await mgr.create_project("Project B")

        await _seed_children(session_factory, pa["id"])
        await _seed_children(session_factory, pb["id"])

        assert await _count(session_factory, Conversation) == 2
        assert await _count(session_factory, Memory) == 2

        await mgr.delete_project(pa["id"])

        # Only project B's records remain
        assert await _count(session_factory, Conversation) == 1
        assert await _count(session_factory, Memory) == 1

        # Project B itself still exists
        assert await mgr.get_project(pb["id"]) is not None


# ===========================================================================
# Data isolation between two projects
# ===========================================================================


class TestProjectIsolation:
    """Two projects running in parallel must have zero data leakage."""

    async def test_two_projects_independent_data(self, mgr, session_factory):
        """Conversations and memory are scoped per project."""
        pa = await mgr.create_project("Project Alpha")
        pb = await mgr.create_project("Project Beta")

        # Seed children in both
        await _seed_children(session_factory, pa["id"])
        await _seed_children(session_factory, pb["id"])

        # Both have independent records
        from sqlalchemy import select

        async with session_factory() as session:
            convs_a = (
                (
                    await session.execute(
                        select(Conversation).where(Conversation.project_id == pa["id"])
                    )
                )
                .scalars()
                .all()
            )
            convs_b = (
                (
                    await session.execute(
                        select(Conversation).where(Conversation.project_id == pb["id"])
                    )
                )
                .scalars()
                .all()
            )

        assert len(convs_a) == 1
        assert len(convs_b) == 1
        assert convs_a[0].id != convs_b[0].id

    async def test_project_names_are_independent(self, mgr):
        pa = await mgr.create_project("Shared Name")
        pb = await mgr.create_project("Shared Name")
        # Both may exist independently (no UNIQUE constraint on name)
        assert pa["id"] != pb["id"]
        assert await mgr.project_exists(pa["id"])
        assert await mgr.project_exists(pb["id"])

    async def test_update_one_does_not_affect_other(self, mgr):
        pa = await mgr.create_project("Project One")
        pb = await mgr.create_project("Project Two")

        await mgr.update_project(pa["id"], name="Renamed One", config={"x": 1})

        fetched_b = await mgr.get_project(pb["id"])
        assert fetched_b["name"] == "Project Two"
        assert fetched_b["config"] == {}


# ===========================================================================
# Isolation mode
# ===========================================================================


class TestIsolationMode:
    def test_default_is_row_level(self, mgr):
        assert mgr.is_per_db_mode is False

    def test_per_db_mode_detected(self, session_factory):
        with patch.dict(os.environ, {"ISOLATION_MODE": "per_db"}):
            mgr = ProjectManager(session_factory)
            assert mgr.is_per_db_mode is True

    def test_per_db_mode_not_set(self, session_factory):
        env = {k: v for k, v in os.environ.items() if k != "ISOLATION_MODE"}
        with patch.dict(os.environ, env, clear=True):
            mgr = ProjectManager(session_factory)
            assert mgr.is_per_db_mode is False

    async def test_per_db_creates_file(self, session_factory, tmp_path):
        """In per_db mode, creating a project creates a SQLite file."""
        with patch.dict(os.environ, {"ISOLATION_MODE": "per_db"}):
            mgr = ProjectManager(session_factory, data_dir=tmp_path)
            p = await mgr.create_project("Per DB Project")
            db_file = tmp_path / "projects" / f"{p['id']}.db"
            assert db_file.exists(), f"Expected DB file at {db_file}"

    async def test_per_db_delete_removes_file(self, session_factory, tmp_path):
        """In per_db mode, deleting a project removes the SQLite file."""
        with patch.dict(os.environ, {"ISOLATION_MODE": "per_db"}):
            mgr = ProjectManager(session_factory, data_dir=tmp_path)
            p = await mgr.create_project("Delete Per DB")
            db_file = tmp_path / "projects" / f"{p['id']}.db"
            assert db_file.exists()
            await mgr.delete_project(p["id"])
            assert not db_file.exists(), "DB file should be deleted with the project"

    def test_get_project_db_url_row_level(self, mgr):
        """In row_level mode, db URL resolves to the platform DB."""
        url = mgr.get_project_db_url(str(uuid.uuid4()))
        assert "sqlite" in url or "postgresql" in url

    def test_get_project_db_url_per_db(self, session_factory, tmp_path):
        """In per_db mode, each project gets its own URL."""
        with patch.dict(os.environ, {"ISOLATION_MODE": "per_db"}):
            mgr = ProjectManager(session_factory, data_dir=tmp_path)
            pid = str(uuid.uuid4())
            url = mgr.get_project_db_url(pid)
            assert pid in url
            assert "sqlite+aiosqlite" in url


# ===========================================================================
# REST API endpoint tests
# ===========================================================================


@pytest_asyncio.fixture
async def test_client(session_factory):
    """HTTPX async test client wired to a fresh in-memory DB."""
    from fastapi import FastAPI

    from src.api.projects import projects_router
    from src.dependencies import get_project_manager
    from src.projects.project_manager import ProjectManager

    app = FastAPI()
    app.include_router(projects_router)

    # Override dependency to use our test session factory
    def override_get_pm():
        return ProjectManager(session_factory)

    app.dependency_overrides[get_project_manager] = override_get_pm

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


class TestProjectsAPICreate:
    async def test_create_returns_201(self, test_client):
        resp = await test_client.post("/api/projects", json={"name": "Test"})
        assert resp.status_code == 201

    async def test_create_returns_uuid_id(self, test_client):
        resp = await test_client.post("/api/projects", json={"name": "UUID Test"})
        body = resp.json()
        uuid.UUID(body["id"])  # raises if not valid UUID

    async def test_create_with_config(self, test_client):
        resp = await test_client.post(
            "/api/projects",
            json={"name": "Cfg", "config": {"budget_usd": 50}},
        )
        assert resp.status_code == 201
        assert resp.json()["config"]["budget_usd"] == 50

    async def test_create_with_explicit_uuid(self, test_client):
        pid = str(uuid.uuid4())
        resp = await test_client.post("/api/projects", json={"name": "Explicit", "project_id": pid})
        assert resp.status_code == 201
        assert resp.json()["id"] == pid

    async def test_create_conflict_on_duplicate_uuid(self, test_client):
        pid = str(uuid.uuid4())
        await test_client.post("/api/projects", json={"name": "First", "project_id": pid})
        resp = await test_client.post("/api/projects", json={"name": "Second", "project_id": pid})
        assert resp.status_code == 409
        body = resp.json()
        assert body["status"] == 409
        assert "already exists" in body["detail"]

    async def test_create_empty_name_400(self, test_client):
        resp = await test_client.post("/api/projects", json={"name": "   "})
        assert resp.status_code in (400, 422)

    async def test_create_invalid_uuid_400(self, test_client):
        resp = await test_client.post(
            "/api/projects", json={"name": "Bad", "project_id": "not-a-uuid"}
        )
        assert resp.status_code in (400, 422)


class TestProjectsAPIList:
    async def test_list_empty(self, test_client):
        resp = await test_client.get("/api/projects")
        assert resp.status_code == 200
        body = resp.json()
        assert body["projects"] == []
        assert body["total"] == 0

    async def test_list_shows_created_projects(self, test_client):
        await test_client.post("/api/projects", json={"name": "Alpha"})
        await test_client.post("/api/projects", json={"name": "Beta"})
        resp = await test_client.get("/api/projects")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    async def test_list_includes_isolation_mode(self, test_client):
        resp = await test_client.get("/api/projects")
        assert "isolation_mode" in resp.json()
        assert resp.json()["isolation_mode"] in ("row_level", "per_db")

    async def test_list_pagination_limit(self, test_client):
        for i in range(5):
            await test_client.post("/api/projects", json={"name": f"P{i}"})
        resp = await test_client.get("/api/projects?limit=3")
        assert resp.json()["total"] == 3

    async def test_list_pagination_offset(self, test_client):
        for i in range(5):
            await test_client.post("/api/projects", json={"name": f"Q{i}"})
        resp = await test_client.get("/api/projects?limit=3&offset=3")
        assert resp.json()["total"] == 2


class TestProjectsAPIGet:
    async def test_get_existing_project(self, test_client):
        create = await test_client.post("/api/projects", json={"name": "Gettable"})
        pid = create.json()["id"]
        resp = await test_client.get(f"/api/projects/{pid}")
        assert resp.status_code == 200
        assert resp.json()["id"] == pid

    async def test_get_nonexistent_returns_404(self, test_client):
        resp = await test_client.get(f"/api/projects/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json()["status"] == 404

    async def test_get_invalid_uuid_returns_400(self, test_client):
        resp = await test_client.get("/api/projects/not-a-uuid-at-all")
        assert resp.status_code == 400
        assert resp.json()["status"] == 400

    async def test_get_problem_detail_format(self, test_client):
        resp = await test_client.get(f"/api/projects/{uuid.uuid4()}")
        body = resp.json()
        assert "type" in body
        assert "title" in body
        assert "status" in body
        assert "detail" in body


class TestProjectsAPIPatch:
    async def test_patch_name(self, test_client):
        create = await test_client.post("/api/projects", json={"name": "Original"})
        pid = create.json()["id"]
        resp = await test_client.patch(f"/api/projects/{pid}", json={"name": "Updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    async def test_patch_config(self, test_client):
        create = await test_client.post(
            "/api/projects", json={"name": "Config", "config": {"old": 1}}
        )
        pid = create.json()["id"]
        resp = await test_client.patch(f"/api/projects/{pid}", json={"config": {"new": 2}})
        assert resp.status_code == 200
        assert resp.json()["config"] == {"new": 2}

    async def test_patch_nonexistent_returns_404(self, test_client):
        resp = await test_client.patch(f"/api/projects/{uuid.uuid4()}", json={"name": "Ghost"})
        assert resp.status_code == 404

    async def test_patch_invalid_uuid_returns_400(self, test_client):
        resp = await test_client.patch("/api/projects/not-a-uuid", json={"name": "X"})
        assert resp.status_code == 400

    async def test_patch_no_fields_returns_400(self, test_client):
        create = await test_client.post("/api/projects", json={"name": "NoField"})
        pid = create.json()["id"]
        resp = await test_client.patch(f"/api/projects/{pid}", json={})
        assert resp.status_code == 400


class TestProjectsAPIDelete:
    async def test_delete_returns_200(self, test_client):
        create = await test_client.post("/api/projects", json={"name": "To Delete"})
        pid = create.json()["id"]
        resp = await test_client.delete(f"/api/projects/{pid}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    async def test_delete_removes_project(self, test_client):
        create = await test_client.post("/api/projects", json={"name": "Gone"})
        pid = create.json()["id"]
        await test_client.delete(f"/api/projects/{pid}")
        resp = await test_client.get(f"/api/projects/{pid}")
        assert resp.status_code == 404

    async def test_delete_nonexistent_returns_404(self, test_client):
        resp = await test_client.delete(f"/api/projects/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_delete_invalid_uuid_returns_400(self, test_client):
        resp = await test_client.delete("/api/projects/not-uuid")
        assert resp.status_code == 400

    async def test_delete_does_not_affect_sibling_projects(self, test_client):
        pa = (await test_client.post("/api/projects", json={"name": "Project A"})).json()
        pb = (await test_client.post("/api/projects", json={"name": "Project B"})).json()

        await test_client.delete(f"/api/projects/{pa['id']}")

        resp = await test_client.get(f"/api/projects/{pb['id']}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Project B"

    async def test_delete_response_message_mentions_cascade(self, test_client):
        create = await test_client.post("/api/projects", json={"name": "Cascade Test"})
        pid = create.json()["id"]
        resp = await test_client.delete(f"/api/projects/{pid}")
        body = resp.json()
        assert "conversations" in body.get("message", "").lower() or "cascade" in str(body).lower()


# ===========================================================================
# Persistence (simulated restart)
# ===========================================================================


class TestPersistence:
    async def test_project_survives_manager_recreation(self, session_factory):
        """Simulates a server restart: new ProjectManager instance, same DB."""
        mgr1 = ProjectManager(session_factory)
        p = await mgr1.create_project("Survive Restart", config={"ver": 1})

        # "Restart": create a new manager with the SAME session factory (= same DB)
        mgr2 = ProjectManager(session_factory)
        fetched = await mgr2.get_project(p["id"])

        assert fetched is not None
        assert fetched["name"] == "Survive Restart"
        assert fetched["config"] == {"ver": 1}

    async def test_all_projects_survive_restart(self, session_factory):
        mgr1 = ProjectManager(session_factory)
        ids = []
        for i in range(3):
            p = await mgr1.create_project(f"Project {i}")
            ids.append(p["id"])

        mgr2 = ProjectManager(session_factory)
        projects = await mgr2.list_projects()
        surviving_ids = {p["id"] for p in projects}
        for pid in ids:
            assert pid in surviving_ids
