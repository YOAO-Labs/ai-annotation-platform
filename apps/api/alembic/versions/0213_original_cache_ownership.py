"""Account for complete original cache generations before any object PUT."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0213"
down_revision = "0212"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_source_cache", "dataset_sources")
    op.create_check_constraint(
        "ck_source_cache",
        "dataset_sources",
        "cache_policy IN ('none','preview','on_demand')",
    )
    op.create_table(
        "original_cache_entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        *(
            sa.Column(name, UUID(as_uuid=True), nullable=False)
            for name in ("scope_location_id", "source_id", "asset_id", "location_id")
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        *(
            sa.Column(name, sa.Integer(), nullable=False)
            for name in (
                "source_revision",
                "policy_revision",
                "location_revision",
                "binding_generation",
            )
        ),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("storage_identity", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("uncertain_write", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        *(
            sa.Column(name, sa.DateTime(timezone=True), nullable=False)
            for name in (
                "last_accessed_at",
                "expires_at",
                "writer_until",
                "retain_until",
                "next_check_at",
            )
        ),
        sa.Column("error_code", sa.String(64)),
        sa.CheckConstraint(
            "state IN ('uploading','ready','deleting')", name="ck_original_cache_state"
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_original_cache_size"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_original_cache_sha"),
    )
    op.create_index(
        "ix_original_cache_due", "original_cache_entries", ["next_check_at", "id"]
    )
    op.create_index(
        "ix_original_cache_quota",
        "original_cache_entries",
        ["scope_location_id", "state", "last_accessed_at"],
    )
    op.create_index(
        "uq_original_cache_current",
        "original_cache_entries",
        ["asset_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('uploading','ready')"),
    )
    op.create_table(
        "original_cache_read_leases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entry_id",
            UUID(as_uuid=True),
            sa.ForeignKey("original_cache_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_original_cache_read_lease",
        "original_cache_read_leases",
        ["entry_id", "expires_at"],
    )


def downgrade():
    op.execute(
        "LOCK TABLE original_cache_entries, dataset_sources, storage_locations, dataset_asset_locations IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text("""
        SELECT EXISTS (SELECT 1 FROM original_cache_entries)
            OR EXISTS (SELECT 1 FROM dataset_sources WHERE cache_policy = 'on_demand')
            OR EXISTS (SELECT 1 FROM storage_locations WHERE config->>'cache_policy' = 'on_demand')
            OR EXISTS (SELECT 1 FROM dataset_asset_locations WHERE role = 'original_cache')
    """)
    ):
        raise RuntimeError(
            "Cannot discard original cache policies or recovery ownership"
        )
    op.drop_table("original_cache_read_leases")
    op.drop_table("original_cache_entries")
    op.drop_constraint("ck_source_cache", "dataset_sources")
    op.create_check_constraint(
        "ck_source_cache", "dataset_sources", "cache_policy IN ('none','preview')"
    )
