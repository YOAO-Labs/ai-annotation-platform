-- Retry authority and snapshot are separately fenced by their admission owner.
-- Display and cooperative cancellation fields cannot select a Task or Dataset.
CREATE FUNCTION aap_jobs_v1.worker_job_scope_digest(p_kind text, p_payload jsonb)
RETURNS bytea LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.sha256(pg_catalog.convert_to((CASE p_kind
        WHEN 'prediction_retry' THEN p_payload - ARRAY['task_display_id','project_display_id',
            'project_name','ml_backend_name','cancel_requested','cancel_requested_at',
            'prediction_authority','video_prediction_authority']
        ELSE p_payload - ARRAY['cancel_requested','cancel_requested_at'] END)::text, 'UTF8'))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.worker_job_scope_digest(text,jsonb) FROM PUBLIC;
-- aap-statement-break
-- Only the Assets executor definer can see this single Job's frozen scope.
-- No row means unproven; an identity mismatch is not an unproven birth.
CREATE FUNCTION aap_jobs_v1.cross_frame_job_birth(p_id uuid,p_project uuid,p_actor uuid,p_payload jsonb)
RETURNS TABLE(matches boolean,dataset_ids uuid[])
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.async_jobs AS job
        WHERE job.id=b.job_id AND job.kind=b.kind AND job.project_id=b.project_id AND job.user_id=b.actor_id
          AND p_project=job.project_id AND p_actor=job.user_id
          AND b.payload_sha256=aap_jobs_v1.worker_job_scope_digest(b.kind,p_payload)
    ), b.dataset_ids FROM aap_jobs_v1.fixture_worker_job_datasets AS b
    WHERE b.job_id=p_id AND b.kind='point_cloud_cross_frame'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.cross_frame_job_birth(uuid,uuid,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.cross_frame_birth_identity_matches(p_id uuid,p_project uuid,p_actor uuid,p_payload jsonb)
RETURNS boolean LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT NOT EXISTS (
        SELECT 1 FROM public.async_jobs AS j WHERE j.id=p_id AND (
            j.kind<>'point_cloud_cross_frame' OR j.project_id IS DISTINCT FROM p_project
            OR j.user_id IS DISTINCT FROM p_actor OR j.payload IS DISTINCT FROM p_payload
        )
    ) AND (p_payload->>'parent_job_id' IS NULL OR EXISTS (
        SELECT 1 FROM public.async_jobs AS parent
        WHERE parent.id = CASE WHEN p_payload->>'parent_job_id' ~
            '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
            THEN (p_payload->>'parent_job_id')::uuid END
          AND parent.id<>p_id AND parent.kind='point_cloud_cross_frame'
          AND parent.project_id=p_project AND parent.user_id=p_actor
          AND parent.status IN ('completed','failed','cancelled')
          AND parent.payload->'scene_id'=p_payload->'scene_id'
          AND parent.payload->'source_task_id'=p_payload->'source_task_id'
          AND pg_catalog.jsonb_path_query_array(parent.payload,'$.sources[*].annotation_id')
              @> pg_catalog.jsonb_path_query_array(p_payload,'$.sources[*].annotation_id')
          AND pg_catalog.jsonb_path_query_array(parent.payload,'$.sources[*].annotation_id')
              <@ pg_catalog.jsonb_path_query_array(p_payload,'$.sources[*].annotation_id')
          AND pg_catalog.jsonb_path_query_array(parent.result,'$.items[*] ? (@.status == "failed" || @.status == "stale").frame_index')
              @> pg_catalog.jsonb_path_query_array(p_payload,'$.targets[*].frame_index')
    ))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.cross_frame_birth_identity_matches(uuid,uuid,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_worker_job_dataset_receipt() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP = 'INSERT' AND pg_catalog.pg_trigger_depth() >= 2 AND
       current_user = pg_catalog.pg_get_userbyid((SELECT relowner FROM pg_catalog.pg_class WHERE oid = TG_RELID)) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_worker_dataset_receipt_immutable';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_worker_job_dataset_receipt() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER worker_dataset_receipt_immutable BEFORE INSERT OR UPDATE OR DELETE
ON aap_jobs_v1.fixture_worker_job_datasets FOR EACH ROW
EXECUTE FUNCTION aap_jobs_v1.guard_worker_job_dataset_receipt();
-- aap-statement-break
-- AFTER INSERT: a rejected or ON CONFLICT DO NOTHING insertion cannot leave a
-- receipt. This trigger runs before the dataset anchor's AFTER INSERT check.
CREATE FUNCTION aap_jobs_v1.record_worker_job_dataset_scope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_datasets uuid[];
BEGIN
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_worker_job_datasets WHERE job_id=NEW.id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_worker_job_identity_reused';
    END IF;
    IF NEW.project_id IS NULL OR NEW.user_id IS NULL THEN RETURN NEW; END IF;
    CASE NEW.kind
        WHEN 'prediction_retry' THEN
            v_datasets := aap_jobs_v1.retry_born_dataset_scope(NEW.id,NEW.project_id,NEW.user_id,NEW.payload);
        WHEN 'point_cloud_cross_frame' THEN
            v_datasets := aap_assets_v1.cross_frame_born_dataset_scope(NEW.id,NEW.project_id,NEW.user_id,NEW.payload);
        ELSE RETURN NEW;
    END CASE;
    IF v_datasets IS NOT NULL THEN
        INSERT INTO aap_jobs_v1.fixture_worker_job_datasets(job_id,kind,project_id,actor_id,dataset_ids,payload_sha256)
        VALUES (NEW.id,NEW.kind,NEW.project_id,NEW.user_id,v_datasets,aap_jobs_v1.worker_job_scope_digest(NEW.kind,NEW.payload));
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.record_worker_job_dataset_scope() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_worker_job_dataset_birth AFTER INSERT ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.record_worker_job_dataset_scope();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_worker_job_scope_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_binding aap_jobs_v1.fixture_worker_job_datasets%ROWTYPE;
BEGIN
    IF NEW.id <> OLD.id AND EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_worker_job_datasets WHERE job_id=NEW.id
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_worker_job_identity_reused';
    END IF;
    SELECT * INTO v_binding FROM aap_jobs_v1.fixture_worker_job_datasets WHERE job_id=OLD.id;
    IF FOUND AND (NEW.id <> OLD.id OR NEW.kind <> OLD.kind
        OR (NEW.project_id IS NOT NULL AND NEW.project_id IS DISTINCT FROM OLD.project_id)
        OR (NEW.user_id IS NOT NULL AND NEW.user_id IS DISTINCT FROM OLD.user_id)
        OR (OLD.kind <> 'prediction_retry' AND NEW.execution_snapshot_id IS DISTINCT FROM OLD.execution_snapshot_id)
        OR (NEW.payload IS DISTINCT FROM OLD.payload
            AND aap_jobs_v1.worker_job_scope_digest(NEW.kind,NEW.payload) IS DISTINCT FROM v_binding.payload_sha256)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_worker_job_scope_immutable';
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_worker_job_scope_change() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_worker_job_scope_change BEFORE UPDATE OF id,kind,project_id,user_id,payload,execution_snapshot_id
ON public.async_jobs FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_worker_job_scope_change();
-- aap-statement-break
-- Only these two families require their newly installed, transactional AFTER
-- birth evidence. Keep the original guard function and all of its denials.
DROP TRIGGER fixture_dataset_job_anchor_guard ON public.async_jobs;
-- aap-statement-break
CREATE TRIGGER fixture_dataset_job_anchor_guard BEFORE UPDATE OF id,kind,project_id,user_id,payload OR DELETE
ON public.async_jobs FOR EACH ROW EXECUTE FUNCTION yoao_fixture_v1.guard_dataset_job_anchor();
-- aap-statement-break
CREATE TRIGGER fixture_dataset_job_anchor_insert BEFORE INSERT ON public.async_jobs FOR EACH ROW
WHEN (NEW.kind NOT IN ('prediction_retry','point_cloud_cross_frame'))
EXECUTE FUNCTION yoao_fixture_v1.guard_dataset_job_anchor();
-- aap-statement-break
CREATE TRIGGER fixture_dataset_job_worker_anchor_insert AFTER INSERT ON public.async_jobs FOR EACH ROW
WHEN (NEW.kind IN ('prediction_retry','point_cloud_cross_frame'))
EXECUTE FUNCTION yoao_fixture_v1.guard_dataset_job_anchor();
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.fixture_job_dataset_scope(
    p_id uuid, p_kind text, p_payload jsonb, p_project_id uuid, p_user_id uuid
) RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_datasets uuid[];
BEGIN
    IF p_kind IN ('prediction_retry','point_cloud_cross_frame') THEN
        SELECT binding.dataset_ids INTO v_datasets FROM aap_jobs_v1.fixture_worker_job_datasets AS binding
        WHERE binding.job_id=p_id AND binding.kind=p_kind AND binding.project_id=p_project_id
          AND binding.actor_id=p_user_id AND binding.payload_sha256=aap_jobs_v1.worker_job_scope_digest(p_kind,p_payload);
    ELSIF p_kind='export' THEN
        SELECT scope.dataset_ids INTO v_datasets FROM public.image_export_scopes AS scope
        WHERE scope.job_id=p_id AND scope.job_born
          AND (p_project_id IS NULL OR scope.project_id=p_project_id)
          AND (p_user_id IS NULL OR scope.actor_id=p_user_id);
    ELSIF p_kind='batch_predict' THEN
        SELECT binding.dataset_ids INTO v_datasets
        FROM aap_jobs_v1.fixture_prediction_job_datasets AS binding
        JOIN aap_jobs_v1.fixture_prediction_job_births AS birth ON birth.job_id=binding.job_id
        WHERE binding.job_id=p_id AND birth.kind=p_kind
          AND birth.project_id IS NOT DISTINCT FROM p_project_id AND birth.actor_id IS NOT DISTINCT FROM p_user_id;
    END IF;
    IF v_datasets IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object('kind','datasets','dataset_ids',pg_catalog.to_jsonb(v_datasets));
    END IF;
    RETURN aap_jobs_v1.fixture_dataset_job_scope(p_id,p_kind,p_payload,p_project_id,p_user_id);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_job_dataset_scope(uuid,text,jsonb,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.retain_unsettled_worker_deletion() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_worker_job_datasets AS b
                   WHERE b.job_id=OLD.id AND pg_catalog.cardinality(b.dataset_ids)>0)
       THEN RETURN OLD; END IF;
    IF OLD.kind='prediction_retry' AND EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS project
        WHERE project.kind='project' AND project.resource_id=OLD.project_id AND project.state='published'
          AND aap_jobs_v1.fixture_real_job_retired(project.run_id,project.resource_id,OLD.id) IS TRUE
    ) THEN RETURN OLD; END IF;
    INSERT INTO aap_jobs_v1.unsettled_worker_job_deletions(job_id) VALUES (OLD.id)
    ON CONFLICT (job_id) DO NOTHING;
    RETURN OLD;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.retain_unsettled_worker_deletion() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_worker_job_retain_deletion BEFORE DELETE ON public.async_jobs
FOR EACH ROW WHEN (OLD.kind IN ('prediction_retry','point_cloud_cross_frame'))
EXECUTE FUNCTION aap_jobs_v1.retain_unsettled_worker_deletion();
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.guard_fixture_dataset_jobs(p_dataset_ids uuid[])
RETURNS void LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='0A000', MESSAGE='fixture_dataset_isolation_unsupported';
    END IF;
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()),5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()),4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Jobs fixture cleanup requires a test database';
    END IF;
    IF p_dataset_ids IS NULL OR pg_catalog.array_position(p_dataset_ids,NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid Jobs fixture dataset scope';
    END IF;
    IF pg_catalog.cardinality(p_dataset_ids)=0 THEN RETURN; END IF;
    IF EXISTS (
        SELECT 1 FROM public.async_jobs AS job
        CROSS JOIN LATERAL (
            SELECT aap_jobs_v1.fixture_job_dataset_scope(
                job.id,job.kind,job.payload,job.project_id,job.user_id) AS value
        ) AS scope
        WHERE (scope.value ->> 'kind'='unknown'
           OR (scope.value ->> 'kind' IN ('dataset','datasets') AND EXISTS (
                SELECT 1 FROM pg_catalog.unnest(p_dataset_ids) AS dataset(id)
                WHERE dataset.id::text=scope.value ->> 'dataset_id'
                   OR (scope.value -> 'dataset_ids') ? dataset.id::text
           )))
          AND NOT EXISTS (
              SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
              WHERE proof.job_id=job.id AND proof.project_id=job.project_id
                AND proof.kind=job.kind AND proof.state='admitted'
          )
          AND NOT EXISTS (
              SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
              WHERE resource.kind='project' AND resource.resource_id=job.project_id
                AND resource.state='published'
                AND aap_jobs_v1.fixture_real_job_retired(resource.run_id,resource.resource_id,job.id) IS TRUE
          )
          AND NOT EXISTS (
              SELECT 1 FROM aap_jobs_v1.fixture_media_backfills AS binding
              WHERE binding.job_id=job.id AND job.kind='media_backfill' AND job.project_id IS NULL
                AND binding.dataset_id=ANY(p_dataset_ids)
                AND aap_jobs_v1.fixture_media_backfill_retired(binding.run_id,binding.dataset_id,job.id) IS TRUE
          )
    ) OR EXISTS (
        SELECT 1 FROM aap_jobs_v1.unsettled_worker_job_deletions AS orphan
        JOIN aap_jobs_v1.fixture_worker_job_datasets AS binding ON binding.job_id=orphan.job_id
        WHERE binding.dataset_ids && p_dataset_ids
          AND NOT EXISTS (SELECT 1 FROM public.async_jobs AS job WHERE job.id=orphan.job_id)
    ) OR EXISTS (
        SELECT 1 FROM public.image_export_scopes AS scope
        WHERE scope.dataset_ids && p_dataset_ids
          AND NOT EXISTS (SELECT 1 FROM public.async_jobs AS job WHERE job.id=scope.job_id)
          AND NOT EXISTS (
              SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS project
              WHERE project.kind='project' AND project.resource_id=scope.project_id
                AND public.fixture_export_execution_retired(project.run_id,project.resource_id,scope.job_id)
          )
    ) OR EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_backfills AS binding
        WHERE binding.dataset_id=ANY(p_dataset_ids)
          AND NOT EXISTS (SELECT 1 FROM public.async_jobs AS job WHERE job.id=binding.job_id)
          AND aap_jobs_v1.fixture_media_backfill_retired(binding.run_id,binding.dataset_id,binding.job_id)
              IS DISTINCT FROM true
    ) OR EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind='dataset' AND resource.state='published'
          AND resource.resource_id=ANY(p_dataset_ids)
          AND aap_assets_v1.fixture_dataset_media_retired(resource.run_id,resource.resource_id)
              IS DISTINCT FROM true
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_dataset_job_unsettled';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_dataset_jobs(uuid[]) FROM PUBLIC;
