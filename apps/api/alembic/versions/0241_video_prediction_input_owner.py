"""Add bounded version-pinned whole-video prediction inputs."""

from alembic import op

revision = "0241"
down_revision = "0240"
branch_labels = None
depends_on = None

_PREVIOUS_OWNER = r"""
    CREATE OR REPLACE FUNCTION guard_prediction_input_owner() RETURNS trigger LANGUAGE plpgsql AS $$
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
                    AND octet_length(entry->>'etag') BETWEEN 1 AND 1024
                    AND length(entry->>'etag')=octet_length(entry->>'etag')
                    AND entry->>'etag' ~ '^[!-~]+$'
                    AND (entry->'version_id'='null'::jsonb OR (jsonb_typeof(entry->'version_id')='string'
                        AND octet_length(entry->>'version_id') BETWEEN 1 AND 1024
                        AND length(entry->>'version_id')=octet_length(entry->>'version_id')
                        AND entry->>'version_id' ~ '^[!-~]+$')))),false) THEN
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
    """

_OWNER = r"""
    CREATE OR REPLACE FUNCTION guard_prediction_input_owner() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        member record; previous record; entry jsonb; total bigint:=0; count_objects integer:=0;
        relation_owner oid; restoring boolean:=false;
    BEGIN
        IF TG_OP='DELETE' THEN
            IF OLD.contract_version NOT IN (2,3) OR OLD.state<>'deleted' OR EXISTS (
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
            IF NEW.contract_version=3 THEN
                IF NOT COALESCE((jsonb_typeof(entry)='object'
                    AND entry ?& ARRAY['sha256','size_bytes','media_type','state','etag','version_id','write_nonce','container','probe']
                    AND entry-ARRAY['sha256','size_bytes','media_type','state','etag','version_id','write_nonce','container','probe']::text[]='{}'::jsonb
                    AND jsonb_typeof(entry->'sha256')='string' AND entry->>'sha256' ~ '^[0-9a-f]{64}$'
                    AND jsonb_typeof(entry->'size_bytes')='number'
                    AND (CASE WHEN entry->>'size_bytes' ~ '^[1-9][0-9]{0,9}$' THEN (entry->>'size_bytes')::bigint<=5368709120 ELSE false END)
                    AND entry->>'media_type'='application/octet-stream'
                    AND entry->>'state' IN ('planned','verified')
                    AND jsonb_typeof(entry->'write_nonce')='string'
                    AND entry->>'write_nonce' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                    AND entry->>'container' IN ('mov','matroska','avi','asf','flv')
                    AND member.key='video/' || (entry->>'sha256') || (CASE entry->>'container' WHEN 'mov' THEN '.mov' WHEN 'matroska' THEN '.mkv' WHEN 'avi' THEN '.avi' WHEN 'asf' THEN '.asf' WHEN 'flv' THEN '.flv' END)
                    AND jsonb_typeof(entry->'probe')='object'
                    AND entry->'probe' ?& ARRAY['probe_contract','video']
                    AND (entry->'probe')-ARRAY['probe_contract','video']::text[]='{}'::jsonb
                    AND entry->'probe'->'probe_contract'='1'::jsonb AND entry->'probe'->>'probe_contract'='1'
                    AND jsonb_typeof(entry->'probe'->'video')='object'
                    AND entry->'probe'->'video' ?& ARRAY['width','height','fps','frame_count','duration_ms','codec']
                    AND (entry->'probe'->'video')-ARRAY['width','height','fps','frame_count','duration_ms','codec']::text[]='{}'::jsonb
                    AND jsonb_typeof(entry->'probe'->'video'->'width')='number'
                    AND jsonb_typeof(entry->'probe'->'video'->'height')='number'
                    AND (CASE WHEN entry->'probe'->'video'->>'width' ~ '^[1-9][0-9]{0,9}$' THEN (entry->'probe'->'video'->>'width')::bigint<=2147483647 ELSE false END)
                    AND (CASE WHEN entry->'probe'->'video'->>'height' ~ '^[1-9][0-9]{0,9}$' THEN (entry->'probe'->'video'->>'height')::bigint<=2147483647 ELSE false END)
                    AND jsonb_typeof(entry->'probe'->'video'->'frame_count')='number'
                    AND (CASE WHEN entry->'probe'->'video'->>'frame_count' ~ '^[1-9][0-9]{0,6}$' THEN (entry->'probe'->'video'->>'frame_count')::bigint<=1000000 ELSE false END)
                    AND (entry->'probe'->'video'->'duration_ms'='null'::jsonb OR (jsonb_typeof(entry->'probe'->'video'->'duration_ms')='number' AND (CASE WHEN entry->'probe'->'video'->>'duration_ms' ~ '^(0|[1-9][0-9]{0,18})$' THEN (entry->'probe'->'video'->>'duration_ms')::numeric<=9223372036854775807 ELSE false END)))
                    AND jsonb_typeof(entry->'probe'->'video'->'fps')='number'
                    AND (CASE WHEN jsonb_typeof(entry->'probe'->'video'->'fps')='number' THEN (entry->'probe'->'video'->>'fps')::numeric>0 ELSE false END)
                    AND jsonb_typeof(entry->'probe'->'video'->'codec')='string'
                    AND length(entry->'probe'->'video'->>'codec') BETWEEN 1 AND 128
                    AND octet_length((entry->'probe')::text)<=4096),false) THEN
                    RAISE EXCEPTION 'Invalid video prediction input object' USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_inventory';
                END IF;
            ELSE
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
            END IF;
            IF NOT COALESCE(((entry->>'state'='planned' AND entry->'etag'='null'::jsonb AND entry->'version_id'='null'::jsonb)
                OR (entry->>'state'='verified' AND jsonb_typeof(entry->'etag')='string'
                    AND octet_length(entry->>'etag') BETWEEN 1 AND 1024
                    AND length(entry->>'etag')=octet_length(entry->>'etag')
                    AND entry->>'etag' ~ '^[!-~]+$'
                    AND (entry->'version_id'='null'::jsonb OR (jsonb_typeof(entry->'version_id')='string'
                        AND octet_length(entry->>'version_id') BETWEEN 1 AND 1024
                        AND length(entry->>'version_id')=octet_length(entry->>'version_id')
                        AND entry->>'version_id' ~ '^[!-~]+$')))),false) THEN
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
    """

_PREVIOUS_VERSION = r"""
    CREATE OR REPLACE FUNCTION guard_prediction_input_version() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE member record;
    BEGIN
        IF NEW.contract_version<>2 THEN RETURN NEW; END IF;
        FOR member IN SELECT key,value FROM jsonb_each(NEW.inventory->'objects') LOOP
            IF member.value->>'state'='verified' AND (
                jsonb_typeof(member.value->'version_id') IS DISTINCT FROM 'string'
                OR member.value->>'version_id'='null'
            ) THEN
                IF TG_OP='UPDATE' THEN
                    IF OLD.inventory->'objects'->member.key=member.value THEN
                        CONTINUE;
                    END IF;
                END IF;
                RAISE EXCEPTION 'New prediction input receipts require immutable object versions'
                    USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_version';
            END IF;
        END LOOP;
        RETURN NEW;
    END $$;
    """

_VERSION = r"""
    CREATE OR REPLACE FUNCTION guard_prediction_input_version() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE member record;
    BEGIN
        IF NEW.contract_version NOT IN (2,3) THEN RETURN NEW; END IF;
        FOR member IN SELECT key,value FROM jsonb_each(NEW.inventory->'objects') LOOP
            IF member.value->>'state'='verified' AND (
                jsonb_typeof(member.value->'version_id') IS DISTINCT FROM 'string'
                OR member.value->>'version_id'='null'
            ) THEN
                IF TG_OP='UPDATE' THEN
                    IF OLD.inventory->'objects'->member.key=member.value THEN
                        CONTINUE;
                    END IF;
                END IF;
                RAISE EXCEPTION 'New prediction input receipts require immutable object versions'
                    USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_version';
            END IF;
        END LOOP;
        RETURN NEW;
    END $$;
    """

_CHECKS = {
    "ck_prediction_input_contract": "contract_version IN (1,2,3)",
    "ck_prediction_input_budget": "(contract_version IN (1,2) AND object_count BETWEEN 0 AND 10000 AND size_bytes BETWEEN 0 AND 4294967296) OR (contract_version=3 AND object_count BETWEEN 0 AND 1 AND size_bytes BETWEEN 0 AND 5368709120)",
    "ck_prediction_input_owner": "(contract_version=1 AND owner_kind NOT IN ('video_batch_predict','video_prediction_retry','video_predict_test','video_interactive','video_tracker')) OR (contract_version=2 AND owner_kind IN ('batch_predict','prediction_retry','interactive_predict','predict_test','mask_repair','secondary_inference')) OR (contract_version=3 AND owner_kind IN ('video_batch_predict','video_prediction_retry','video_predict_test','video_interactive','video_tracker'))",
    "ck_prediction_input_pin": "(contract_version=1 AND storage_ref IS NULL AND maintenance_revision IS NULL AND inventory IS NULL AND source_ref IS NULL AND object_count=0 AND size_bytes=0 AND deleted_at IS NULL AND state<>'deleted') OR (contract_version IN (2,3) AND maintenance_revision>=1 AND maintenance_revision IS NOT NULL AND \nCOALESCE((jsonb_typeof(storage_ref)='object'\n    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision','binding_generation','endpoint_url','bucket','key_prefix','storage_identity']\n    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision','binding_generation','endpoint_url','bucket','key_prefix','storage_identity']::text[]='{}'::jsonb\n    AND storage_ref->'schema'='1'::jsonb AND storage_ref->>'schema'='1'\n    AND storage_ref->>'purpose'='import'\n    AND storage_ref->>'location_id'='4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'\n    AND storage_ref->'config_revision'='1'::jsonb AND storage_ref->>'config_revision'='1'\n    AND storage_ref->'binding_generation'='1'::jsonb AND storage_ref->>'binding_generation'='1'\n    AND jsonb_typeof(storage_ref->'endpoint_url')='string'\n    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'\n    AND position(chr(92) in (storage_ref->>'endpoint_url'))=0\n    AND jsonb_typeof(storage_ref->'bucket')='string'\n    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255\n    AND storage_ref->>'bucket' !~ '[[:space:]/?#@%:]'\n    AND position(chr(92) in (storage_ref->>'bucket'))=0\n    AND storage_ref->>'bucket'=bucket\n    AND storage_ref->>'key_prefix'='prediction-inputs/' || id::text || '/'\n    AND jsonb_typeof(storage_ref->'storage_identity')='string'\n    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'\n    AND storage_ref->>'storage_identity'=storage_identity),false)\n)",
}

_PREVIOUS_CHECKS = {
    "ck_prediction_input_contract": "contract_version IN (1,2)",
    "ck_prediction_input_budget": "object_count BETWEEN 0 AND 10000 AND size_bytes BETWEEN 0 AND 4294967296",
    "ck_prediction_input_owner": "contract_version=1 OR owner_kind IN ('batch_predict','prediction_retry','interactive_predict','predict_test','mask_repair','secondary_inference')",
    "ck_prediction_input_pin": "(contract_version=1 AND storage_ref IS NULL AND maintenance_revision IS NULL AND inventory IS NULL AND source_ref IS NULL AND object_count=0 AND size_bytes=0 AND deleted_at IS NULL AND state<>'deleted') OR (contract_version=2 AND maintenance_revision>=1 AND maintenance_revision IS NOT NULL AND \nCOALESCE((jsonb_typeof(storage_ref)='object'\n    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision','binding_generation','endpoint_url','bucket','key_prefix','storage_identity']\n    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision','binding_generation','endpoint_url','bucket','key_prefix','storage_identity']::text[]='{}'::jsonb\n    AND storage_ref->'schema'='1'::jsonb AND storage_ref->>'schema'='1'\n    AND storage_ref->>'purpose'='import'\n    AND storage_ref->>'location_id'='4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'\n    AND storage_ref->'config_revision'='1'::jsonb AND storage_ref->>'config_revision'='1'\n    AND storage_ref->'binding_generation'='1'::jsonb AND storage_ref->>'binding_generation'='1'\n    AND jsonb_typeof(storage_ref->'endpoint_url')='string'\n    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'\n    AND position(chr(92) in (storage_ref->>'endpoint_url'))=0\n    AND jsonb_typeof(storage_ref->'bucket')='string'\n    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255\n    AND storage_ref->>'bucket' !~ '[[:space:]/?#@%:]'\n    AND position(chr(92) in (storage_ref->>'bucket'))=0\n    AND storage_ref->>'bucket'=bucket\n    AND storage_ref->>'key_prefix'='prediction-inputs/' || id::text || '/'\n    AND jsonb_typeof(storage_ref->'storage_identity')='string'\n    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'\n    AND storage_ref->>'storage_identity'=storage_identity),false)\n)",
}


def _constraints(values):
    for name, expression in values.items():
        op.drop_constraint(name, "prediction_input_leases", type_="check")
        op.create_check_constraint(name, "prediction_input_leases", expression)


def upgrade():
    _constraints(_CHECKS)
    op.execute(_OWNER)
    op.execute(_VERSION)


def downgrade():
    op.execute("LOCK TABLE prediction_input_leases IN ACCESS EXCLUSIVE MODE")
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM prediction_input_leases WHERE contract_version=3) THEN
            RAISE EXCEPTION 'Video prediction input responsibility prevents downgrade, including tombstones'
                USING ERRCODE='23514';
        END IF;
    END $$;
    """)
    op.execute(_PREVIOUS_OWNER)
    op.execute(_PREVIOUS_VERSION)
    _constraints(_PREVIOUS_CHECKS)
