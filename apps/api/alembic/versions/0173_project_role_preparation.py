"""Additive preparation for project-scoped employee roles.

Adds membership versioning, an independent invitation project role and
nullable task contributor evidence.  Every column is additive and
default-safe so the legacy binaries keep reading and writing these tables
without a coordinated deployment.  No role value, account default or
existing row is converted here; historical unknowns stay NULL.

Revision ID: 0173
Revises: 0172
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0173"
down_revision: str | Sequence[str] | None = "0172"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # project_members: a stable membership version and last-change timestamp.
    # assigned_at stays the original join time.  The server default keeps legacy
    # inserts (which do not name these columns) valid.
    op.add_column(
        "project_members",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "project_members",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # user_invitations: the project role is independent from the platform role.
    # project_id deliberately keeps no foreign key so a deleted target remains
    # detectable and fails closed at acceptance.
    op.add_column(
        "user_invitations",
        sa.Column("project_role", sa.String(length=32), nullable=True),
    )

    # tasks: nullable contributor evidence.  NULL means "unknown/legacy"; this
    # additive column carries no default that could mark historical rows or
    # rows written by old binaries as complete.
    op.add_column(
        "tasks",
        sa.Column(
            "annotation_contributor_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "review_contributor_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "review_submitter_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_tasks_review_submitter_id_users",
        "tasks",
        "users",
        ["review_submitter_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_tasks_review_submitter_id_users", "tasks", type_="foreignkey"
    )
    op.drop_column("tasks", "review_submitter_id")
    op.drop_column("tasks", "review_contributor_ids")
    op.drop_column("tasks", "annotation_contributor_ids")
    op.drop_column("user_invitations", "project_role")
    op.drop_column("project_members", "updated_at")
    op.drop_column("project_members", "version")
