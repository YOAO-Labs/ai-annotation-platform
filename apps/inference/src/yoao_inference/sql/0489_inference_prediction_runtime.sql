CREATE FUNCTION yoao_inference_topology_v1.lock_video_prediction_registry(p_registry_id uuid)
RETURNS TABLE (state text, capabilities jsonb)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT backend.state::text, backend.health_meta->'capabilities'
    FROM public.ml_backend_registry AS backend
    WHERE backend.id = p_registry_id
    FOR SHARE NOWAIT
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_inference_topology_v1.lock_video_prediction_pool(
    p_project_id uuid, p_registry_id uuid, p_expected_pool_id uuid
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_pool_id uuid;
    v_traffic_state text;
    v_pool_enabled boolean;
    v_project_enabled boolean;
BEGIN
    SELECT member.pool_id, member.traffic_state INTO v_pool_id, v_traffic_state
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id
    FOR SHARE NOWAIT;
    IF v_traffic_state IS DISTINCT FROM 'active' THEN
        RETURN NULL;
    END IF;

    SELECT pool.enabled INTO v_pool_enabled
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = v_pool_id
    FOR SHARE NOWAIT;
    SELECT association.enabled INTO v_project_enabled
    FROM public.project_ml_backend_pool AS association
    WHERE association.project_id = p_project_id AND association.pool_id = v_pool_id
    FOR SHARE NOWAIT;

    IF v_pool_enabled IS TRUE AND v_project_enabled IS TRUE
       AND (p_expected_pool_id IS NULL OR p_expected_pool_id = v_pool_id) THEN
        RETURN v_pool_id;
    END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_inference_topology_v1.lock_native_prediction_route(
    p_project_id uuid, p_pool_id uuid, p_registry_id uuid
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_pool_enabled boolean;
    v_project_enabled boolean;
    v_traffic_state text;
BEGIN
    SELECT pool.enabled INTO v_pool_enabled
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR SHARE NOWAIT;
    SELECT association.enabled INTO v_project_enabled
    FROM public.project_ml_backend_pool AS association
    WHERE association.project_id = p_project_id AND association.pool_id = p_pool_id
    FOR SHARE NOWAIT;
    SELECT member.traffic_state INTO v_traffic_state
    FROM public.ml_backend_pool_members AS member
    WHERE member.pool_id = p_pool_id AND member.registry_id = p_registry_id
    FOR SHARE NOWAIT;
    PERFORM 1 FROM public.ml_backend_registry AS backend
    WHERE backend.id = p_registry_id
    FOR SHARE NOWAIT;

    RETURN FOUND AND v_pool_enabled IS TRUE AND v_project_enabled IS TRUE
        AND v_traffic_state IS NOT DISTINCT FROM 'active';
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_inference_pipelines_v1.create_prediction_execution_snapshot(
    p_project_id uuid, p_digest text, p_payload jsonb
) RETURNS uuid
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.prediction_execution_snapshots (id, project_id, schema_version, digest, payload)
    VALUES (pg_catalog.gen_random_uuid(), p_project_id, 1, p_digest, p_payload)
    RETURNING id
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_inference_pipelines_v1.read_prediction_execution_snapshot(
    p_snapshot_id uuid, p_project_id uuid
) RETURNS TABLE (payload jsonb, digest text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT snapshot.payload, snapshot.digest::text
    FROM public.prediction_execution_snapshots AS snapshot
    WHERE snapshot.id = p_snapshot_id AND snapshot.project_id IS NOT DISTINCT FROM p_project_id
      AND snapshot.schema_version = 1
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.prediction_execution_snapshot_in_use(p_snapshot_id uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.async_jobs AS job WHERE job.execution_snapshot_id = p_snapshot_id)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.prediction_execution_snapshot_in_use(p_snapshot_id uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.failed_predictions AS failure WHERE failure.execution_snapshot_id = p_snapshot_id)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_inference_pipelines_v1.prune_prediction_execution_snapshots(p_cutoff timestamptz)
RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    DELETE FROM public.prediction_execution_snapshots AS snapshot
    WHERE snapshot.created_at < p_cutoff
      AND NOT yoao_jobs_v1.prediction_execution_snapshot_in_use(snapshot.id)
      AND NOT yoao_labeling_v1.prediction_execution_snapshot_in_use(snapshot.id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_inference_topology_v1.lock_video_prediction_registry(uuid),
    yoao_inference_topology_v1.lock_video_prediction_pool(uuid,uuid,uuid),
    yoao_inference_topology_v1.lock_native_prediction_route(uuid,uuid,uuid),
    yoao_inference_pipelines_v1.create_prediction_execution_snapshot(uuid,text,jsonb),
    yoao_inference_pipelines_v1.read_prediction_execution_snapshot(uuid,uuid),
    yoao_inference_pipelines_v1.prune_prediction_execution_snapshots(timestamptz),
    yoao_jobs_v1.prediction_execution_snapshot_in_use(uuid),
    yoao_labeling_v1.prediction_execution_snapshot_in_use(uuid)
FROM PUBLIC;
