CREATE FUNCTION aap_jobs_v1._async_job_list_rows(
    p_actor_id uuid,
    p_platform_role text,
    p_statuses text[],
    p_kinds text[],
    p_project_id uuid,
    p_search_pattern text
)
RETURNS SETOF public.async_jobs
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH workspace_projects AS (
        SELECT project_scope.project_id, project_scope.owner_id
        FROM aap_workspace_project_query_v1.project_search_scope(
            p_actor_id,
            p_platform_role
        ) AS project_scope
    ),
    valid_memberships AS (
        SELECT membership.project_id, membership.member_role
        FROM aap_workspace_v1.actor_project_memberships(p_actor_id) AS membership
        WHERE p_platform_role IN (
                  'super_admin', 'project_admin', 'employee', 'viewer'
              )
          AND membership.member_role IN ('annotator', 'reviewer', 'viewer')
          AND (
              p_platform_role <> 'viewer'
              OR membership.member_role = 'viewer'
          )
    ),
    visible_projects AS (
        SELECT membership.project_id
        FROM valid_memberships AS membership
        UNION
        SELECT project_scope.project_id
        FROM workspace_projects AS project_scope
        WHERE p_platform_role = 'project_admin'
          AND project_scope.owner_id = p_actor_id
    ),
    export_projects AS (
        SELECT membership.project_id
        FROM valid_memberships AS membership
        WHERE membership.member_role = 'reviewer'
        UNION
        SELECT project_scope.project_id
        FROM workspace_projects AS project_scope
        WHERE p_platform_role = 'project_admin'
          AND project_scope.owner_id = p_actor_id
    )
    SELECT job.*
    FROM public.async_jobs AS job
    WHERE (
        COALESCE(job.project_id, job.retained_project_id) IS NULL
        OR EXISTS (
            SELECT 1
            FROM workspace_projects AS project_scope
            WHERE project_scope.project_id = COALESCE(
                job.project_id,
                job.retained_project_id
            )
        )
    )
      AND (
          COALESCE(job.project_id, job.retained_project_id) IS NOT NULL
          OR job.kind IN ('audit_archive', 'dataset_import')
      )
      AND (
          p_platform_role = 'super_admin'
          OR (
              job.user_id = p_actor_id
              AND (
                  (
                      job.kind <> 'export'
                      AND COALESCE(job.project_id, job.retained_project_id) IS NULL
                  )
                  OR COALESCE(job.project_id, job.retained_project_id) IN (
                      SELECT visible.project_id FROM visible_projects AS visible
                  )
              )
              AND (
                  job.kind <> 'export'
                  OR (
                      COALESCE(job.project_id, job.retained_project_id) IS NOT NULL
                      AND COALESCE(job.project_id, job.retained_project_id) IN (
                          SELECT visible.project_id FROM export_projects AS visible
                      )
                  )
              )
          )
      )
      AND (
          p_statuses IS NULL
          OR pg_catalog.cardinality(p_statuses) = 0
          OR job.status::text = ANY(p_statuses)
      )
      AND (
          p_kinds IS NULL
          OR pg_catalog.cardinality(p_kinds) = 0
          OR job.kind::text = ANY(p_kinds)
      )
      AND (
          p_project_id IS NULL
          OR COALESCE(job.project_id, job.retained_project_id) = p_project_id
      )
      AND (
          p_search_pattern IS NULL
          OR job.payload ->> 'prompt' ILIKE p_search_pattern
          OR job.payload ->> 'batch_display_id' ILIKE p_search_pattern
          OR job.payload ->> 'task_display_id' ILIKE p_search_pattern
          OR job.payload ->> 'model_key' ILIKE p_search_pattern
          OR job.payload ->> 'ml_backend_name' ILIKE p_search_pattern
          OR job.payload ->> 'error_type' ILIKE p_search_pattern
      )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._async_job_list_rows(
    uuid, text, text[], text[], uuid, text
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.async_job_list_page(
    p_actor_id uuid,
    p_platform_role text,
    p_statuses text[],
    p_kinds text[],
    p_project_id uuid,
    p_search_pattern text,
    p_limit integer,
    p_offset integer
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
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT job.id,
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
    FROM aap_jobs_v1._async_job_list_rows(
        p_actor_id,
        p_platform_role,
        p_statuses,
        p_kinds,
        p_project_id,
        p_search_pattern
    ) AS job
    ORDER BY job.created_at DESC, job.id DESC
    LIMIT p_limit
    OFFSET p_offset
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.async_job_list_page(
    uuid, text, text[], text[], uuid, text, integer, integer
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.async_job_list_total(
    p_actor_id uuid,
    p_platform_role text,
    p_statuses text[],
    p_kinds text[],
    p_project_id uuid,
    p_search_pattern text
)
RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.count(*)
    FROM aap_jobs_v1._async_job_list_rows(
        p_actor_id,
        p_platform_role,
        p_statuses,
        p_kinds,
        p_project_id,
        p_search_pattern
    ) AS job
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.async_job_list_total(
    uuid, text, text[], text[], uuid, text
) FROM PUBLIC;
