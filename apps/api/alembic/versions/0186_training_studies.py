"""Finite studies and fenced native-process accounting.

Revision ID: 0186
Revises: 0185
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0186"
down_revision = "0185"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "training_studies",
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
            "dataset_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "initialization_model_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("trained_model_versions.id", ondelete="RESTRICT"),
        ),
        sa.Column("request_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("scheduling", sa.Boolean(), nullable=False),
        sa.Column("plan", pg.JSONB(), nullable=False),
        sa.Column("authorization", pg.JSONB(), nullable=False),
        sa.Column("origin", pg.JSONB()),
        sa.Column("message", sa.String(1000)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "project_id", "request_id", name="uq_training_study_request"
        ),
    )
    op.create_index(
        "ix_training_study_project_created",
        "training_studies",
        ["project_id", "created_at"],
    )
    op.add_column("training_runs", sa.Column("study_id", pg.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_training_run_study",
        "training_runs",
        "training_studies",
        ["study_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_training_run_study_identity", "training_runs", ["id", "study_id"]
    )
    op.create_index(
        "uq_training_study_active_run",
        "training_runs",
        ["study_id"],
        unique=True,
        postgresql_where=sa.text(
            "study_id IS NOT NULL AND status IN ('queued','preparing','training','evaluating','uploading','cancelling','lost')"
        ),
    )
    op.add_column("training_attempts", sa.Column("native_budget", pg.JSONB()))
    op.add_column("training_attempts", sa.Column("native_accounting", pg.JSONB()))
    op.create_table(
        "training_trials",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "study_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("training_studies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("run_id", pg.UUID(as_uuid=True)),
        sa.Column("config", pg.JSONB(), nullable=False),
        sa.Column("config_digest", sa.String(64), nullable=False),
        sa.Column("values", pg.JSONB(), nullable=False),
        sa.UniqueConstraint("study_id", "ordinal", name="uq_training_trial_ordinal"),
        sa.UniqueConstraint("run_id", name="uq_training_trial_run"),
        sa.ForeignKeyConstraint(
            ["run_id", "study_id"],
            ["training_runs.id", "training_runs.study_id"],
            name="fk_training_trial_same_study_run",
            ondelete="RESTRICT",
        ),
    )


def downgrade():
    op.drop_table("training_trials")
    op.drop_column("training_attempts", "native_accounting")
    op.drop_column("training_attempts", "native_budget")
    op.drop_index("uq_training_study_active_run", table_name="training_runs")
    op.drop_constraint(
        "uq_training_run_study_identity", "training_runs", type_="unique"
    )
    op.drop_constraint("fk_training_run_study", "training_runs", type_="foreignkey")
    op.drop_column("training_runs", "study_id")
    op.drop_index("ix_training_study_project_created", table_name="training_studies")
    op.drop_table("training_studies")
