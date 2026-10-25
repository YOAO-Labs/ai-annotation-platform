"""Keep current dataset original addresses independent of future allocations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0251"
down_revision = "0250"
branch_labels = None
depends_on = None

_REF_CHECK = "\nstorage_ref IS NULL OR COALESCE((\n    jsonb_typeof(storage_ref)='object'\n    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','key_prefix','object_key','storage_identity']\n    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','key_prefix','object_key','storage_identity']::text[]='{}'::jsonb\n    AND storage_ref->'schema'='1'::jsonb AND storage_ref->>'schema'='1'\n    AND storage_ref->>'purpose'='datasets'\n    AND jsonb_typeof(storage_ref->'location_id')='string'\n    AND storage_ref->>'location_id'=location_id::text\n    AND jsonb_typeof(storage_ref->'config_revision')='number'\n    AND storage_ref->>'config_revision' ~ '^[1-9][0-9]*$'\n    AND jsonb_typeof(storage_ref->'binding_generation')='number'\n    AND storage_ref->>'binding_generation' ~ '^[1-9][0-9]*$'\n    AND jsonb_typeof(storage_ref->'endpoint_url')='string'\n    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'\n    AND position(chr(92) in (storage_ref->>'endpoint_url'))=0\n    AND jsonb_typeof(storage_ref->'bucket')='string'\n    AND storage_ref->>'bucket'=bucket\n    AND length(bucket) BETWEEN 1 AND 255\n    AND bucket !~ '[[:space:][:cntrl:]/?#@%:]' AND position(chr(92) in bucket)=0\n    AND jsonb_typeof(storage_ref->'object_key')='string'\n    AND storage_ref->>'object_key'=object_key\n    AND length(object_key) BETWEEN 1 AND 1000 AND octet_length(object_key)<=1024\n    AND object_key !~ '[[:cntrl:]]'\n    AND jsonb_typeof(storage_ref->'key_prefix')='string'\n    AND length(storage_ref->>'key_prefix')<=891\n    AND octet_length(storage_ref->>'key_prefix')<=915\n    AND jsonb_typeof(storage_ref->'storage_identity')='string'\n    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'\n    AND ((location_id='4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid\n          AND storage_ref->>'config_revision'='1' AND storage_ref->>'key_prefix'='')\n      OR (location_id<>'4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid\n          AND storage_ref->>'key_prefix' ~ '^([^/]+/)*_aap_managed/datasets/$'\n          AND storage_ref->>'key_prefix' !~ '[[:cntrl:]]|(^|/)[.][.]?(/|$)|//'\n          AND position(chr(92) in (storage_ref->>'key_prefix'))=0\n          AND left(object_key,length(storage_ref->>'key_prefix'))=storage_ref->>'key_prefix'\n          AND length(object_key)>length(storage_ref->>'key_prefix')))\n),false)\n"
_SCOPE = "(owner_kind='dataset' AND dataset_id IS NOT NULL AND project_id IS NULL AND task_id IS NULL) OR (owner_kind='dataset_auxiliary' AND dataset_id IS NOT NULL AND project_id IS NULL AND task_id IS NULL AND origin<>'direct' AND object_key=COALESCE(configuration->'original_ref'->>'key_prefix','') || '__aap_auxiliary_originals__/' || dataset_id::text || '/' || id::text || '/payload') OR (owner_kind='task' AND dataset_id IS NULL AND project_id IS NOT NULL AND task_id IS NOT NULL AND origin='direct' AND object_key='__aap_task_originals__/' || task_id::text || '/' || id::text || '/payload')"
_OLD_SCOPE = "(owner_kind='dataset' AND dataset_id IS NOT NULL AND project_id IS NULL AND task_id IS NULL) OR (owner_kind='dataset_auxiliary' AND dataset_id IS NOT NULL AND project_id IS NULL AND task_id IS NULL AND origin<>'direct' AND object_key='__aap_auxiliary_originals__/' || dataset_id::text || '/' || id::text || '/payload') OR (owner_kind='task' AND dataset_id IS NULL AND project_id IS NOT NULL AND task_id IS NOT NULL AND origin='direct' AND object_key='__aap_task_originals__/' || task_id::text || '/' || id::text || '/payload')"

_AUX_KEY = "object_key = COALESCE(storage_ref->>'key_prefix','') || '__aap_auxiliary_originals__/' || dataset_id::text || '/' || id::text || '/payload'"
_OLD_AUX_KEY = "object_key = '__aap_auxiliary_originals__/' || dataset_id::text || '/' || id::text || '/payload'"
_OLD_AUX_LOCATION = "location_id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid"


def _replace(name, table, expression):
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, expression)


def upgrade():
    for table, constraint in (
        ("dataset_asset_locations", "ck_dataset_asset_location_storage_ref"),
        ("dataset_auxiliary_originals", "ck_auxiliary_original_storage_ref"),
    ):
        op.add_column(
            table, sa.Column("storage_ref", JSONB(none_as_null=True), nullable=True)
        )
        op.create_check_constraint(constraint, table, _REF_CHECK)
        op.execute(
            f"CREATE INDEX ix_{table}_saved_location ON {table} ((storage_ref->>'location_id'))"
        )
    op.create_check_constraint(
        "ck_auxiliary_original_storage_identity",
        "dataset_auxiliary_originals",
        "storage_ref IS NULL OR storage_ref->>'storage_identity'=storage_identity",
    )
    _replace(
        "ck_auxiliary_original_location",
        "dataset_auxiliary_originals",
        "storage_ref IS NOT NULL OR " + _OLD_AUX_LOCATION,
    )
    _replace("ck_auxiliary_original_key", "dataset_auxiliary_originals", _AUX_KEY)
    _replace("ck_managed_creation_scope", "managed_original_creations", _SCOPE)
    op.execute(
        "CREATE INDEX ix_managed_creation_final_location ON managed_original_creations (location_id)"
    )
    op.execute("""
        CREATE FUNCTION guard_dataset_original_storage_ref() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='UPDATE' AND NEW.storage_ref IS DISTINCT FROM OLD.storage_ref THEN
                RAISE EXCEPTION 'Saved dataset original address is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    for table in ("dataset_asset_locations", "dataset_auxiliary_originals"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_storage_ref BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION guard_dataset_original_storage_ref()"
        )


def downgrade():
    op.execute(
        "LOCK TABLE dataset_asset_locations, dataset_auxiliary_originals, managed_original_creations, storage_bindings IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM dataset_asset_locations WHERE storage_ref IS NOT NULL)
               OR EXISTS (SELECT 1 FROM dataset_auxiliary_originals WHERE storage_ref IS NOT NULL)
               OR EXISTS (SELECT 1 FROM managed_original_creations WHERE configuration->>'schema'='2' AND configuration ? 'original_ref')
               OR EXISTS (SELECT 1 FROM storage_bindings WHERE purpose='datasets' AND generation<>1) THEN
                RAISE EXCEPTION 'Saved dataset original responsibility prevents destructive downgrade' USING ERRCODE='23514';
            END IF;
        END $$
    """)
    for table in ("dataset_asset_locations", "dataset_auxiliary_originals"):
        op.execute(f"DROP TRIGGER trg_{table}_storage_ref ON {table}")
    op.execute("DROP FUNCTION guard_dataset_original_storage_ref()")
    op.drop_index(
        "ix_managed_creation_final_location", table_name="managed_original_creations"
    )
    _replace("ck_managed_creation_scope", "managed_original_creations", _OLD_SCOPE)
    _replace("ck_auxiliary_original_key", "dataset_auxiliary_originals", _OLD_AUX_KEY)
    _replace(
        "ck_auxiliary_original_location",
        "dataset_auxiliary_originals",
        _OLD_AUX_LOCATION,
    )
    op.drop_constraint(
        "ck_auxiliary_original_storage_identity",
        "dataset_auxiliary_originals",
        type_="check",
    )
    for table, constraint in (
        ("dataset_asset_locations", "ck_dataset_asset_location_storage_ref"),
        ("dataset_auxiliary_originals", "ck_auxiliary_original_storage_ref"),
    ):
        op.drop_index(f"ix_{table}_saved_location", table_name=table)
        op.drop_constraint(constraint, table, type_="check")
        op.drop_column(table, "storage_ref")
