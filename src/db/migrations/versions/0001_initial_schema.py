"""Initial schema: projects, conversations, messages, agent_actions, memory.

Revision ID: 0001
Revises:
Create Date: 2026-03-11

Creates the five core tables that serve as the Single Source of Truth (SSOT)
for the multi-project agent platform:

  - projects       — top-level project container
  - conversations  — conversation threads within a project
  - messages       — user/assistant/system messages (conversation SSOT)
  - agent_actions  — internal agent audit trail (tool calls, decisions, handoffs)
  - memory         — project-scoped persistent key/value store

All tables use UUID strings (String(36)) as primary keys for portability across
SQLite and PostgreSQL.  Every FK has CASCADE DELETE so referential integrity is
enforced at the database level.

Indexes created:
  idx_conversations_project_active     — (project_id, last_active_at)
  idx_messages_conversation_ts         — (conversation_id, timestamp)
  idx_agent_actions_conversation_ts    — (conversation_id, timestamp)
  idx_agent_actions_conversation_role  — (conversation_id, agent_role)
  idx_memory_project_key               — (project_id, key)  [also covers UNIQUE]

To apply:
    alembic upgrade head

To roll back:
    alembic downgrade base
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Upgrade — create all tables and indexes
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ── projects ─────────────────────────────────────────────────────────
    # Top-level project container. All conversations and memory cascade from here.
    """Apply this migration to the database schema."""
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ── conversations ─────────────────────────────────────────────────────
    # Conversation threads within a project. Survives server restarts.
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_conversations_project_id",
            ondelete="CASCADE",
        ),
    )
    # Hot path: list conversations for a project sorted by recency.
    op.create_index(
        "idx_conversations_project_active",
        "conversations",
        ["project_id", "last_active_at"],
    )

    # ── messages ──────────────────────────────────────────────────────────
    # Append-only message log. Reconstruct LLM context by ordering by timestamp.
    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_messages_conversation_id",
            ondelete="CASCADE",
        ),
    )
    # Hot path: fetch all messages for a conversation in chronological order.
    op.create_index(
        "idx_messages_conversation_ts",
        "messages",
        ["conversation_id", "timestamp"],
    )

    # ── agent_actions ─────────────────────────────────────────────────────
    # Internal agent audit trail: tool calls, decisions, handoffs, memory ops.
    op.create_table(
        "agent_actions",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("agent_role", sa.String(100), nullable=False),
        sa.Column("action_type", sa.String(100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_agent_actions_conversation_id",
            ondelete="CASCADE",
        ),
    )
    # Hot path: fetch ordered action log for a conversation.
    op.create_index(
        "idx_agent_actions_conversation_ts",
        "agent_actions",
        ["conversation_id", "timestamp"],
    )
    # Filter: actions by a specific agent within a conversation.
    op.create_index(
        "idx_agent_actions_conversation_role",
        "agent_actions",
        ["conversation_id", "agent_role"],
    )

    # ── memory ────────────────────────────────────────────────────────────
    # Project-scoped key/value store that survives across conversation boundaries.
    op.create_table(
        "memory",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("key", sa.String(500), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_memory_project_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("project_id", "key", name="uq_memory_project_key"),
    )
    # Covers both the UNIQUE constraint and the hot read path (lookup by key).
    op.create_index(
        "idx_memory_project_key",
        "memory",
        ["project_id", "key"],
    )


# ---------------------------------------------------------------------------
# Downgrade — drop all tables in reverse FK dependency order
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Drop in reverse order of creation (child tables first to honour FK constraints)
    """Revert this migration from the database schema."""
    op.drop_index("idx_memory_project_key", table_name="memory")
    op.drop_table("memory")

    op.drop_index("idx_agent_actions_conversation_role", table_name="agent_actions")
    op.drop_index("idx_agent_actions_conversation_ts", table_name="agent_actions")
    op.drop_table("agent_actions")

    op.drop_index("idx_messages_conversation_ts", table_name="messages")
    op.drop_table("messages")

    op.drop_index("idx_conversations_project_active", table_name="conversations")
    op.drop_table("conversations")

    op.drop_table("projects")
