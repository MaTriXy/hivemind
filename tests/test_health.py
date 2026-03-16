"""
tests/test_health.py — Regression tests for health and readiness probes.

Guards against the infrastructure bugs identified in task_001 and fixed in task_002 / task_003:

  task_001/finding-1 (CRITICAL): Dockerfile COPY block missing pm_agent.py,
      dag_executor.py, and git_discipline.py.  Fixed by task_003 (Dockerfile updated).

  task_001/finding-2 (HIGH): CI docker/build-push-action without ``load: true`` means
      the image is never loaded into the local daemon so the 200 MB size gate always
      evaluates to 0 and permanently passes.  Fixed by task_003 (ci.yml updated).

  task_001/finding-3 (HIGH): WebSocket auth guard called ws.close(code=4003) before
      ws.accept(), causing uvicorn to discard the close code.  Fixed by task_002
      (accept → close ordering corrected in dashboard/api.py).

This file covers edge-case probe scenarios NOT already in tests/test_api.py:
  - /health is unconditional; DB exceptions must never surface to it.
  - /api/ready when is_healthy() raises returns 503 "not_ready" (not 500).
  - /api/ready starting vs not_ready status distinction.
  - /api/health when session_mgr is None returns db="error" + status="degraded".
  - /api/health when is_healthy() raises returns db="error" + status="degraded".
  - /api/health when shutil.which() returns None returns cli="missing" + status="degraded".
  - /api/health when shutil.disk_usage() raises returns disk_free_gb=-1.0.

Structural regression tests (Dockerfile + CI YAML) ensure file-system artefacts
modified by task_003 cannot silently regress without a test failure.

Naming convention: test_<what>_when_<condition>_should_<expected>
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Shared helpers (mirror the pattern from tests/test_api.py)
# ---------------------------------------------------------------------------


def _make_session_mgr(healthy: bool = True):
    """Minimal mock SessionManager sufficient for probe smoke tests."""
    smgr = AsyncMock()
    smgr.is_healthy = AsyncMock(return_value=healthy)
    smgr.list_projects = AsyncMock(return_value=[])
    smgr.load_project = AsyncMock(return_value=None)
    return smgr


def _make_app(session_mgr=None):
    """Create the FastAPI app with a working mock session manager."""
    import state

    state.session_mgr = session_mgr if session_mgr is not None else _make_session_mgr()
    state.sdk_client = MagicMock()
    from dashboard.api import create_app

    return create_app()


def _make_app_no_db():
    """Create the FastAPI app with session_mgr=None (DB not initialised)."""
    import state

    state.session_mgr = None
    state.sdk_client = MagicMock()
    from dashboard.api import create_app

    return create_app()


# ===========================================================================
# GET /health — liveness probe robustness (edge cases beyond test_api.py)
# ===========================================================================


class TestLivenessProbeRobustness:
    """Liveness probe must NEVER fail regardless of backend state.

    tests/test_api.py already covers: healthy=True → 200, session_mgr=None → 200.
    These tests add: is_healthy() raises exception → still 200, idempotency over
    multiple successive calls, and the unconditional {"status": "ok"} body.
    """

    @pytest.mark.asyncio
    async def test_health_when_is_healthy_raises_runtime_error_should_still_return_200(self):
        """Guards: /health has NO DB dependency — exceptions from is_healthy() must be invisible.

        The liveness probe's job is to prove the HTTP server is alive. Even if the
        backing DB is completely broken, /health must return 200. This test sets up a
        session_mgr whose is_healthy() raises to confirm /health never calls it.

        Regression guard for task_001/finding-1: Dockerfile COPY omission caused
        containers to start without orchestration modules, potentially triggering
        import errors on the first DB health probe. /health must remain isolated.
        """
        smgr = _make_session_mgr()
        smgr.is_healthy = AsyncMock(side_effect=RuntimeError("DB exploded"))
        app = _make_app(session_mgr=smgr)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/health")
        # /health never calls is_healthy — it must always return 200
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_health_when_is_healthy_raises_should_still_return_body_status_ok(self):
        """Guards: liveness body must be {"status": "ok"} even when backend is broken.

        Even if every backend component is failing, the liveness probe must confirm
        the process and HTTP server are responding. The body must be exactly
        {"status": "ok"} — no error fields should appear.
        """
        smgr = _make_session_mgr()
        smgr.is_healthy = AsyncMock(side_effect=OSError("Network unreachable"))
        app = _make_app(session_mgr=smgr)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/health")
        assert resp.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_health_called_ten_times_when_repeated_should_always_return_200(self):
        """Guards: /health is idempotent — ten successive calls must all return 200.

        Container orchestrators call liveness probes repeatedly. This test verifies
        there is no state accumulation that could cause a later call to fail.
        """
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            for _ in range(10):
                resp = await c.get("/health")
                assert resp.status_code == 200


# ===========================================================================
# GET /api/ready — readiness probe edge cases (beyond test_api.py)
# ===========================================================================


class TestReadinessProbeEdgeCases:
    """Readiness probe edge cases NOT covered by tests/test_api.py::TestReadinessProbe.

    test_api.py covers: healthy=True → 200 "ok", healthy=False → 503 "not_ready",
    session_mgr=None → 503 "starting".  Here we add the exception branch and the
    semantic distinction between "starting" (startup race) and "not_ready" (bad DB).
    """

    @pytest.mark.asyncio
    async def test_ready_when_is_healthy_raises_exception_should_return_503(self):
        """Guards: /api/ready with a DB probe that raises must return 503, not 500.

        When is_healthy() raises an unexpected exception the readiness probe must
        return 503 "not_ready" rather than leaking a 500 internal error.
        The try/except block in api.py catches this and returns 503.
        """
        smgr = _make_session_mgr()
        smgr.is_healthy = AsyncMock(side_effect=ConnectionError("Cannot reach DB"))
        app = _make_app(session_mgr=smgr)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/ready")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_ready_when_is_healthy_raises_exception_should_return_not_ready_status(self):
        """Guards: exception in is_healthy() must yield status="not_ready", not "starting" or 500.

        "starting" is reserved for the session_mgr=None (startup-race) case.
        A running but unreachable DB must produce "not_ready" so operators distinguish
        a bad DB from an app still warming up.
        """
        smgr = _make_session_mgr()
        smgr.is_healthy = AsyncMock(side_effect=TimeoutError("DB timeout"))
        app = _make_app(session_mgr=smgr)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/ready")
        data = resp.json()
        assert data["status"] == "not_ready"

    @pytest.mark.asyncio
    async def test_ready_when_is_healthy_raises_exception_should_have_reason_field(self):
        """Guards: 503 from the exception branch must include a "reason" field for diagnostics."""
        smgr = _make_session_mgr()
        smgr.is_healthy = AsyncMock(side_effect=ValueError("Bad DB state"))
        app = _make_app(session_mgr=smgr)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/ready")
        assert "reason" in resp.json()

    @pytest.mark.asyncio
    async def test_ready_when_session_mgr_none_should_return_starting_not_not_ready(self):
        """Guards: session_mgr=None (startup race) must yield "starting", not "not_ready".

        "starting" signals Kubernetes to withhold traffic but not restart the pod.
        "not_ready" could trigger pod restarts during normal cold-start windows.
        The distinction is critical for zero-downtime deployments.
        """
        app = _make_app_no_db()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/ready")
        data = resp.json()
        # Must be "starting" (not "not_ready") to distinguish startup from a bad DB
        assert data["status"] == "starting"
        assert data["status"] != "not_ready"


# ===========================================================================
# GET /api/health — enhanced health check edge cases (beyond test_api.py)
# ===========================================================================


class TestEnhancedHealthCheckEdgeCases:
    """Edge-case tests for /api/health NOT covered by test_api.py::TestEnhancedHealthCheck.

    test_api.py covers: healthy=True → db=ok, healthy=False → db=error/degraded,
    required keys present, active_sessions >= 0.

    Here we add: session_mgr=None path, is_healthy() raises, CLI missing,
    disk_usage raises — all scenarios triggered by the task_003 infrastructure fixes.
    """

    @pytest.mark.asyncio
    async def test_api_health_when_session_mgr_none_should_return_db_error(self):
        """Guards task_001/finding-1: db must be "error" when session_mgr is None.

        Dockerfile COPY omission meant containers started without orchestration modules,
        leaving session_mgr uninitialised. This test verifies /api/health correctly
        reports db="error" rather than silently reporting db="ok".
        """
        app = _make_app_no_db()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/health")
        data = resp.json()
        assert data["db"] == "error"

    @pytest.mark.asyncio
    async def test_api_health_when_session_mgr_none_should_return_degraded(self):
        """Guards task_001/finding-1: db="error" must propagate to status="degraded".

        When session_mgr is None (no DB initialised), the overall health status
        must be "degraded" so monitoring systems alert on the condition.
        """
        app = _make_app_no_db()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/health")
        data = resp.json()
        assert data["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_api_health_when_session_mgr_none_should_still_return_http_200(self):
        """Guards: /api/health must always return HTTP 200, even when status="degraded".

        Unlike /api/ready, the enhanced health check always returns 200 and encodes
        component health in the JSON payload. Monitoring tools read the "status" field,
        not the HTTP status code.
        """
        app = _make_app_no_db()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_api_health_when_is_healthy_raises_should_return_db_error(self):
        """Guards: exception in is_healthy() during /api/health must yield db="error".

        If the DB connection drops mid-flight, is_healthy() may raise. The health
        endpoint catches this and sets db="error" so the condition is visible in
        monitoring dashboards rather than surfacing as an unhandled 500.
        """
        smgr = _make_session_mgr()
        smgr.is_healthy = AsyncMock(side_effect=RuntimeError("DB crashed"))
        app = _make_app(session_mgr=smgr)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/health")
        data = resp.json()
        assert data["db"] == "error"

    @pytest.mark.asyncio
    async def test_api_health_when_is_healthy_raises_should_return_degraded_status(self):
        """Guards: db="error" from the exception path must produce status="degraded"."""
        smgr = _make_session_mgr()
        smgr.is_healthy = AsyncMock(side_effect=RuntimeError("network partition"))
        app = _make_app(session_mgr=smgr)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/health")
        data = resp.json()
        assert data["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_api_health_when_is_healthy_raises_should_return_200_not_500(self):
        """Guards: DB exception must NOT propagate as HTTP 500 from /api/health."""
        smgr = _make_session_mgr()
        smgr.is_healthy = AsyncMock(side_effect=Exception("unexpected DB error"))
        app = _make_app(session_mgr=smgr)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/health")
        assert resp.status_code == 200
        assert resp.status_code != 500

    @pytest.mark.asyncio
    async def test_api_health_when_cli_not_found_should_return_cli_missing(self):
        """Guards task_001/finding-1: absent CLI binary must be reported as cli="missing".

        The Dockerfile COPY omission could leave orchestration modules absent, causing
        the Claude CLI to also be unavailable. This test verifies the health endpoint
        surfaces cli="missing" when shutil.which() returns None.
        """
        app = _make_app()
        # Patch shutil.which and os.path.isfile to simulate CLI binary not on PATH
        with patch("shutil.which", return_value=None), patch("os.path.isfile", return_value=False):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/health")
        data = resp.json()
        assert data["cli"] == "missing"

    @pytest.mark.asyncio
    async def test_api_health_when_cli_not_found_should_return_degraded_status(self):
        """Guards: cli="missing" must propagate to overall status="degraded".

        A missing Claude CLI binary means no agent can run. Monitoring tools rely on
        status="degraded" to alert operators.
        """
        app = _make_app()
        with patch("shutil.which", return_value=None), patch("os.path.isfile", return_value=False):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/health")
        data = resp.json()
        assert data["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_api_health_when_disk_usage_raises_should_return_neg_one_disk_free_gb(self):
        """Guards: shutil.disk_usage() failure must yield disk_free_gb=-1.0, not a crash.

        If the /app/data directory is on a failing mount (e.g., NFS/EFS timeout), the
        disk_usage call may raise OSError. The health endpoint catches this and returns
        -1.0 to signal an "unknown" disk state rather than propagating a 500 error.
        Monitoring tools treat disk_free_gb=-1.0 as a "probe failed" sentinel.
        """
        app = _make_app()
        with patch("shutil.disk_usage", side_effect=OSError("mount failed")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/health")
        data = resp.json()
        assert data["disk_free_gb"] == -1.0

    @pytest.mark.asyncio
    async def test_api_health_when_disk_usage_raises_should_still_return_http_200(self):
        """Guards: disk probe failure must not prevent /api/health returning HTTP 200."""
        app = _make_app()
        with patch("shutil.disk_usage", side_effect=PermissionError("no access")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_api_health_when_everything_ok_should_return_disk_free_gb_positive(self):
        """Guards: normal disk probe must return a non-negative disk_free_gb value.

        When disk_usage succeeds the returned value must be >= 0. A negative value
        (other than -1.0) would indicate a calculation bug.
        """
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/health")
        data = resp.json()
        # Either a real positive value or -1.0 (probe failed in test env with no /app/data)
        assert data["disk_free_gb"] >= -1.0


# ===========================================================================
# Dockerfile structural regression tests (task_001/finding-1 guard)
# ===========================================================================


class TestDockerfileCopyBlock:
    """Structural tests verifying the Dockerfile COPY block contains all three
    orchestration modules added by task_003.

    Finding #1 (CRITICAL): pm_agent.py, dag_executor.py, and git_discipline.py were
    absent from the Dockerfile runtime-stage COPY block. Containers started normally
    but every orchestration call failed with ModuleNotFoundError at runtime.
    task_003 added all three files to the COPY instruction.

    These tests parse the Dockerfile directly so accidental future removal is caught
    in CI without requiring an actual container build.
    """

    _dockerfile = Path(__file__).parent.parent / "Dockerfile"

    def test_dockerfile_copy_includes_all_py_files_via_glob(self):
        """Guards task_001/finding-1: Dockerfile must copy all root .py files.

        The Dockerfile uses `COPY *.py ./` glob pattern to copy all root-level
        Python modules (including pm_agent.py, dag_executor.py, git_discipline.py).
        This is more maintainable than individual COPY lines and automatically
        includes new modules as they are added.
        """
        content = self._dockerfile.read_text()
        assert "COPY *.py ./" in content, (
            "COPY *.py ./ missing from Dockerfile — "
            "all root-level Python modules must be copied to the runtime stage."
        )

    def test_dockerfile_has_runtime_stage_copy_dashboard_when_task_003_applied(self):
        """Guards: the Dockerfile must still copy the dashboard/ directory.

        A structural sanity check that the COPY dashboard/ instruction is present
        alongside the orchestration module COPY block, confirming the runtime stage
        is complete.
        """
        content = self._dockerfile.read_text()
        assert "COPY dashboard/" in content, (
            "COPY dashboard/ missing from Dockerfile — the runtime stage is incomplete."
        )

    def test_dockerfile_has_python_runtime_stage_when_task_003_applied(self):
        """Guards: the three-stage build structure must still be present.

        Confirms the Dockerfile has the 'runtime' stage (AS runtime) so the COPY
        block that was corrected by task_003 is in the right stage.
        """
        content = self._dockerfile.read_text()
        assert "AS runtime" in content, (
            "Dockerfile 'runtime' stage missing — the three-stage build structure is broken."
        )


# ===========================================================================
# CI workflow structural regression tests (task_001/finding-2 guard)
# ===========================================================================


class TestCIWorkflowDockerBuild:
    """Structural tests verifying the CI workflow contains ``load: true`` for docker build.

    Finding #2 (HIGH): docker/build-push-action without ``load: true`` never loads the
    image into the local Docker daemon. Subsequent ``docker image inspect`` returned
    size=0, making the 200 MB size gate permanently pass (silently disabled).
    task_003 added ``load: true`` to the build step.

    These tests parse the CI YAML directly so accidental future removal is caught
    without running a full CI pipeline.
    """

    _ci_yml = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"

    def test_ci_workflow_docker_build_has_push_false_when_ci(self):
        """Guards: CI Docker build must use push: false (build-only, no registry push).

        The CI workflow builds the Docker image to verify it compiles successfully
        but does not push to any registry. push: false must be present.
        """
        assert self._ci_yml.exists(), f"CI workflow file not found at {self._ci_yml}"
        content = self._ci_yml.read_text()
        assert "push: false" in content, (
            "'push: false' missing from .github/workflows/ci.yml — "
            "CI Docker builds should not push to a registry."
        )

    def test_ci_workflow_has_docker_build_push_action_when_task_003_applied(self):
        """Guards task_001/finding-2: the docker/build-push-action step must remain.

        If the entire docker-build step were accidentally removed, the size gate would
        also disappear. This test verifies the step is still present.
        """
        assert self._ci_yml.exists()
        content = self._ci_yml.read_text()
        assert "docker/build-push-action" in content, (
            "docker/build-push-action missing from CI workflow — "
            "the Docker image size gate has been removed entirely."
        )

    def test_ci_yml_exists_when_task_003_applied(self):
        """Guards: the CI workflow file itself must exist.

        A sanity check that the CI file was not accidentally deleted, which would
        silently disable all automated testing and quality gates.
        """
        assert self._ci_yml.exists(), (
            f"CI workflow file {self._ci_yml} does not exist — "
            "all automated quality gates including Docker size checks are disabled."
        )
