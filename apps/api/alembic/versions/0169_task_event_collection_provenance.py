"""Mark legacy task-event time and qualify new session ingestion.

Revision ID: 0169
Revises: 0168
Create Date: 2026-09-14
"""

import sqlalchemy as sa
from alembic import op


revision = "0169"
down_revision = "0168"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_events",
        sa.Column("collector_version", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "task_events",
        sa.Column(
            "collection_source",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'legacy'"),
        ),
    )
    op.add_column(
        "task_events",
        sa.Column(
            "collection_coverage",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'unverified_collection'"),
        ),
    )
    op.create_check_constraint(
        "ck_task_events_collection_source",
        "task_events",
        "collection_source IN ('legacy', 'session')",
    )
    op.create_check_constraint(
        "ck_task_events_collection_coverage",
        "task_events",
        "collection_coverage IN ('unverified_collection', 'qualified')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_task_events_collection_coverage", "task_events", type_="check"
    )
    op.drop_constraint("ck_task_events_collection_source", "task_events", type_="check")
    op.drop_column("task_events", "collection_coverage")
    op.drop_column("task_events", "collection_source")
    op.drop_column("task_events", "collector_version")
