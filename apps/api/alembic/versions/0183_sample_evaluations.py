"""Frozen sample evaluations, fenced attempts and reusable evidence.

Revision ID: 0183
Revises: 0182
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0183"
down_revision = "0182"
branch_labels = depends_on = None


def uid(name, target=None, *, nullable=True, primary_key=False, ondelete="RESTRICT"):
    args = [sa.ForeignKey(target, ondelete=ondelete)] if target else []
    return sa.Column(
        name, pg.UUID(as_uuid=True), *args, nullable=nullable, primary_key=primary_key
    )


def created():
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def upgrade():
    states = "'queued','running','cancelling','lost','succeeded','failed','cancelled'"
    op.create_table(
        "sample_evaluation_runs",
        uid("id", nullable=False, primary_key=True),
        uid("project_id", "projects.id", nullable=False),
        uid("creator_id", "users.id", ondelete="SET NULL"),
        uid("dataset_id", "training_dataset_versions.id", nullable=False),
        uid("model_version_id", "trained_model_versions.id", nullable=False),
        uid("deployment_id", "trained_model_deployments.id", nullable=False),
        uid("request_id", nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("spec", pg.JSONB(), nullable=False),
        sa.Column("spec_digest", sa.String(64), nullable=False),
        sa.Column("authorization", pg.JSONB(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        uid("current_attempt_id"),
        uid("async_job_id", "async_jobs.id", ondelete="SET NULL"),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_message", sa.String(1000)),
        created(),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "project_id", "request_id", name="uq_sample_evaluation_request"
        ),
        sa.CheckConstraint(f"status IN ({states})", name="ck_sample_evaluation_status"),
    )
    op.create_index(
        "ix_sample_evaluation_project_created",
        "sample_evaluation_runs",
        ["project_id", "created_at"],
    )
    op.create_table(
        "sample_evaluation_attempts",
        uid("id", nullable=False, primary_key=True),
        uid("run_id", "sample_evaluation_runs.id", nullable=False, ondelete="CASCADE"),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        uid("claim_id"),
        uid("async_job_id", "async_jobs.id", ondelete="SET NULL"),
        sa.Column("report", pg.JSONB()),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_message", sa.String(1000)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("maintenance_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        created(),
        sa.UniqueConstraint(
            "run_id", "ordinal", name="uq_sample_evaluation_attempt_ordinal"
        ),
        sa.CheckConstraint(
            "ordinal > 0 AND generation > 0",
            name="ck_sample_evaluation_attempt_generation",
        ),
        sa.CheckConstraint(
            f"status IN ({states})", name="ck_sample_evaluation_attempt_status"
        ),
    )
    op.create_table(
        "sample_evaluation_samples",
        uid(
            "run_id",
            "sample_evaluation_runs.id",
            nullable=False,
            primary_key=True,
            ondelete="CASCADE",
        ),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        uid("request_id", nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("request", pg.JSONB()),
        sa.Column("evidence", pg.JSONB()),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_message", sa.String(1000)),
        uid("produced_attempt_id"),
        sa.UniqueConstraint("request_id"),
        sa.CheckConstraint(
            "ordinal >= 0 AND ordinal < 32", name="ck_sample_evaluation_sample_ordinal"
        ),
        sa.CheckConstraint(
            "status IN ('pending','inflight','succeeded','failed')",
            name="ck_sample_evaluation_sample_status",
        ),
    )


def downgrade():
    op.drop_table("sample_evaluation_samples")
    op.drop_table("sample_evaluation_attempts")
    op.drop_table("sample_evaluation_runs")
