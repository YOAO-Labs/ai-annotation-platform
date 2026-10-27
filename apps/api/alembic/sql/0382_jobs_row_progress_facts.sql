-- Forward extension: owner-read progress facts for an existing async-job row.
--
-- The frozen Jobs row capabilities (0326-0332) project the lifecycle columns
-- only. The merged AsyncJob model also stores last_progress_at and
-- progress_observation, so callers that hydrate a Job from a fixed row
-- capability re-read these two facts from the Jobs owner in the same
-- statement instead of reading the private async_jobs relation.
CREATE FUNCTION aap_jobs_v1.read_job_progress(p_job_id pg_catalog.uuid)
RETURNS TABLE(
    last_progress_at timestamp with time zone,
    progress_observation pg_catalog.jsonb
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT job.last_progress_at, job.progress_observation
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.read_job_progress(pg_catalog.uuid) FROM PUBLIC;
