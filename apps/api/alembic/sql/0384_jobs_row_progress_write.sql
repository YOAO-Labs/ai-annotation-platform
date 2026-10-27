-- Forward extension: Jobs-owned write of the storage progress facts.
--
-- The merged AsyncJob model stores last_progress_at and progress_observation,
-- but the frozen lifecycle capability (0327) rejects them and the runtime role
-- keeps no direct SELECT FOR UPDATE or UPDATE on the private async_jobs
-- relation. Storage owners keep their marker/sequence policy in application
-- code and use these two narrow capabilities instead:
--
--   read_job_progress_for_update  fence + lock the current running execution
--   record_job_progress           write only the two observation columns
--
-- Neither capability touches execution_snapshot_id; that binding stays with
-- aap_jobs_v1.bind_prediction_execution_snapshot.
CREATE FUNCTION aap_jobs_v1.read_job_progress_for_update(
    p_job_id pg_catalog.uuid,
    p_kind pg_catalog.text,
    p_task_id pg_catalog.text
)
RETURNS TABLE(
    last_progress_at timestamp with time zone,
    progress_observation pg_catalog.jsonb,
    observed_at timestamp with time zone
)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT
        job.last_progress_at,
        job.progress_observation,
        pg_catalog.clock_timestamp()
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id
      AND job.kind = p_kind
      AND job.status = 'running'
      AND job.celery_task_id = p_task_id
    FOR UPDATE
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.read_job_progress_for_update(
    pg_catalog.uuid,
    pg_catalog.text,
    pg_catalog.text
) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.record_job_progress(
    p_job_id pg_catalog.uuid,
    p_kind pg_catalog.text,
    p_task_id pg_catalog.text,
    p_last_progress_at timestamp with time zone,
    p_observation pg_catalog.jsonb
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_changed integer;
BEGIN
    IF p_job_id IS NULL
       OR p_kind IS NULL
       OR p_task_id IS NULL
       OR p_last_progress_at IS NULL
       OR pg_catalog.jsonb_typeof(p_observation) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid_jobs_progress_request';
    END IF;

    UPDATE public.async_jobs AS job
    SET last_progress_at = p_last_progress_at,
        progress_observation = p_observation,
        updated_at = pg_catalog.now()
    WHERE job.id = p_job_id
      AND job.kind = p_kind
      AND job.status = 'running'
      AND job.celery_task_id = p_task_id;

    GET DIAGNOSTICS v_changed = ROW_COUNT;
    RETURN v_changed = 1;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.record_job_progress(
    pg_catalog.uuid,
    pg_catalog.text,
    pg_catalog.text,
    timestamp with time zone,
    pg_catalog.jsonb
) FROM PUBLIC;
