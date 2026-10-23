"""Preserve controlled creation receipts independently of business publication."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0219"
down_revision = "0218"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "managed_original_creations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset_id", UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", UUID(as_uuid=True), nullable=False),
        sa.Column("storage_identity", sa.String(64), nullable=False),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("object_key", sa.String(1000), nullable=False),
        sa.Column("logical_path", sa.String(1000), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("max_bytes", sa.BigInteger(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("requested_by", UUID(as_uuid=True)),
        sa.Column("authority", JSONB, nullable=False),
        sa.Column("configuration", JSONB, nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("maintenance_revision", sa.Integer(), nullable=False),
        sa.Column("staging_bucket", sa.String(255)),
        sa.Column("staging_key", sa.String(1000), unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="allocated"),
        sa.Column("operation_id", UUID(as_uuid=True), unique=True),
        sa.Column("source", JSONB),
        sa.Column("prepared", JSONB),
        sa.Column("sha256", sa.String(64)),
        sa.Column("md5", sa.String(32)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("receipt", JSONB),
        sa.Column("etag", sa.Text()),
        sa.Column("version_id", sa.Text()),
        sa.Column("request_id", sa.Text()),
        sa.Column("verification", JSONB),
        sa.Column("error_code", sa.String(80)),
        sa.Column(
            "allocated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("frozen_at", sa.DateTime(timezone=True)),
        sa.Column("write_started_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("readback_at", sa.DateTime(timezone=True)),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "origin IN ('direct','zip','connector','directory_copy')",
            name="ck_managed_creation_origin",
        ),
        sa.CheckConstraint(
            "status IN ('allocated','preparing','prepared','writing','written','verified','failed','conflict','unknown')",
            name="ck_managed_creation_status",
        ),
        sa.CheckConstraint(
            "max_bytes >= 0 AND timeout_seconds >= 60 AND maintenance_revision >= 1",
            name="ck_managed_creation_budget",
        ),
        sa.CheckConstraint(
            "storage_identity ~ '^[0-9a-f]{64}$' AND input_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_managed_creation_identity",
        ),
        sa.CheckConstraint(
            "(origin='direct' AND staging_bucket IS NOT NULL AND staging_key IS NOT NULL) OR (origin<>'direct' AND staging_bucket IS NULL AND staging_key IS NULL)",
            name="ck_managed_creation_staging",
        ),
        sa.CheckConstraint(
            "(prepared IS NULL AND sha256 IS NULL AND md5 IS NULL AND size_bytes IS NULL AND frozen_at IS NULL) OR (prepared IS NOT NULL AND sha256 IS NOT NULL AND md5 IS NOT NULL AND size_bytes IS NOT NULL AND frozen_at IS NOT NULL AND sha256 ~ '^[0-9a-f]{64}$' AND md5 ~ '^[0-9a-f]{32}$' AND size_bytes >= 0 AND size_bytes <= max_bytes)",
            name="ck_managed_creation_frozen",
        ),
        sa.CheckConstraint(
            "(receipt IS NULL AND etag IS NULL AND version_id IS NULL AND request_id IS NULL AND acknowledged_at IS NULL) OR (receipt IS NOT NULL AND etag IS NOT NULL AND length(etag)>0 AND acknowledged_at IS NOT NULL AND write_started_at IS NOT NULL AND prepared IS NOT NULL)",
            name="ck_managed_creation_receipt",
        ),
        sa.CheckConstraint(
            "version_id IS NULL OR (length(version_id)>0 AND version_id<>'null')",
            name="ck_managed_creation_version",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,79}$'",
            name="ck_managed_creation_error",
        ),
        sa.CheckConstraint(
            "(verification IS NULL AND readback_at IS NULL) OR (verification IS NOT NULL AND readback_at IS NOT NULL AND receipt IS NOT NULL)",
            name="ck_managed_creation_readback",
        ),
        sa.CheckConstraint(
            "(status IN ('failed','conflict','unknown') AND completed_at IS NOT NULL AND error_code IS NOT NULL) OR (status NOT IN ('failed','conflict','unknown') AND error_code IS NULL AND ((status='allocated' AND claimed_at IS NULL AND operation_id IS NULL AND prepared IS NULL AND completed_at IS NULL) OR (status='preparing' AND claimed_at IS NOT NULL AND prepared IS NULL AND completed_at IS NULL) OR (status='prepared' AND claimed_at IS NOT NULL AND operation_id IS NOT NULL AND prepared IS NOT NULL AND write_started_at IS NULL AND completed_at IS NULL) OR (status='writing' AND claimed_at IS NOT NULL AND operation_id IS NOT NULL AND prepared IS NOT NULL AND write_started_at IS NOT NULL AND receipt IS NULL AND completed_at IS NULL) OR (status='written' AND claimed_at IS NOT NULL AND operation_id IS NOT NULL AND receipt IS NOT NULL AND completed_at IS NULL) OR (status='verified' AND claimed_at IS NOT NULL AND operation_id IS NOT NULL AND receipt IS NOT NULL AND verification IS NOT NULL AND settled_at IS NOT NULL AND completed_at IS NOT NULL)))",
            name="ck_managed_creation_lifecycle",
        ),
        sa.UniqueConstraint(
            "storage_identity",
            "bucket",
            "object_key",
            name="uq_managed_creation_reserved_key",
        ),
    )
    op.create_index(
        "ix_managed_creation_dataset",
        "managed_original_creations",
        ["dataset_id", "allocated_at", "id"],
    )
    for table in ("dataset_asset_locations", "managed_original_retirements"):
        op.add_column(table, sa.Column("creation_id", UUID(as_uuid=True)))
        op.create_foreign_key(
            "fk_" + table + "_creation_id",
            table,
            "managed_original_creations",
            ["creation_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.execute("""
        CREATE FUNCTION guard_managed_original_creation_history() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='DELETE' THEN
                RAISE EXCEPTION 'Managed original creation history is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_managed_creation_immutable';
            END IF;
            IF ROW(NEW.id,NEW.dataset_id,NEW.location_id,NEW.storage_identity,NEW.bucket,NEW.object_key,
                   NEW.logical_path,NEW.origin,NEW.max_bytes,NEW.timeout_seconds,NEW.content_type,NEW.requested_by,
                   NEW.authority,NEW.configuration,NEW.input_fingerprint,NEW.maintenance_revision,
                   NEW.staging_bucket,NEW.staging_key,NEW.allocated_at)
               IS DISTINCT FROM
               ROW(OLD.id,OLD.dataset_id,OLD.location_id,OLD.storage_identity,OLD.bucket,OLD.object_key,
                   OLD.logical_path,OLD.origin,OLD.max_bytes,OLD.timeout_seconds,OLD.content_type,OLD.requested_by,
                   OLD.authority,OLD.configuration,OLD.input_fingerprint,OLD.maintenance_revision,
                   OLD.staging_bucket,OLD.staging_key,OLD.allocated_at)
               OR (OLD.claimed_at IS NOT NULL AND NEW.claimed_at IS DISTINCT FROM OLD.claimed_at)
               OR (OLD.operation_id IS NOT NULL AND NEW.operation_id IS DISTINCT FROM OLD.operation_id)
               OR (OLD.source IS NOT NULL AND NEW.source IS DISTINCT FROM OLD.source)
               OR (OLD.prepared IS NOT NULL AND ROW(NEW.prepared,NEW.sha256,NEW.md5,NEW.size_bytes,NEW.frozen_at)
                   IS DISTINCT FROM ROW(OLD.prepared,OLD.sha256,OLD.md5,OLD.size_bytes,OLD.frozen_at))
               OR (OLD.write_started_at IS NOT NULL AND NEW.write_started_at IS DISTINCT FROM OLD.write_started_at)
               OR (OLD.receipt IS NOT NULL AND ROW(NEW.receipt,NEW.etag,NEW.version_id,NEW.request_id,NEW.acknowledged_at)
                   IS DISTINCT FROM ROW(OLD.receipt,OLD.etag,OLD.version_id,OLD.request_id,OLD.acknowledged_at))
               OR (OLD.verification IS NOT NULL AND ROW(NEW.verification,NEW.readback_at)
                   IS DISTINCT FROM ROW(OLD.verification,OLD.readback_at))
               OR (OLD.settled_at IS NOT NULL AND NEW.settled_at IS DISTINCT FROM OLD.settled_at)
               OR (OLD.status IN ('verified','failed','conflict','unknown') AND NEW IS DISTINCT FROM OLD)
               OR (NEW.status IS DISTINCT FROM OLD.status AND NOT (
                     (OLD.status='allocated' AND NEW.status IN ('preparing','failed'))
                  OR (OLD.status='preparing' AND NEW.status IN ('prepared','failed','unknown'))
                  OR (OLD.status='prepared' AND NEW.status IN ('writing','failed','unknown'))
                  OR (OLD.status='writing' AND NEW.status IN ('written','failed','conflict','unknown'))
                  OR (OLD.status='written' AND NEW.status IN ('verified','failed','unknown')))) THEN
                RAISE EXCEPTION 'Managed original creation history is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_managed_creation_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_managed_original_creation_history
        BEFORE UPDATE OR DELETE ON managed_original_creations
        FOR EACH ROW EXECUTE FUNCTION guard_managed_original_creation_history()
    """)
    op.execute("""
        CREATE FUNCTION guard_managed_original_creation_attachment() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='UPDATE' AND OLD.creation_id IS DISTINCT FROM NEW.creation_id
                AND (TG_TABLE_NAME='managed_original_retirements' OR OLD.creation_id IS NOT NULL) THEN
                RAISE EXCEPTION 'Managed original creation link is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_managed_creation_link';
            END IF;
            IF TG_TABLE_NAME='managed_original_retirements' THEN
                RETURN NEW;
            END IF;
            IF TG_OP='UPDATE' AND OLD.creation_id IS NOT NULL THEN
                IF ROW(NEW.asset_id,NEW.location_id,NEW.object_key,NEW.role)
                    IS DISTINCT FROM ROW(OLD.asset_id,OLD.location_id,OLD.object_key,OLD.role) THEN
                    RAISE EXCEPTION 'Managed original creation link is immutable'
                        USING ERRCODE='23514', CONSTRAINT='ck_managed_creation_link';
                END IF;
                -- The link is historical creation provenance. A controlled
                -- offline bucket remap preserves that source fact; current
                -- applicability is revalidated by attach/configuration pins.
                RETURN NEW;
            END IF;
            IF NEW.creation_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM managed_original_creations c
                JOIN dataset_assets a ON a.id=NEW.asset_id
                JOIN dataset_items i ON i.id=a.dataset_item_id
                WHERE c.id=NEW.creation_id AND c.status='verified'
                    AND c.dataset_id=i.dataset_id AND c.location_id=NEW.location_id
                    AND c.bucket=NEW.bucket AND c.object_key=NEW.object_key
                    AND NEW.role='managed_original' AND a.verification_state='verified'
                    AND a.checksum_algorithm='sha256' AND a.checksum_value=c.sha256
                    AND a.size_bytes=c.size_bytes
            ) THEN
                RAISE EXCEPTION 'Managed original creation attachment does not match'
                    USING ERRCODE='23514', CONSTRAINT='ck_managed_creation_link';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_managed_original_creation_attachment
        BEFORE INSERT OR UPDATE ON dataset_asset_locations
        FOR EACH ROW EXECUTE FUNCTION guard_managed_original_creation_attachment()
    """)
    op.execute("""
        CREATE TRIGGER trg_managed_original_retirement_creation
        BEFORE UPDATE ON managed_original_retirements
        FOR EACH ROW EXECUTE FUNCTION guard_managed_original_creation_attachment()
    """)


def downgrade():
    op.execute(
        "LOCK TABLE managed_original_creations, dataset_asset_locations, managed_original_retirements IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM managed_original_creations)")
    ):
        raise RuntimeError(
            "Managed original creation facts prevent destructive downgrade"
        )
    op.execute(
        "DROP TRIGGER trg_managed_original_retirement_creation ON managed_original_retirements"
    )
    op.execute(
        "DROP TRIGGER trg_managed_original_creation_attachment ON dataset_asset_locations"
    )
    op.execute("DROP FUNCTION guard_managed_original_creation_attachment()")
    for table in ("dataset_asset_locations", "managed_original_retirements"):
        op.drop_constraint("fk_" + table + "_creation_id", table, type_="foreignkey")
        op.drop_column(table, "creation_id")
    op.drop_table("managed_original_creations")
    op.execute("DROP FUNCTION guard_managed_original_creation_history()")
