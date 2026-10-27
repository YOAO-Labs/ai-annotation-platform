"""Fixed bulk quality review previews and transactional member outcomes."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0208"
down_revision = "0207"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dataset_quality_review_batches",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "quality_run_id",
            UUID,
            sa.ForeignKey("dataset_quality_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column(
            "async_job_id", UUID, sa.ForeignKey("async_jobs.id", ondelete="SET NULL")
        ),
        sa.Column("authority", JSONB, nullable=False),
        sa.Column("selection", JSONB, nullable=False),
        sa.Column("proof", JSONB, nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("note", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("total", sa.Integer, nullable=False),
        sa.Column("actionable_count", sa.Integer, nullable=False),
        sa.Column("historical_count", sa.Integer, nullable=False),
        sa.Column("processed", sa.Integer, nullable=False),
        sa.Column("summary", JSONB, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(80)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_dataset_quality_review_batches_quality_run_id",
        "dataset_quality_review_batches",
        ["quality_run_id"],
    )
    op.create_table(
        "dataset_quality_review_members",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "batch_id",
            UUID,
            sa.ForeignKey("dataset_quality_review_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("result_id", UUID, nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reason_code", sa.String(80)),
        sa.Column("applied_revision", sa.Integer),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("batch_id", "result_id", name="uq_quality_review_member"),
    )
    op.create_index(
        "ix_quality_review_member_state",
        "dataset_quality_review_members",
        ["batch_id", "state", "id"],
    )


def downgrade():
    op.drop_table("dataset_quality_review_members")
    op.drop_table("dataset_quality_review_batches")
