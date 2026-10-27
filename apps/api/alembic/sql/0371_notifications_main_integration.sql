CREATE OR REPLACE FUNCTION aap_notifications_v1._visible_inbox_rows(
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
               ) AS requires_export_capability,
               (
                   notification.target_type = 'async_job'
                   AND COALESCE(
                       job.kind,
                       notification.payload ->> 'kind'
                   ) IN (
                       'storage_migration_inventory',
                       'storage_migration_copy'
                   )
               ) AS is_migration_inventory
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
        WHEN p_platform_role = 'super_admin' THEN true
        ELSE
            NOT visible.is_migration_inventory
            AND (
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
CREATE OR REPLACE FUNCTION aap_notifications_v1._notification_is_migration_inventory(
    p_notification_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE((
        SELECT notification.target_type = 'async_job'
           AND COALESCE(job.kind, notification.payload ->> 'kind') IN (
               'storage_migration_inventory',
               'storage_migration_copy'
           )
        FROM aap_notifications_v1.notification_facts AS notification
        LEFT JOIN LATERAL (
            SELECT job_scope.kind
            FROM aap_jobs_v1.notification_scope_facts AS job_scope
            WHERE job_scope.id = notification.target_id
        ) AS job ON true
        WHERE notification.id = p_notification_id
    ), false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_notifications_v1._notification_is_migration_inventory(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_notifications_v1.read_state_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation text;
    v_user_id uuid;
    v_notification_id uuid;
    v_read_at timestamptz;
    v_updated_ids jsonb := '[]'::jsonb;
    v_updated integer := 0;
    v_super boolean;
BEGIN
    v_operation := p_request ->> 'operation';
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-notification-read-state/v1'
       OR v_operation IS NULL
       OR v_operation NOT IN ('mark_read', 'mark_all_read')
       OR NOT aap_notifications_v1._preference_object_keys_valid(
            p_request,
            CASE v_operation
                WHEN 'mark_read' THEN ARRAY[
                    'schema', 'operation', 'user_id', 'notification_id', 'read_at'
                ]
                ELSE ARRAY['schema', 'operation', 'user_id', 'read_at']
            END,
            CASE v_operation WHEN 'mark_read' THEN 5 ELSE 4 END,
            CASE v_operation WHEN 'mark_read' THEN 5 ELSE 4 END
       )
       OR pg_catalog.jsonb_typeof(p_request -> 'user_id') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'user_id'
                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE
       OR pg_catalog.jsonb_typeof(p_request -> 'read_at') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'read_at'
                ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?Z$',
            false
       ) IS NOT TRUE
       OR (
            v_operation = 'mark_read'
            AND (
                pg_catalog.jsonb_typeof(p_request -> 'notification_id')
                    IS DISTINCT FROM 'string'
                OR COALESCE(
                    p_request ->> 'notification_id'
                        ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
                    false
                ) IS NOT TRUE
            )
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-read-state/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_user_id := (p_request ->> 'user_id')::uuid;
    IF v_operation = 'mark_read' THEN
        v_notification_id := (p_request ->> 'notification_id')::uuid;
    END IF;
    BEGIN
        v_read_at := (p_request ->> 'read_at')::timestamptz;
    EXCEPTION WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-read-state/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END;
    SELECT actor.role = 'super_admin'
    INTO v_super
    FROM aap_identity_v1.account_profiles AS actor
    WHERE actor.id = v_user_id AND actor.is_active IS TRUE;
    v_super := COALESCE(v_super, false);

    WITH updated AS (
        UPDATE public.notifications AS notification
        SET read_at = v_read_at
        WHERE notification.user_id = v_user_id
          AND notification.read_at IS NULL
          AND (v_operation = 'mark_all_read' OR notification.id = v_notification_id)
          AND (
              v_super
              OR NOT aap_notifications_v1._notification_is_migration_inventory(
                  notification.id
              )
          )
        RETURNING notification.id
    )
    SELECT COALESCE(
               pg_catalog.jsonb_agg(updated.id ORDER BY updated.id),
               '[]'::jsonb
           )
    INTO v_updated_ids
    FROM updated;

    v_updated := pg_catalog.jsonb_array_length(v_updated_ids);
    IF v_operation = 'mark_read' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-read-state/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation', 'mark_read',
                'updated', v_updated = 1,
                'updated_ids', v_updated_ids
            )
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-notification-read-state/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation', 'mark_all_read',
            'updated', v_updated,
            'updated_ids', v_updated_ids
        )
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.read_state_command(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_notifications_v1.delete_command(p_request pg_catalog.jsonb)
RETURNS pg_catalog.jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation text;
    v_user_id uuid;
    v_notification_id uuid;
    v_deleted bigint := 0;
    v_super boolean;
BEGIN
    v_operation := p_request ->> 'operation';
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-notification-delete/v1'
       OR v_operation IS NULL
       OR v_operation NOT IN ('delete_one', 'clear_read')
       OR pg_catalog.jsonb_typeof(p_request -> 'user_id') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'user_id'
                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-delete/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_user_id := (p_request ->> 'user_id')::uuid;
    SELECT actor.role = 'super_admin'
    INTO v_super
    FROM aap_identity_v1.account_profiles AS actor
    WHERE actor.id = v_user_id AND actor.is_active IS TRUE;
    v_super := COALESCE(v_super, false);

    IF v_operation = 'delete_one' THEN
        IF NOT aap_notifications_v1._preference_object_keys_valid(
            p_request,
            ARRAY['schema', 'operation', 'user_id', 'notification_id'],
            4,
            4
        )
           OR pg_catalog.jsonb_typeof(p_request -> 'notification_id')
                IS DISTINCT FROM 'string'
           OR COALESCE(
                p_request ->> 'notification_id'
                    ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
                false
           ) IS NOT TRUE THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-notification-delete/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        v_notification_id := (p_request ->> 'notification_id')::uuid;
        DELETE FROM public.notifications AS notification
        WHERE notification.user_id = v_user_id
          AND notification.id = v_notification_id
          AND (
              v_super
              OR NOT aap_notifications_v1._notification_is_migration_inventory(
                  notification.id
              )
          );
        GET DIAGNOSTICS v_deleted = ROW_COUNT;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-delete/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation', 'delete_one',
                'deleted', v_deleted = 1
            )
        );
    END IF;

    IF NOT aap_notifications_v1._preference_object_keys_valid(
        p_request,
        ARRAY['schema', 'operation', 'user_id'],
        3,
        3
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-delete/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    DELETE FROM public.notifications AS notification
    WHERE notification.user_id = v_user_id
      AND notification.read_at IS NOT NULL
      AND (
          v_super
          OR NOT aap_notifications_v1._notification_is_migration_inventory(
              notification.id
          )
      );
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-notification-delete/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation', 'clear_read',
            'deleted', v_deleted
        )
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.delete_command(pg_catalog.jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_notifications_v1.read_state_command_scoped(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation text;
    v_user_id uuid;
    v_notification_id uuid;
    v_read_at timestamptz;
    v_updated_ids jsonb := '[]'::jsonb;
    v_updated integer := 0;
BEGIN
    v_operation := p_request ->> 'operation';
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-notification-read-state/v1'
       OR v_operation IS NULL
       OR v_operation NOT IN ('mark_read', 'mark_all_read')
       OR NOT aap_notifications_v1._preference_object_keys_valid(
            p_request,
            CASE v_operation
                WHEN 'mark_read' THEN ARRAY[
                    'schema', 'operation', 'user_id', 'notification_id', 'read_at'
                ]
                ELSE ARRAY['schema', 'operation', 'user_id', 'read_at']
            END,
            CASE v_operation WHEN 'mark_read' THEN 5 ELSE 4 END,
            CASE v_operation WHEN 'mark_read' THEN 5 ELSE 4 END
       )
       OR pg_catalog.jsonb_typeof(p_request -> 'user_id') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'user_id'
                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE
       OR pg_catalog.jsonb_typeof(p_request -> 'read_at') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'read_at'
                ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?Z$',
            false
       ) IS NOT TRUE
       OR (
            v_operation = 'mark_read'
            AND (
                pg_catalog.jsonb_typeof(p_request -> 'notification_id')
                    IS DISTINCT FROM 'string'
                OR COALESCE(
                    p_request ->> 'notification_id'
                        ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
                    false
                ) IS NOT TRUE
            )
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-read-state/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_user_id := (p_request ->> 'user_id')::uuid;
    IF v_operation = 'mark_read' THEN
        v_notification_id := (p_request ->> 'notification_id')::uuid;
    END IF;
    BEGIN
        v_read_at := (p_request ->> 'read_at')::timestamptz;
    EXCEPTION WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-read-state/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END;
    WITH updated AS (
        UPDATE public.notifications AS notification
        SET read_at = v_read_at
        WHERE notification.user_id = v_user_id
          AND notification.read_at IS NULL
          AND (v_operation = 'mark_all_read' OR notification.id = v_notification_id)
          AND NOT aap_notifications_v1._notification_is_migration_inventory(
                  notification.id
              )
        RETURNING notification.id
    )
    SELECT COALESCE(
               pg_catalog.jsonb_agg(updated.id ORDER BY updated.id),
               '[]'::jsonb
           )
    INTO v_updated_ids
    FROM updated;

    v_updated := pg_catalog.jsonb_array_length(v_updated_ids);
    IF v_operation = 'mark_read' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-read-state/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation', 'mark_read',
                'updated', v_updated = 1,
                'updated_ids', v_updated_ids
            )
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-notification-read-state/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation', 'mark_all_read',
            'updated', v_updated,
            'updated_ids', v_updated_ids
        )
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.read_state_command_scoped(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_notifications_v1.delete_command_scoped(p_request pg_catalog.jsonb)
RETURNS pg_catalog.jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation text;
    v_user_id uuid;
    v_notification_id uuid;
    v_deleted bigint := 0;
BEGIN
    v_operation := p_request ->> 'operation';
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-notification-delete/v1'
       OR v_operation IS NULL
       OR v_operation NOT IN ('delete_one', 'clear_read')
       OR pg_catalog.jsonb_typeof(p_request -> 'user_id') IS DISTINCT FROM 'string'
       OR COALESCE(
            p_request ->> 'user_id'
                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-delete/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_user_id := (p_request ->> 'user_id')::uuid;
    IF v_operation = 'delete_one' THEN
        IF NOT aap_notifications_v1._preference_object_keys_valid(
            p_request,
            ARRAY['schema', 'operation', 'user_id', 'notification_id'],
            4,
            4
        )
           OR pg_catalog.jsonb_typeof(p_request -> 'notification_id')
                IS DISTINCT FROM 'string'
           OR COALESCE(
                p_request ->> 'notification_id'
                    ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
                false
           ) IS NOT TRUE THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-notification-delete/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        v_notification_id := (p_request ->> 'notification_id')::uuid;
        DELETE FROM public.notifications AS notification
        WHERE notification.user_id = v_user_id
          AND notification.id = v_notification_id
          AND NOT aap_notifications_v1._notification_is_migration_inventory(
                  notification.id
              );
        GET DIAGNOSTICS v_deleted = ROW_COUNT;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-delete/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation', 'delete_one',
                'deleted', v_deleted = 1
            )
        );
    END IF;

    IF NOT aap_notifications_v1._preference_object_keys_valid(
        p_request,
        ARRAY['schema', 'operation', 'user_id'],
        3,
        3
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-notification-delete/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    DELETE FROM public.notifications AS notification
    WHERE notification.user_id = v_user_id
      AND notification.read_at IS NOT NULL
      AND NOT aap_notifications_v1._notification_is_migration_inventory(
                  notification.id
              );
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-notification-delete/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation', 'clear_read',
            'deleted', v_deleted
        )
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.delete_command_scoped(pg_catalog.jsonb) FROM PUBLIC;

-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_notifications_v1._visible_inbox_rows_scoped(
    p_actor_id uuid,
    p_platform_role text
)
RETURNS SETOF aap_notifications_v1.notification_facts
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT notification.*
    FROM aap_notifications_v1._visible_inbox_rows(
        p_actor_id,
        p_platform_role
    ) AS notification
    WHERE NOT aap_notifications_v1._notification_is_migration_inventory(
        notification.id
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_notifications_v1._visible_inbox_rows_scoped(uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_notifications_v1.inbox_page_scoped(
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
    FROM aap_notifications_v1._visible_inbox_rows_scoped(
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
    aap_notifications_v1.inbox_page_scoped(uuid, text, boolean, integer, integer)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_notifications_v1.inbox_total_count_scoped(
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
    FROM aap_notifications_v1._visible_inbox_rows_scoped(
        p_actor_id,
        p_platform_role
    ) AS notification
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_notifications_v1.inbox_total_count_scoped(uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_notifications_v1.inbox_unread_count_scoped(
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
    FROM aap_notifications_v1._visible_inbox_rows_scoped(
        p_actor_id,
        p_platform_role
    ) AS notification
    WHERE notification.read_at IS NULL
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_notifications_v1.inbox_unread_count_scoped(uuid, text)
    FROM PUBLIC;
