"""SQLAlchemy async ORM models for the multi-project agent platform.

This module defines the six core tables that serve as the Single Source of Truth
(SSOT) for all users, sessions, conversations, messages, agent actions, and project
memory. All state is persisted here — nothing is ephemeral-only.

Design decisions:
- UUIDs as primary keys (String(36)) — portable across SQLite and Postgres.
- JSON columns for flexible payloads (config, metadata, action payloads/results).
- Cascade DELETE rules enforce referential integrity at the DB level.
- Indexes cover the highest-frequency read patterns (hot-query paths).
- timezone=True on all DateTime columns — always store UTC.

Table hierarchy (FK ownership):
    users
    └── projects (user_id → users.id, nullable for legacy/anonymous)
        ├── conversations (project_id → projects.id)
        │   ├── messages (conversation_id → conversations.id)
        │   └── agent_actions (conversation_id → conversations.id)
        └── memory (project_id → projects.id)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
from _shared_utils import utcnow as _utcnow


def _new_uuid() -> str:
    """Generate a new UUID4 string suitable for use as a primary key."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    pass


# ---------------------------------------------------------------------------
# Model: users
# ---------------------------------------------------------------------------


class User(Base):
    """Represents a platform user — the root of the multi-tenant hierarchy.

    Every user owns zero or more projects. Per-user DB isolation is supported
    via the ``db_path`` column: when non-null, that user's project data is
    stored in a dedicated SQLite file rather than the shared ``platform.db``.
    This enables true filesystem-level isolation without a Postgres schema-per-user
    setup, while remaining compatible with the single-DB default.

    Columns:
        id          - UUID primary key.
        external_id - Opaque string from the identity provider (OAuth sub, GitHub ID, etc.).
                      Null for anonymous or API-key-only users.
        email       - Unique email address. Null for anonymous users.
        display_name- Optional human-readable display name (e.g. "Alice Smith").
        db_path     - Absolute path to per-user SQLite database file.
                      Null → user's data lives in the shared platform.db.
                      Non-null → route all project queries to this file.
        created_at  - UTC timestamp of account creation.
        updated_at  - UTC timestamp of last profile modification.

    Relationships:
        projects - All projects owned by this user (cascade delete).

    Indexes:
        uq_users_external_id - UNIQUE on external_id (when non-null).
        uq_users_email        - UNIQUE on email (when non-null).
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_uuid,
        doc="UUID primary key.",
    )
    external_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
        doc=(
            "Opaque identifier from an OAuth/identity provider (e.g. GitHub sub claim). "
            "Null for anonymous or local-only users."
        ),
    )
    email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
        doc="Unique email address. Null for anonymous users.",
    )
    display_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc="Human-readable display name for the user.",
    )
    db_path: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        doc=(
            "Absolute path to the per-user SQLite database file. "
            "Null → use the shared platform.db. "
            "Non-null → route all project/conversation queries to this file for isolation."
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="UTC timestamp when the user account was created.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="UTC timestamp of the last modification to the user record.",
    )

    # ── Relationships ──────────────────────────────────────────────────────
    projects: Mapped[list[Project]] = relationship(
        "Project",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id!r} email={self.email!r}>"


# ---------------------------------------------------------------------------
# Model: projects
# ---------------------------------------------------------------------------


class Project(Base):
    """Represents a top-level project that groups conversations and memory.

    A project is the highest-level isolation boundary. Each project has its
    own conversations, its own persistent memory store, and its own
    configuration JSON blob.

    Columns:
        id          - UUID primary key.
        name        - Human-readable display name (e.g. "My Web App").
        config_json - Arbitrary project-level settings (budget, model, tools, etc.)
                      stored as a JSON blob. Schema is validated at the application
                      layer. Example: {"budget_usd": 50, "default_model": "claude-3-5-sonnet"}.
        created_at  - UTC timestamp of project creation.
        updated_at  - UTC timestamp of last modification (updated by application layer).

    Relationships:
        conversations - All conversations belonging to this project (cascade delete).
        memory        - All key/value memory entries scoped to this project (cascade delete).
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_uuid,
        doc="UUID primary key.",
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        doc=(
            "FK → users.id. Null for legacy/anonymous projects. "
            "SET NULL on user delete preserves project data."
        ),
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="Human-readable display name for the project.",
    )
    project_dir: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
        doc="Absolute path to the project's working directory on disk.",
    )
    config_json: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        default=dict,
        doc=(
            "Arbitrary project-level configuration blob. "
            "Suggested keys: budget_usd, default_model, away_mode, tags."
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="UTC timestamp when the project was created.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="UTC timestamp of the last modification to the project record.",
    )

    # ── Relationships ──────────────────────────────────────────────────────
    user: Mapped[User | None] = relationship(
        "User",
        back_populates="projects",
    )
    conversations: Mapped[list[Conversation]] = relationship(
        "Conversation",
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="select",
    )
    memory: Mapped[list[Memory]] = relationship(
        "Memory",
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Project id={self.id!r} name={self.name!r}>"

    # ── Indexes ────────────────────────────────────────────────────────────
    __table_args__ = (
        Index(
            "idx_projects_user_id",
            "user_id",
        ),
    )


# ---------------------------------------------------------------------------
# Model: conversations
# ---------------------------------------------------------------------------


class Conversation(Base):
    """Represents a single conversation thread within a project.

    A conversation groups a sequence of messages and agent actions into a
    coherent context window. It maps 1:1 to a Claude SDK session at runtime
    but survives server restarts — the conversation can be resumed by
    replaying messages from this table.

    Columns:
        id             - UUID primary key.
        project_id     - FK → projects.id (CASCADE DELETE).
        title          - Optional human-readable title (e.g. "Fix login bug").
                         May be auto-generated from the first user message.
        created_at     - UTC timestamp when the conversation was created.
        last_active_at - UTC timestamp of the last message or action. Updated
                         every time a message is appended. Used for ordering
                         the conversation list and for idle-timeout detection.

    Relationships:
        project       - Parent project.
        messages      - All messages in this conversation (cascade delete).
        agent_actions - All agent actions in this conversation (cascade delete).

    Indexes:
        idx_conversations_project_active - (project_id, last_active_at DESC)
            Hot path: fetch all conversations for a project sorted by recency.
    """

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_uuid,
        doc="UUID primary key.",
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        doc="FK → projects.id. Deleting a project deletes all its conversations.",
    )
    title: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        doc=(
            "Human-readable title for this conversation. "
            "May be null until set by the user or auto-generated from the first message."
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="UTC timestamp when this conversation was first created.",
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc=(
            "UTC timestamp of the most recent activity (message or agent action). "
            "Updated on every append to keep the conversation list sorted by recency."
        ),
    )

    # ── Relationships ──────────────────────────────────────────────────────
    project: Mapped[Project] = relationship(
        "Project",
        back_populates="conversations",
    )
    messages: Mapped[list[Message]] = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.timestamp",
        lazy="select",
    )
    agent_actions: Mapped[list[AgentAction]] = relationship(
        "AgentAction",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AgentAction.timestamp",
        lazy="select",
    )

    # ── Indexes ────────────────────────────────────────────────────────────
    __table_args__ = (
        Index(
            "idx_conversations_project_active",
            "project_id",
            "last_active_at",
            # DESC on last_active_at gives most-recent-first ordering
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Conversation id={self.id!r} project_id={self.project_id!r} title={self.title!r}>"


# ---------------------------------------------------------------------------
# Model: messages
# ---------------------------------------------------------------------------


class Message(Base):
    """Represents a single message in a conversation (user, assistant, or system).

    Messages are the primary SSOT for conversation history. They are append-only
    in normal operation — edits are not supported (create a new message instead).
    The full conversation can be reconstructed by fetching all messages ordered
    by timestamp, making this table the ground truth for LLM context replay.

    Columns:
        id              - UUID primary key.
        conversation_id - FK → conversations.id (CASCADE DELETE).
        role            - Message role: 'user' | 'assistant' | 'system' | 'tool'.
                          Matches the Claude/OpenAI message role vocabulary.
        content         - Full message content (text). May be empty string for
                          tool-result messages that only carry metadata.
        timestamp       - UTC timestamp when the message was created. Used for
                          ordering and for context-window truncation decisions.
        metadata_json   - Optional metadata blob. Suggested keys:
                            model         : str   – model used for this turn
                            input_tokens  : int   – prompt token count
                            output_tokens : int   – completion token count
                            cost_usd      : float – computed API cost
                            stop_reason   : str   – 'end_turn' | 'max_tokens' | 'tool_use'
                            tool_use_id   : str   – for role='tool' messages

    Indexes:
        idx_messages_conversation_ts - (conversation_id, timestamp ASC)
            Hot path: fetch all messages for a conversation in chronological order
            for LLM context reconstruction.
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_uuid,
        doc="UUID primary key.",
    )
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        doc="FK → conversations.id. Deleting a conversation deletes all its messages.",
    )
    role: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        doc="Message role: 'user' | 'assistant' | 'system' | 'tool'.",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        doc="Full message content as plain text.",
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="UTC timestamp when this message was appended.",
    )
    metadata_json: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        doc=(
            "Optional metadata blob. Keys: model, input_tokens, output_tokens, "
            "cost_usd, stop_reason, tool_use_id."
        ),
    )

    # ── Relationships ──────────────────────────────────────────────────────
    conversation: Mapped[Conversation] = relationship(
        "Conversation",
        back_populates="messages",
    )

    # ── Indexes ────────────────────────────────────────────────────────────
    __table_args__ = (
        Index(
            "idx_messages_conversation_ts",
            "conversation_id",
            "timestamp",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        snippet = (self.content or "")[:40].replace("\n", " ")
        return f"<Message id={self.id!r} role={self.role!r} content={snippet!r}>"


# ---------------------------------------------------------------------------
# Model: agent_actions
# ---------------------------------------------------------------------------


class AgentAction(Base):
    """Records every action taken by an agent within a conversation.

    Agent actions are the audit trail of the system's internal behavior —
    tool calls, sub-agent handoffs, planning decisions, memory reads/writes, etc.
    They are distinct from messages (which are the user-visible conversation)
    but share the same conversation scope.

    Columns:
        id              - UUID primary key.
        conversation_id - FK → conversations.id (CASCADE DELETE).
        agent_role      - Which agent performed the action:
                            'orchestrator' | 'pm' | 'memory' | 'specialist' | <custom>
        action_type     - Category of action:
                            'tool_call'   – executed a tool (bash, file, search, …)
                            'message'     – sent a sub-agent message
                            'decision'    – made a routing/planning decision
                            'handoff'     – transferred control to another agent
                            'memory_read' – read from project memory
                            'memory_write'– wrote to project memory
        payload_json    - Input to the action (tool args, message content, decision rationale).
        result_json     - Output of the action (tool output, response, error info).
                          May be null if the action is still in-progress.
        timestamp       - UTC timestamp when the action was initiated.

    Indexes:
        idx_agent_actions_conversation_ts   - (conversation_id, timestamp ASC)
            Hot path: fetch ordered action log for a conversation.
        idx_agent_actions_conversation_role - (conversation_id, agent_role)
            Used to filter actions by a specific agent within a conversation.
    """

    __tablename__ = "agent_actions"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_uuid,
        doc="UUID primary key.",
    )
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        doc="FK → conversations.id. Deleting a conversation deletes all its agent actions.",
    )
    agent_role: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc=(
            "Role of the agent that performed the action. "
            "E.g. 'orchestrator', 'pm', 'memory', 'specialist'."
        ),
    )
    action_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc=(
            "Category of action: 'tool_call' | 'message' | 'decision' | "
            "'handoff' | 'memory_read' | 'memory_write'."
        ),
    )
    payload_json: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        doc="Input/arguments for this action (tool args, message content, decision rationale).",
    )
    result_json: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        doc=(
            "Output/result of this action. Null if still in-progress. "
            "Should include 'success': bool and 'error': str on failure."
        ),
    )
    task_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc=(
            "DAG task ID this action belongs to. "
            "Links the agent action to a specific task in the DAG execution graph. "
            "Null for actions not associated with a specific DAG task."
        ),
    )
    round: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        doc=(
            "Orchestration round number (0-indexed) when this action occurred. "
            "Enables chronological ordering within a single task. "
            "Null for non-DAG actions."
        ),
    )
    cost_usd: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        doc=(
            "USD cost of the API call associated with this action. "
            "Computed from input/output token counts × model pricing. "
            "Null for non-API actions (tool calls with no LLM cost)."
        ),
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="UTC timestamp when this action was initiated.",
    )

    # ── Relationships ──────────────────────────────────────────────────────
    conversation: Mapped[Conversation] = relationship(
        "Conversation",
        back_populates="agent_actions",
    )

    # ── Indexes ────────────────────────────────────────────────────────────
    __table_args__ = (
        Index(
            "idx_agent_actions_conversation_ts",
            "conversation_id",
            "timestamp",
        ),
        Index(
            "idx_agent_actions_conversation_role",
            "conversation_id",
            "agent_role",
        ),
        Index(
            "idx_agent_actions_task_id",
            "task_id",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<AgentAction id={self.id!r} agent_role={self.agent_role!r} "
            f"action_type={self.action_type!r}>"
        )


# ---------------------------------------------------------------------------
# Model: memory
# ---------------------------------------------------------------------------


class Memory(Base):
    """Persistent key/value memory store scoped to a project.

    The memory table acts as the project-level key/value store that agents
    can read and write to persist facts, preferences, and learned context
    across conversations. Unlike messages (which are conversation-scoped),
    memory is project-scoped and survives across conversation boundaries.

    Columns:
        id         - UUID primary key.
        project_id - FK → projects.id (CASCADE DELETE).
        key        - String key for the memory entry. Convention: use dot-notation
                     namespacing, e.g. 'user.name', 'project.tech_stack',
                     'agent.orchestrator.last_plan'. Max 500 chars.
        value_json - Arbitrary JSON value. Can be a string, number, array,
                     or nested object. Null is valid (used to soft-clear a key).
        updated_at - UTC timestamp of the last write. Useful for staleness detection.

    Constraints:
        UNIQUE(project_id, key) — enforces one value per key per project.
            Use upsert (INSERT ... ON CONFLICT DO UPDATE) for writes.

    Indexes:
        idx_memory_project_key - (project_id, key)
            Covers both the UNIQUE constraint and the hot read path
            (look up a key for a given project).
    """

    __tablename__ = "memory"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_uuid,
        doc="UUID primary key.",
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        doc="FK → projects.id. Deleting a project deletes all its memory entries.",
    )
    key: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        doc=(
            "Memory key using dot-notation namespacing. "
            "E.g. 'user.name', 'project.tech_stack', 'agent.orchestrator.last_plan'."
        ),
    )
    value_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSON,
        nullable=True,
        doc="Arbitrary JSON value. Use upsert semantics on write.",
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=_utcnow,
        doc="UTC timestamp when this memory entry was first created. NULL for rows created before this column existed.",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="UTC timestamp of the last write to this memory entry.",
    )

    # ── Relationships ──────────────────────────────────────────────────────
    project: Mapped[Project] = relationship(
        "Project",
        back_populates="memory",
    )

    # ── Constraints & Indexes ──────────────────────────────────────────────
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_memory_project_key"),
        Index(
            "idx_memory_project_key",
            "project_id",
            "key",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Memory id={self.id!r} project_id={self.project_id!r} key={self.key!r}>"


# ---------------------------------------------------------------------------
# Convenience exports
# ---------------------------------------------------------------------------

__all__ = [
    "AgentAction",
    "Base",
    "Conversation",
    "Memory",
    "Message",
    "Project",
    "User",
]
