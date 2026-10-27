"""Fixed dataset quality membership, evidence and resumable runs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0207"
down_revision = "0206"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dataset_quality_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "dataset_id",
            UUID,
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "async_job_id", UUID, sa.ForeignKey("async_jobs.id", ondelete="SET NULL")
        ),
        sa.Column("authority", JSONB, nullable=False),
        sa.Column("rule_version", sa.String(40), nullable=False),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("total", sa.Integer, nullable=False),
        sa.Column("processed", sa.Integer, nullable=False),
        sa.Column("summary", JSONB, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(80)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_dataset_quality_runs_dataset_id", "dataset_quality_runs", ["dataset_id"]
    )
    op.create_index(
        "uq_dataset_quality_active",
        "dataset_quality_runs",
        ["dataset_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_table(
        "dataset_quality_results",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "run_id",
            UUID,
            sa.ForeignKey("dataset_quality_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_id", UUID, nullable=False),
        sa.Column("asset_id", UUID),
        sa.Column("source_id", UUID),
        sa.Column("file_name", sa.String(500), nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reason_code", sa.String(80)),
        sa.Column("bytes_verified", sa.Boolean, nullable=False),
        sa.Column("findings", JSONB, nullable=False),
        sa.Column("width", sa.Integer),
        sa.Column("height", sa.Integer),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("review_state", sa.String(20), nullable=False),
        sa.Column("review_note", sa.String(2000), nullable=False),
        sa.Column("review_revision", sa.Integer, nullable=False),
        sa.Column("reviewed_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "item_id", name="uq_dataset_quality_member"),
    )
    op.create_index(
        "ix_dataset_quality_results_run_id", "dataset_quality_results", ["run_id"]
    )
    op.create_index(
        "ix_dataset_quality_result_state",
        "dataset_quality_results",
        ["run_id", "state", "id"],
    )


def downgrade():
    op.drop_table("dataset_quality_results")
    op.drop_table("dataset_quality_runs")
