-- Labeling owns retry eligibility and legacy failure references.
CREATE FUNCTION aap_labeling_v1.retryable_deployment_usage(
    p_deployment_id uuid, p_registry_id uuid, p_model_version_id uuid
)
RETURNS bigint
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT count(*) FROM public.failed_predictions AS failure
    WHERE failure.dismissed_at IS NULL AND (
        failure.extra @> pg_catalog.jsonb_build_object(
            'request_context', pg_catalog.jsonb_build_object(
                'frozen_model_ref', pg_catalog.jsonb_build_object(
                    'deployment_id', p_deployment_id::text)))
        OR (failure.execution_snapshot_id IS NOT NULL
            AND aap_inference_pipelines_v1.frozen_snapshot_uses_deployment(
                failure.execution_snapshot_id, p_deployment_id))
        OR CASE WHEN (failure.extra #>> '{execution_snapshot,id}') ~
                '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN aap_inference_pipelines_v1.frozen_snapshot_uses_deployment(
                (failure.extra #>> '{execution_snapshot,id}')::uuid, p_deployment_id)
            ELSE false END
        OR (failure.ml_backend_id = p_registry_id AND (
            failure.extra #>> '{request_context,frozen_model_ref,model_version_id}' = p_model_version_id::text
            OR failure.model_version = 'trained:' || p_model_version_id::text
        ))
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.retryable_deployment_usage(uuid,uuid,uuid) FROM PUBLIC;
