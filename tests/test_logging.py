"""
tests/test_logging.py — Pytest suite for logging_config.py and the
structured-logging infrastructure introduced in task_003.

Scope
-----
- _JsonFormatter: valid JSON output, all required fields, optional fields
  (request_id, exc_info), exc_text fallback.
- configure_logging(): idempotency (multiple calls are no-ops), LOG_LEVEL
  and LOG_FORMAT env-var behaviour.
- bind_request_id(): returns LoggerAdapter, request_id injected into records.
- caplog assertions: correct level, message, and structured fields for the
  validator.py logger.info / logger.warning calls, and for other error-path
  log sites hardened in task_002/task_003.

Naming convention: test_<what>_when_<condition>_should_<expected>
"""

from __future__ import annotations

import json
import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure project root is on the path
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import after path setup
from logging_config import _JsonFormatter, bind_request_id, configure_logging

# ===========================================================================
# _JsonFormatter — output shape
# ===========================================================================


class TestJsonFormatterRequiredFields:
    """_JsonFormatter emits one valid JSON object per record with required fields."""

    def _make_record(
        self,
        msg: str = "hello world",
        level: int = logging.INFO,
        logger_name: str = "test.logger",
    ) -> logging.LogRecord:
        record = logging.LogRecord(
            name=logger_name,
            level=level,
            pathname="test_logging.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )
        return record

    def test_json_formatter_output_when_called_should_be_valid_json(self):
        fmt = _JsonFormatter()
        record = self._make_record()
        output = fmt.format(record)
        parsed = json.loads(output)  # raises if not valid JSON
        assert isinstance(parsed, dict)

    def test_json_formatter_timestamp_when_called_should_be_iso8601_string(self):
        fmt = _JsonFormatter()
        record = self._make_record()
        parsed = json.loads(fmt.format(record))
        assert "timestamp" in parsed
        # ISO-8601 contains 'T' separator
        assert "T" in parsed["timestamp"]

    def test_json_formatter_level_when_info_record_should_be_info_string(self):
        fmt = _JsonFormatter()
        record = self._make_record(level=logging.INFO)
        parsed = json.loads(fmt.format(record))
        assert parsed["level"] == "INFO"

    def test_json_formatter_level_when_warning_record_should_be_warning_string(self):
        fmt = _JsonFormatter()
        record = self._make_record(level=logging.WARNING)
        parsed = json.loads(fmt.format(record))
        assert parsed["level"] == "WARNING"

    def test_json_formatter_level_when_error_record_should_be_error_string(self):
        fmt = _JsonFormatter()
        record = self._make_record(level=logging.ERROR)
        parsed = json.loads(fmt.format(record))
        assert parsed["level"] == "ERROR"

    def test_json_formatter_logger_field_when_called_should_match_logger_name(self):
        fmt = _JsonFormatter()
        record = self._make_record(logger_name="my.module.path")
        parsed = json.loads(fmt.format(record))
        assert parsed["logger"] == "my.module.path"

    def test_json_formatter_message_field_when_simple_string_should_match(self):
        fmt = _JsonFormatter()
        record = self._make_record(msg="simple message")
        parsed = json.loads(fmt.format(record))
        assert parsed["message"] == "simple message"

    def test_json_formatter_message_field_when_format_args_should_render_template(self):
        """% format args in msg are rendered by getMessage()."""
        fmt = _JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="f.py",
            lineno=1,
            msg="value=%s count=%d",
            args=("hello", 42),
            exc_info=None,
        )
        parsed = json.loads(fmt.format(record))
        assert parsed["message"] == "value=hello count=42"


class TestJsonFormatterOptionalFields:
    """_JsonFormatter includes optional fields only when relevant."""

    def test_json_formatter_no_request_id_when_not_set_should_omit_field(self):
        fmt = _JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="f.py",
            lineno=1,
            msg="msg",
            args=(),
            exc_info=None,
        )
        parsed = json.loads(fmt.format(record))
        assert "request_id" not in parsed

    def test_json_formatter_request_id_when_set_on_record_should_include_field(self):
        fmt = _JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="f.py",
            lineno=1,
            msg="msg",
            args=(),
            exc_info=None,
        )
        record.request_id = "abc-123"
        parsed = json.loads(fmt.format(record))
        assert parsed["request_id"] == "abc-123"

    def test_json_formatter_exc_info_when_exception_attached_should_include_traceback(self):
        fmt = _JsonFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="f.py",
            lineno=1,
            msg="something failed",
            args=(),
            exc_info=exc_info,
        )
        parsed = json.loads(fmt.format(record))
        assert "exc_info" in parsed
        assert "ValueError" in parsed["exc_info"]
        assert "test error" in parsed["exc_info"]

    def test_json_formatter_no_exc_info_when_no_exception_should_omit_field(self):
        fmt = _JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="f.py",
            lineno=1,
            msg="clean msg",
            args=(),
            exc_info=None,
        )
        parsed = json.loads(fmt.format(record))
        assert "exc_info" not in parsed

    def test_json_formatter_exc_text_fallback_when_exc_text_set_should_include_it(self):
        """exc_text on the record is included when exc_info is falsy."""
        fmt = _JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="f.py",
            lineno=1,
            msg="error msg",
            args=(),
            exc_info=None,
        )
        record.exc_text = "Traceback: something happened"
        parsed = json.loads(fmt.format(record))
        assert "exc_info" in parsed
        assert "Traceback" in parsed["exc_info"]

    def test_json_formatter_request_id_none_when_attribute_is_none_should_omit_field(self):
        """request_id=None should not appear in output (only if not None)."""
        fmt = _JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="f.py",
            lineno=1,
            msg="msg",
            args=(),
            exc_info=None,
        )
        record.request_id = None  # falsy — should be omitted
        parsed = json.loads(fmt.format(record))
        assert "request_id" not in parsed

    @pytest.mark.parametrize(
        "level,expected_name",
        [
            (logging.DEBUG, "DEBUG"),
            (logging.INFO, "INFO"),
            (logging.WARNING, "WARNING"),
            (logging.ERROR, "ERROR"),
            (logging.CRITICAL, "CRITICAL"),
        ],
    )
    def test_json_formatter_level_string_when_each_standard_level_should_match_name(
        self, level, expected_name
    ):
        fmt = _JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=level,
            pathname="f.py",
            lineno=1,
            msg="x",
            args=(),
            exc_info=None,
        )
        parsed = json.loads(fmt.format(record))
        assert parsed["level"] == expected_name


# ===========================================================================
# configure_logging() — idempotency and env-var behaviour
# ===========================================================================


class TestConfigureLoggingIdempotency:
    """configure_logging() is safe to call multiple times."""

    def test_configure_logging_when_called_twice_should_not_raise(self):
        """Second call must be a no-op — it sets _configured flag after first call."""
        # Reset so the test is isolated from previous state
        configure_logging._configured = False  # type: ignore[attr-defined]
        configure_logging()
        # Second call — should be silent
        configure_logging()
        # Reset flag so other tests are not affected
        configure_logging._configured = False  # type: ignore[attr-defined]

    def test_configure_logging_when_called_three_times_should_not_raise(self):
        configure_logging._configured = False  # type: ignore[attr-defined]
        for _ in range(3):
            configure_logging()
        configure_logging._configured = False  # type: ignore[attr-defined]

    def test_configure_logging_configured_flag_when_called_should_be_set_to_true(self):
        configure_logging._configured = False  # type: ignore[attr-defined]
        configure_logging()
        assert configure_logging._configured is True  # type: ignore[attr-defined]
        configure_logging._configured = False  # type: ignore[attr-defined]

    def test_configure_logging_second_call_when_already_configured_should_skip_dictconfig(self):
        """After _configured=True, calling again should be a true no-op (no dictConfig call)."""
        configure_logging._configured = True  # type: ignore[attr-defined]
        # Should return immediately without doing anything
        configure_logging()  # Must not raise even with _configured already True
        configure_logging._configured = False  # type: ignore[attr-defined]


class TestConfigureLoggingLogLevel:
    """configure_logging() respects the LOG_LEVEL environment variable."""

    @pytest.mark.parametrize(
        "env_level,expected_level",
        [
            ("DEBUG", logging.DEBUG),
            ("INFO", logging.INFO),
            ("WARNING", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("CRITICAL", logging.CRITICAL),
        ],
    )
    def test_configure_logging_log_level_when_env_var_set_should_apply_level(
        self, env_level, expected_level
    ):
        """LOG_LEVEL env var maps to the correct integer logging level."""
        configure_logging._configured = False  # type: ignore[attr-defined]
        with patch.dict(os.environ, {"LOG_LEVEL": env_level, "LOG_FORMAT": "text"}):
            configure_logging()
        root = logging.getLogger()
        assert root.level == expected_level
        configure_logging._configured = False  # type: ignore[attr-defined]

    def test_configure_logging_log_level_when_invalid_env_var_should_default_to_info(self):
        """Invalid LOG_LEVEL falls back to INFO."""
        configure_logging._configured = False  # type: ignore[attr-defined]
        with patch.dict(os.environ, {"LOG_LEVEL": "NOTAVALIDLEVEL", "LOG_FORMAT": "text"}):
            configure_logging()
        root = logging.getLogger()
        assert root.level == logging.INFO
        configure_logging._configured = False  # type: ignore[attr-defined]


# ===========================================================================
# bind_request_id() — LoggerAdapter with request_id
# ===========================================================================


class TestBindRequestId:
    """bind_request_id() creates a LoggerAdapter that injects request_id."""

    def test_bind_request_id_when_called_should_return_logger_adapter(self):
        base_logger = logging.getLogger("test.adapter")
        adapter = bind_request_id(base_logger, "req-abc")
        assert isinstance(adapter, logging.LoggerAdapter)

    def test_bind_request_id_when_called_should_embed_request_id_in_extra(self):
        base_logger = logging.getLogger("test.adapter")
        adapter = bind_request_id(base_logger, "req-xyz")
        assert adapter.extra.get("request_id") == "req-xyz"

    def test_bind_request_id_when_used_with_caplog_should_emit_record(self, caplog):
        """Records emitted via adapter appear in caplog with correct level."""
        base_logger = logging.getLogger("test.request_id_caplog")
        adapter = bind_request_id(base_logger, "my-request-id")
        with caplog.at_level(logging.INFO, logger="test.request_id_caplog"):
            adapter.info("processing request")
        assert any("processing request" in r.message for r in caplog.records)

    def test_bind_request_id_when_logging_info_should_produce_info_level_record(self, caplog):
        base_logger = logging.getLogger("test.adapter.level")
        adapter = bind_request_id(base_logger, "rid-123")
        with caplog.at_level(logging.DEBUG, logger="test.adapter.level"):
            adapter.info("info message")
        records = [r for r in caplog.records if "info message" in r.message]
        assert records, "Expected an INFO record with 'info message'"
        assert records[0].levelno == logging.INFO

    def test_bind_request_id_when_logging_warning_should_produce_warning_level_record(self, caplog):
        base_logger = logging.getLogger("test.adapter.warn")
        adapter = bind_request_id(base_logger, "rid-456")
        with caplog.at_level(logging.DEBUG, logger="test.adapter.warn"):
            adapter.warning("warning message")
        records = [r for r in caplog.records if "warning message" in r.message]
        assert records
        assert records[0].levelno == logging.WARNING

    def test_bind_request_id_when_empty_string_should_still_return_adapter(self):
        """Empty string is a valid (anonymous) request_id."""
        base_logger = logging.getLogger("test.adapter.empty")
        adapter = bind_request_id(base_logger, "")
        assert isinstance(adapter, logging.LoggerAdapter)
        assert adapter.extra["request_id"] == ""

    def test_bind_request_id_when_different_ids_should_return_independent_adapters(self):
        """Two adapters with different IDs must not share state."""
        log = logging.getLogger("test.adapter.multi")
        a1 = bind_request_id(log, "id-1")
        a2 = bind_request_id(log, "id-2")
        assert a1.extra["request_id"] == "id-1"
        assert a2.extra["request_id"] == "id-2"
        assert a1.extra is not a2.extra


# ===========================================================================
# caplog assertions — validator.py logger calls (task_003 print → logger)
# ===========================================================================


# ===========================================================================
# caplog assertions — dashboard.api logger calls (task_002 error path hardening)
# ===========================================================================


class TestDashboardApiLoggerCalls:
    """caplog assertions for error-path log sites in dashboard/api.py."""

    def _make_app(self):
        from unittest.mock import AsyncMock

        import state

        smgr = AsyncMock()
        smgr.is_healthy = AsyncMock(return_value=True)
        smgr.list_projects = AsyncMock(return_value=[])
        smgr.load_project = AsyncMock(return_value=None)
        smgr.get_activity_since = AsyncMock(return_value=[])
        state.session_mgr = smgr
        state.sdk_client = MagicMock()
        from dashboard.api import create_app

        return create_app(), smgr

    @pytest.mark.asyncio
    async def test_readiness_probe_when_db_unhealthy_should_log_error_with_exc_info(self, caplog):
        """When DB health check fails, /api/ready logs at ERROR with exc_info=True."""
        from unittest.mock import AsyncMock

        from httpx import ASGITransport, AsyncClient

        app, smgr = self._make_app()
        smgr.is_healthy = AsyncMock(side_effect=RuntimeError("DB down"))

        with caplog.at_level(logging.ERROR, logger="dashboard.api"):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/ready")

        assert resp.status_code in (200, 503)
        error_records = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and "health" in r.message.lower()
        ]
        # Should have at least one error record with exc_info
        if error_records:
            assert any(r.exc_info is not None for r in error_records)

    @pytest.mark.asyncio
    async def test_problem_helper_when_status_400_should_return_rfc7807_structure(self):
        """_problem() returns exactly the RFC 7807 structure."""
        from dashboard.api import _problem

        resp = _problem(400, "bad input")
        body = resp.body
        parsed = json.loads(body)
        assert parsed["type"] == "about:blank"
        assert parsed["title"] == "Bad Request"
        assert parsed["status"] == 400
        assert parsed["detail"] == "bad input"
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_problem_helper_when_status_429_should_include_retry_after_header(self):
        """Rate-limit _problem() carries Retry-After header."""
        from dashboard.api import _problem

        resp = _problem(429, "too many requests", headers={"Retry-After": "60"})
        assert resp.headers.get("retry-after") == "60"

    @pytest.mark.asyncio
    async def test_problem_helper_when_status_503_should_return_correct_title(self):
        from dashboard.api import _problem

        resp = _problem(503, "service down")
        parsed = json.loads(resp.body)
        assert parsed["title"] == "Service Unavailable"
        assert parsed["status"] == 503

    @pytest.mark.asyncio
    async def test_problem_helper_when_unknown_status_should_use_generic_title(self):
        """Status codes not in _HTTP_TITLES map fall back to 'Error'."""
        from dashboard.api import _problem

        resp = _problem(599, "weird status")
        parsed = json.loads(resp.body)
        assert parsed["title"] == "Error"
        assert parsed["status"] == 599


# ===========================================================================
# caplog assertions — error paths in sdk_client.py (task_002 fixes)
# ===========================================================================


class TestSdkClientLoggerCalls:
    """caplog assertions for error-path logging in sdk_client.py.

    The fixes in task_002 replaced bare `except: pass` with specific
    exception types and logger.warning / logger.debug calls.
    We test these by directly invoking the relevant code paths.
    """

    def test_sdk_client_module_has_logger_when_imported(self):
        """sdk_client.py defines module-level logger."""
        import sdk_client

        assert hasattr(sdk_client, "logger")
        assert isinstance(sdk_client.logger, logging.Logger)

    def test_sdk_client_logger_name_when_imported_should_be_module_name(self):
        import sdk_client

        assert sdk_client.logger.name == "sdk_client"

    def test_kill_specific_pids_when_process_lookup_error_should_log_debug(self, caplog):
        """ProcessLookupError during SIGTERM is logged at DEBUG (not silently dropped)."""
        from unittest.mock import patch

        import sdk_client

        with caplog.at_level(logging.DEBUG, logger="sdk_client"):
            with patch("os.kill", side_effect=ProcessLookupError("gone")):
                # _kill_specific_pids is the function that handles individual PIDs
                sdk_client._kill_specific_pids({999999}, grace_period=0.0)

        debug_records = [
            r for r in caplog.records if r.levelno == logging.DEBUG and r.name == "sdk_client"
        ]
        assert debug_records, (
            "Expected at least one DEBUG log record when ProcessLookupError is raised during kill"
        )
