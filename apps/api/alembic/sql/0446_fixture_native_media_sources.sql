-- Link each real native worker original admission to its immutable job lease.
-- The operation may settle only through the owner's actual physical-close path.
CREATE TABLE aap_assets_v1.fixture_native_media_sources (
    admission_token uuid NOT NULL UNIQUE,
    source_operation_id uuid PRIMARY KEY,
    creation_id uuid NOT NULL,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NOT NULL,
    family text NOT NULL CHECK (family IN ('image','media')),
    job_id uuid NOT NULL,
    job_generation bigint NOT NULL CHECK (job_generation > 0),
    lease_token uuid NOT NULL,
    source_snapshot text NOT NULL CHECK (source_snapshot ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('open','settled'))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_media_sources FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_media_source()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.source_operation_id,NEW.admission_token,NEW.creation_id,NEW.run_id,NEW.dataset_id,
        NEW.item_id,NEW.family,NEW.job_id,NEW.job_generation,NEW.lease_token,NEW.source_snapshot)
       IS DISTINCT FROM (OLD.source_operation_id,OLD.admission_token,OLD.creation_id,OLD.run_id,OLD.dataset_id,
        OLD.item_id,OLD.family,OLD.job_id,OLD.job_generation,OLD.lease_token,OLD.source_snapshot)
       OR OLD.state<>'open' OR NEW.state<>'settled' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_source_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_media_source() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_media_source_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_media_sources FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_media_source();
-- aap-statement-break
-- Ordinary producers retain their native Job and output owner.  Their actual
-- original-read capability records the exact claimed attempt and physical
-- source permit without granting the ordinary worker fixture mutation rights.
CREATE TABLE aap_assets_v1.fixture_external_media_sources (
    source_operation_id uuid PRIMARY KEY,
    creation_id uuid NOT NULL,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NOT NULL,
    family text NOT NULL CHECK (family IN ('image','media')),
    job_id uuid NOT NULL,
    job_generation bigint NOT NULL CHECK (job_generation>0),
    lease_token uuid NOT NULL,
    source_snapshot text NOT NULL CHECK (source_snapshot ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('open','settled')),
    FOREIGN KEY (family,job_id) REFERENCES aap_assets_v1.fixture_external_media_jobs(family,job_id)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_external_media_sources FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_external_media_source()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.source_operation_id,NEW.creation_id,NEW.run_id,NEW.dataset_id,NEW.item_id,
        NEW.family,NEW.job_id,NEW.job_generation,NEW.lease_token,NEW.source_snapshot)
       IS DISTINCT FROM (OLD.source_operation_id,OLD.creation_id,OLD.run_id,OLD.dataset_id,OLD.item_id,
        OLD.family,OLD.job_id,OLD.job_generation,OLD.lease_token,OLD.source_snapshot)
       OR OLD.state<>'open' OR NEW.state<>'settled' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_external_source_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_external_media_source() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_external_media_source_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_external_media_sources FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_external_media_source();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.capture_fixture_native_media_source(
    p_family text,p_job uuid,p_generation bigint,p_lease uuid,p_snapshot text,p_reply jsonb
) RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_native_media_jobs%ROWTYPE; v_creation uuid; v_operation uuid; v_token uuid;
BEGIN
    IF p_reply #>> '{data,ownership}' IS DISTINCT FROM 'fixture' THEN RETURN; END IF;
    BEGIN
        v_creation := (p_reply #>> '{data,creation_id}')::uuid;
        v_operation := (p_reply #>> '{data,operation_id}')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_source_unproven';
    END;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_native_media_jobs
    WHERE family=p_family AND job_id=p_job;
    IF NOT FOUND THEN
        INSERT INTO aap_assets_v1.fixture_external_media_sources(
            source_operation_id,creation_id,run_id,dataset_id,item_id,family,job_id,
            job_generation,lease_token,source_snapshot,state)
        SELECT v_operation,v_creation,external.run_id,external.dataset_id,external.item_id,
               p_family,p_job,p_generation,p_lease,p_snapshot,'open'
        FROM aap_assets_v1.fixture_external_media_jobs AS external
            JOIN yoao_fixture_v1.fixture_run_resources AS resource
              ON resource.run_id=external.run_id AND resource.kind='creation'
             AND resource.item_id=external.item_id AND resource.creation_id=v_creation
             AND resource.state='published'
            JOIN public.managed_original_creations AS creation ON creation.id=resource.creation_id
            JOIN public.storage_operations AS operation ON operation.id=v_operation
              AND operation.location_id=creation.location_id
              AND operation.purpose='media_original_' || pg_catalog.replace(creation.id::text,'-','')
              AND operation.node_id='aap-assets-media'
        WHERE external.family=p_family AND external.job_id=p_job AND p_generation>0
          AND p_lease IS NOT NULL AND p_snapshot ~ '^[0-9a-f]{64}$'
          AND creation.status='verified' AND creation.settled_at IS NOT NULL;
        IF FOUND THEN RETURN; END IF;
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_job_untracked';
    END IF;
    IF p_snapshot IS NULL OR p_snapshot !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_job_untracked';
    END IF;
    SELECT admission.token INTO v_token
    FROM aap_assets_v1.fixture_native_media_admissions AS admission
    WHERE admission.run_id=v_birth.run_id AND admission.dataset_id=v_birth.dataset_id
      AND admission.item_id=v_birth.item_id AND admission.family=p_family AND admission.job_id=p_job
      AND admission.job_generation=p_generation AND admission.lease_token=p_lease
      AND admission.business_pid=pg_catalog.pg_backend_pid() AND admission.state='admitted'
      AND aap_assets_v1.fixture_native_source_lock_held(admission.business_pid,admission.token)
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_admission_required';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        JOIN public.managed_original_creations AS creation ON creation.id=resource.creation_id
        JOIN public.storage_operations AS operation ON operation.id=v_operation
          AND operation.location_id=creation.location_id
          AND operation.purpose='media_original_' || pg_catalog.replace(creation.id::text,'-','')
          AND operation.node_id='aap-assets-media'
        WHERE resource.run_id=v_birth.run_id AND resource.kind='creation'
          AND resource.creation_id=v_creation AND resource.item_id=v_birth.item_id
          AND resource.state='published' AND creation.status='verified'
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_source_unproven';
    END IF;
    UPDATE aap_assets_v1.fixture_native_media_admissions SET state='consumed'
    WHERE token=v_token AND state='admitted';
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_admission_changed';
    END IF;
    INSERT INTO aap_assets_v1.fixture_native_media_sources
        (source_operation_id,admission_token,creation_id,run_id,dataset_id,item_id,family,job_id,
         job_generation,lease_token,source_snapshot,state)
    VALUES (v_operation,v_token,v_creation,v_birth.run_id,v_birth.dataset_id,v_birth.item_id,
        p_family,p_job,p_generation,p_lease,p_snapshot,'open');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.capture_fixture_native_media_source(text,uuid,bigint,uuid,text,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.settle_fixture_native_media_source()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_writes AS write
        WHERE write.source_operation_id=OLD.id AND write.state<>'verified'
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_cache_unsettled';
    END IF;
    UPDATE aap_assets_v1.fixture_native_media_sources SET state='settled'
    WHERE source_operation_id=OLD.id AND state='open';
    UPDATE aap_assets_v1.fixture_external_media_sources SET state='settled'
    WHERE source_operation_id=OLD.id AND state='open';
    RETURN OLD;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.settle_fixture_native_media_source() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_media_source_settlement BEFORE DELETE ON public.storage_operations
FOR EACH ROW WHEN (OLD.node_id='aap-assets-media'
    AND pg_catalog.starts_with(OLD.purpose,'media_original_'))
EXECUTE FUNCTION aap_assets_v1.settle_fixture_native_media_source();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_external_image_attempt_sources_settled(
    p_run uuid,p_dataset uuid,p_item uuid,p_job uuid,p_generation bigint,p_lease uuid
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF aap_jobs_v1.fixture_media_principal_matches(p_run) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_media_principal_required';
    END IF;
    RETURN EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_external_media_sources AS source
        WHERE source.run_id=p_run AND source.dataset_id=p_dataset AND source.item_id=p_item
          AND source.family='image' AND source.job_id=p_job AND source.job_generation=p_generation
          AND source.lease_token=p_lease
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_external_media_sources AS source
        WHERE source.run_id=p_run AND source.dataset_id=p_dataset AND source.item_id=p_item
          AND source.family='image' AND source.job_id=p_job AND source.job_generation=p_generation
          AND source.lease_token=p_lease AND source.state<>'settled'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_external_image_attempt_sources_settled(uuid,uuid,uuid,uuid,bigint,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.begin_image_original_worker(
    p_job_id uuid, p_generation bigint, p_lease_token uuid,
    p_source_snapshot text, p_object_key text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_job public.asset_image_jobs%ROWTYPE;
    v_reply jsonb;
    v_current jsonb;
    v_source jsonb;
    v_item_id uuid;
    v_task_id uuid;
    v_message text;
BEGIN
    SELECT job.* INTO v_job FROM public.asset_image_jobs AS job
    WHERE job.job_id = p_job_id FOR SHARE NOWAIT;
    IF NOT FOUND OR p_generation IS NULL OR p_lease_token IS NULL
       OR v_job.status <> 'running' OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at <= pg_catalog.clock_timestamp()
       OR v_job.owner_id IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    IF v_job.source_snapshot IS DISTINCT FROM p_source_snapshot
       OR v_job.source_facts ->> 'file_path' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    v_reply := aap_assets_v1.resolve_internal_image_source(
        v_job.owner_kind, v_job.owner_id, v_job.source_snapshot, v_job.source_facts -> 'object_identity'
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_source := v_reply -> 'data';
    IF v_source ->> 'source_snapshot' IS DISTINCT FROM p_source_snapshot
       OR v_source ->> 'file_path' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    v_item_id := (v_source ->> 'item_id')::uuid;
    v_task_id := CASE WHEN v_item_id IS NULL THEN (v_source ->> 'task_id')::uuid ELSE NULL END;
    IF v_item_id IS NOT NULL THEN
        PERFORM 1 FROM public.dataset_items AS item WHERE item.id = v_item_id FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
    END IF;
    v_reply := aap_assets_v1._image_reply(
        aap_assets_v1._begin_media_original_read(v_item_id, v_task_id, p_object_key), NULL
    );
    v_current := aap_assets_v1.resolve_internal_image_source(
        v_job.owner_kind, v_job.owner_id, v_job.source_snapshot, v_job.source_facts -> 'object_identity'
    );
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_source_authority_changed';
    END IF;
    IF v_current #>> '{data,source_snapshot}' IS DISTINCT FROM p_source_snapshot THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    PERFORM aap_assets_v1.capture_fixture_native_media_source(
        'image',p_job_id,p_generation,p_lease_token,p_source_snapshot,v_reply
    );
    RETURN v_reply;
EXCEPTION
    WHEN raise_exception THEN
        IF SQLERRM = 'image_source_authority_changed' THEN RETURN v_current; END IF;
        RAISE;
    WHEN lock_not_available THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
        END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.begin_media_original_worker(
    p_job_id uuid, p_generation bigint, p_lease_token uuid,
    p_source_snapshot text, p_object_key text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_job public.asset_media_jobs%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
    v_source jsonb;
    v_reply jsonb;
    v_message text;
BEGIN
    SELECT job.* INTO v_job FROM public.asset_media_jobs AS job
    WHERE job.job_id = p_job_id FOR SHARE NOWAIT;
    IF NOT FOUND OR p_generation IS NULL OR p_lease_token IS NULL
       OR v_job.status <> 'running' OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at <= pg_catalog.clock_timestamp()
       OR v_job.dataset_item_id IS NULL THEN
        RETURN aap_assets_v1._media_reply(NULL, 'lease_lost');
    END IF;
    SELECT item.* INTO v_item FROM public.dataset_items AS item
    WHERE item.id = v_job.dataset_item_id FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._media_reply(NULL, 'source_missing'); END IF;
    v_source := aap_assets_v1._media_source_json(v_item);
    IF v_source ->> 'source_snapshot' IS DISTINCT FROM p_source_snapshot
       OR v_source ->> 'file_path' IS DISTINCT FROM p_object_key
       OR v_item.media_source_generation IS DISTINCT FROM v_job.source_generation THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    v_reply := aap_assets_v1._media_reply(
        aap_assets_v1._begin_media_original_read(v_item.id, NULL, p_object_key), NULL
    );
    PERFORM aap_assets_v1.capture_fixture_native_media_source(
        'media',p_job_id,p_generation,p_lease_token,p_source_snapshot,v_reply
    );
    RETURN v_reply;
EXCEPTION
    WHEN lock_not_available THEN RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
        END IF;
        RAISE;
END;
$function$;
