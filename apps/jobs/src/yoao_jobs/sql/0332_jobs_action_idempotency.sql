CREATE FUNCTION aap_jobs_v1.lock_action_idempotency(
    p_action text, p_project_id uuid, p_actor_id uuid, p_key text
)
RETURNS void
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
    SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        'aap:data-manager:' || COALESCE(p_action, 'None') || ':' ||
        COALESCE(p_project_id::text, 'None') || ':' ||
        COALESCE(p_actor_id::text, 'None') || ':' || COALESCE(p_key, 'None'), 0
    ))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.lock_action_idempotency(text,uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.find_action_idempotent_jobs(
    p_kind text, p_project_id uuid, p_actor_id uuid, p_key text
)
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
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
BEGIN
    -- Keep the original NULL predicates and indexable equality query shapes.
    RETURN QUERY EXECUTE pg_catalog.format($query$
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
        WHERE %s AND %s AND %s AND %s
        ORDER BY job.created_at DESC, job.id DESC
        FOR UPDATE
    $query$,
        CASE WHEN p_kind IS NULL THEN 'job.kind IS NULL' ELSE 'job.kind = $1' END,
        CASE WHEN p_project_id IS NULL THEN 'job.project_id IS NULL' ELSE 'job.project_id = $2' END,
        CASE WHEN p_actor_id IS NULL THEN 'job.user_id IS NULL' ELSE 'job.user_id = $3' END,
        CASE WHEN p_key IS NULL THEN '(job.payload ->> ''data_manager_idempotency_key'') IS NULL'
             ELSE 'job.payload ->> ''data_manager_idempotency_key'' = $4' END
    ) USING p_kind, p_project_id, p_actor_id, p_key;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.find_action_idempotent_jobs(text,uuid,uuid,text) FROM PUBLIC;
