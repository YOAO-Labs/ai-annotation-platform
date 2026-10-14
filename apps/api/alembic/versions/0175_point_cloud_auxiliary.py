"""Add opt-in source-bound point-cloud auxiliary assets.

Revision ID: 0175
Revises: 0174
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0175"
down_revision = "0174"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "point_cloud_enhancement_config",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.create_table(
        "point_cloud_auxiliary_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dataset_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("binding_digest", sa.String(64), nullable=False),
        sa.Column("profile", sa.String(64), nullable=False),
        sa.Column("active_generation", sa.Integer()),
        sa.Column("building_generation", sa.Integer()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "dataset_item_id",
            "binding_digest",
            "profile",
            name="uq_point_cloud_auxiliary_owner_binding_profile",
        ),
        sa.CheckConstraint(
            "char_length(binding_digest) = 64",
            name="ck_point_cloud_auxiliary_binding_digest",
        ),
        sa.CheckConstraint(
            "active_generation IS NULL OR active_generation > 0",
            name="ck_point_cloud_auxiliary_active_generation",
        ),
        sa.CheckConstraint(
            "building_generation IS NULL OR building_generation > 0",
            name="ck_point_cloud_auxiliary_building_generation",
        ),
    )
    op.create_table(
        "point_cloud_auxiliary_generations",
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("point_cloud_auxiliary_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("source_manifest", postgresql.JSONB(), nullable=False),
        sa.Column(
            "provenance", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("cameras", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("asset_id", "generation"),
        sa.CheckConstraint(
            "generation > 0", name="ck_point_cloud_auxiliary_generation"
        ),
        sa.CheckConstraint(
            "char_length(input_digest) = 64",
            name="ck_point_cloud_auxiliary_input_digest",
        ),
        sa.CheckConstraint(
            "status IN ('building', 'ready', 'failed')",
            name="ck_point_cloud_auxiliary_status",
        ),
    )
    op.create_index(
        "uq_point_cloud_auxiliary_inflight",
        "point_cloud_auxiliary_generations",
        ["asset_id"],
        unique=True,
        postgresql_where=sa.text("status = 'building'"),
    )
    op.create_index(
        "ix_point_cloud_auxiliary_status_updated",
        "point_cloud_auxiliary_generations",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_table("point_cloud_auxiliary_generations")
    op.drop_table("point_cloud_auxiliary_assets")
    op.drop_column("projects", "point_cloud_enhancement_config")
