"""Opt-in automatic training policies and durable trigger audit.

Revision ID: 0178
Revises: 0177
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0178"
down_revision = "0177"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "learning_policies",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "creator_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", pg.JSONB(), nullable=False),
        sa.Column("authorization", pg.JSONB(), nullable=False),
        sa.Column(
            "baseline_dataset_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
        ),
        sa.Column("baseline", pg.JSONB(), nullable=False),
        sa.Column("blocked_train_digest", sa.String(64)),
        sa.Column("last_snapshot_digest", sa.String(64)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("next_check_at", sa.DateTime(timezone=True)),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("message", sa.String(1000)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "learning_triggers",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "policy_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("learning_policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column(
            "dataset_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("training_dataset_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("candidate_digest", sa.String(64)),
        sa.Column("train_digest", sa.String(64)),
        sa.Column("validation_digest", sa.String(64)),
        sa.Column("changed_samples", sa.Integer(), nullable=False),
        sa.Column("message", sa.String(1000)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "project_id",
            "policy_revision",
            "candidate_digest",
            name="uq_learning_candidate",
        ),
    )
    op.create_index(
        "ix_learning_trigger_project_created",
        "learning_triggers",
        ["project_id", "created_at"],
    )
    op.create_index(
        "uq_learning_active_project",
        "learning_triggers",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('building','queued','running')"),
    )
    op.add_column(
        "training_runs",
        sa.Column(
            "auto_trigger_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("learning_triggers.id", ondelete="RESTRICT"),
            unique=True,
        ),
    )
    op.create_index(
        "uq_training_auto_active_project",
        "training_runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text(
            "auto_trigger_id IS NOT NULL AND status IN ('queued','preparing','training','evaluating','uploading','cancelling','lost')"
        ),
    )


def downgrade():
    op.drop_index("uq_training_auto_active_project", table_name="training_runs")
    op.drop_column("training_runs", "auto_trigger_id")
    op.drop_table("learning_triggers")
    op.drop_table("learning_policies")
