-- Jobs-owned bounded usage facts for Learning's locked deployment retirement.
-- Frozen references in every stage matter, not just the full-image root.
CREATE FUNCTION aap_jobs_v1.frozen_deployment_usage(
    p_deployment_id pg_catalog.uuid,
    p_registry_id pg_catalog.uuid,
    p_model_version_id pg_catalog.uuid
)
RETURNS TABLE(active_jobs pg_catalog.bigint, retryable_failures pg_catalog.bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT
      (SELECT count(*) FROM public.async_jobs AS job
       LEFT JOIN public.prediction_execution_snapshots AS snapshot
         ON snapshot.id = job.execution_snapshot_id
       WHERE job.status IN ('pending', 'running') AND (
         job.payload @> pg_catalog.jsonb_build_object('frozen_model_ref', pg_catalog.jsonb_build_object('deployment_id', p_deployment_id::text))
         OR snapshot.payload @> pg_catalog.jsonb_build_object('stages', pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object('frozen_model_ref', pg_catalog.jsonb_build_object('deployment_id', p_deployment_id::text))))
       )),
      (SELECT count(*) FROM public.failed_predictions AS failure
       LEFT JOIN public.prediction_execution_snapshots AS snapshot
         ON snapshot.id = failure.execution_snapshot_id
         OR snapshot.id::text = failure.extra #>> '{execution_snapshot,id}'
       WHERE failure.dismissed_at IS NULL AND (
         failure.extra @> pg_catalog.jsonb_build_object('request_context', pg_catalog.jsonb_build_object('frozen_model_ref', pg_catalog.jsonb_build_object('deployment_id', p_deployment_id::text)))
         OR snapshot.payload @> pg_catalog.jsonb_build_object('stages', pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object('frozen_model_ref', pg_catalog.jsonb_build_object('deployment_id', p_deployment_id::text))))
         OR (failure.ml_backend_id = p_registry_id AND (
             failure.extra #>> '{request_context,frozen_model_ref,model_version_id}' = p_model_version_id::text
             OR failure.model_version = 'trained:' || p_model_version_id::text
         ))
       ))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.frozen_deployment_usage(uuid, uuid, uuid) FROM PUBLIC;
