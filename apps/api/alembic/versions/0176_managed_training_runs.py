"""Add managed training experiments, attempts, events and exclusive resources.

Revision ID: 0176
Revises: 0175
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0176"
down_revision = "0175"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "training_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("creator_id", sa.UUID(), nullable=True),
        sa.Column("dataset_id", sa.UUID(), nullable=False),
        sa.Column("async_job_id", sa.UUID(), nullable=True),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "authorization", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("current_attempt_id", sa.UUID(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','preparing','training','evaluating','uploading','cancelling','lost','succeeded','failed','cancelled')",
            name="ck_training_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["async_job_id"], ["async_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["training_dataset_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "request_id", name="uq_training_run_request"),
    )
    op.create_index(
        "ix_training_run_project_created",
        "training_runs",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "training_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("resume_from_id", sa.UUID(), nullable=True),
        sa.Column("executor_id", sa.String(length=100), nullable=True),
        sa.Column("boot_id", sa.UUID(), nullable=True),
        sa.Column("lease_digest", sa.String(length=64), nullable=True),
        sa.Column("claim_id", sa.UUID(), nullable=True),
        sa.Column("finish_request_digest", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=200), nullable=True),
        sa.Column("runtime", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "upload_inventory", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("artifacts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued','preparing','training','evaluating','uploading','cancelling','lost','succeeded','failed','cancelled')",
            name="ck_training_attempt_status",
        ),
        sa.CheckConstraint("ordinal > 0", name="ck_training_attempt_ordinal"),
        sa.ForeignKeyConstraint(
            ["resume_from_id"], ["training_attempts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["training_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_training_attempt_ordinal"),
    )
    op.create_table(
        "training_events",
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["training_attempts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("attempt_id", "sequence"),
    )
    op.create_table(
        "training_resource_leases",
        sa.Column("resource_id", sa.String(length=200), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=True),
        sa.Column("executor_id", sa.String(length=100), nullable=True),
        sa.Column("boot_id", sa.UUID(), nullable=True),
        sa.Column("runtime", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["training_attempts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("resource_id"),
        sa.UniqueConstraint("attempt_id"),
    )


def downgrade():
    op.drop_table("training_resource_leases")
    op.drop_table("training_events")
    op.drop_table("training_attempts")
    op.drop_table("training_runs")
