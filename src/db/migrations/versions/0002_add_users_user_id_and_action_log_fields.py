"""Migration 0002: Add users table, user_id on projects, task_id/round/cost on agent_actions.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-11

Changes in this migration:
1. CREATE TABLE users
   - id, external_id (unique nullable), email (unique nullable), display_name,
     db_path (per-user SQLite routing), created_at, updated_at

2. ALTER TABLE projects — ADD COLUMN user_id (FK → users.id, nullable, SET NULL on delete)
   - Nullable so existing projects remain valid (backward compatible).
   - SET NULL on user delete preserves project data.

3. CREATE INDEX idx_projects_user_id ON projects(user_id)
   - Hot path: list all projects for a given user.

4. ALTER TABLE agent_actions — ADD COLUMN task_id (String(255), nullable)
   - Links an agent action to a specific DAG task ID.

5. ALTER TABLE agent_actions — ADD COLUMN round (Integer, nullable)
   - Orchestration round number for ordering within a task.

6. ALTER TABLE agent_actions — ADD COLUMN cost_usd (Float, nullable)
   - USD cost of the API call associated with this action.

7. CREATE INDEX idx_agent_actions_task_id ON agent_actions(task_id)
   - Hot path: look up all actions for a specific task.

All changes are backward compatible:
- New columns are nullable → existing rows remain valid without re-inserting.
- SQLite ALTER TABLE limitations handled by render_as_batch=True in env.py.

To apply:
    alembic upgrade head

To roll back:
    alembic downgrade 0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ── 1. users table ───────────────────────────────────────────────────
    # Root of the multi-tenant hierarchy. Every project optionally belongs
    # to a user. Per-user SQLite isolation is signalled via db_path.
    """Apply this migration to the database schema."""
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True, unique=True),
        sa.Column("email", sa.String(255), nullable=True, unique=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("db_path", sa.String(1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ── 2. projects.user_id — nullable FK → users.id ─────────────────────
    # Nullable so existing (legacy) projects are not invalidated.
    # SET NULL on user delete: project data survives the user being removed.
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            "fk_projects_user_id",
            "users",
            ["user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # ── 3. Index: projects.user_id ────────────────────────────────────────
    # Hot path: fetch all projects for a given user sorted by created_at.
    op.create_index(
        "idx_projects_user_id",
        "projects",
        ["user_id"],
    )

    # ── 4-6. agent_actions: task_id, round, cost_usd ─────────────────────
    # All nullable for backward compatibility with existing action log rows.
    with op.batch_alter_table("agent_actions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "task_id",
                sa.String(255),
                nullable=True,
                comment="DAG task ID this action belongs to.",
            )
        )
        batch_op.add_column(
            sa.Column(
                "round",
                sa.Integer(),
                nullable=True,
                comment="Orchestration round number (0-indexed).",
            )
        )
        batch_op.add_column(
            sa.Column(
                "cost_usd",
                sa.Float(),
                nullable=True,
                comment="USD cost of the LLM API call for this action.",
            )
        )

    # ── 7. Index: agent_actions.task_id ──────────────────────────────────
    # Look up all actions associated with a specific DAG task.
    op.create_index(
        "idx_agent_actions_task_id",
        "agent_actions",
        ["task_id"],
    )


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Drop in reverse order of creation.

    # Remove agent_actions additions
    """Revert this migration from the database schema."""
    op.drop_index("idx_agent_actions_task_id", table_name="agent_actions")
    with op.batch_alter_table("agent_actions", schema=None) as batch_op:
        batch_op.drop_column("cost_usd")
        batch_op.drop_column("round")
        batch_op.drop_column("task_id")

    # Remove projects.user_id
    op.drop_index("idx_projects_user_id", table_name="projects")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_constraint("fk_projects_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    # Drop users table last (referenced by projects.user_id FK above)
    op.drop_table("users")
