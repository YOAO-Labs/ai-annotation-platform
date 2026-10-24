CREATE FUNCTION aap_notifications_v1._visible_inbox_rows(
    p_actor_id uuid,
    p_platform_role text
)
RETURNS SETOF aap_notifications_v1.notification_facts
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH actor_facts AS (
        SELECT actor.role, actor.is_active
        FROM aap_identity_v1.account_profiles AS actor
        WHERE actor.id = p_actor_id
    ),
    valid_memberships AS (
        SELECT membership.project_id, membership.member_role
        FROM aap_workspace_v1.actor_project_memberships(p_actor_id) AS membership
        JOIN actor_facts AS actor ON true
        WHERE actor.is_active IS TRUE
          AND actor.role IN ('super_admin', 'project_admin', 'employee', 'viewer')
          AND membership.member_role IN ('annotator', 'reviewer', 'viewer')
          AND (
              actor.role <> 'viewer'
              OR membership.member_role = 'viewer'
          )
    ),
    owned_projects AS (
        SELECT project_scope.project_id
        FROM aap_workspace_project_query_v1.project_search_scope(
            p_actor_id,
            p_platform_role
        ) AS project_scope
        WHERE project_scope.owner_id = p_actor_id
    ),
    scoped_notifications AS (
        SELECT notification.id,
               notification.user_id,
               notification.type,
               notification.target_type,
               notification.target_id,
               notification.payload,
               notification.read_at,
               notification.created_at,
               job.id AS job_id,
               job.kind AS job_kind,
               job.scope_project_id AS job_project_id,
               CASE
                   WHEN notification.target_type IN ('export', 'async_job')
                   THEN job.scope_project_id::text
                   ELSE notification.payload ->> 'project_id'
               END AS effective_project_text,
               (
                   notification.target_type = 'export'
                   OR (
                       notification.target_type = 'async_job'
                       AND (
                           job.scope_project_id IS NOT NULL
                           OR job.id IS NULL
                           OR job.kind NOT IN ('audit_archive', 'dataset_import')
                       )
                   )
                   OR (
                       notification.target_type NOT IN ('export', 'async_job')
                       AND notification.payload ->> 'project_id' IS NOT NULL
                   )
               ) AS is_restricted,
               (
                   notification.target_type = 'export'
                   OR (
                       notification.target_type = 'async_job'
                       AND job.kind = 'export'
                   )
               ) AS requires_export_capability
        FROM aap_notifications_v1.notification_facts AS notification
        LEFT JOIN LATERAL (
            SELECT job_scope.id, job_scope.kind, job_scope.scope_project_id
            FROM aap_jobs_v1.notification_scope_facts AS job_scope
            WHERE job_scope.id = notification.target_id
              AND notification.target_type IN ('export', 'async_job')
        ) AS job ON true
        WHERE notification.user_id = p_actor_id
    )
    SELECT visible.id,
           visible.user_id,
           visible.type,
           visible.target_type,
           visible.target_id,
           visible.payload,
           visible.read_at,
           visible.created_at
    FROM scoped_notifications AS visible
    WHERE CASE
        WHEN p_platform_role = 'super_admin' THEN
            visible.target_type NOT IN ('export', 'async_job')
            OR (
                visible.job_id IS NOT NULL
                AND (
                    (
                        visible.target_type = 'async_job'
                        AND visible.job_project_id IS NULL
                        AND visible.job_kind IN ('audit_archive', 'dataset_import')
                    )
                    OR (
                        visible.job_project_id IS NOT NULL
                        AND EXISTS (
                            SELECT 1
                            FROM aap_workspace_project_query_v1.project_search_scope(
                                p_actor_id,
                                p_platform_role
                            ) AS project_scope
                            WHERE project_scope.project_id = visible.job_project_id
                        )
                    )
                )
            )
        ELSE
            (
                NOT visible.is_restricted
                OR visible.effective_project_text IN (
                    SELECT membership.project_id::text
                    FROM valid_memberships AS membership
                    UNION
                    SELECT owned.project_id::text
                    FROM owned_projects AS owned
                    WHERE p_platform_role = 'project_admin'
                )
            )
            AND (
                NOT visible.requires_export_capability
                OR visible.effective_project_text IN (
                    SELECT membership.project_id::text
                    FROM valid_memberships AS membership
                    WHERE membership.member_role = 'reviewer'
                    UNION
                    SELECT owned.project_id::text
                    FROM owned_projects AS owned
                    WHERE p_platform_role = 'project_admin'
                )
            )
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1._visible_inbox_rows(uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.inbox_page(
    p_actor_id uuid,
    p_platform_role text,
    p_unread_only boolean,
    p_limit integer,
    p_offset integer
)
RETURNS SETOF aap_notifications_v1.notification_facts
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT notification.id,
           notification.user_id,
           notification.type,
           notification.target_type,
           notification.target_id,
           notification.payload,
           notification.read_at,
           notification.created_at
    FROM aap_notifications_v1._visible_inbox_rows(
        p_actor_id,
        p_platform_role
    ) AS notification
    WHERE NOT p_unread_only OR notification.read_at IS NULL
    ORDER BY notification.created_at DESC
    LIMIT p_limit
    OFFSET p_offset
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_notifications_v1.inbox_page(uuid, text, boolean, integer, integer)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.inbox_total_count(
    p_actor_id uuid,
    p_platform_role text
)
RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.count(*)
    FROM aap_notifications_v1._visible_inbox_rows(
        p_actor_id,
        p_platform_role
    ) AS notification
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_notifications_v1.inbox_total_count(uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.inbox_unread_count(
    p_actor_id uuid,
    p_platform_role text
)
RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.count(*)
    FROM aap_notifications_v1._visible_inbox_rows(
        p_actor_id,
        p_platform_role
    ) AS notification
    WHERE notification.read_at IS NULL
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_notifications_v1.inbox_unread_count(uuid, text)
    FROM PUBLIC;
