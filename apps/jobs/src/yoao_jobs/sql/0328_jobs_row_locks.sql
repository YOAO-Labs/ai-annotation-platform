CREATE FUNCTION aap_jobs_v1.read_job_for_update(p_job_id uuid)
RETURNS TABLE(
    id uuid,
    kind character varying,
    project_id uuid,
    retained_project_id uuid,
    user_id uuid,
    status character varying,
    progress_pct integer,
    payload jsonb,
    result jsonb,
    error_message text,
    celery_task_id character varying,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone,
    updated_at timestamp with time zone
)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT
        job.id,
        job.kind,
        job.project_id,
        job.retained_project_id,
        job.user_id,
        job.status,
        job.progress_pct,
        job.payload,
        job.result,
        job.error_message,
        job.celery_task_id,
        job.started_at,
        job.completed_at,
        job.created_at,
        job.updated_at
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id
    FOR UPDATE
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.read_job_for_update(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.read_job_for_share_nowait(p_job_id uuid)
RETURNS TABLE(
    id uuid,
    kind character varying,
    project_id uuid,
    retained_project_id uuid,
    user_id uuid,
    status character varying,
    progress_pct integer,
    payload jsonb,
    result jsonb,
    error_message text,
    celery_task_id character varying,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone,
    updated_at timestamp with time zone
)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT
        job.id,
        job.kind,
        job.project_id,
        job.retained_project_id,
        job.user_id,
        job.status,
        job.progress_pct,
        job.payload,
        job.result,
        job.error_message,
        job.celery_task_id,
        job.started_at,
        job.completed_at,
        job.created_at,
        job.updated_at
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id
    FOR SHARE NOWAIT
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.read_job_for_share_nowait(uuid) FROM PUBLIC;
-- aap-statement-break
