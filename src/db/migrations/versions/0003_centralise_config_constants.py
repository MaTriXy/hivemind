"""Migration 0003: Centralise config constants — schema normalisation pass.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-11

Context
-------
This migration accompanies the task_004 "centralised config" refactor.  The
primary change is structural (Python-side), but a small schema normalisation
is included to make the database consistent with the new ``schema_registry``
status-enum constants defined in ``src/models/schema_registry.py``.

Changes
-------
1. No breaking table or column changes — all existing rows remain valid.

2. ADD INDEX idx_messages_conversation_role ON messages(conversation_id, role)
   - New covering index for the common filter: "fetch only 'user' messages
     for a conversation".  Used by context-window truncation queries.

3. ADD INDEX idx_projects_name ON projects(name)
   - Used by the dashboard's project-search endpoint which filters by name
     prefix.  Not present in 0001/0002.

4. ADD INDEX idx_memory_updated_at ON memory(project_id, updated_at DESC)
   - Enables "get recently updated memory entries for a project" without a
     full scan.  Useful for the memory-staleness pruning job.

All changes are:
- Additive only (no columns dropped, no NOT NULL constraints added).
- Idempotent (CREATE INDEX IF NOT EXISTS, ADD COLUMN IF NOT EXISTS).
- Compatible with both SQLite (render_as_batch=True in env.py) and PostgreSQL.

To apply:
    alembic upgrade head

To roll back:
    alembic downgrade 0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Upgrade — add normalisation indexes
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ── 1. messages role index ───────────────────────────────────────────
    # Covers: SELECT ... FROM messages WHERE conversation_id=? AND role='user'
    # Used by context-window truncation (filter user messages only).
    """Apply this migration to the database schema."""
    op.create_index(
        "idx_messages_conversation_role",
        "messages",
        ["conversation_id", "role"],
        if_not_exists=True,
    )

    # ── 2. projects name index ───────────────────────────────────────────
    # Covers: SELECT ... FROM projects WHERE name LIKE 'prefix%' (dashboard search).
    op.create_index(
        "idx_projects_name",
        "projects",
        ["name"],
        if_not_exists=True,
    )

    # ── 3. memory updated_at index ───────────────────────────────────────
    # Covers: SELECT ... FROM memory WHERE project_id=? ORDER BY updated_at DESC
    # Used by memory staleness detection / eviction queries.
    op.create_index(
        "idx_memory_project_updated",
        "memory",
        ["project_id", sa.text("updated_at DESC")],
        if_not_exists=True,
    )


# ---------------------------------------------------------------------------
# Downgrade — remove the indexes added above
# ---------------------------------------------------------------------------


def downgrade() -> None:
    """Revert this migration from the database schema."""
    op.drop_index("idx_memory_project_updated", table_name="memory", if_exists=True)
    op.drop_index("idx_projects_name", table_name="projects", if_exists=True)
    op.drop_index("idx_messages_conversation_role", table_name="messages", if_exists=True)
