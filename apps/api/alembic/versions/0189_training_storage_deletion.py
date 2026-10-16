"""Retain retryable storage cleanup after a training record is deleted.

Revision ID: 0189
Revises: 0188
"""

from alembic import op
import sqlalchemy as sa

revision = "0189"
down_revision = "0188"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "training_storage_deletions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("prefix", sa.String(500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bucket", "prefix", name="uq_training_storage_deletion_prefix"
        ),
    )
    op.create_index(
        "ix_training_storage_deletion_attempt",
        "training_storage_deletions",
        ["last_attempted_at", "id"],
    )


def downgrade():
    op.drop_table("training_storage_deletions")
