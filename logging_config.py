"""
logging_config.py — Centralised structured logging configuration.

Controls (via environment variables):
  LOG_LEVEL  (default: INFO)   — root logger minimum level.
                                 Accepts any standard name: DEBUG, INFO,
                                 WARNING, ERROR, CRITICAL.
  LOG_FORMAT (default: text)   — "json" → machine-parseable JSON log lines,
                                 "text" → human-readable coloured output.

Wiring:
  Call ``configure_logging()`` exactly once at application startup, *before*
  any other module creates a logger.  After that every module simply does::

      import logging
      logger = logging.getLogger(__name__)

JSON output fields (one JSON object per line):
  timestamp  — ISO-8601 UTC time (millisecond precision)
  level      — log level name (DEBUG / INFO / WARNING / ERROR / CRITICAL)
  logger     — dotted module name (__name__ convention)
  message    — formatted log message
  request_id — present only when set on the LogRecord via the
               ``extra={"request_id": ...}`` kwarg or the
               ``bind_request_id()`` context helper.
  exc_info   — exception traceback string, present only when an exception
               is attached to the record.
"""

from __future__ import annotations

import json
import logging
import logging.config
import logging.handlers
import os
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["bind_request_id", "configure_logging"]

# ---------------------------------------------------------------------------
# Internal JSON formatter
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Emit one UTF-8 JSON object per log record.

    Output fields (all strings / null):
      timestamp, level, logger, message, [request_id], [exc_info]
    """

    def format(self, record: logging.LogRecord) -> str:
        # Ensure the message is fully rendered (applies % formatting)
        """Format a log record with structured prefix and optional color."""
        record.message = record.getMessage()

        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
        }

        # Optional request_id — attached via extra={"request_id": ...}
        request_id: str | None = getattr(record, "request_id", None)
        if request_id is not None:
            payload["request_id"] = request_id

        # Exception traceback — only when an exception is present
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_info"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc_info"] = record.exc_text

        return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def bind_request_id(logger_: logging.Logger, request_id: str) -> logging.LoggerAdapter:
    """Return a LoggerAdapter that injects *request_id* into every record.

    Usage in a FastAPI route handler::

        from logging_config import bind_request_id
        import logging

        logger = logging.getLogger(__name__)

        async def my_endpoint(request: Request):
            log = bind_request_id(logger, request.headers.get("X-Request-ID", ""))
            log.info("Handling request")
    """
    return logging.LoggerAdapter(logger_, extra={"request_id": request_id})


# ---------------------------------------------------------------------------
# Main configuration entry point
# ---------------------------------------------------------------------------


def configure_logging() -> None:
    """Configure the root logger using ``logging.config.dictConfig``.

    Safe to call multiple times — subsequent calls are no-ops once the
    ``_CONFIGURED`` flag is set, so importing this module in tests never
    re-configures a partially captured logging tree.

    Environment variables read:
      LOG_LEVEL  — root logger level (default: INFO)
      LOG_FORMAT — "json" or "text" (default: text)
    """
    if getattr(configure_logging, "_configured", False):
        return

    raw_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    level: int = getattr(logging, raw_level, logging.INFO)

    log_format: str = os.getenv("LOG_FORMAT", "text").lower()
    use_json: bool = log_format == "json"

    # Build log directory for the rotating file handler
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    text_fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    config: dict = {
        "version": 1,
        # Keep any handlers that pytest (caplog) or third-party code already
        # attached to the root logger — do NOT wipe them out.
        "disable_existing_loggers": False,
        "formatters": {
            "text": {
                "format": text_fmt,
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "json": {
                "()": _JsonFormatter,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": "json" if use_json else "text",
                "level": level,
            },
            "rotating_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir / "app.log"),
                "maxBytes": 5 * 1024 * 1024,  # 5 MB
                "backupCount": 5,
                "encoding": "utf-8",
                # File handler always uses JSON for machine parsing
                "formatter": "json" if use_json else "text",
                "level": level,
            },
        },
        "root": {
            "level": level,
            "handlers": ["console", "rotating_file"],
        },
        # Quieten noisy third-party loggers that flood at DEBUG level
        "loggers": {
            "uvicorn": {
                "level": "WARNING",
                "propagate": False,
            },
            "uvicorn.access": {
                "level": "WARNING",
                "propagate": False,
            },
            "uvicorn.error": {
                "level": "WARNING",
                "propagate": False,
            },
            "httpx": {
                "level": "WARNING",
                "propagate": True,
            },
            "httpcore": {
                "level": "WARNING",
                "propagate": True,
            },
        },
    }

    logging.config.dictConfig(config)
    configure_logging._configured = True  # type: ignore[attr-defined]

    # Emit a startup banner so we can confirm the format in logs
    _startup_logger = logging.getLogger(__name__)
    _startup_logger.info(
        "Logging configured: level=%s format=%s",
        raw_level,
        "json" if use_json else "text",
    )
