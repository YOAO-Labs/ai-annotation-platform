"""Separate ownership, derived previews and source lifecycle from locators."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0200"
down_revision = "0199"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column(
            "ownership_mode", sa.String(20), nullable=False, server_default="reference"
        ),
        sa.Column("cache_policy", sa.String(20), nullable=False, server_default="none"),
        sa.Column("policy_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unregistered_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("dataset_sources", column)
    op.create_check_constraint(
        "ck_source_ownership",
        "dataset_sources",
        "ownership_mode IN ('reference','managed_copy')",
    )
    op.create_check_constraint(
        "ck_source_cache", "dataset_sources", "cache_policy IN ('none','preview')"
    )
    op.add_column(
        "dataset_registration_entries",
        sa.Column("preview_estimated_bytes", sa.BigInteger()),
    )
    op.add_column(
        "dataset_registration_entries", sa.Column("preview_error", sa.String(60))
    )
    op.add_column(
        "dataset_registration_entries",
        sa.Column(
            "original_estimated_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_table(
        "dataset_source_items",
        sa.Column(
            "source_id",
            UUID(),
            sa.ForeignKey("dataset_sources.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("path_digest", sa.String(64), primary_key=True),
        sa.Column("run_id", UUID(), nullable=False),
        sa.Column(
            "dataset_item_id",
            UUID(),
            sa.ForeignKey("dataset_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            UUID(),
            sa.ForeignKey("dataset_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("ownership_mode", sa.String(20), nullable=False),
        sa.Column("owns_item", sa.Boolean(), nullable=False, server_default="true"),
        sa.ForeignKeyConstraint(
            ["run_id", "path_digest"],
            ["dataset_scan_entries.run_id", "dataset_scan_entries.path_digest"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "ownership_mode IN ('reference','managed_copy')",
            name="ck_source_item_ownership",
        ),
    )
    op.create_index(
        "ix_dataset_source_items_dataset_item_id",
        "dataset_source_items",
        ["dataset_item_id"],
    )
    op.execute("""INSERT INTO dataset_source_items
        (source_id,path_digest,run_id,dataset_item_id,asset_id,content_sha256,ownership_mode)
        SELECT r.source_id,r.path_digest,r.run_id,r.dataset_item_id,a.id,a.checksum_value,'reference'
        FROM dataset_references r JOIN dataset_assets a ON a.dataset_item_id=r.dataset_item_id""")
    op.create_table(
        "dataset_source_previews",
        sa.Column("source_id", UUID(), primary_key=True),
        sa.Column("path_digest", sa.String(64), primary_key=True),
        sa.Column(
            "asset_id",
            UUID(),
            sa.ForeignKey("dataset_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("spec", sa.String(60), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(60)),
        sa.Column("location_id", UUID(), sa.ForeignKey("storage_locations.id")),
        sa.Column("bucket", sa.String(255)),
        sa.Column("object_key", sa.String(1000)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column(
            "measured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "path_digest"],
            ["dataset_source_items.source_id", "dataset_source_items.path_digest"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "state IN ('ready','failed')", name="ck_source_preview_state"
        ),
    )
    op.execute(
        "CREATE TRIGGER source_item_immutable BEFORE UPDATE ON dataset_source_items FOR EACH ROW EXECUTE FUNCTION protect_dataset_reference()"
    )
    op.execute("""CREATE FUNCTION protect_directory_item() RETURNS trigger AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM dataset_source_items WHERE dataset_item_id=OLD.id)
           AND (NEW.file_path IS DISTINCT FROM OLD.file_path OR NEW.dataset_id IS DISTINCT FROM OLD.dataset_id) THEN
            RAISE EXCEPTION 'Directory item binding is immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER directory_item_immutable BEFORE UPDATE ON dataset_items FOR EACH ROW EXECUTE FUNCTION protect_directory_item()"
    )
    op.execute("""CREATE FUNCTION protect_directory_asset() RETURNS trigger AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM dataset_source_items WHERE asset_id=OLD.id)
           AND (NEW.dataset_item_id IS DISTINCT FROM OLD.dataset_item_id OR NEW.checksum_algorithm IS DISTINCT FROM OLD.checksum_algorithm OR NEW.checksum_value IS DISTINCT FROM OLD.checksum_value OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes OR NEW.verification_state IS DISTINCT FROM OLD.verification_state) THEN
            RAISE EXCEPTION 'Directory asset identity is immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER directory_asset_immutable BEFORE UPDATE ON dataset_assets FOR EACH ROW EXECUTE FUNCTION protect_directory_asset()"
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("""SELECT
        EXISTS(SELECT 1 FROM dataset_source_items) OR
        EXISTS(SELECT 1 FROM dataset_sources WHERE ownership_mode <> 'reference'
            OR cache_policy <> 'none' OR policy_revision <> 1 OR unregistered_at IS NOT NULL)
    """)
    ):
        raise RuntimeError(
            "Retain directory identities and policies on application rollback"
        )
    op.execute("DROP TRIGGER directory_item_immutable ON dataset_items")
    op.execute("DROP FUNCTION protect_directory_item()")
    op.execute("DROP TRIGGER directory_asset_immutable ON dataset_assets")
    op.execute("DROP FUNCTION protect_directory_asset()")
    op.drop_table("dataset_source_previews")
    op.drop_table("dataset_source_items")
    op.drop_column("dataset_registration_entries", "preview_error")
    op.drop_column("dataset_registration_entries", "original_estimated_bytes")
    op.drop_column("dataset_registration_entries", "preview_estimated_bytes")
    op.drop_constraint("ck_source_cache", "dataset_sources")
    op.drop_constraint("ck_source_ownership", "dataset_sources")
    for name in (
        "unregistered_at",
        "policy_revision",
        "cache_policy",
        "ownership_mode",
    ):
        op.drop_column("dataset_sources", name)
