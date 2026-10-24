"""Freeze prediction input locators and exact per-object responsibility."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0238"
down_revision = "0237"
branch_labels = None
depends_on = None

_REF = r"""
COALESCE((jsonb_typeof(storage_ref)='object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision','binding_generation','endpoint_url','bucket','key_prefix','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision','binding_generation','endpoint_url','bucket','key_prefix','storage_identity']::text[]='{}'::jsonb
    AND storage_ref->'schema'='1'::jsonb AND storage_ref->>'schema'='1'
    AND storage_ref->>'purpose'='import'
    AND storage_ref->>'location_id'='4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
    AND storage_ref->'config_revision'='1'::jsonb AND storage_ref->>'config_revision'='1'
    AND storage_ref->'binding_generation'='1'::jsonb AND storage_ref->>'binding_generation'='1'
    AND jsonb_typeof(storage_ref->'endpoint_url')='string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url'))=0
    AND jsonb_typeof(storage_ref->'bucket')='string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:]/?#@%:]'
    AND position(chr(92) in (storage_ref->>'bucket'))=0
    AND storage_ref->>'bucket'=bucket
    AND storage_ref->>'key_prefix'='prediction-inputs/' || id::text || '/'
    AND jsonb_typeof(storage_ref->'storage_identity')='string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
    AND storage_ref->>'storage_identity'=storage_identity),false)
"""


def upgrade():
    op.add_column(
        "prediction_input_leases",
        sa.Column("contract_version", sa.Integer(), nullable=False, server_default="1"),
    )
    for name in ("storage_ref", "source_ref", "inventory"):
        op.add_column("prediction_input_leases", sa.Column(name, JSONB()))
    op.add_column(
        "prediction_input_leases", sa.Column("maintenance_revision", sa.Integer())
    )
    op.add_column(
        "prediction_input_leases",
        sa.Column("object_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "prediction_input_leases",
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "prediction_input_leases", sa.Column("deleted_at", sa.DateTime(timezone=True))
    )
    op.alter_column("prediction_input_leases", "contract_version", server_default="2")
    op.drop_constraint(
        "ck_prediction_input_lease_state", "prediction_input_leases", type_="check"
    )
    op.create_check_constraint(
        "ck_prediction_input_lease_state",
        "prediction_input_leases",
        "state IN ('active','cleanup_pending','deleted')",
    )
    op.create_check_constraint(
        "ck_prediction_input_contract",
        "prediction_input_leases",
        "contract_version IN (1,2)",
    )
    op.create_check_constraint(
        "ck_prediction_input_budget",
        "prediction_input_leases",
        "object_count BETWEEN 0 AND 10000 AND size_bytes BETWEEN 0 AND 4294967296",
    )
    op.create_check_constraint(
        "ck_prediction_input_owner",
        "prediction_input_leases",
        "contract_version=1 OR owner_kind IN ('batch_predict','prediction_retry','interactive_predict','predict_test','mask_repair')",
    )
    op.create_check_constraint(
        "ck_prediction_input_pin",
        "prediction_input_leases",
        "(contract_version=1 AND storage_ref IS NULL AND maintenance_revision IS NULL AND inventory IS NULL AND source_ref IS NULL AND object_count=0 AND size_bytes=0 AND deleted_at IS NULL AND state<>'deleted') OR (contract_version=2 AND maintenance_revision>=1 AND maintenance_revision IS NOT NULL AND "
        + _REF
        + ")",
    )
    op.create_check_constraint(
        "ck_prediction_input_documents",
        "prediction_input_leases",
        "contract_version=1 OR COALESCE((jsonb_typeof(inventory)='object' AND inventory ?& ARRAY['schema','objects'] AND inventory-ARRAY['schema','objects']::text[]='{}'::jsonb AND inventory->'schema'='1'::jsonb AND inventory->>'schema'='1' AND jsonb_typeof(inventory->'objects')='object' AND octet_length(inventory::text)<=4194304 AND (source_ref IS NULL OR (jsonb_typeof(source_ref)='object' AND octet_length(source_ref::text)<=1048576))),false)",
    )
    op.create_check_constraint(
        "ck_prediction_input_times",
        "prediction_input_leases",
        "contract_version=1 OR (expires_at=created_at+interval '2 hours' AND retain_until=expires_at+interval '7 days' AND ((state='deleted' AND deleted_at IS NOT NULL AND deleted_at>=created_at) OR (state<>'deleted' AND deleted_at IS NULL)))",
    )
    op.execute(r"""
    CREATE FUNCTION guard_prediction_input_owner() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        member record; previous record; entry jsonb; total bigint:=0; count_objects integer:=0;
        relation_owner oid; restoring boolean:=false;
    BEGIN
        IF TG_OP='DELETE' THEN
            IF OLD.contract_version<>2 OR OLD.state<>'deleted' OR EXISTS (
                SELECT 1 FROM storage_operations WHERE purpose='prediction_input_' || replace(OLD.id::text,'-','')
            ) THEN
                RAISE EXCEPTION 'Prediction input responsibility is not deleted' USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_delete';
            END IF;
            RETURN OLD;
        END IF;
        IF TG_OP='UPDATE' THEN
            SELECT relowner INTO relation_owner FROM pg_class WHERE oid=TG_RELID;
            restoring:=current_setting('aap.restore_training_storage',true)='on'
                AND pg_has_role(current_user,relation_owner,'MEMBER')
                AND NEW.bucket IS DISTINCT FROM OLD.bucket
                AND NEW.storage_identity IS DISTINCT FROM OLD.storage_identity
                AND (to_jsonb(NEW)-ARRAY['bucket','storage_identity','storage_ref']::text[])
                    = (to_jsonb(OLD)-ARRAY['bucket','storage_identity','storage_ref']::text[])
                AND ((OLD.contract_version=1 AND NEW.storage_ref IS NULL)
                    OR (NEW.storage_ref IS NOT NULL AND OLD.storage_ref IS NOT NULL
                        AND NEW.storage_ref-ARRAY['bucket','storage_identity']::text[]
                            =OLD.storage_ref-ARRAY['bucket','storage_identity']::text[]));
            IF restoring THEN RETURN NEW; END IF;
            IF ROW(NEW.id,NEW.owner_id,NEW.owner_kind,NEW.task_id,NEW.contract_version,NEW.bucket,
                NEW.storage_identity,NEW.storage_ref,NEW.maintenance_revision,NEW.source_ref,
                NEW.created_at,NEW.expires_at,NEW.retain_until)
               IS DISTINCT FROM ROW(OLD.id,OLD.owner_id,OLD.owner_kind,OLD.task_id,OLD.contract_version,OLD.bucket,
                OLD.storage_identity,OLD.storage_ref,OLD.maintenance_revision,OLD.source_ref,
                OLD.created_at,OLD.expires_at,OLD.retain_until) THEN
                RAISE EXCEPTION 'Prediction input facts are immutable' USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_immutable';
            END IF;
            IF OLD.state='deleted' AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Deleted prediction input is terminal' USING ERRCODE='23514';
            END IF;
            IF (OLD.state='cleanup_pending' AND NEW.state NOT IN ('cleanup_pending','deleted'))
                OR (OLD.state='active' AND NEW.state NOT IN ('active','cleanup_pending'))
                OR (OLD.uncertain_write AND NOT NEW.uncertain_write)
                OR (OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at) THEN
                RAISE EXCEPTION 'Prediction input state cannot reverse' USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_transition';
            END IF;
        END IF;
        IF NEW.contract_version=1 THEN RETURN NEW; END IF;
        FOR member IN SELECT key,value FROM jsonb_each(NEW.inventory->'objects') LOOP
            entry:=member.value;
            IF NOT COALESCE((jsonb_typeof(entry)='object'
                AND entry ?& ARRAY['sha256','size_bytes','media_type','state','etag','version_id','write_nonce']
                AND entry-ARRAY['sha256','size_bytes','media_type','state','etag','version_id','write_nonce']::text[]='{}'::jsonb
                AND jsonb_typeof(entry->'sha256')='string' AND entry->>'sha256' ~ '^[0-9a-f]{64}$'
                AND jsonb_typeof(entry->'size_bytes')='number' AND entry->>'size_bytes' ~ '^[1-9][0-9]{0,7}$'
                AND (CASE WHEN entry->>'size_bytes' ~ '^[1-9][0-9]{0,7}$'
                    THEN (entry->>'size_bytes')::bigint<=67108864 ELSE false END)
                AND entry->>'media_type' IN ('image/jpeg','image/png','image/webp')
                AND entry->>'state' IN ('planned','verified')
                AND jsonb_typeof(entry->'write_nonce')='string'
                AND entry->>'write_nonce' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                AND member.key ~ '^(original/[0-9a-f]{64}[.](jpg|png|webp)|crops/(0|[1-9][0-9]{0,9})-[0-9a-f]{64}[.]jpg)$'
                AND right(member.key,64+length(CASE entry->>'media_type' WHEN 'image/jpeg' THEN '.jpg' WHEN 'image/png' THEN '.png' ELSE '.webp' END))
                    =(entry->>'sha256') || (CASE entry->>'media_type' WHEN 'image/jpeg' THEN '.jpg' WHEN 'image/png' THEN '.png' ELSE '.webp' END)),false) THEN
                RAISE EXCEPTION 'Invalid prediction input object' USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_inventory';
            END IF;
            IF NOT COALESCE(((entry->>'state'='planned' AND entry->'etag'='null'::jsonb AND entry->'version_id'='null'::jsonb)
                OR (entry->>'state'='verified' AND jsonb_typeof(entry->'etag')='string'
                    AND entry->>'etag' ~ '^[!-~]{1,1024}$'
                    AND (entry->'version_id'='null'::jsonb OR (jsonb_typeof(entry->'version_id')='string'
                        AND entry->>'version_id' ~ '^[!-~]{1,1024}$')))),false) THEN
                RAISE EXCEPTION 'Invalid prediction verification receipt' USING ERRCODE='23514';
            END IF;
            count_objects:=count_objects+1;
            total:=total+(entry->>'size_bytes')::bigint;
            IF TG_OP='INSERT' AND entry->>'state'<>'planned' THEN
                RAISE EXCEPTION 'Objects must first be planned' USING ERRCODE='23514';
            END IF;
            IF TG_OP='UPDATE' AND NOT (OLD.inventory->'objects' ? member.key)
                AND entry->>'state'<>'planned' THEN
                RAISE EXCEPTION 'New objects must first be planned' USING ERRCODE='23514';
            END IF;
        END LOOP;
        IF count_objects<>NEW.object_count OR total<>NEW.size_bytes THEN
            RAISE EXCEPTION 'Prediction input totals do not match inventory' USING ERRCODE='23514';
        END IF;
        IF TG_OP='UPDATE' THEN
            FOR previous IN SELECT key,value FROM jsonb_each(OLD.inventory->'objects') LOOP
                entry:=NEW.inventory->'objects'->previous.key;
                IF entry IS NULL OR entry-ARRAY['state','etag','version_id']::text[]
                    <>previous.value-ARRAY['state','etag','version_id']::text[]
                    OR (previous.value->>'state'='verified' AND entry<>previous.value) THEN
                    RAISE EXCEPTION 'Prediction input objects are immutable' USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_append_only';
                END IF;
            END LOOP;
            IF NEW.inventory IS DISTINCT FROM OLD.inventory AND (
                NEW.state<>'active' OR OLD.state<>'active' OR NEW.expires_at<=statement_timestamp()
            ) THEN
                RAISE EXCEPTION 'Retired prediction inventory is frozen' USING ERRCODE='23514';
            END IF;
        END IF;
        RETURN NEW;
    END $$;
    """)
    op.execute(
        "CREATE TRIGGER trg_prediction_input_owner BEFORE INSERT OR UPDATE OR DELETE "
        "ON prediction_input_leases FOR EACH ROW EXECUTE FUNCTION guard_prediction_input_owner()"
    )


def downgrade():
    op.execute("LOCK TABLE prediction_input_leases IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM prediction_input_leases WHERE contract_version=2)"
        )
    ):
        raise RuntimeError(
            "Cannot discard prediction input responsibility, including tombstones"
        )
    op.execute("DROP TRIGGER trg_prediction_input_owner ON prediction_input_leases")
    op.execute("DROP FUNCTION guard_prediction_input_owner()")
    for name in (
        "times",
        "documents",
        "pin",
        "owner",
        "budget",
        "contract",
        "lease_state",
    ):
        op.drop_constraint(
            "ck_prediction_input_" + name, "prediction_input_leases", type_="check"
        )
    op.create_check_constraint(
        "ck_prediction_input_lease_state",
        "prediction_input_leases",
        "state IN ('active','cleanup_pending')",
    )
    for name in (
        "deleted_at",
        "size_bytes",
        "object_count",
        "maintenance_revision",
        "inventory",
        "source_ref",
        "storage_ref",
        "contract_version",
    ):
        op.drop_column("prediction_input_leases", name)
