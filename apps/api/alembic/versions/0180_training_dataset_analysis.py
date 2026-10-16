"""Frozen preparation reports and disposable analysis indexes.

Revision ID: 0180
Revises: 0179
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0180"
down_revision = "0179"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "training_dataset_versions", sa.Column("preparation_report", pg.JSONB())
    )
    op.create_table(
        "training_dataset_analyses",
        sa.Column(
            "version_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("training_dataset_versions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("summary", pg.JSONB(), nullable=False),
    )
    op.create_table(
        "training_dataset_analyzed_samples",
        sa.Column(
            "version_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("training_dataset_analyses.version_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("task_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("exclusion_reason", sa.String(100)),
        sa.Column("issue", sa.String(100)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("label_sha256", sa.String(64)),
        sa.Column("semantic_label_digest", sa.String(64)),
        sa.Column("split", sa.String(8)),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("instances", sa.Integer()),
        sa.Column("is_negative", sa.Boolean()),
        sa.Column("explicit_negative", sa.Boolean()),
        sa.Column("class_ids", pg.JSONB(), nullable=False),
        sa.Column("labels", pg.JSONB(), nullable=False),
        sa.Column("media_state", sa.String(16), nullable=False),
        sa.Column("media_checked_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_training_analysis_sample_split",
        "training_dataset_analyzed_samples",
        ["version_id", "split", "task_id"],
    )
    op.create_index(
        "ix_training_analysis_sample_content",
        "training_dataset_analyzed_samples",
        ["version_id", "sha256"],
    )
    op.create_index(
        "ix_training_analysis_sample_classes",
        "training_dataset_analyzed_samples",
        ["class_ids"],
        postgresql_using="gin",
    )


def downgrade():
    op.drop_table("training_dataset_analyzed_samples")
    op.drop_table("training_dataset_analyses")
    op.drop_column("training_dataset_versions", "preparation_report")
