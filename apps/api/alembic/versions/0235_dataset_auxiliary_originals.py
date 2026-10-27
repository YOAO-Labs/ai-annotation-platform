"""Own original NuScenes raw sensor and map bytes independently of business rows."""

from alembic import op
import re
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0235"
down_revision = "0234"
branch_labels = None
depends_on = None


OLD_SCOPE = "(owner_kind='dataset' AND dataset_id IS NOT NULL AND project_id IS NULL AND task_id IS NULL) OR (owner_kind='task' AND dataset_id IS NULL AND project_id IS NOT NULL AND task_id IS NOT NULL AND origin='direct' AND object_key='__aap_task_originals__/' || task_id::text || '/' || id::text || '/payload')"
SCOPE = (
    OLD_SCOPE
    + " OR (owner_kind='dataset_auxiliary' AND dataset_id IS NOT NULL AND project_id IS NULL AND task_id IS NULL AND origin<>'direct' AND object_key='__aap_auxiliary_originals__/' || dataset_id::text || '/' || id::text || '/payload')"
)


def _ddl(value):
    for statement in re.split(r";\s*(?=CREATE )", value.strip()):
        op.execute(statement)


def upgrade():
    op.drop_constraint(
        "ck_managed_creation_scope", "managed_original_creations", type_="check"
    )
    op.create_check_constraint(
        "ck_managed_creation_scope", "managed_original_creations", SCOPE
    )
    op.create_table(
        "dataset_auxiliary_originals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "creation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("managed_original_creations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("dataset_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("source_contract", JSONB, nullable=False),
        sa.Column("location_id", UUID(as_uuid=True), nullable=False),
        sa.Column("storage_identity", sa.String(64), nullable=False),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("object_key", sa.String(1000), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=False),
        sa.Column("version_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("creation_id", name="uq_auxiliary_original_creation"),
        sa.CheckConstraint(
            "id = creation_id", name="ck_auxiliary_original_creation_id"
        ),
        sa.CheckConstraint(
            "location_id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid",
            name="ck_auxiliary_original_location",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$' AND storage_identity ~ '^[0-9a-f]{64}$'",
            name="ck_auxiliary_original_sha",
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 0 AND 268435456", name="ck_auxiliary_original_size"
        ),
        sa.CheckConstraint(
            "kind IN ('raw_lidar','map')", name="ck_auxiliary_original_kind"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_contract)='object' AND octet_length(source_contract::text)<=65536",
            name="ck_auxiliary_original_contract",
        ),
        sa.CheckConstraint(
            "object_key = '__aap_auxiliary_originals__/' || dataset_id::text || '/' || id::text || '/payload'",
            name="ck_auxiliary_original_key",
        ),
        sa.CheckConstraint(
            "length(bucket) BETWEEN 1 AND 255 AND bucket !~ '[[:space:][:cntrl:]/?#@]' AND position(chr(92) in bucket) = 0",
            name="ck_auxiliary_original_bucket",
        ),
        sa.CheckConstraint(
            "length(etag) BETWEEN 1 AND 1024 AND etag !~ '[[:cntrl:]]'",
            name="ck_auxiliary_original_etag",
        ),
        sa.CheckConstraint(
            "version_id IS NULL OR (length(version_id) BETWEEN 1 AND 1024 AND version_id <> 'null' AND version_id !~ '[[:cntrl:]]')",
            name="ck_auxiliary_original_version",
        ),
    )
    op.create_index(
        "ix_dataset_auxiliary_originals_dataset_id",
        "dataset_auxiliary_originals",
        ["dataset_id"],
    )
    for table, column in (
        ("dataset_items", "nuscenes_raw_original_id"),
        ("scenes", "nuscenes_map_original_id"),
    ):
        op.add_column(table, sa.Column(column, UUID(as_uuid=True)))
        op.create_foreign_key(
            "fk_" + table + "_" + column,
            table,
            "dataset_auxiliary_originals",
            [column],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index("ix_" + table + "_" + column, table, [column])
    _ddl("""
    CREATE FUNCTION guard_auxiliary_original() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE relation_owner oid;
    BEGIN
        IF TG_OP='DELETE' THEN
            RAISE EXCEPTION 'Auxiliary original responsibility cannot be deleted' USING ERRCODE='23514';
        END IF;
        IF TG_OP='INSERT' THEN
            IF NOT EXISTS (SELECT 1 FROM managed_original_creations c
                WHERE c.id=NEW.id AND c.id=NEW.creation_id AND c.owner_kind='dataset_auxiliary'
                AND c.dataset_id=NEW.dataset_id AND c.status='verified'
                AND c.location_id=NEW.location_id AND c.storage_identity=NEW.storage_identity
                AND c.bucket=NEW.bucket AND c.object_key=NEW.object_key
                AND c.sha256=NEW.sha256 AND c.size_bytes=NEW.size_bytes AND c.etag=NEW.etag
                AND c.version_id IS NOT DISTINCT FROM NEW.version_id AND c.completed_at=NEW.created_at
                AND c.authority->'auxiliary'->>'kind'=NEW.kind
                AND c.authority->'auxiliary'->'source_contract'=NEW.source_contract) THEN
                RAISE EXCEPTION 'Auxiliary original requires matching verified creation' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END IF;
        IF ROW(NEW.id,NEW.creation_id,NEW.dataset_id,NEW.kind,NEW.source_contract,NEW.location_id,
               NEW.object_key,NEW.sha256,NEW.size_bytes,NEW.etag,NEW.version_id,NEW.created_at)
           IS DISTINCT FROM ROW(OLD.id,OLD.creation_id,OLD.dataset_id,OLD.kind,OLD.source_contract,OLD.location_id,
               OLD.object_key,OLD.sha256,OLD.size_bytes,OLD.etag,OLD.version_id,OLD.created_at) THEN
            RAISE EXCEPTION 'Auxiliary original facts are immutable' USING ERRCODE='23514';
        END IF;
        IF ROW(NEW.bucket,NEW.storage_identity) IS DISTINCT FROM ROW(OLD.bucket,OLD.storage_identity) THEN
            SELECT relowner INTO relation_owner FROM pg_class WHERE oid=TG_RELID;
            IF current_setting('aap.restore_training_storage',true)='on'
                AND pg_has_role(current_user,relation_owner,'MEMBER')
                AND NEW.bucket IS DISTINCT FROM OLD.bucket AND NEW.storage_identity IS DISTINCT FROM OLD.storage_identity THEN RETURN NEW;
            END IF;
            RAISE EXCEPTION 'Auxiliary original location is immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER trg_auxiliary_original BEFORE INSERT OR UPDATE OR DELETE ON dataset_auxiliary_originals
        FOR EACH ROW EXECUTE FUNCTION guard_auxiliary_original();
    CREATE FUNCTION guard_auxiliary_pointer() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE pointer uuid; old_pointer uuid; expected_kind text; expected_contract jsonb; source jsonb;
    BEGIN
        IF TG_TABLE_NAME='dataset_items' THEN
            pointer := NEW.nuscenes_raw_original_id;
            expected_kind := 'raw_lidar';
            source := NEW.metadata->'nuscenes_export';
            expected_contract := jsonb_build_object('sample',source->'sample','sample_data',source->'sample_data',
                'calibrated_sensor',source->'calibrated_sensor','sensor',source->'sensor','ego_pose',source->'ego_pose');
            IF TG_OP='UPDATE' THEN old_pointer := OLD.nuscenes_raw_original_id; END IF;
            IF pointer IS NOT NULL AND NEW.file_type <> 'point_cloud' THEN
                RAISE EXCEPTION 'Raw original requires primary point cloud' USING ERRCODE='23514';
            END IF;
        ELSE
            pointer := NEW.nuscenes_map_original_id;
            expected_kind := 'map';
            expected_contract := NEW.source_metadata->'nuscenes_export'->'map';
            IF TG_OP='UPDATE' THEN old_pointer := OLD.nuscenes_map_original_id; END IF;
        END IF;
        IF old_pointer IS NOT NULL AND pointer IS DISTINCT FROM old_pointer THEN
            RAISE EXCEPTION 'Auxiliary pointer cannot be rebound' USING ERRCODE='23514';
        END IF;
        IF pointer IS NOT NULL AND TG_TABLE_NAME='dataset_items' THEN
            IF NOT EXISTS (SELECT 1 FROM scenes s WHERE s.id=NEW.scene_id AND s.dataset_id=NEW.dataset_id
                AND s.source_format='nuscenes' AND source->'sample'->>'scene_token'=s.source_metadata->'nuscenes_export'->'scene'->>'token') THEN
                RAISE EXCEPTION 'Raw original scene mismatch' USING ERRCODE='23514';
            END IF;
        END IF;
        IF pointer IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dataset_auxiliary_originals o
            WHERE o.id=pointer AND o.dataset_id=NEW.dataset_id AND o.kind=expected_kind
              AND o.source_contract=expected_contract) THEN
            RAISE EXCEPTION 'Auxiliary pointer contract mismatch' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER trg_auxiliary_raw_pointer BEFORE INSERT OR UPDATE ON dataset_items FOR EACH ROW EXECUTE FUNCTION guard_auxiliary_pointer();
    CREATE TRIGGER trg_auxiliary_map_pointer BEFORE INSERT OR UPDATE ON scenes FOR EACH ROW EXECUTE FUNCTION guard_auxiliary_pointer();
    """)
    # The existing link guard must not attach an auxiliary Creation to an Asset.
    _ddl("""
    CREATE FUNCTION reject_auxiliary_creation_asset() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.creation_id IS NOT NULL AND EXISTS (SELECT 1 FROM managed_original_creations c
            WHERE c.id=NEW.creation_id AND c.owner_kind <> 'dataset') THEN
            RAISE EXCEPTION 'Dataset asset requires dataset creation' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER trg_auxiliary_creation_asset BEFORE INSERT OR UPDATE ON dataset_asset_locations FOR EACH ROW EXECUTE FUNCTION reject_auxiliary_creation_asset();
    """)


def downgrade():
    op.execute(
        "LOCK TABLE dataset_auxiliary_originals, managed_original_creations, dataset_items, scenes IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM dataset_auxiliary_originals) OR EXISTS (SELECT 1 FROM managed_original_creations WHERE owner_kind='dataset_auxiliary') OR EXISTS (SELECT 1 FROM dataset_items WHERE nuscenes_raw_original_id IS NOT NULL) OR EXISTS (SELECT 1 FROM scenes WHERE nuscenes_map_original_id IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "Auxiliary original responsibility prevents destructive downgrade"
        )
    op.execute("DROP TRIGGER trg_auxiliary_creation_asset ON dataset_asset_locations")
    op.execute("DROP FUNCTION reject_auxiliary_creation_asset()")
    for table, column, trigger in (
        ("dataset_items", "nuscenes_raw_original_id", "trg_auxiliary_raw_pointer"),
        ("scenes", "nuscenes_map_original_id", "trg_auxiliary_map_pointer"),
    ):
        op.execute("DROP TRIGGER " + trigger + " ON " + table)
        op.drop_index("ix_" + table + "_" + column, table_name=table)
        op.drop_constraint("fk_" + table + "_" + column, table, type_="foreignkey")
        op.drop_column(table, column)
    op.execute("DROP FUNCTION guard_auxiliary_pointer()")
    op.execute("DROP TRIGGER trg_auxiliary_original ON dataset_auxiliary_originals")
    op.execute("DROP FUNCTION guard_auxiliary_original()")
    op.drop_table("dataset_auxiliary_originals")
    op.drop_constraint(
        "ck_managed_creation_scope", "managed_original_creations", type_="check"
    )
    op.create_check_constraint(
        "ck_managed_creation_scope", "managed_original_creations", OLD_SCOPE
    )
