CREATE FUNCTION aap_jobs_v1.lock_cross_frame_singleflight(p_actor_id uuid, p_key text)
RETURNS void
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        'aap:cross-frame-singleflight:' || p_actor_id::pg_catalog.text || ':' || p_key,
        0
    ))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.lock_cross_frame_singleflight(uuid,text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.active_cross_frame_job(p_actor_id uuid, p_key text)
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
STABLE
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
    WHERE job.kind = 'point_cloud_cross_frame'
      AND job.user_id = p_actor_id
      AND job.status IN ('pending', 'running')
      AND job.payload ->> 'singleflight_key' = p_key
    ORDER BY job.created_at DESC
    LIMIT 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.active_cross_frame_job(uuid,text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.cross_frame_job_page(p_actor_id uuid, p_scene_id text, p_limit integer)
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
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 50 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid cross-frame page limit';
    END IF;
    RETURN QUERY
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
    WHERE job.kind = 'point_cloud_cross_frame'
      AND job.user_id = p_actor_id
      AND job.payload ->> 'scene_id' = p_scene_id
    ORDER BY job.created_at DESC
    LIMIT p_limit;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.cross_frame_job_page(uuid,text,integer) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.cross_frame_job_count(p_actor_id uuid, p_scene_id text)
RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.count(*)
    FROM public.async_jobs AS job
    WHERE job.kind = 'point_cloud_cross_frame'
      AND job.user_id = p_actor_id
      AND job.payload ->> 'scene_id' = p_scene_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.cross_frame_job_count(uuid,text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.latest_batch_prediction_times(p_project_ids uuid[])
RETURNS TABLE(project_id uuid, latest_created_at timestamp with time zone)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT job.project_id, pg_catalog.max(job.created_at)
    FROM public.async_jobs AS job
    WHERE job.kind = 'batch_predict'
      AND job.project_id = ANY(p_project_ids)
    GROUP BY job.project_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.latest_batch_prediction_times(uuid[]) FROM PUBLIC;
-- aap-statement-break
