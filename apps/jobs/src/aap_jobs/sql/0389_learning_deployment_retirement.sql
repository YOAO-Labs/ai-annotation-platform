-- Jobs-owned bounded usage facts for Learning's locked deployment retirement.
-- Frozen references in every stage matter, not just the full-image root.
CREATE FUNCTION aap_jobs_v1.frozen_deployment_usage(
    p_deployment_id pg_catalog.uuid,
    p_registry_id pg_catalog.uuid,
    p_model_version_id pg_catalog.uuid
)
RETURNS TABLE(active_jobs pg_catalog.int8, retryable_failures pg_catalog.int8)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT
      (SELECT count(*) FROM public.async_jobs AS job
       WHERE job.status IN ('pending', 'running') AND (
         job.payload @> pg_catalog.jsonb_build_object('frozen_model_ref', pg_catalog.jsonb_build_object('deployment_id', p_deployment_id::text))
         OR (job.execution_snapshot_id IS NOT NULL
             AND aap_inference_pipelines_v1.frozen_snapshot_uses_deployment(
                 job.execution_snapshot_id, p_deployment_id))
       )),
      aap_labeling_v1.retryable_deployment_usage(
          p_deployment_id, p_registry_id, p_model_version_id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.frozen_deployment_usage(uuid, uuid, uuid) FROM PUBLIC;
