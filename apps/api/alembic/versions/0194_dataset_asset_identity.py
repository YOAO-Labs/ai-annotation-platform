"""Add dataset content identity without rewriting existing business records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0194"
down_revision = "0193"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("datasets", sa.Column("storage_scan_prefix", sa.String(1000)))
    # This is only the previously configured scan root, never an inferred object
    # locator. Existing item/task keys and timestamps remain untouched.
    op.execute("UPDATE datasets SET storage_scan_prefix = name || '/'")
    op.create_table(
        "storage_locations",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("config", pg.JSONB(), nullable=False),
    )
    op.create_table(
        "storage_bindings",
        sa.Column("purpose", sa.String(30), primary_key=True),
        sa.Column(
            "location_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("storage_locations.id"),
            nullable=False,
        ),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_table(
        "dataset_assets",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_item_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dataset_items.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("media_type", sa.String(20), nullable=False),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("checksum_algorithm", sa.String(20)),
        sa.Column("checksum_value", sa.String(64)),
        sa.Column(
            "verification_state",
            sa.String(20),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column("legacy_hash_hint", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="ck_dataset_assets_size"
        ),
        sa.CheckConstraint(
            "(verification_state = 'unverified' AND checksum_algorithm IS NULL AND checksum_value IS NULL) OR (verification_state = 'verified' AND checksum_algorithm IS NOT NULL AND checksum_value IS NOT NULL AND checksum_algorithm = 'sha256' AND checksum_value ~ '^[0-9a-f]{64}$' AND size_bytes IS NOT NULL)",
            name="ck_dataset_assets_verification",
        ),
    )
    op.create_index(
        "ix_dataset_assets_checksum_value", "dataset_assets", ["checksum_value"]
    )
    op.create_table(
        "dataset_asset_locations",
        sa.Column(
            "asset_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dataset_assets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "location_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("storage_locations.id"),
            primary_key=True,
        ),
        sa.Column("bucket", sa.String(255), primary_key=True),
        sa.Column("object_key", sa.String(1000), nullable=False),
        sa.Column(
            "role", sa.String(30), nullable=False, server_default="managed_original"
        ),
        sa.Column(
            "reference_state",
            sa.String(20),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column("etag", sa.Text()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "asset_id",
            "location_id",
            "bucket",
            "object_key",
            name="uq_dataset_asset_location_locator",
        ),
        sa.CheckConstraint(
            "reference_state IN ('unverified', 'present', 'missing', 'changed')",
            name="ck_dataset_asset_location_state",
        ),
    )
    op.execute("""
        CREATE FUNCTION guard_verified_dataset_asset() RETURNS trigger AS $$
        BEGIN
            IF OLD.verification_state = 'verified' AND
               ROW(NEW.id, NEW.dataset_item_id, NEW.media_type, NEW.size_bytes,
                   NEW.checksum_algorithm, NEW.checksum_value, NEW.verification_state)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.dataset_item_id, OLD.media_type, OLD.size_bytes,
                   OLD.checksum_algorithm, OLD.checksum_value, OLD.verification_state)
            THEN
                RAISE EXCEPTION 'verified dataset content identity is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER dataset_asset_identity_guard
        BEFORE UPDATE ON dataset_assets FOR EACH ROW
        EXECUTE FUNCTION guard_verified_dataset_asset();
    """)


def downgrade():
    used = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM dataset_assets) OR EXISTS (SELECT 1 FROM storage_bindings)"
            )
        )
        .scalar()
    )
    if used:
        raise RuntimeError(
            "Retain used identity mappings on application rollback; only an unused schema can be downgraded"
        )
    op.drop_table("dataset_asset_locations")
    op.drop_table("dataset_assets")
    op.execute("DROP FUNCTION guard_verified_dataset_asset()")
    op.drop_table("storage_bindings")
    op.drop_table("storage_locations")
    op.drop_column("datasets", "storage_scan_prefix")
