"""Pin each retained training version to its explicit physical namespace."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0220"
down_revision = "0219"
branch_labels = None
depends_on = None

# Keep migrations independent of application imports and future model changes.
_REF_CHECK = "\nstorage_ref IS NULL OR COALESCE((\n    jsonb_typeof(storage_ref) = 'object'\n    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','prefix','storage_identity',\n        'origin','evidence']\n    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','prefix','storage_identity',\n        'origin','evidence']::text[] = '{}'::jsonb\n    AND storage_ref->'schema' = '1'::jsonb\n    AND storage_ref->>'purpose' = 'training-dataset'\n    AND jsonb_typeof(storage_ref->'location_id') = 'string'\n    AND storage_ref->'config_revision' = '1'::jsonb\n    AND storage_ref->'binding_generation' = '1'::jsonb\n    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'\n    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'\n    AND jsonb_typeof(storage_ref->'bucket') = 'string'\n    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255\n    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'\n    AND position(chr(92) in (storage_ref->>'bucket')) = 0\n    AND jsonb_typeof(storage_ref->'prefix') = 'string'\n    AND storage_ref->>'prefix' ~ '^projects/[0-9a-f-]{36}/datasets/[0-9a-f-]{36}/$'\n    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'\n    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'\n    AND storage_ref->>'origin' IN ('new','verified-retained')\n    AND (storage_ref->'evidence' = 'null'::jsonb\n        OR jsonb_typeof(storage_ref->'evidence') = 'object')\n    AND (storage_ref->>'origin' != 'new' OR storage_ref->'evidence' = 'null'::jsonb)\n), false)\n"


def upgrade():
    for table, check in (
        ("training_dataset_versions", "ck_training_dataset_storage_ref"),
        ("training_storage_deletions", "ck_training_deletion_storage_ref"),
    ):
        op.add_column(table, sa.Column("storage_ref", JSONB(none_as_null=True)))
        op.create_check_constraint(check, table, _REF_CHECK)
    op.create_check_constraint(
        "ck_training_dataset_storage_prefix",
        "training_dataset_versions",
        "storage_ref IS NULL OR storage_ref->>'prefix' = "
        "'projects/' || project_id::text || '/datasets/' || id::text || '/'",
    )
    op.create_check_constraint(
        "ck_training_deletion_storage_locator",
        "training_storage_deletions",
        "storage_ref IS NULL OR (storage_ref->>'bucket' = bucket "
        "AND storage_ref->>'prefix' = prefix)",
    )
    op.execute("""
        CREATE FUNCTION guard_training_dataset_storage_ref() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            relation_owner oid;
        BEGIN
            IF OLD.storage_ref IS NOT NULL AND NEW.storage_ref IS DISTINCT FROM OLD.storage_ref THEN
                SELECT relowner INTO relation_owner FROM pg_class WHERE oid = TG_RELID;
                IF current_setting('aap.restore_training_storage', true) = 'on'
                   AND pg_has_role(current_user, relation_owner, 'MEMBER')
                   AND NEW.storage_ref IS NOT NULL
                   AND (NEW.storage_ref - ARRAY['bucket','storage_identity']::text[])
                       = (OLD.storage_ref - ARRAY['bucket','storage_identity']::text[])
                   AND NEW.storage_ref->>'bucket' IS DISTINCT FROM OLD.storage_ref->>'bucket'
                   AND NEW.storage_ref->>'storage_identity' IS DISTINCT FROM OLD.storage_ref->>'storage_identity'
                THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'Training dataset storage ownership is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_training_storage_ref_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    for table in ("training_dataset_versions", "training_storage_deletions"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_storage_ref "
            f"BEFORE UPDATE ON {table} FOR EACH ROW "
            "EXECUTE FUNCTION guard_training_dataset_storage_ref()"
        )


def downgrade():
    op.execute(
        "LOCK TABLE training_dataset_versions, training_storage_deletions IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM training_dataset_versions WHERE storage_ref IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM training_storage_deletions WHERE storage_ref IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "Pinned training storage facts prevent destructive downgrade"
        )
    for table in ("training_dataset_versions", "training_storage_deletions"):
        op.execute(f"DROP TRIGGER trg_{table}_storage_ref ON {table}")
    op.execute("DROP FUNCTION guard_training_dataset_storage_ref()")
    op.drop_constraint(
        "ck_training_dataset_storage_prefix", "training_dataset_versions", type_="check"
    )
    op.drop_constraint(
        "ck_training_deletion_storage_locator",
        "training_storage_deletions",
        type_="check",
    )
    for table, check in (
        ("training_dataset_versions", "ck_training_dataset_storage_ref"),
        ("training_storage_deletions", "ck_training_deletion_storage_ref"),
    ):
        op.drop_constraint(check, table, type_="check")
        op.drop_column(table, "storage_ref")
