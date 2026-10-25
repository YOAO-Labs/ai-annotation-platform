"""Retain separate daily original-read and preview-generation denominators."""

from alembic import op
import sqlalchemy as sa

revision = "0261"
down_revision = "0260"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "storage_observation_buckets",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("location_id", sa.Uuid(), primary_key=True),
        sa.Column("source_id", sa.Uuid(), primary_key=True),
        sa.Column("metric", sa.String(24), primary_key=True),
        sa.Column("success_count", sa.BigInteger(), nullable=False),
        sa.Column("failure_count", sa.BigInteger(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "metric IN ('original_read','preview_generate')",
            name="ck_storage_observation_metric",
        ),
        sa.CheckConstraint(
            "success_count >= 0 AND failure_count >= 0 AND "
            "(success_count > 0 OR failure_count > 0)",
            name="ck_storage_observation_counts",
        ),
        sa.CheckConstraint(
            "first_observed_at <= last_observed_at",
            name="ck_storage_observation_times",
        ),
    )


def downgrade():
    op.execute("LOCK TABLE storage_observation_buckets IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM storage_observation_buckets)")
    ):
        raise RuntimeError(
            "Storage rate observations must be retained; downgrade requires an empty table"
        )
    op.drop_table("storage_observation_buckets")
