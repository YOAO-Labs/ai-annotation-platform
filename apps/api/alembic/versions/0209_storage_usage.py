"""Persist successful usage snapshots and a recoverable platform inventory."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0209"
down_revision = "0208"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "storage_usage_snapshots",
        sa.Column("scope_key", sa.String(64), primary_key=True),
        sa.Column("object_count", sa.BigInteger),
        sa.Column("total_size_bytes", sa.BigInteger),
        sa.Column("measurement_started_at", sa.DateTime(timezone=True)),
        sa.Column("measured_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_status", sa.String(20), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(80)),
    )
    op.create_table(
        "storage_usage_inventories",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column(
            "async_job_id", UUID, sa.ForeignKey("async_jobs.id", ondelete="SET NULL")
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("authority", JSONB, nullable=False),
        sa.Column("scope", JSONB, nullable=False),
        sa.Column("bucket_index", sa.Integer, nullable=False),
        sa.Column("checkpoint", JSONB, nullable=False),
        sa.Column("success_count", sa.Integer, nullable=False),
        sa.Column("failed_count", sa.Integer, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("storage_usage_inventories")
    op.drop_table("storage_usage_snapshots")
