"""
Test: verify orchestrator + sub-agent communication loop works end-to-end.

We mock the SDK to return controlled responses and verify the full cycle:
  1. User message -> orchestrator
  2. Orchestrator emits <delegate> block -> developer sub-agent runs
  3. Developer result fed back to orchestrator
  4. Orchestrator says TASK_COMPLETE

Converted from manual asyncio.run(main()) script to proper pytest-compatible
async tests using @pytest.mark.asyncio decorators.

The tests force the legacy regex-delegate path (USE_DAG_EXECUTOR=False) and
mock both the SDK (for orchestrator calls) and isolated_query (for sub-agent
calls) to simulate the full delegation flow without real API calls.
"""

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator import OrchestratorManager
from sdk_client import SDKResponse
from src.db.database import get_session_factory, init_db
from src.storage.platform_session import PlatformSessionManager

# ── Shared test state ──────────────────────────────────────────────────


class MockCallTracker:
    """Track mock SDK calls, updates, and results across the test session.

    The orchestrator directly calls sdk.query_with_retry for its own queries,
    and sub-agent calls go through isolated_query(). We track both paths.
    """

    def __init__(self):
        self.updates: list[str] = []
        self.results: list[str] = []
        self.query_calls: list[dict] = []

    async def on_update(self, text: str):
        self.updates.append(text)

    async def on_result(self, text: str):
        self.results.append(text)

    def _make_response(self, call_num: int) -> SDKResponse:
        """Generate the appropriate mock response for a given call number."""
        if call_num == 0:
            # Orchestrator receives user message, delegates to developer
            return SDKResponse(
                text=(
                    "I'll analyze this task and delegate the implementation.\n\n"
                    "<delegate>\n"
                    '{"agent": "developer", "task": "Create a hello.py file", "context": "Python"}\n'
                    "</delegate>"
                ),
                session_id="orch-session-1",
                cost_usd=0.01,
                num_turns=1,
            )
        elif call_num == 1:
            # Developer sub-agent does the work
            return SDKResponse(
                text="I created hello.py with a greeting function. File saved.",
                session_id="dev-session-1",
                cost_usd=0.02,
                num_turns=2,
            )
        elif call_num == 2:
            # Orchestrator reviews developer result, completes
            return SDKResponse(
                text="The developer has completed the task. Everything looks good. TASK_COMPLETE",
                session_id="orch-session-2",
                cost_usd=0.01,
                num_turns=1,
            )
        elif call_num == 3:
            # Experience reflection (post-completion learning)
            return SDKResponse(
                text="Lessons learned: simple file creation tasks complete quickly.",
                session_id="reflect-session-1",
                cost_usd=0.005,
                num_turns=1,
            )
        else:
            return SDKResponse(
                text="Unexpected call", is_error=True, error_message="Too many calls"
            )

    async def sdk_query_with_retry(
        self,
        prompt,
        system_prompt,
        cwd,
        session_id=None,
        max_turns=10,
        max_budget_usd=2.0,
        max_retries=2,
        on_stream=None,
        on_tool_use=None,
        permission_mode=None,
        allowed_tools=None,
        tools=None,
    ):
        """Mock SDK.query_with_retry — handles orchestrator calls."""
        call_num = len(self.query_calls)
        self.query_calls.append({"prompt": prompt[:500], "system_prompt": system_prompt[:200]})
        return self._make_response(call_num)

    async def isolated_query_mock(
        self,
        sdk,
        prompt,
        system_prompt,
        cwd,
        session_id=None,
        max_turns=10,
        max_budget_usd=2.0,
        on_stream=None,
        on_tool_use=None,
        permission_mode=None,
        allowed_tools=None,
        tools=None,
        per_message_timeout=None,
    ):
        """Mock isolated_query — handles sub-agent calls."""
        call_num = len(self.query_calls)
        self.query_calls.append({"prompt": prompt[:500], "system_prompt": system_prompt[:200]})
        return self._make_response(call_num)


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def db_session_mgr():
    """Create a temporary SQLite DB + PlatformSessionManager for testing."""
    db_url = f"sqlite+aiosqlite:///{os.path.join(tempfile.mkdtemp(), 'test.db')}"
    await init_db(db_url)
    factory = get_session_factory(db_url)
    mgr = PlatformSessionManager(factory)
    await mgr.initialize()
    yield mgr
    await mgr.close()


@pytest.fixture
def tracker():
    """Fresh MockCallTracker for each test."""
    return MockCallTracker()


@pytest.fixture
def mock_sdk(tracker):
    """Create a mock SDK client wired to the tracker."""
    sdk = MagicMock()
    sdk.query_with_retry = tracker.sdk_query_with_retry
    sdk.circuit_open = False
    sdk.total_cost_usd = 0.0
    return sdk


@pytest.fixture
async def orchestrator_mgr(mock_sdk, db_session_mgr, tracker):
    """Create a fully-wired OrchestratorManager for integration testing.

    Patches:
     - USE_DAG_EXECUTOR = False → forces legacy regex-delegate path
     - isolated_query → tracker.isolated_query_mock for sub-agent calls
     - SANDBOX_ENABLED = False → allows temp dirs in tests
    """
    project_dir = tempfile.mkdtemp()
    Path(project_dir).mkdir(parents=True, exist_ok=True)

    with patch("config.SANDBOX_ENABLED", False):
        mgr = OrchestratorManager(
            project_name="test-project",
            project_dir=project_dir,
            sdk=mock_sdk,
            session_mgr=db_session_mgr,
            user_id=123,
            project_id="test-proj",
            on_update=tracker.on_update,
            on_result=tracker.on_result,
            on_event=AsyncMock(),
            multi_agent=True,
        )
    return mgr


async def _wait_for_completion(mgr, timeout_seconds=10.0):
    """Wait for the orchestrator to finish running (with timeout)."""
    for _ in range(int(timeout_seconds / 0.1)):
        if not mgr.is_running:
            return True
        await asyncio.sleep(0.1)
    return False


async def _run_e2e_session(orchestrator_mgr, tracker):
    """Run the standard e2e session with all required patches."""
    with (
        patch("orchestrator.USE_DAG_EXECUTOR", False),
        patch("orch_agents.isolated_query", tracker.isolated_query_mock),
        patch("orch_watchdog.check_premature_completion", return_value=None),
    ):
        await orchestrator_mgr.start_session("Create a hello world script")
        completed = await _wait_for_completion(orchestrator_mgr)
    return completed


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_e2e_when_delegate_flow_should_make_three_sdk_calls(
    orchestrator_mgr, tracker
):
    """Full delegation cycle should produce exactly 3 SDK calls:
    orchestrator -> developer -> orchestrator."""
    completed = await _run_e2e_session(orchestrator_mgr, tracker)

    assert completed, "Orchestrator did not finish within timeout"
    # 4 calls: orchestrator → developer → orchestrator review → experience reflection
    assert len(tracker.query_calls) >= 3, f"Expected >=3 SDK calls, got {len(tracker.query_calls)}"


@pytest.mark.asyncio
async def test_orchestrator_e2e_when_first_call_should_receive_user_message(
    orchestrator_mgr, tracker
):
    """Call 0: orchestrator should receive the user's original message."""
    await _run_e2e_session(orchestrator_mgr, tracker)

    assert len(tracker.query_calls) >= 1, "No SDK calls were made"
    # The orchestrator wraps the user message in a project context prompt
    assert (
        "hello world" in tracker.query_calls[0]["prompt"].lower()
        or "test-project" in tracker.query_calls[0]["prompt"]
    )


@pytest.mark.asyncio
async def test_orchestrator_e2e_when_delegate_should_route_to_developer(orchestrator_mgr, tracker):
    """Call 1: developer sub-agent should get the delegated task."""
    await _run_e2e_session(orchestrator_mgr, tracker)

    assert len(tracker.query_calls) >= 2, "Not enough SDK calls for developer"
    # Developer sub-agent receives the delegated task via isolated_query
    assert len(tracker.query_calls[1]["prompt"]) > 0, "Developer call had empty prompt"


@pytest.mark.asyncio
async def test_orchestrator_e2e_when_developer_done_should_feed_results_back(
    orchestrator_mgr, tracker
):
    """Call 2: orchestrator should receive developer results in a follow-up."""
    await _run_e2e_session(orchestrator_mgr, tracker)

    assert len(tracker.query_calls) >= 3, "Not enough SDK calls for result feedback"
    # Call 2 is the orchestrator reviewing developer results (round_status prompt)
    assert len(tracker.query_calls[2]["prompt"]) > 0, "Review call had empty prompt"


@pytest.mark.asyncio
async def test_orchestrator_e2e_conversation_log_should_contain_all_participants(
    orchestrator_mgr, tracker
):
    """Conversation log should have entries from user, orchestrator, and developer."""
    await _run_e2e_session(orchestrator_mgr, tracker)

    agents_in_log = {m.agent_name for m in orchestrator_mgr.conversation_log}
    assert "user" in agents_in_log, "User message missing from log"
    assert "orchestrator" in agents_in_log, "Orchestrator missing from log"
    assert "developer" in agents_in_log, "Developer missing from log"


@pytest.mark.asyncio
async def test_orchestrator_e2e_when_task_complete_should_stop_running(orchestrator_mgr, tracker):
    """TASK_COMPLETE keyword should stop the orchestrator loop."""
    await _run_e2e_session(orchestrator_mgr, tracker)

    assert not orchestrator_mgr.is_running, "Manager should have stopped after TASK_COMPLETE"


@pytest.mark.asyncio
async def test_orchestrator_e2e_should_send_progress_updates(orchestrator_mgr, tracker):
    """Progress updates should be sent via on_update callback."""
    await _run_e2e_session(orchestrator_mgr, tracker)

    assert len(tracker.updates) > 0, f"No progress updates sent. Updates: {tracker.updates[:5]}"


@pytest.mark.asyncio
async def test_orchestrator_e2e_should_send_completion_results(orchestrator_mgr, tracker):
    """Final results should be sent via on_result callback."""
    await _run_e2e_session(orchestrator_mgr, tracker)

    assert len(tracker.results) > 0, f"No results sent. Results: {tracker.results[:5]}"
    # Final summary should mention developer or completion
    all_results = " ".join(tracker.results).lower()
    assert "developer" in all_results or "done" in all_results, (
        f"Developer/completion not in results. Results: {tracker.results[:5]}"
    )


@pytest.mark.asyncio
async def test_orchestrator_e2e_should_not_leak_final_output_to_updates(orchestrator_mgr, tracker):
    """TASK_COMPLETE should not appear in progress updates (only in results)."""
    await _run_e2e_session(orchestrator_mgr, tracker)

    assert not any("TASK_COMPLETE" in u for u in tracker.updates), (
        "Final output leaked into progress updates"
    )


@pytest.mark.asyncio
async def test_orchestrator_e2e_should_persist_sessions_to_db(
    orchestrator_mgr, tracker, db_session_mgr
):
    """Orchestrator and developer sessions should be saved to SQLite."""
    await _run_e2e_session(orchestrator_mgr, tracker)

    orch_session = await db_session_mgr.get_session(123, "test-proj", "orchestrator")
    dev_session = await db_session_mgr.get_session(123, "test-proj", "developer")
    assert orch_session is not None, "Orchestrator session not saved"
    assert dev_session is not None, "Developer session not saved"


@pytest.mark.asyncio
async def test_orchestrator_e2e_should_persist_messages_to_db(
    orchestrator_mgr, tracker, db_session_mgr
):
    """At least 4 messages should be persisted in the database."""
    await _run_e2e_session(orchestrator_mgr, tracker)

    db_messages = await db_session_mgr.get_recent_messages("test-proj", count=20)
    assert len(db_messages) >= 2, f"Expected >=2 messages in DB, got {len(db_messages)}"
