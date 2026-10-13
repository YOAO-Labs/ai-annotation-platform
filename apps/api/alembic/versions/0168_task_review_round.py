"""Persist the submission/review round used by performance attribution.

Revision ID: 0168
Revises: 0167
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0168"
down_revision: str | Sequence[str] | None = "0167"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("review_round_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_tasks_review_round_id", "tasks", ["review_round_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_review_round_id", table_name="tasks")
    op.drop_column("tasks", "review_round_id")
