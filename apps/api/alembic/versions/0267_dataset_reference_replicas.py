"""Publish directed same-content reference replicas without changing identity."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0267"
down_revision = "0266"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dataset_reference_replicas",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "primary_asset_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dataset_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "replica_asset_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dataset_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "created_by",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "revoked_by",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "primary_asset_id <> replica_asset_id AND priority BETWEEN 0 AND 1000",
            name="ck_reference_replica_target",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)",
            name="ck_reference_replica_revocation",
        ),
    )
    op.create_index(
        "uq_reference_replica_active",
        "dataset_reference_replicas",
        ["primary_asset_id", "replica_asset_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_reference_replica_primary",
        "dataset_reference_replicas",
        ["primary_asset_id"],
    )
    op.create_index(
        "ix_reference_replica_target",
        "dataset_reference_replicas",
        ["replica_asset_id"],
    )
    op.execute("""CREATE FUNCTION protect_reference_replica() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'UPDATE' THEN
            IF NEW.primary_asset_id IS DISTINCT FROM OLD.primary_asset_id
               OR NEW.replica_asset_id IS DISTINCT FROM OLD.replica_asset_id
               OR NEW.created_by IS DISTINCT FROM OLD.created_by
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR (OLD.revoked_at IS NOT NULL AND NEW IS DISTINCT FROM OLD) THEN
                RAISE EXCEPTION 'Replica identity and revoked grants are immutable'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM dataset_assets a
            JOIN dataset_items ai ON ai.id = a.dataset_item_id
            JOIN dataset_references ar ON ar.dataset_item_id = ai.id
            JOIN dataset_assets b ON b.id = NEW.replica_asset_id
            JOIN dataset_items bi ON bi.id = b.dataset_item_id
            JOIN dataset_references br ON br.dataset_item_id = bi.id
            WHERE a.id = NEW.primary_asset_id AND ai.dataset_id = bi.dataset_id
              AND a.media_type = 'image' AND b.media_type = 'image'
              AND a.verification_state = 'verified' AND b.verification_state = 'verified'
              AND a.checksum_algorithm = 'sha256' AND b.checksum_algorithm = 'sha256'
              AND a.checksum_value = b.checksum_value AND a.size_bytes = b.size_bytes
        ) THEN
            RAISE EXCEPTION 'Replica requires same-dataset verified reference content'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER reference_replica_identity BEFORE INSERT OR UPDATE "
        "ON dataset_reference_replicas FOR EACH ROW EXECUTE FUNCTION protect_reference_replica()"
    )


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM dataset_reference_replicas)"))
        .scalar()
    ):
        raise RuntimeError("Preserve retained reference replica grants")
    op.drop_table("dataset_reference_replicas")
    op.execute("DROP FUNCTION protect_reference_replica()")
