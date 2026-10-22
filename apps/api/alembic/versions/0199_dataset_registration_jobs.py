"""Keep preflight and registration checkpoints beyond short-lived task history."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0199"
down_revision = "0198"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dataset_registration_runs",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "scan_run_id",
            UUID(),
            sa.ForeignKey("dataset_scan_runs.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "source_id",
            UUID(),
            sa.ForeignKey("dataset_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "async_job_id",
            UUID(),
            sa.ForeignKey("async_jobs.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("lease_token", UUID()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(60)),
        sa.Column("counts", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_dataset_registration_status",
        ),
        sa.CheckConstraint(
            "phase IN ('preflight','registering')", name="ck_dataset_registration_phase"
        ),
    )
    op.create_index(
        "ix_dataset_registration_runs_source_id",
        "dataset_registration_runs",
        ["source_id"],
    )
    op.create_index(
        "uq_dataset_source_active_registration",
        "dataset_registration_runs",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','running')"),
    )
    op.create_table(
        "dataset_registration_entries",
        sa.Column(
            "run_id",
            UUID(),
            sa.ForeignKey("dataset_registration_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path_digest", sa.String(64), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column(
            "item_id", UUID(), sa.ForeignKey("dataset_items.id", ondelete="SET NULL")
        ),
        sa.Column("error_code", sa.String(60)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("refresh_unverified", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending','ready','registered','unchanged','error')",
            name="ck_dataset_registration_entry_state",
        ),
    )
    op.create_index(
        "ix_dataset_registration_work",
        "dataset_registration_entries",
        ["run_id", "state", "path_digest"],
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM dataset_registration_runs)")
    ):
        raise RuntimeError("Retain registration checkpoints on application rollback")
    op.drop_table("dataset_registration_entries")
    op.drop_table("dataset_registration_runs")
