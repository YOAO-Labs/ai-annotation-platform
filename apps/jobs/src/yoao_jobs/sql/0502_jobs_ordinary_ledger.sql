CREATE FUNCTION yoao_jobs_v1.dataset_job_visible(p_actor uuid, p_job uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.async_jobs AS job
        WHERE job.id = p_job
          AND CASE job.kind
              WHEN 'dataset_scan' THEN yoao_assets_v1.directory_job_visible(
                  p_actor, job.kind, job.id, job.payload ->> 'run_id', NULL)
              WHEN 'dataset_registration' THEN yoao_assets_v1.directory_job_visible(
                  p_actor, job.kind, job.id, job.payload ->> 'registration_id', NULL)
              WHEN 'dataset_quality' THEN yoao_assets_v1.dataset_quality_job_visible(
                  p_actor, job.kind, job.payload ->> 'run_id')
              WHEN 'dataset_quality_review' THEN yoao_assets_v1.dataset_quality_job_visible(
                  p_actor, job.kind, job.payload ->> 'batch_id')
              ELSE false
          END
    )
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1._directory_job_ids(p_actor uuid, p_location uuid)
RETURNS SETOF uuid
LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH visible_runs AS MATERIALIZED (
        SELECT run.kind, run.run_id, run.async_job_id
        FROM yoao_assets_v1.directory_location_job_runs(p_actor, p_location) AS run
        WHERE p_location IS NOT NULL
    )
    SELECT job.id
    FROM visible_runs AS run
    JOIN public.async_jobs AS job ON job.id = run.async_job_id AND job.kind = run.kind
    UNION
    SELECT job.id
    FROM visible_runs AS run
    JOIN public.async_jobs AS job
      ON job.kind = 'dataset_scan' AND job.payload ->> 'run_id' = run.run_id
    WHERE run.kind = 'dataset_scan'
    UNION
    SELECT job.id
    FROM visible_runs AS run
    JOIN public.async_jobs AS job
      ON job.kind = 'dataset_registration' AND job.payload ->> 'registration_id' = run.run_id
    WHERE run.kind = 'dataset_registration'
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.directory_job_page(
    p_actor uuid, p_location uuid, p_limit integer, p_offset integer
)
RETURNS TABLE(id uuid, kind character varying, status character varying,
              created_at timestamptz, completed_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT job.id, job.kind, job.status, job.created_at, job.completed_at
    FROM yoao_jobs_v1._directory_job_ids(p_actor, p_location) AS visible(id)
    JOIN public.async_jobs AS job ON job.id = visible.id
    ORDER BY job.created_at DESC, job.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 200 AND p_offset >= 0 THEN p_limit ELSE 0 END
    OFFSET greatest(p_offset, 0)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.directory_job_total(p_actor uuid, p_location uuid)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.count(*)
    FROM yoao_jobs_v1._directory_job_ids(p_actor, p_location)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.dataset_active_job_facts(p_dataset uuid)
RETURNS TABLE(id uuid, kind character varying, status character varying)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT job.id, job.kind, job.status FROM public.async_jobs AS job
    WHERE job.status IN ('pending', 'running')
      AND job.payload ->> 'dataset_id' = p_dataset::text
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.active_storage_writer_job_counts()
RETURNS TABLE(kind character varying, status character varying, count bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT job.kind, job.status, pg_catalog.count(*) FROM public.async_jobs AS job
    WHERE job.status IN ('pending', 'running')
    GROUP BY job.kind, job.status
    ORDER BY job.kind, job.status
    LIMIT 101
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.settle_dataset_scan_job(
    p_job uuid, p_task text, p_status text, p_completed_at timestamptz, p_code text, p_result jsonb
)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH applied AS (
        UPDATE public.async_jobs AS job
        SET status = p_status, progress_pct = CASE WHEN p_status = 'completed' THEN 100 ELSE 0 END,
            error_message = p_code, completed_at = p_completed_at, result = p_result,
            updated_at = pg_catalog.now()
        WHERE job.id = p_job AND job.kind = 'dataset_scan'
          AND job.celery_task_id IS NOT DISTINCT FROM p_task
          AND p_status IN ('completed', 'failed', 'cancelled')
          AND p_completed_at IS NOT NULL AND p_result IS NOT NULL
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM applied)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.transition_directory_registration_job(
    p_job uuid, p_run uuid, p_task text, p_transition text, p_next_task text,
    p_stamp timestamptz, p_code text, p_result jsonb
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    CASE
        WHEN p_transition = 'claim' AND p_stamp IS NOT NULL THEN
            UPDATE public.async_jobs AS job
            SET status = 'running', started_at = COALESCE(job.started_at, p_stamp),
                updated_at = pg_catalog.now()
            WHERE job.id = p_job AND job.kind = 'dataset_registration'
              AND job.payload ->> 'registration_id' = p_run::text
              AND job.celery_task_id IS NOT DISTINCT FROM p_task
              AND job.status = 'pending';
        WHEN p_transition = 'requeue' AND p_next_task <> ''
             AND p_next_task IS DISTINCT FROM p_task THEN
            UPDATE public.async_jobs AS job
            SET status = 'pending', celery_task_id = p_next_task,
                updated_at = pg_catalog.now()
            WHERE job.id = p_job AND job.kind = 'dataset_registration'
              AND job.payload ->> 'registration_id' = p_run::text
              AND job.celery_task_id IS NOT DISTINCT FROM p_task
              AND job.status = 'running';
        WHEN p_transition IN ('completed', 'failed', 'cancelled')
             AND p_stamp IS NOT NULL AND p_result IS NOT NULL THEN
            UPDATE public.async_jobs AS job
            SET status = p_transition,
                progress_pct = CASE WHEN p_transition = 'completed' THEN 100 ELSE job.progress_pct END,
                error_message = p_code, completed_at = p_stamp, result = p_result,
                updated_at = pg_catalog.now()
            WHERE job.id = p_job AND job.kind = 'dataset_registration'
              AND job.payload ->> 'registration_id' = p_run::text
              AND job.celery_task_id IS NOT DISTINCT FROM p_task;
        ELSE RETURN false;
    END CASE;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_jobs_v1.dataset_job_visible(uuid,uuid),
    yoao_jobs_v1._directory_job_ids(uuid,uuid),
    yoao_jobs_v1.directory_job_page(uuid,uuid,integer,integer),
    yoao_jobs_v1.directory_job_total(uuid,uuid),
    yoao_jobs_v1.dataset_active_job_facts(uuid),
    yoao_jobs_v1.active_storage_writer_job_counts(),
    yoao_jobs_v1.settle_dataset_scan_job(uuid,text,text,timestamptz,text,jsonb),
    yoao_jobs_v1.transition_directory_registration_job(uuid,uuid,text,text,text,timestamptz,text,jsonb)
FROM PUBLIC;
