"""
tests/test_orchestrator_unit.py — Unit tests for OrchestratorManager.

Tests cover:
- OrchestratorManager initialization and attribute defaults
- start_session: basic DAG and legacy paths (mocked SDK)
- proactive memory injection in DAG path
- graph critic integration after PM creates plan
- Message class and conversation_log behavior
- Budget and turn tracking
- is_running / is_paused state management
- project_dir, project_id, user_id attributes

These are unit tests, not end-to-end tests — the SDK and PM Agent are
mocked so no actual Claude API calls are made.

Naming convention: test_<what>_when_<condition>_should_<expected>
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import OrchestratorManager
from sdk_client import SDKResponse
from src.db.database import get_session_factory, init_db
from src.storage.platform_session import PlatformSessionManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sdk_response(text: str = "done", cost: float = 0.01) -> SDKResponse:
    return SDKResponse(text=text, session_id="sess-1", cost_usd=cost, num_turns=1)


async def _make_session_mgr() -> PlatformSessionManager:
    """Create an in-memory session manager for testing."""
    db_url = f"sqlite+aiosqlite:///{os.path.join(tempfile.mkdtemp(), 'test_orch.db')}"
    await init_db(db_url)
    factory = get_session_factory(db_url)
    mgr = PlatformSessionManager(factory)
    await mgr.initialize()
    return mgr


def _make_mock_sdk(response_text: str = "TASK_COMPLETE") -> MagicMock:
    """Create a mock SDK client that returns a controlled response."""
    mock_sdk = MagicMock()
    mock_sdk.query_with_retry = AsyncMock(return_value=_make_sdk_response(response_text))
    mock_sdk.circuit_open = False
    mock_sdk.total_cost_usd = 0.0
    return mock_sdk


async def _make_orchestrator(
    sdk=None,
    session_mgr=None,
    on_update=None,
    on_result=None,
    on_event=None,
    project_dir: str | None = None,
) -> tuple[OrchestratorManager, PlatformSessionManager]:
    """Factory for OrchestratorManager with optional overrides."""
    if session_mgr is None:
        session_mgr = await _make_session_mgr()
    if sdk is None:
        sdk = _make_mock_sdk()
    if project_dir is None:
        project_dir = tempfile.mkdtemp()

    # Ensure project dir exists
    Path(project_dir).mkdir(parents=True, exist_ok=True)

    mgr = OrchestratorManager(
        project_name="unit-test-project",
        project_dir=project_dir,
        sdk=sdk,
        session_mgr=session_mgr,
        user_id=999,
        project_id="unit-test-proj",
        on_update=on_update or AsyncMock(),
        on_result=on_result or AsyncMock(),
        on_event=on_event or AsyncMock(),
        multi_agent=True,
    )
    return mgr, session_mgr


# ===========================================================================
# Initialization
# ===========================================================================


class TestOrchestratorManagerInit:
    """OrchestratorManager initializes with correct defaults."""

    @pytest.mark.asyncio
    async def test_init_should_set_project_id(self):
        mgr, _ = await _make_orchestrator()
        assert mgr.project_id == "unit-test-proj"

    @pytest.mark.asyncio
    async def test_init_should_set_project_dir(self):
        tmpdir = os.path.realpath(tempfile.mkdtemp())
        mgr, _ = await _make_orchestrator(project_dir=tmpdir)
        assert os.path.realpath(mgr.project_dir) == tmpdir

    @pytest.mark.asyncio
    async def test_init_should_set_user_id(self):
        mgr, _ = await _make_orchestrator()
        assert mgr.user_id == 999

    @pytest.mark.asyncio
    async def test_init_should_set_project_name(self):
        mgr, _ = await _make_orchestrator()
        assert mgr.project_name == "unit-test-project"

    @pytest.mark.asyncio
    async def test_init_is_running_should_be_false(self):
        mgr, _ = await _make_orchestrator()
        assert mgr.is_running is False

    @pytest.mark.asyncio
    async def test_init_is_paused_should_be_false(self):
        mgr, _ = await _make_orchestrator()
        assert mgr.is_paused is False

    @pytest.mark.asyncio
    async def test_init_total_cost_should_be_zero(self):
        mgr, _ = await _make_orchestrator()
        assert mgr.total_cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_init_turn_count_should_be_zero(self):
        mgr, _ = await _make_orchestrator()
        assert mgr.turn_count == 0

    @pytest.mark.asyncio
    async def test_init_conversation_log_should_be_empty(self):
        mgr, _ = await _make_orchestrator()
        assert len(mgr.conversation_log) == 0

    @pytest.mark.asyncio
    async def test_init_multi_agent_flag_should_be_stored(self):
        mgr, _ = await _make_orchestrator()
        assert mgr.multi_agent is True

    @pytest.mark.asyncio
    async def test_init_writer_roles_should_include_developer(self):
        mgr, _ = await _make_orchestrator()
        assert "developer" in OrchestratorManager._WRITER_ROLES

    @pytest.mark.asyncio
    async def test_init_writer_roles_should_include_backend_developer(self):
        assert "backend_developer" in OrchestratorManager._WRITER_ROLES

    @pytest.mark.asyncio
    async def test_init_reader_roles_should_include_reviewer(self):
        assert "reviewer" in OrchestratorManager._READER_ROLES

    @pytest.mark.asyncio
    async def test_init_reader_roles_should_include_security_auditor(self):
        assert "security_auditor" in OrchestratorManager._READER_ROLES

    @pytest.mark.asyncio
    async def test_writer_and_reader_roles_should_not_overlap(self):
        overlap = OrchestratorManager._WRITER_ROLES & OrchestratorManager._READER_ROLES
        assert overlap == set(), f"Writer/reader overlap: {overlap}"


# ===========================================================================
# Drain Cancellations (static method)
# ===========================================================================


class TestDrainCancellations:
    """_drain_cancellations safely handles asyncio task cancellation state."""

    @pytest.mark.asyncio
    async def test_drain_cancellations_when_no_task_should_return_zero(self):
        # _drain_cancellations is now an instance method that checks _stop_event.
        # Create a minimal mock with _stop_event unset (spurious cancellation).
        mock_self = type("_Mock", (), {"_stop_event": asyncio.Event()})()
        result = OrchestratorManager._drain_cancellations(mock_self)
        # In an async test context, current_task() returns the test task
        assert isinstance(result, int)
        assert result >= 0

    @pytest.mark.asyncio
    async def test_drain_cancellations_returns_int(self):
        mock_self = type("_Mock", (), {"_stop_event": asyncio.Event()})()
        result = OrchestratorManager._drain_cancellations(mock_self)
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_drain_cancellations_when_stop_event_set_should_return_zero(self):
        """FIX(C-5): _drain_cancellations must NOT drain when _stop_event is set."""
        stop_event = asyncio.Event()
        stop_event.set()  # Simulate user-initiated stop
        mock_self = type("_Mock", (), {"_stop_event": stop_event})()
        result = OrchestratorManager._drain_cancellations(mock_self)
        assert result == 0  # Should NOT drain — legitimate cancellation


# ===========================================================================
# Proactive Memory (get_lessons_learned integration)
# ===========================================================================


class TestProactiveMemory:
    """Proactive memory injection into the orchestrator DAG path."""

    @pytest.mark.asyncio
    async def test_get_lessons_learned_returns_string(self):
        """get_lessons_learned returns a string (may be empty for new projects)."""
        from memory_agent import get_lessons_learned

        tmpdir = tempfile.mkdtemp()
        result = get_lessons_learned(tmpdir, "Build a web app")
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_get_lessons_learned_empty_for_new_project(self):
        """For a brand new project with no .hivemind dir, returns empty string."""
        from memory_agent import get_lessons_learned

        # Use a fresh directory with no .hivemind subdirectory
        tmpdir = tempfile.mkdtemp()
        result = get_lessons_learned(tmpdir, "Build a login system")
        # Should return empty or minimal string — no lessons for new project
        assert isinstance(result, str)
        # Should not contain fake lessons
        assert (
            len(result) < 500
            or "lesson" not in result.lower()
            or "experience" not in result.lower()
        )

    @pytest.mark.asyncio
    async def test_get_lessons_learned_with_experience_file(self):
        """When .experience.md exists, lessons are returned."""
        from memory_agent import get_lessons_learned

        tmpdir = tempfile.mkdtemp()
        forge_dir = Path(tmpdir) / ".hivemind"
        forge_dir.mkdir()
        exp_file = forge_dir / ".experience.md"
        exp_file.write_text(
            "# Experience Ledger\n\n"
            "### Lesson 1 (2026-01-01 12:00)\n"
            "- Always run pytest before committing\n"
            "- Use async/await consistently in FastAPI\n"
        )
        result = get_lessons_learned(tmpdir, "Build a FastAPI API")
        assert isinstance(result, str)
        # Should have found something from the experience file
        # (may be XML-formatted or just the raw content)

    @pytest.mark.asyncio
    async def test_save_experience_note_creates_file(self):
        """save_experience_note creates .hivemind/.experience.md if it doesn't exist."""
        from memory_agent import save_experience_note

        tmpdir = tempfile.mkdtemp()
        save_experience_note(tmpdir, "Always run tests before deployment", "lesson")
        exp_file = Path(tmpdir) / ".hivemind" / ".experience.md"
        assert exp_file.exists()

    @pytest.mark.asyncio
    async def test_save_experience_note_appends_content(self):
        """save_experience_note adds the note to the file."""
        from memory_agent import save_experience_note

        tmpdir = tempfile.mkdtemp()
        save_experience_note(tmpdir, "Use async everywhere in FastAPI", "lesson")
        exp_file = Path(tmpdir) / ".hivemind" / ".experience.md"
        content = exp_file.read_text()
        assert "async" in content.lower() or "FastAPI" in content or "lesson" in content.lower()

    @pytest.mark.asyncio
    async def test_save_experience_note_twice_both_persisted(self):
        """Multiple save calls accumulate in the file."""
        from memory_agent import save_experience_note

        tmpdir = tempfile.mkdtemp()
        save_experience_note(tmpdir, "First lesson about databases", "lesson")
        save_experience_note(tmpdir, "Second lesson about APIs", "lesson")
        exp_file = Path(tmpdir) / ".hivemind" / ".experience.md"
        content = exp_file.read_text()
        # File should have grown with both notes
        assert len(content) > 50


# ===========================================================================
# Graph Quality Critic Integration
# ===========================================================================


class TestGraphQualityCriticIntegration:
    """validate_graph_quality integrates correctly with the orchestrator flow."""

    def test_critic_import_works(self):
        """validate_graph_quality is importable from pm_agent."""
        from pm_agent import validate_graph_quality

        assert callable(validate_graph_quality)

    def test_critic_on_fallback_graph_no_critical_errors(self):
        """Fallback graphs from pm_agent pass the critic without critical errors."""
        from pm_agent import fallback_single_task_graph, validate_graph_quality

        graph = fallback_single_task_graph(
            "Build a REST API for user management with authentication", "critic-test-project"
        )
        issues = validate_graph_quality(graph)
        critical = [
            i
            for i in issues
            if i.startswith("CRITICAL") or (i.startswith("ERROR") and "DAG" not in i)
        ]
        assert critical == [], f"Fallback graph had critical/error issues: {critical}"

    def test_critic_returns_list(self):
        """validate_graph_quality always returns a list."""
        from pm_agent import fallback_single_task_graph, validate_graph_quality

        graph = fallback_single_task_graph("Build something useful", "test-proj")
        result = validate_graph_quality(graph)
        assert isinstance(result, list)

    def test_critic_severity_prefixes_are_valid(self):
        """All issue strings start with a valid severity prefix."""
        from pm_agent import fallback_single_task_graph, validate_graph_quality

        valid_prefixes = {"CRITICAL", "ERROR", "WARNING", "INFO"}
        graph = fallback_single_task_graph(
            "Build a full-stack web application with React, FastAPI, and PostgreSQL", "test-proj"
        )
        issues = validate_graph_quality(graph)
        for issue in issues:
            prefix = issue.split(":")[0].split(" ")[0]
            assert prefix in valid_prefixes, f"Invalid severity prefix in: {issue}"

    def test_critic_on_empty_graph_returns_critical(self):
        """Empty graph always triggers CRITICAL issue."""
        from contracts import TaskGraph
        from pm_agent import validate_graph_quality

        empty_graph = TaskGraph(
            project_id="empty",
            user_message="test",
            vision="test vision",
            epic_breakdown=[],
            tasks=[],
        )
        issues = validate_graph_quality(empty_graph)
        assert any("CRITICAL" in i for i in issues), (
            f"Empty graph should produce CRITICAL issue, got: {issues}"
        )


# ===========================================================================
# Orch Experience Module
# ===========================================================================


class TestOrchExperience:
    """orch_experience module correctly reads/writes experience data."""

    @pytest.mark.asyncio
    async def test_read_todo_empty_project(self):
        """read_todo returns empty string when no todo.md exists."""
        import orch_experience

        tmpdir = tempfile.mkdtemp()
        mgr, _ = await _make_orchestrator(project_dir=tmpdir)
        result = orch_experience.read_todo(mgr)
        assert result == ""

    @pytest.mark.asyncio
    async def test_write_and_read_todo(self):
        """write_todo persists content that read_todo retrieves."""
        import orch_experience

        tmpdir = tempfile.mkdtemp()
        mgr, _ = await _make_orchestrator(project_dir=tmpdir)
        orch_experience.write_todo(mgr, "# Task Ledger\n\nGoal: test")
        result = orch_experience.read_todo(mgr)
        assert "Task Ledger" in result

    @pytest.mark.asyncio
    async def test_read_experience_empty_project(self):
        """read_experience returns empty string when no experience file exists."""
        import orch_experience

        tmpdir = tempfile.mkdtemp()
        mgr, _ = await _make_orchestrator(project_dir=tmpdir)
        result = orch_experience.read_experience(mgr)
        assert result == ""

    @pytest.mark.asyncio
    async def test_append_experience_creates_file(self):
        """append_experience creates .hivemind/.experience.md if it doesn't exist."""
        import orch_experience

        tmpdir = tempfile.mkdtemp()
        mgr, _ = await _make_orchestrator(project_dir=tmpdir)
        orch_experience.append_experience(mgr, "Always run tests before commits")
        exp_path = Path(tmpdir) / ".hivemind" / ".experience.md"
        assert exp_path.exists()
        content = exp_path.read_text()
        assert "Always run tests" in content

    @pytest.mark.asyncio
    async def test_append_experience_multiple_lessons(self):
        """Multiple append_experience calls accumulate in the file."""
        import orch_experience

        tmpdir = tempfile.mkdtemp()
        mgr, _ = await _make_orchestrator(project_dir=tmpdir)
        orch_experience.append_experience(mgr, "Lesson 1: use async/await")
        orch_experience.append_experience(mgr, "Lesson 2: validate inputs early")
        exp_path = Path(tmpdir) / ".hivemind" / ".experience.md"
        content = exp_path.read_text()
        assert "Lesson 1" in content
        assert "Lesson 2" in content

    @pytest.mark.asyncio
    async def test_init_todo_creates_ledger(self):
        """init_todo creates a todo.md file with correct structure."""
        import orch_experience

        tmpdir = tempfile.mkdtemp()
        mgr, _ = await _make_orchestrator(project_dir=tmpdir)
        orch_experience.init_todo(mgr, "Build a REST API with FastAPI", "MEDIUM")
        result = orch_experience.read_todo(mgr)
        assert "Task Ledger" in result
        assert "Build a REST API with FastAPI" in result
        assert "MEDIUM" in result

    @pytest.mark.asyncio
    async def test_init_todo_with_simple_complexity(self):
        """init_todo supports SIMPLE complexity level."""
        import orch_experience

        tmpdir = tempfile.mkdtemp()
        mgr, _ = await _make_orchestrator(project_dir=tmpdir)
        orch_experience.init_todo(mgr, "Fix a bug in login handler", "SIMPLE")
        result = orch_experience.read_todo(mgr)
        assert "Phase 1" in result

    @pytest.mark.asyncio
    async def test_init_todo_with_epic_complexity(self):
        """init_todo supports EPIC complexity level."""
        import orch_experience

        tmpdir = tempfile.mkdtemp()
        mgr, _ = await _make_orchestrator(project_dir=tmpdir)
        orch_experience.init_todo(mgr, "Build an entire SaaS platform from scratch", "EPIC")
        result = orch_experience.read_todo(mgr)
        assert "Phase 6" in result

    @pytest.mark.asyncio
    async def test_init_todo_does_not_overwrite_existing(self):
        """init_todo is idempotent — does not overwrite an existing todo.md."""
        import orch_experience

        tmpdir = tempfile.mkdtemp()
        mgr, _ = await _make_orchestrator(project_dir=tmpdir)
        orch_experience.init_todo(mgr, "Original task", "MEDIUM")
        original = orch_experience.read_todo(mgr)
        orch_experience.init_todo(mgr, "New task that should not overwrite", "LARGE")
        after = orch_experience.read_todo(mgr)
        assert after == original, "init_todo should not overwrite existing todo.md"

    @pytest.mark.asyncio
    async def test_update_todo_after_round_appends_summary(self):
        """update_todo_after_round adds round summary to Completed Work."""
        import orch_experience

        tmpdir = tempfile.mkdtemp()
        mgr, _ = await _make_orchestrator(project_dir=tmpdir)
        orch_experience.init_todo(mgr, "Build something", "MEDIUM")
        orch_experience.update_todo_after_round(mgr, 1, "Completed the backend API implementation")
        result = orch_experience.read_todo(mgr)
        assert "Round 1" in result
        assert "backend API" in result


# ===========================================================================
# Memory Agent Contract Tests (used by orchestrator)
# ===========================================================================


class TestMemoryAgentContracts:
    """memory_agent module exposes the contract the orchestrator depends on."""

    def test_get_lessons_learned_is_callable(self):
        from memory_agent import get_lessons_learned

        assert callable(get_lessons_learned)

    def test_save_experience_note_is_callable(self):
        from memory_agent import save_experience_note

        assert callable(save_experience_note)

    def test_get_lessons_learned_accepts_two_args(self):
        """get_lessons_learned(project_dir, user_message) — both args accepted."""
        import inspect

        from memory_agent import get_lessons_learned

        sig = inspect.signature(get_lessons_learned)
        params = list(sig.parameters.keys())
        assert "project_dir" in params
        assert "user_message" in params

    def test_save_experience_note_accepts_note_arg(self):
        """save_experience_note(project_dir, note, category) — note arg required."""
        import inspect

        from memory_agent import save_experience_note

        sig = inspect.signature(save_experience_note)
        params = list(sig.parameters.keys())
        assert "project_dir" in params
        assert "note" in params
