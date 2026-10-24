"""Persist current-object category observations and index exact saved addresses."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0242"
down_revision = "0241"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "storage_usage_snapshots",
        sa.Column(
            "category_totals", postgresql.JSONB(none_as_null=True), nullable=True
        ),
    )
    for table, index in (
        ("dataset_asset_locations", "ix_asset_location_usage_locator"),
        ("dataset_source_previews", "ix_source_preview_usage_locator"),
    ):
        op.create_index(
            index, table, [sa.text("md5(bucket)"), sa.text("md5(object_key)")]
        )


def downgrade():
    op.drop_index(
        "ix_source_preview_usage_locator", table_name="dataset_source_previews"
    )
    op.drop_index(
        "ix_asset_location_usage_locator", table_name="dataset_asset_locations"
    )
    op.drop_column("storage_usage_snapshots", "category_totals")
