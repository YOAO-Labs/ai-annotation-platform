"""Candidate decisions and bounded lineage indexes.

Revision ID: 0184
Revises: 0183
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0184"
down_revision = "0183"
branch_labels = depends_on = None

INDEXES = {
    "training_dataset_versions": ["parent_id"],
    "training_runs": ["dataset_id"],
    "training_attempts": ["resume_from_id", "run_id"],
    "trained_model_versions": ["run_id", "attempt_id"],
    "trained_model_deployments": ["model_version_id"],
    "project_model_releases": [
        "model_version_id",
        "deployment_id",
        "previous_release_id",
        "rollback_from_id",
    ],
    "training_recipe_revisions": ["recipe_id"],
    "sample_evaluation_runs": ["dataset_id", "model_version_id"],
}


def upgrade():
    op.create_table(
        "model_decisions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "model_version_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("trained_model_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "creator_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("creator_label", sa.String(100), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("request_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("disposition", sa.String(20), nullable=False),
        sa.Column("note", sa.String(2000), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "model_version_id", "sequence", name="uq_model_decision_sequence"
        ),
        sa.UniqueConstraint(
            "model_version_id", "request_id", name="uq_model_decision_request"
        ),
        sa.CheckConstraint("sequence > 0", name="ck_model_decision_sequence"),
        sa.CheckConstraint(
            "disposition IN ('active','research','declined','archived')",
            name="ck_model_decision_disposition",
        ),
    )
    op.create_index("ix_model_decisions_project_id", "model_decisions", ["project_id"])
    for table, columns in INDEXES.items():
        for column in columns:
            op.create_index(
                f"ix_lineage_{table}_{column}", table, [column, "created_at", "id"]
            )
    op.create_index(
        "ix_lineage_prediction_model",
        "prediction_metas",
        [sa.text("(extra -> 'trained_model' ->> 'model_version_id')"), "id"],
    )


def downgrade():
    op.drop_index("ix_lineage_prediction_model", table_name="prediction_metas")
    for table, columns in INDEXES.items():
        for column in columns:
            op.drop_index(f"ix_lineage_{table}_{column}", table_name=table)
    op.drop_table("model_decisions")
