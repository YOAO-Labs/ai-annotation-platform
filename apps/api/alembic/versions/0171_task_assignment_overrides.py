"""Distinguish selected-task assignments from inherited batch defaults.

Revision ID: 0171
Revises: 0170
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision = "0171"
down_revision: str | Sequence[str] | None = "0170"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("assignee_is_override", "reviewer_is_override"):
        op.add_column(
            "tasks",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    # Existing assignments which differ from the batch are already task-level
    # overrides under the effective-assignee contract. Equal values retain the
    # legacy cascade behavior; prior intent cannot be inferred from those rows.
    op.execute(
        """
        UPDATE tasks AS task
        SET assignee_is_override = task.assignee_id IS NOT NULL
                AND task.assignee_id IS DISTINCT FROM batch.annotator_id,
            reviewer_is_override = task.reviewer_id IS NOT NULL
                AND task.reviewer_id IS DISTINCT FROM batch.reviewer_id
        FROM task_batches AS batch
        WHERE task.batch_id = batch.id
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET assignee_is_override = assignee_id IS NOT NULL,
            reviewer_is_override = reviewer_id IS NOT NULL
        WHERE batch_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("tasks", "reviewer_is_override")
    op.drop_column("tasks", "assignee_is_override")
