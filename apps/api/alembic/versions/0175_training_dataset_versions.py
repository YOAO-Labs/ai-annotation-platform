"""Add durable project training dataset versions.

Revision ID: 0175
Revises: 0174
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0175"
down_revision = "0174"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "training_dataset_versions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "creator_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "parent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "async_job_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("async_jobs.id", ondelete="SET NULL"),
        ),
        sa.Column("request_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot", pg.JSONB(), nullable=False),
        sa.Column("snapshot_digest", sa.String(64), nullable=False),
        sa.Column("manifest", pg.JSONB(), nullable=False),
        sa.Column("progress_pct", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_message", sa.String(1000)),
        sa.Column("package_key", sa.String(500)),
        sa.Column("package_sha256", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "project_id", "request_id", name="uq_training_dataset_request"
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','ready','failed','cancelled')",
            name="ck_training_dataset_status",
        ),
    )
    op.create_index(
        "ix_training_dataset_project_created",
        "training_dataset_versions",
        ["project_id", "created_at"],
    )


def downgrade():
    op.drop_table("training_dataset_versions")
