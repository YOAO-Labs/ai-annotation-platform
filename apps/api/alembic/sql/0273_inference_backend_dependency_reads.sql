-- Fixed Learning-owned Sample Evaluation reference lookup used by registry deletion.
CREATE FUNCTION aap_learning_v1.registry_evaluation_reference_exists(
    p_registry_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF p_registry_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'registry_id must not be null';
    END IF;

    RETURN EXISTS (
        SELECT 1
        FROM public.sample_evaluation_runs AS evaluation
        JOIN public.trained_model_deployments AS deployment
          ON deployment.id = evaluation.deployment_id
        WHERE deployment.registry_id = p_registry_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_learning_v1.registry_evaluation_reference_exists(uuid)
    FROM PUBLIC;
-- aap-statement-break
COMMENT ON FUNCTION
    aap_learning_v1.registry_evaluation_reference_exists(uuid)
    IS 'Returns whether any Sample Evaluation run references a deployment on this registry, without status filtering.';
-- aap-statement-break
-- Fixed Jobs-owned count replacing the legacy API-side AsyncJob ID materialization.
CREATE FUNCTION aap_jobs_v1.count_running_registry_predictions(
    p_registry_id uuid
)
RETURNS bigint
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF p_registry_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'registry_id must not be null';
    END IF;

    RETURN (
        SELECT pg_catalog.count(*)
        FROM public.async_jobs AS job
        WHERE job.kind = 'batch_predict'
          AND job.payload ->> 'ml_backend_id' = p_registry_id::text
          AND job.status = 'running'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_jobs_v1.count_running_registry_predictions(uuid)
    FROM PUBLIC;
-- aap-statement-break
COMMENT ON FUNCTION
    aap_jobs_v1.count_running_registry_predictions(uuid)
    IS 'Counts running batch_predict jobs whose JSON payload names this registry UUID.';
