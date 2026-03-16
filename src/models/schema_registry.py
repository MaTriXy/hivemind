"""Canonical SQLAlchemy schema registry — single source of truth for all ORM models.

This module re-exports every ORM model class and defines the string enum
constants for all discriminator columns (message roles, action types, task
statuses, etc.).  Import from here — not directly from ``src.db.models`` — so
that there is exactly one dependency path to the schema definition.

Schema hierarchy
----------------
    users
    └── projects (user_id → users.id, SET NULL on delete)
        ├── conversations (project_id → projects.id, CASCADE)
        │   ├── messages (conversation_id → conversations.id, CASCADE)
        │   └── agent_actions (conversation_id → conversations.id, CASCADE)
        └── memory (project_id → projects.id, CASCADE)

Status enum constants
---------------------
String literals used as discriminator values in table columns are defined as
``StrEnum``-style classes here so that:
  - Application code never contains raw string literals like "queued" or
    "tool_call" — it uses ``TaskStatus.queued``.
  - Renaming a value requires only one change in this file.
  - Static analysis tools can find all usages.

Table name constants
--------------------
``TABLE_NAMES`` maps a canonical logical name to the physical table name,
making it easy to write raw SQL or Alembic operations without magic strings.
"""

from __future__ import annotations

from enum import StrEnum

# Re-export all ORM models from the canonical source.
# Application code should import from here, not from src.db.models directly.
from src.db.models import (
    AgentAction,
    Base,
    Conversation,
    Memory,
    Message,
    Project,
    User,
)

__all__ = [
    # Registry maps
    "ALL_MODELS",
    "TABLE_NAMES",
    "AgentAction",
    "AgentActionType",
    "Conversation",
    "IsolationMode",
    "Memory",
    "Message",
    # Enums
    "MessageRole",
    "Project",
    "TaskStatus",
    # Models
    "User",
]


# ---------------------------------------------------------------------------
# String enum: message roles
# ---------------------------------------------------------------------------


class MessageRole(StrEnum):
    """Valid values for ``messages.role``.

    These match the Claude/OpenAI conversation role vocabulary.  They are
    stored as plain strings in the DB (no native enum column) so the schema
    is portable across SQLite and PostgreSQL without an ALTER TYPE statement
    on migration.
    """

    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"  # tool-result messages returned by the SDK


# ---------------------------------------------------------------------------
# String enum: agent action types
# ---------------------------------------------------------------------------


class AgentActionType(StrEnum):
    """Valid values for ``agent_actions.action_type``.

    These categorise what kind of work the agent performed.  Use these
    constants whenever writing ``action_type`` to the DB so that all callers
    agree on the vocabulary.

    Values
    ------
    tool_call    — agent invoked a tool (bash, file read/write, search, …)
    message      — agent sent a message (to user or to a sub-agent)
    decision     — agent made a planning or routing decision
    handoff      — agent transferred control to another agent
    memory_read  — agent read from persistent project memory
    memory_write — agent wrote to persistent project memory
    """

    tool_call = "tool_call"
    message = "message"
    decision = "decision"
    handoff = "handoff"
    memory_read = "memory_read"
    memory_write = "memory_write"


# ---------------------------------------------------------------------------
# String enum: task status (parallel task queue)
# ---------------------------------------------------------------------------


class TaskStatus(StrEnum):
    """Lifecycle states for a parallel task in the ``ProjectTaskQueue``.

    These mirror the ``TaskStatus`` enum in ``src/workers/task_queue.py``
    but are defined here as the canonical source so that:
    - DB queries that filter on task status use the same literals.
    - The workers package (owned by task_003) can import from here to ensure
      consistency.

    Transitions
    -----------
        queued → running → done
        queued → running → failed
    """

    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


# ---------------------------------------------------------------------------
# String enum: project isolation modes
# ---------------------------------------------------------------------------


class IsolationMode(StrEnum):
    """Valid values for the ``ISOLATION_MODE`` config setting.

    row_level — (default) all data in shared platform.db, FK-filtered per project.
    per_db    — each project gets its own dedicated SQLite file.
    """

    row_level = "row_level"
    per_db = "per_db"


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------


#: Ordered list of all ORM model classes in table-creation dependency order.
#: Use this when you need to iterate over all models (e.g. for health checks,
#: integration test setup, or dynamic schema inspection).
ALL_MODELS: list[type[Base]] = [  # type: ignore[type-arg]
    User,
    Project,
    Conversation,
    Message,
    AgentAction,
    Memory,
]

#: Map from logical model name to physical table name.
#: Use instead of hard-coding table name strings in raw SQL or Alembic ops.
TABLE_NAMES: dict[str, str] = {
    "users": User.__tablename__,
    "projects": Project.__tablename__,
    "conversations": Conversation.__tablename__,
    "messages": Message.__tablename__,
    "agent_actions": AgentAction.__tablename__,
    "memory": Memory.__tablename__,
}
