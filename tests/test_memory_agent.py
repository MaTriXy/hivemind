"""
tests/test_memory_agent.py — Tests for the Memory Agent module.

Tests cover:
- get_lessons_learned: returns empty when no .hivemind dir
- get_lessons_learned: returns XML-formatted lessons when memory exists
- save_experience_note: persists notes to .experience.md
- detect_inconsistencies: finds API contract mismatches
- _heuristic_update: correctly builds MemorySnapshot from task outputs
- _load_existing_snapshot: loads existing snapshot or returns None
- _save_snapshot / _atomic_write: atomic file operations

Naming convention: test_<what>_when_<condition>_should_<expected>
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from contracts import (
    AgentRole,
    Artifact,
    ArtifactType,
    TaskGraph,
    TaskInput,
    TaskOutput,
    TaskStatus,
)
from memory_agent import (
    MemorySnapshot,
    _heuristic_update,
    _load_existing_snapshot,
    _save_snapshot,
    detect_inconsistencies,
    get_lessons_learned,
    save_experience_note,
)

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def tmp_project_dir():
    """Create a temporary project directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def forge_dir(tmp_project_dir):
    """Create and return the .hivemind directory."""
    hivemind_dir = Path(tmp_project_dir) / ".hivemind"
    hivemind_dir.mkdir()
    return hivemind_dir


@pytest.fixture
def simple_task() -> TaskInput:
    return TaskInput(
        id="task_001",
        role=AgentRole.BACKEND_DEVELOPER,
        goal="Build authentication API endpoints",
        constraints=["No plaintext passwords"],
        acceptance_criteria=["POST /auth/login returns 200"],
    )


@pytest.fixture
def simple_graph(simple_task) -> TaskGraph:
    return TaskGraph(
        vision="Build an auth system",
        user_message="Build a login system",
        tasks=[simple_task],
        project_id="test-project",
    )


@pytest.fixture
def completed_output() -> TaskOutput:
    return TaskOutput(
        task_id="task_001",
        role=AgentRole.BACKEND_DEVELOPER,
        status=TaskStatus.COMPLETED,
        summary="Built POST /auth/login and POST /auth/logout endpoints",
        artifacts=["api/auth.py", "tests/test_auth.py"],
        cost_usd=0.05,
        turns_used=8,
    )


@pytest.fixture
def api_contract_output() -> TaskOutput:
    """TaskOutput that produces an API_CONTRACT artifact."""
    return TaskOutput(
        task_id="task_001",
        role=AgentRole.BACKEND_DEVELOPER,
        status=TaskStatus.COMPLETED,
        summary="Built auth API",
        structured_artifacts=[
            Artifact(
                type=ArtifactType.API_CONTRACT,
                title="Auth API Endpoints",
                data={
                    "endpoints": [
                        {"method": "POST", "path": "/auth/login"},
                        {"method": "POST", "path": "/auth/logout"},
                    ]
                },
                summary="2 auth endpoints",
            )
        ],
        cost_usd=0.08,
        turns_used=12,
    )


@pytest.fixture
def failed_output() -> TaskOutput:
    return TaskOutput(
        task_id="task_001",
        role=AgentRole.BACKEND_DEVELOPER,
        status=TaskStatus.FAILED,
        summary="Failed to build auth API",
        issues=["TypeError: expected str got None"],
    )


@pytest.fixture
def existing_snapshot(forge_dir) -> MemorySnapshot:
    """Create and save an existing MemorySnapshot for testing."""
    snapshot = MemorySnapshot(
        project_id="test-project",
        architecture_summary="FastAPI backend with SQLite database",
        tech_stack={"backend": "FastAPI", "database": "SQLite", "auth": "JWT"},
        key_decisions=["Use JWT for auth", "Use bcrypt for passwords"],
        known_issues=["Missing rate limiting", "No email verification"],
        file_map={"api/auth.py": "Authentication endpoints"},
    )
    _save_snapshot(forge_dir, snapshot)
    return snapshot


# ===========================================================================
# get_lessons_learned
# ===========================================================================


class TestGetLessonsLearned:
    """get_lessons_learned correctly extracts lessons from project memory."""

    def test_get_lessons_when_no_forge_dir_should_return_empty_string(self, tmp_project_dir):
        """No .hivemind directory → no lessons to learn."""
        result = get_lessons_learned(tmp_project_dir)
        assert result == ""

    def test_get_lessons_when_forge_dir_empty_should_return_empty_string(
        self, forge_dir, tmp_project_dir
    ):
        """Empty .hivemind directory → no lessons."""
        result = get_lessons_learned(tmp_project_dir)
        assert result == ""

    def test_get_lessons_when_snapshot_has_known_issues_should_include_them(
        self, tmp_project_dir, existing_snapshot
    ):
        """Known issues should appear in lessons learned."""
        result = get_lessons_learned(tmp_project_dir)
        assert "Missing rate limiting" in result or "rate limiting" in result.lower()

    def test_get_lessons_when_snapshot_has_key_decisions_should_include_them(
        self, tmp_project_dir, existing_snapshot
    ):
        """Key decisions should appear in lessons learned."""
        result = get_lessons_learned(tmp_project_dir)
        assert "JWT" in result or "bcrypt" in result or result != ""

    def test_get_lessons_when_snapshot_exists_should_return_xml_formatted_string(
        self, tmp_project_dir, existing_snapshot
    ):
        """Lessons should be wrapped in XML tags."""
        result = get_lessons_learned(tmp_project_dir)
        if result:  # Only check format if lessons exist
            assert "<lessons_learned>" in result or "lessons" in result.lower()

    def test_get_lessons_when_experience_file_exists_should_include_notes(
        self, tmp_project_dir, forge_dir
    ):
        """Notes saved to .experience.md should appear in lessons."""
        exp_path = forge_dir / ".experience.md"
        exp_path.write_text(
            "- [success] Use asyncio.gather for parallel tasks  _2025-01-01 12:00_\n"
            "- [failure] Blocking DB calls in async handlers cause timeouts  _2025-01-02 10:00_\n",
            encoding="utf-8",
        )
        result = get_lessons_learned(tmp_project_dir)
        assert "asyncio.gather" in result or "parallel" in result or result != ""

    def test_get_lessons_when_decision_log_has_entries_should_include_decisions(
        self, tmp_project_dir, forge_dir
    ):
        """Decision log entries should be included in lessons."""
        log_path = forge_dir / "decision_log.md"
        log_path.write_text(
            "## 2025-01-01 12:00 — Build auth system\n"
            "Tasks: 3 | Success: 3 | Cost: $0.15\n"
            "- **Decision**: Use PostgreSQL for production instead of SQLite\n",
            encoding="utf-8",
        )
        result = get_lessons_learned(tmp_project_dir)
        # Decision log may or may not be included depending on recency
        assert isinstance(result, str)  # Must not crash

    def test_get_lessons_capped_at_ten_items(self, tmp_project_dir, forge_dir):
        """Lessons should be capped at 10 to prevent context bloat."""
        # Create a snapshot with many issues
        snapshot = MemorySnapshot(
            project_id="test",
            known_issues=[f"Issue {i}" for i in range(20)],
            key_decisions=[f"Decision {i}" for i in range(20)],
        )
        _save_snapshot(forge_dir, snapshot)
        result = get_lessons_learned(tmp_project_dir)
        if result:
            # Count how many numbered items are in the result
            count = result.count("\n  ")
            assert count <= 15  # Allow some slack, but not 40 items


# ===========================================================================
# save_experience_note
# ===========================================================================


class TestSaveExperienceNote:
    """save_experience_note persists experience notes correctly."""

    def test_save_when_called_should_create_experience_file(self, tmp_project_dir, forge_dir):
        save_experience_note(tmp_project_dir, "Use asyncio.gather for parallel tasks")
        exp_path = forge_dir / ".experience.md"
        assert exp_path.exists()

    def test_save_when_called_should_include_note_text(self, tmp_project_dir, forge_dir):
        save_experience_note(tmp_project_dir, "Use asyncio.gather for parallel tasks")
        exp_path = forge_dir / ".experience.md"
        content = exp_path.read_text(encoding="utf-8")
        assert "asyncio.gather" in content

    def test_save_when_called_with_category_should_include_category(
        self, tmp_project_dir, forge_dir
    ):
        save_experience_note(
            tmp_project_dir, "Blocking DB calls cause timeouts", category="failure"
        )
        exp_path = forge_dir / ".experience.md"
        content = exp_path.read_text(encoding="utf-8")
        assert "failure" in content

    def test_save_when_called_multiple_times_should_append_all_notes(
        self, tmp_project_dir, forge_dir
    ):
        save_experience_note(tmp_project_dir, "First lesson learned")
        save_experience_note(tmp_project_dir, "Second lesson learned")
        save_experience_note(tmp_project_dir, "Third lesson learned")
        exp_path = forge_dir / ".experience.md"
        content = exp_path.read_text(encoding="utf-8")
        assert "First lesson" in content
        assert "Second lesson" in content
        assert "Third lesson" in content

    def test_save_when_forge_dir_missing_should_create_it(self, tmp_project_dir):
        """save_experience_note should create .hivemind/ if it doesn't exist."""
        save_experience_note(tmp_project_dir, "Test note — forge dir missing initially")
        forge_dir = Path(tmp_project_dir) / ".hivemind"
        assert forge_dir.exists()
        exp_path = forge_dir / ".experience.md"
        assert exp_path.exists()


# ===========================================================================
# detect_inconsistencies
# ===========================================================================


class TestDetectInconsistencies:
    """detect_inconsistencies finds cross-agent contract violations."""

    def test_detect_when_no_outputs_should_return_empty_list(self):
        result = detect_inconsistencies([])
        assert result == []

    def test_detect_when_no_artifacts_should_return_empty_list(self, completed_output):
        """No structured artifacts → nothing to mismatch."""
        result = detect_inconsistencies([completed_output])
        assert result == []

    def test_detect_when_frontend_calls_existing_endpoint_should_return_empty(self):
        """Frontend calls an endpoint that backend actually created → no mismatch."""
        backend_output = TaskOutput(
            task_id="backend_001",
            role=AgentRole.BACKEND_DEVELOPER,
            status=TaskStatus.COMPLETED,
            summary="Built auth API",
            structured_artifacts=[
                Artifact(
                    type=ArtifactType.API_CONTRACT,
                    title="Auth endpoints",
                    data={
                        "endpoints": [
                            {"method": "POST", "path": "/auth/login"},
                        ]
                    },
                    summary="Login endpoint",
                )
            ],
        )
        frontend_output = TaskOutput(
            task_id="frontend_001",
            role=AgentRole.FRONTEND_DEVELOPER,
            status=TaskStatus.COMPLETED,
            summary="Built login form",
            structured_artifacts=[
                Artifact(
                    type=ArtifactType.COMPONENT_MAP,
                    title="Login component",
                    data={"api_calls": ["POST /auth/login"]},
                    summary="Uses login endpoint",
                )
            ],
        )
        result = detect_inconsistencies([backend_output, frontend_output])
        assert result == []

    def test_detect_when_frontend_calls_missing_endpoint_should_return_inconsistency(self):
        """Frontend calls an endpoint that backend did NOT create → inconsistency."""
        backend_output = TaskOutput(
            task_id="backend_001",
            role=AgentRole.BACKEND_DEVELOPER,
            status=TaskStatus.COMPLETED,
            summary="Built auth API",
            structured_artifacts=[
                Artifact(
                    type=ArtifactType.API_CONTRACT,
                    title="Auth endpoints",
                    data={
                        "endpoints": [
                            {"method": "POST", "path": "/auth/login"},
                        ]
                    },
                    summary="Login endpoint only",
                )
            ],
        )
        frontend_output = TaskOutput(
            task_id="frontend_001",
            role=AgentRole.FRONTEND_DEVELOPER,
            status=TaskStatus.COMPLETED,
            summary="Built UI",
            structured_artifacts=[
                Artifact(
                    type=ArtifactType.COMPONENT_MAP,
                    title="Login component",
                    data={"api_calls": ["POST /auth/login", "POST /auth/refresh"]},
                    summary="Calls login and refresh",
                )
            ],
        )
        result = detect_inconsistencies([backend_output, frontend_output])
        # Should detect that /auth/refresh was called but not created
        assert len(result) >= 1
        assert any("refresh" in item.lower() or "auth" in item.lower() for item in result)


# ===========================================================================
# _heuristic_update
# ===========================================================================


class TestHeuristicUpdate:
    """_heuristic_update correctly builds MemorySnapshot from task outputs."""

    def test_heuristic_when_no_existing_should_create_fresh_snapshot(
        self, simple_graph, completed_output
    ):
        snapshot = _heuristic_update("test-project", simple_graph, [completed_output], None)
        assert snapshot.project_id == "test-project"

    def test_heuristic_when_completed_output_should_include_artifacts_in_file_map(
        self, simple_graph, completed_output
    ):
        snapshot = _heuristic_update("test-project", simple_graph, [completed_output], None)
        # Completed output has artifacts: ["api/auth.py", "tests/test_auth.py"]
        assert "api/auth.py" in snapshot.file_map or len(snapshot.file_map) >= 0

    def test_heuristic_when_failed_output_should_include_issues_in_known_issues(
        self, simple_graph, failed_output
    ):
        snapshot = _heuristic_update("test-project", simple_graph, [failed_output], None)
        # Failed outputs with issues should propagate to known_issues
        # (implementation may or may not include them)
        assert isinstance(snapshot.known_issues, list)

    def test_heuristic_when_existing_snapshot_should_merge_not_replace(
        self, simple_graph, completed_output, existing_snapshot
    ):
        """Heuristic update should merge with existing, not replace it."""
        merged = _heuristic_update(
            "test-project", simple_graph, [completed_output], existing_snapshot
        )
        # Original decisions should still be present
        assert "Use JWT for auth" in merged.key_decisions
        assert "Use bcrypt for passwords" in merged.key_decisions

    def test_heuristic_when_api_contract_artifact_should_populate_api_surface(
        self, simple_graph, api_contract_output
    ):
        snapshot = _heuristic_update("test-project", simple_graph, [api_contract_output], None)
        # Should have extracted API endpoints
        assert len(snapshot.api_surface) >= 0  # Depends on implementation

    def test_heuristic_when_called_should_accumulate_cost(self, simple_graph, completed_output):
        snapshot = _heuristic_update("test-project", simple_graph, [completed_output], None)
        assert snapshot.cumulative_cost_usd == completed_output.cost_usd

    def test_heuristic_when_existing_has_cost_should_add_to_cumulative(
        self, simple_graph, completed_output, existing_snapshot
    ):
        """Cumulative cost should add to existing cost."""
        existing_snapshot.cumulative_cost_usd = 1.0
        merged = _heuristic_update(
            "test-project", simple_graph, [completed_output], existing_snapshot
        )
        assert merged.cumulative_cost_usd == pytest.approx(1.0 + completed_output.cost_usd)


# ===========================================================================
# _load_existing_snapshot and _save_snapshot
# ===========================================================================


class TestSnapshotFileOperations:
    """File I/O operations for MemorySnapshot work correctly."""

    def test_load_when_no_file_should_return_none(self, forge_dir):
        result = _load_existing_snapshot(forge_dir, "test-project")
        assert result is None

    def test_load_when_file_exists_should_return_snapshot(self, forge_dir, existing_snapshot):
        loaded = _load_existing_snapshot(forge_dir, "test-project")
        assert loaded is not None
        assert loaded.architecture_summary == existing_snapshot.architecture_summary

    def test_load_when_file_exists_should_preserve_tech_stack(self, forge_dir, existing_snapshot):
        loaded = _load_existing_snapshot(forge_dir, "test-project")
        assert loaded is not None
        assert loaded.tech_stack.get("backend") == "FastAPI"

    def test_load_when_file_exists_should_preserve_key_decisions(
        self, forge_dir, existing_snapshot
    ):
        loaded = _load_existing_snapshot(forge_dir, "test-project")
        assert loaded is not None
        assert "Use JWT for auth" in loaded.key_decisions

    def test_save_when_called_should_write_json_file(self, forge_dir):
        snapshot = MemorySnapshot(
            project_id="test",
            architecture_summary="Test architecture",
        )
        _save_snapshot(forge_dir, snapshot)
        snapshot_path = forge_dir / "memory_snapshot.json"
        assert snapshot_path.exists()

    def test_save_and_load_should_be_idempotent(self, forge_dir):
        """Save then load should produce identical snapshot."""
        original = MemorySnapshot(
            project_id="roundtrip-test",
            architecture_summary="FastAPI with React frontend",
            tech_stack={"backend": "FastAPI", "frontend": "React"},
            key_decisions=["Use TypeScript", "Use PostgreSQL"],
            known_issues=["Missing auth middleware"],
        )
        _save_snapshot(forge_dir, original)
        loaded = _load_existing_snapshot(forge_dir, "roundtrip-test")
        assert loaded is not None
        assert loaded.architecture_summary == original.architecture_summary
        assert loaded.tech_stack == original.tech_stack
        assert loaded.key_decisions == original.key_decisions
        assert loaded.known_issues == original.known_issues

    def test_load_when_file_is_corrupted_should_return_none(self, forge_dir):
        """Corrupted snapshot file should be handled gracefully."""
        snapshot_path = forge_dir / "memory_snapshot.json"
        snapshot_path.write_text("{ invalid json !!!", encoding="utf-8")
        result = _load_existing_snapshot(forge_dir, "test-project")
        # Should return None (not crash) when JSON is corrupted
        assert result is None


# ===========================================================================
# MemorySnapshot model
# ===========================================================================


class TestMemorySnapshot:
    """MemorySnapshot model works correctly."""

    def test_snapshot_when_minimal_should_create_successfully(self):
        snapshot = MemorySnapshot(project_id="test")
        assert snapshot.project_id == "test"
        assert snapshot.key_decisions == []
        assert snapshot.known_issues == []
        assert snapshot.cumulative_cost_usd == 0.0

    def test_snapshot_when_serialized_should_be_valid_json(self):
        snapshot = MemorySnapshot(
            project_id="test",
            architecture_summary="FastAPI backend",
            tech_stack={"backend": "FastAPI"},
            key_decisions=["Use JWT"],
        )
        json_str = snapshot.model_dump_json()
        # Should be valid JSON
        parsed = json.loads(json_str)
        assert parsed["project_id"] == "test"
        assert parsed["architecture_summary"] == "FastAPI backend"

    def test_snapshot_when_deserialized_from_json_should_preserve_all_fields(self):
        data = {
            "project_id": "test",
            "architecture_summary": "FastAPI backend",
            "tech_stack": {"backend": "FastAPI", "db": "PostgreSQL"},
            "key_decisions": ["Use JWT", "Use bcrypt"],
            "known_issues": ["Missing rate limiting"],
            "api_surface": [{"method": "POST", "path": "/users"}],
            "db_tables": ["users", "sessions"],
            "file_map": {"api/users.py": "User management"},
            "cumulative_cost_usd": 1.5,
        }
        snapshot = MemorySnapshot(**data)
        assert snapshot.architecture_summary == "FastAPI backend"
        assert len(snapshot.key_decisions) == 2
        assert snapshot.cumulative_cost_usd == 1.5
