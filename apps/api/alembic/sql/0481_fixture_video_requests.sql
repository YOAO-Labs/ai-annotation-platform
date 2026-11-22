ALTER TABLE public.video_derivative_outputs ADD COLUMN fixture_request_versions jsonb;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.capture_fixture_video_request()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_run uuid; v_dataset uuid; v_binding jsonb; v_subject jsonb;
    v_target yoao_assets_v1.fixture_native_cache_targets%ROWTYPE;
    v_config_revision bigint; v_versions jsonb;
    v_root_prefix constant text := '_aap_managed/media-cache/';
BEGIN
    IF TG_OP='UPDATE' THEN
        IF NEW.fixture_request_versions IS DISTINCT FROM OLD.fixture_request_versions THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_video_request_versions_immutable';
        END IF;
        IF OLD.fixture_request_versions IS NULL OR OLD.state<>'building' OR NEW.state<>'ready' THEN
            RETURN NEW;
        END IF;
        SELECT pg_catalog.jsonb_object_agg(version.object_key,pg_catalog.jsonb_build_object(
            'etag',version.etag,'version_id',version.version_id)) INTO v_versions
        FROM yoao_assets_v1.fixture_media_versions AS version
        JOIN yoao_assets_v1.fixture_independent_video_attempts AS attempt
          ON attempt.output_id=NEW.id AND attempt.delivery_id=NEW.id AND attempt.lease_token=NEW.lease_token
        JOIN yoao_assets_v1.fixture_independent_media_executions AS execution
          ON execution.delivery_id=attempt.delivery_id AND execution.token=version.execution_token
        JOIN yoao_assets_v1.fixture_media_births AS birth
          ON birth.owner_kind='video_output' AND birth.owner_id=NEW.id AND birth.generation=0
         AND (birth.run_id,birth.dataset_id,birth.item_id)=(version.run_id,version.dataset_id,version.item_id)
        WHERE version.execution_kind='independent' AND version.execution_id=NEW.id
          AND version.owner_kind='video_output' AND version.owner_id=NEW.id AND version.state='verified'
          AND version.version_id NOT IN ('','null') AND version.etag IS NOT NULL
          AND NEW.inventory->'objects'->version.object_key=pg_catalog.jsonb_build_object(
              'sha256',version.sha256,'size_bytes',version.size_bytes);
        IF v_versions IS NULL OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(v_versions))
            <> (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(NEW.inventory->'objects'))
            OR EXISTS (
                SELECT 1 FROM yoao_assets_v1.fixture_media_versions AS version
                WHERE version.owner_kind='video_output' AND version.owner_id=NEW.id
                GROUP BY version.object_key HAVING pg_catalog.count(*)<>1
            ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_video_request_versions_unproven';
        END IF;
        NEW.fixture_request_versions := v_versions;
        RETURN NEW;
    END IF;

    IF NEW.fixture_request_versions IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_video_request_versions_immutable';
    END IF;
    IF NEW.kind NOT IN ('frame','chunk') OR NEW.auth_ref->>'mode' IS DISTINCT FROM 'http' THEN
        RETURN NEW;
    END IF;
    SELECT resource.run_id INTO v_run
    FROM yoao_fixture_v1.fixture_run_resources AS resource
    JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=resource.run_id
    WHERE resource.kind='item' AND resource.resource_id=NEW.dataset_item_id
      AND run.family='owned' AND run.parent_run_id IS NOT NULL;
    IF NOT FOUND THEN RETURN NEW; END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock_shared(
        pg_catalog.hashtextextended('aap-fixture-video-requests:' || v_run::text,0)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='55P03', MESSAGE='fixture_video_request_drain_busy';
    END IF;
    v_binding := yoao_assets_v1.media_source_binding('dataset_item',NEW.dataset_item_id,NEW.original_ref);
    v_dataset := (v_binding->>'dataset_id')::uuid;
    IF v_binding->>'ownership' IS DISTINCT FROM 'fixture' OR v_binding->>'run_id' IS DISTINCT FROM v_run::text
       OR v_binding->>'item_id' IS DISTINCT FROM NEW.dataset_item_id::text
       OR NEW.auth_ref->'original' IS DISTINCT FROM NEW.original_ref THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_video_request_source_unproven';
    END IF;
    IF NEW.auth_ref->>'owner_kind'='task' THEN
        v_subject := yoao_assets_v1.media_source_binding('task',(NEW.auth_ref->>'owner_id')::uuid,NEW.original_ref);
        IF v_subject IS DISTINCT FROM v_binding THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_video_request_subject_changed';
        END IF;
    ELSIF NEW.auth_ref->>'owner_kind' IS DISTINCT FROM 'dataset_item'
          OR NEW.auth_ref->>'owner_id' IS DISTINCT FROM NEW.dataset_item_id::text THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_video_request_subject_changed';
    END IF;

    PERFORM 1 FROM public.dataset_items AS item
    JOIN yoao_fixture_v1.fixture_run_resources AS owned_dataset
      ON owned_dataset.run_id=v_run AND owned_dataset.kind='dataset'
     AND owned_dataset.resource_id=item.dataset_id AND owned_dataset.state='published'
    JOIN yoao_fixture_v1.fixture_run_resources AS owned_creation
      ON owned_creation.run_id=v_run AND owned_creation.kind='creation' AND owned_creation.item_id=item.id
     AND owned_creation.state='published'
    JOIN public.dataset_assets AS asset ON asset.dataset_item_id=item.id
    JOIN public.dataset_asset_locations AS location ON location.asset_id=asset.id
      AND location.creation_id=owned_creation.creation_id AND location.role='managed_original'
      AND location.reference_state='present' AND location.object_key=item.file_path
    JOIN public.managed_original_creations AS creation ON creation.id=location.creation_id
      AND creation.dataset_id=item.dataset_id AND creation.location_id=location.location_id
      AND creation.object_key=location.object_key AND creation.status='verified' AND creation.settled_at IS NOT NULL
      AND creation.owner_kind='dataset' AND creation.version_id NOT IN ('','null')
      AND creation.sha256=asset.checksum_value AND creation.size_bytes=asset.size_bytes
    JOIN public.video_derivative_outputs AS metadata ON metadata.id=item.video_output_id
      AND metadata.id=NEW.metadata_output_id AND metadata.kind='metadata' AND metadata.state='ready'
      AND metadata.dataset_item_id=item.id AND metadata.original_ref=NEW.original_ref
    WHERE item.id=NEW.dataset_item_id AND item.dataset_id=v_dataset AND item.file_type='video'
      AND asset.id::text=NEW.original_ref->>'content_id'
      AND location.location_id::text=NEW.original_ref->>'location_id'
    FOR SHARE OF metadata,creation;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_video_request_source_unproven';
    END IF;

    SELECT * INTO v_target FROM yoao_assets_v1.fixture_native_cache_targets WHERE run_id=v_run FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_video_request_target_unproven';
    END IF;
    SELECT location.config_revision INTO v_config_revision FROM public.storage_locations AS location
    WHERE location.id=v_target.location_id AND location.enabled AND location.connection_id=v_target.connection_id
    FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_video_request_target_unproven';
    END IF;
    NEW.storage_ref := pg_catalog.jsonb_build_object(
        'schema',2,'purpose','media-cache','location_id',v_target.location_id,
        'config_revision',v_config_revision,'binding_generation',v_target.binding_generation,
        'endpoint_url',v_target.endpoint,'bucket',v_target.bucket,'key_prefix',v_root_prefix,
        'prefix',v_root_prefix || 'video-derivatives/objects/' || NEW.id::text || '/',
        'storage_identity',v_target.storage_identity);
    NEW.fixture_request_versions := '{}'::jsonb;
    INSERT INTO yoao_assets_v1.fixture_media_births
        (owner_kind,owner_id,generation,run_id,dataset_id,item_id,producer_admission_id)
    VALUES ('video_output',NEW.id,0,v_run,v_dataset,NEW.dataset_item_id,NEW.id);
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.capture_fixture_video_request() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_video_request_capture BEFORE INSERT OR UPDATE ON public.video_derivative_outputs
FOR EACH ROW EXECUTE FUNCTION yoao_assets_v1.capture_fixture_video_request();
-- aap-statement-break
DROP TRIGGER fixture_video_output_birth ON public.video_derivative_outputs;
-- aap-statement-break
CREATE TRIGGER fixture_video_output_birth AFTER INSERT ON public.video_derivative_outputs
FOR EACH ROW WHEN (NEW.fixture_request_versions IS NULL)
EXECUTE FUNCTION yoao_assets_v1.record_fixture_media_birth();
-- aap-statement-break
DROP TRIGGER fixture_video_target_guard ON public.video_derivative_outputs;
-- aap-statement-break
CREATE TRIGGER fixture_video_target_guard AFTER INSERT OR UPDATE OF storage_ref ON public.video_derivative_outputs
FOR EACH ROW WHEN (NEW.fixture_request_versions IS NULL)
EXECUTE FUNCTION yoao_assets_v1.guard_fixture_native_target_output();
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_assets_v1.fixture_explicit_media_producer(
    p_run uuid,p_dataset uuid,p_item uuid,p_task uuid,p_delivery uuid,p_producer text
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM yoao_assets_v1.fixture_screenshot_producer_admissions AS admission
        WHERE p_producer='video_derivatives' AND p_task IS NULL
          AND admission.run_id=p_run AND admission.dataset_id=p_dataset AND admission.item_id=p_item
          AND admission.delivery_id=p_delivery AND admission.state='admitted'
          AND admission.business_pid=pg_catalog.pg_backend_pid()
          AND yoao_assets_v1.fixture_native_source_lock_held(admission.business_pid,admission.id)
    ) OR EXISTS (
        SELECT 1 FROM yoao_assets_v1.fixture_legacy_birth_admissions AS admission
        WHERE admission.run_id=p_run AND admission.dataset_id IS NOT DISTINCT FROM p_dataset
          AND admission.item_id IS NOT DISTINCT FROM p_item AND admission.task_id IS NOT DISTINCT FROM p_task
          AND admission.delivery_id=p_delivery AND admission.state='admitted'
          AND admission.business_pid=pg_catalog.pg_backend_pid()
          AND yoao_assets_v1.fixture_native_source_lock_held(admission.business_pid,admission.id)
    ) OR EXISTS (
        SELECT 1 FROM yoao_assets_v1.fixture_media_births AS birth
        JOIN public.video_derivative_outputs AS output ON output.id=birth.owner_id
        WHERE p_producer='video_derivatives' AND p_task IS NULL
          AND birth.owner_kind='video_output' AND birth.generation=0
          AND (birth.run_id,birth.dataset_id,birth.item_id)=(p_run,p_dataset,p_item)
          AND birth.owner_id=p_delivery AND birth.producer_admission_id=p_delivery
          AND output.dataset_item_id=p_item AND output.fixture_request_versions IS NOT NULL
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.prepare_fixture_video_request_delivery()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_prepared jsonb;
BEGIN
    v_prepared := yoao_jobs_v1.prepare_media_delivery(pg_catalog.jsonb_build_object(
        'schema','aap-jobs-media-prepare/v1','owner_kind','dataset_item','owner_id',NEW.dataset_item_id,
        'producer_kind','video_derivatives','delivery_id',NEW.id,
        'source',pg_catalog.jsonb_build_object('schema','aap-video-derivative-inputs/v1','outputs',
            pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object('output_id',NEW.id,'original',NEW.original_ref))),
        'envelope',pg_catalog.jsonb_build_object('schema','aap-jobs-queue/v2','operation','dispatch',
            'task_name','app.workers.media.generate_video_derivatives','execution_id',NEW.id,
            'args',pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_array(NEW.id)),
            'kwargs','{}'::jsonb,'options',pg_catalog.jsonb_build_object('queue','media'))));
    IF v_prepared->>'ownership' IS DISTINCT FROM 'fixture' OR v_prepared->>'delivery_id' IS DISTINCT FROM NEW.id::text THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_video_request_delivery_unproven';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.prepare_fixture_video_request_delivery() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_video_request_delivery AFTER INSERT ON public.video_derivative_outputs
FOR EACH ROW WHEN (NEW.fixture_request_versions IS NOT NULL)
EXECUTE FUNCTION yoao_assets_v1.prepare_fixture_video_request_delivery();
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.fixture_video_request_delivery_matches(p_run uuid,p_delivery uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM yoao_jobs_v1.fixture_media_deliveries AS delivery
        JOIN yoao_assets_v1.fixture_media_births AS birth
          ON birth.owner_kind='video_output' AND birth.generation=0
         AND birth.owner_id=delivery.delivery_id AND birth.producer_admission_id=delivery.delivery_id
         AND (birth.run_id,birth.dataset_id,birth.item_id)=(delivery.run_id,delivery.dataset_id,delivery.item_id)
        JOIN public.video_derivative_outputs AS output ON output.id=birth.owner_id
          AND output.kind IN ('frame','chunk') AND output.fixture_request_versions IS NOT NULL
          AND output.dataset_item_id=birth.item_id AND output.original_ref=delivery.source
        WHERE delivery.run_id=p_run AND delivery.delivery_id=p_delivery
          AND delivery.producer_kind='video_derivatives' AND delivery.owner_kind='dataset_item'
          AND delivery.owner_id=birth.item_id
          AND delivery.envelope->>'task_name'='app.workers.media.generate_video_derivatives'
          AND delivery.queue='aap_workspace_worker_' || pg_catalog.replace(p_run::text,'-','') || '_media'
          AND delivery.envelope #>> '{options,queue}'=delivery.queue
          AND delivery.envelope->'args'=pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_array(p_delivery))
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_jobs_v1.fixture_video_request_delivery_matches(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.next_fixture_video_request_publication(p_run uuid)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT delivery.delivery_id FROM yoao_jobs_v1.fixture_media_deliveries AS delivery
    JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=delivery.run_id AND run.state='open'
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=run.parent_run_id AND root.state='open'
    WHERE delivery.run_id=p_run AND yoao_jobs_v1.fixture_media_principal_matches(p_run)
      AND yoao_jobs_v1.fixture_video_request_delivery_matches(p_run,delivery.delivery_id)
      AND NOT EXISTS (SELECT 1 FROM yoao_jobs_v1.fixture_media_publication_attempts AS attempt
                      WHERE attempt.delivery_id=delivery.delivery_id)
    ORDER BY delivery.delivery_id LIMIT 1;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_jobs_v1.next_fixture_video_request_publication(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.begin_fixture_video_request_publication(p_run uuid,p_delivery uuid,p_nonce uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT yoao_jobs_v1.fixture_media_principal_matches(p_run)
       OR NOT yoao_jobs_v1.fixture_video_request_delivery_matches(p_run,p_delivery)
       OR EXISTS (SELECT 1 FROM yoao_jobs_v1.fixture_media_publication_attempts AS attempt
                  WHERE attempt.delivery_id=p_delivery AND attempt.state='entered') THEN RETURN NULL; END IF;
    RETURN yoao_jobs_v1.begin_fixture_media_publication(p_delivery,p_nonce);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_jobs_v1.begin_fixture_video_request_publication(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.finish_fixture_video_request_publication(
    p_run uuid,p_delivery uuid,p_nonce uuid,p_outcome text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT yoao_jobs_v1.fixture_media_principal_matches(p_run)
       OR NOT yoao_jobs_v1.fixture_video_request_delivery_matches(p_run,p_delivery) THEN RETURN false; END IF;
    RETURN yoao_jobs_v1.finish_fixture_media_publication(p_delivery,p_nonce,p_outcome);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_jobs_v1.finish_fixture_video_request_publication(uuid,uuid,uuid,text) FROM PUBLIC;
