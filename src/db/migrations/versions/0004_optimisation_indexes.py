"""Migration 0004: Optimisation indexes for multi-tenant query patterns.

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-11

Context
-------
This migration adds the composite indexes identified as missing during the
task_004 database audit.  All changes are purely additive — no columns are
dropped or renamed, and no NOT NULL constraints are added.

Changes
-------
1. ADD INDEX idx_conversations_project_created ON conversations(project_id, created_at DESC)
   - Composite "tenant_id + created_at" index required by acceptance criteria.
   - The project_id column acts as the effective tenant boundary for conversations.
   - Enables fast queries like "list conversations for a project ordered by creation
     date" (useful for keyset pagination by created_at rather than last_active_at).
   - Complements the existing idx_conversations_project_active (project_id, last_active_at).

2. ADD INDEX idx_projects_user_created ON projects(user_id, created_at DESC)
   - Upgrade from the single-column idx_projects_user_id to a composite index.
   - Allows "list projects for a user, newest first" queries to be satisfied entirely
     from the index (covering index for the ORDER BY), eliminating a filesort.
   - The single-column index idx_projects_user_id remains and still satisfies FK
     lookups; this composite is additive.

3. ADD INDEX idx_agent_actions_conversation_type ON agent_actions(conversation_id, action_type)
   - Covers filter queries like "fetch only tool_call actions for a conversation".
   - Complements idx_agent_actions_conversation_role (which filters by agent_role).
   - Without this index, action_type filters cause a full scan of the conversation's
     action rows (can be thousands of rows for long-running agents).

All changes:
- Use IF NOT EXISTS so re-running is safe (idempotent).
- Compatible with SQLite (via render_as_batch=True in env.py) and PostgreSQL.
- Have corresponding downgrade statements.

To apply:
    alembic upgrade head

To roll back:
    alembic downgrade 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Upgrade — add optimisation indexes
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ── 1. conversations: (project_id, created_at DESC) ──────────────────
    # Acceptance-criteria index: "tenant_id + created_at composite on conversations".
    # project_id is the effective tenant key for the conversation table.
    # DESC on created_at gives newest-first ordering without a post-sort.
    """Apply this migration to the database schema."""
    op.create_index(
        "idx_conversations_project_created",
        "conversations",
        ["project_id", sa.text("created_at DESC")],
        if_not_exists=True,
    )

    # ── 2. projects: (user_id, created_at DESC) ──────────────────────────
    # Composite upgrade from single-column idx_projects_user_id.
    # Eliminates filesort for: SELECT ... FROM projects WHERE user_id=? ORDER BY created_at DESC
    op.create_index(
        "idx_projects_user_created",
        "projects",
        ["user_id", sa.text("created_at DESC")],
        if_not_exists=True,
    )

    # ── 3. agent_actions: (conversation_id, action_type) ─────────────────
    # Covers: SELECT ... FROM agent_actions WHERE conversation_id=? AND action_type='tool_call'
    # Without this, action_type filters cause a full scan of the conversation's
    # action rows (O(N) instead of O(log N)).
    op.create_index(
        "idx_agent_actions_conversation_type",
        "agent_actions",
        ["conversation_id", "action_type"],
        if_not_exists=True,
    )


# ---------------------------------------------------------------------------
# Downgrade — remove the indexes added above
# ---------------------------------------------------------------------------


def downgrade() -> None:
    """Revert this migration from the database schema."""
    op.drop_index(
        "idx_agent_actions_conversation_type",
        table_name="agent_actions",
        if_exists=True,
    )
    op.drop_index(
        "idx_projects_user_created",
        table_name="projects",
        if_exists=True,
    )
    op.drop_index(
        "idx_conversations_project_created",
        table_name="conversations",
        if_exists=True,
    )
