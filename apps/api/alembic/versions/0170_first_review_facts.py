"""Persist retention-safe first-review facts for project performance.

Revision ID: 0170
Revises: 0169
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0170"
down_revision: str | Sequence[str] | None = "0169"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add without a default so pre-rollout rows remain NULL (unknown), then
    # install the default for tasks inserted after this revision.
    op.add_column(
        "tasks",
        sa.Column(
            "first_review_eligible",
            sa.Boolean(),
            nullable=True,
        ),
    )
    op.alter_column(
        "tasks",
        "first_review_eligible",
        server_default=sa.text("true"),
    )
    op.add_column(
        "tasks",
        sa.Column("first_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_tasks_project_first_reviewed_at",
        "tasks",
        ["project_id", "first_reviewed_at"],
    )
    op.add_column(
        "tasks",
        sa.Column("first_review_result", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "first_review_contributor_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "first_review_contributor_ids")
    op.drop_column("tasks", "first_review_result")
    op.drop_index("ix_tasks_project_first_reviewed_at", table_name="tasks")
    op.drop_column("tasks", "first_reviewed_at")
    op.drop_column("tasks", "first_review_eligible")
