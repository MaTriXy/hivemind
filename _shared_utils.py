"""Shared utility functions for the Hivemind codebase.

Centralises small helpers that were previously copy-pasted across modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def extract_json(text: str) -> dict | list | None:
    """Extract the first JSON object or array from *text*.

    Tries ``json.loads`` on the full string first, then falls back to
    regex extraction of ``{…}`` or ``[…]`` blocks.
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group())
            except (json.JSONDecodeError, TypeError):
                continue
    return None


async def drain_cancellations() -> None:
    """Yield control so any pending ``CancelledError`` can propagate."""
    try:
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        pass


_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,126}[a-z0-9]$|^[a-z0-9]$")


def valid_project_id(pid: str) -> bool:
    """Return *True* if *pid* matches the expected slug format."""
    return bool(_PROJECT_ID_RE.match(pid))


def problem(status: int, detail: str) -> dict:
    """Build an RFC-7807 Problem Detail payload.

    Returns a plain dict.  Call ``problem_response()`` when you need
    a ``JSONResponse`` directly.
    """
    _TITLES = {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        409: "Conflict",
        422: "Unprocessable Content",
        429: "Too Many Requests",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }
    return {
        "type": "about:blank",
        "title": _TITLES.get(status, "Error"),
        "status": status,
        "detail": detail,
    }
