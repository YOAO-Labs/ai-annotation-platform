"""Add management-only task streams without rewriting frozen training inputs.

Revision ID: 0188
Revises: 0187
"""

from alembic import op
import sqlalchemy as sa

revision = "0188"
down_revision = "0187"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "project_task_datasets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("task", sa.String(32), nullable=False),
        sa.Column("next_ordinal", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "task", name="uq_project_task_dataset"),
        sa.CheckConstraint("next_ordinal > 0", name="ck_task_dataset_next_ordinal"),
    )
    op.create_table(
        "task_dataset_version_memberships",
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("task_dataset_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id"], ["training_dataset_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["task_dataset_id"], ["project_task_datasets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("version_id"),
        sa.UniqueConstraint(
            "task_dataset_id", "ordinal", name="uq_task_dataset_ordinal"
        ),
        sa.CheckConstraint("ordinal > 0", name="ck_task_dataset_ordinal"),
    )


def downgrade():
    op.drop_table("task_dataset_version_memberships")
    op.drop_table("project_task_datasets")
