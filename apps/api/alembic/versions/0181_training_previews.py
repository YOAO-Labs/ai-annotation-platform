"""Native augmentation previews and independent CPU fencing.

Revision ID: 0181
Revises: 0180
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0181"
down_revision = "0180"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "training_previews",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "creator_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "async_job_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("async_jobs.id", ondelete="SET NULL"),
        ),
        sa.Column("request_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("request", pg.JSONB(), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("spec", pg.JSONB(), nullable=False),
        sa.Column("authorization", pg.JSONB(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("resource_id", sa.String(200), nullable=False),
        sa.Column("executor_id", sa.String(100)),
        sa.Column("boot_id", pg.UUID(as_uuid=True)),
        sa.Column("claim_id", pg.UUID(as_uuid=True)),
        sa.Column("lease_digest", sa.String(64)),
        sa.Column("finish_digest", sa.String(64)),
        sa.Column("runtime", pg.JSONB(), nullable=False),
        sa.Column("upload_inventory", pg.JSONB(), nullable=False),
        sa.Column("artifacts", pg.JSONB(), nullable=False),
        sa.Column("error_message", sa.String(1000)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("put_expires_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "project_id", "request_id", name="uq_training_preview_request"
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','cancelling','lost','succeeded','failed','cancelled','expired')",
            name="ck_training_preview_status",
        ),
    )
    op.create_index("ix_training_preview_expiry", "training_previews", ["expires_at"])
    op.create_index(
        "ix_training_preview_queue", "training_previews", ["status", "created_at"]
    )
    op.create_table(
        "training_preview_resource_leases",
        sa.Column("resource_id", sa.String(200), primary_key=True),
        sa.Column(
            "preview_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("training_previews.id", ondelete="RESTRICT"),
            unique=True,
        ),
        sa.Column("executor_id", sa.String(100)),
        sa.Column("boot_id", pg.UUID(as_uuid=True)),
        sa.Column("runtime", pg.JSONB(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table("training_preview_resource_leases")
    op.drop_table("training_previews")
