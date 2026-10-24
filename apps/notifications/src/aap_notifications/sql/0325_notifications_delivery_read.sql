CREATE FUNCTION aap_notifications_v1.read_notification(
    p_notification_id uuid
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
    FROM aap_notifications_v1.notification_facts AS notification
    WHERE notification.id = p_notification_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.read_notification(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.find_terminal_notification(
    p_user_id uuid,
    p_type text,
    p_target_id uuid
)
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT notification.id
    FROM aap_notifications_v1.notification_facts AS notification
    WHERE notification.user_id = p_user_id
      AND notification.type = p_type
      AND notification.target_type = 'async_job'
      AND notification.target_id = p_target_id
    LIMIT 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_notifications_v1.find_terminal_notification(uuid, text, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.delivery_job_scopes(
    p_job_ids uuid[]
)
RETURNS TABLE(id uuid, scope_project_id uuid, kind varchar(40))
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT job_scope.id, job_scope.scope_project_id, job_scope.kind
    FROM aap_jobs_v1.notification_scope_facts AS job_scope
    WHERE job_scope.id = ANY(p_job_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.delivery_job_scopes(uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.delivery_member_pairs(
    p_pairs jsonb
)
RETURNS TABLE(project_id uuid, user_id uuid)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_pairs IS NULL
       OR pg_catalog.jsonb_typeof(p_pairs) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Notifications delivery pair array';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(p_pairs) AS item(value)
        WHERE CASE
            WHEN pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'object'
                THEN true
            ELSE pg_catalog.jsonb_object_length(item.value) <> 2
              OR NOT (item.value ?& ARRAY['project_id', 'user_id']::text[])
              OR pg_catalog.jsonb_typeof(item.value->'project_id') IS DISTINCT FROM 'string'
              OR pg_catalog.jsonb_typeof(item.value->'user_id') IS DISTINCT FROM 'string'
              OR (item.value->>'project_id') !~
                 '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
              OR (item.value->>'user_id') !~
                 '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        END
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Notifications delivery pair';
    END IF;

    RETURN QUERY
    WITH requested_pairs AS (
        SELECT DISTINCT
               (item.value->>'project_id')::uuid AS project_id,
               (item.value->>'user_id')::uuid AS user_id
        FROM pg_catalog.jsonb_array_elements(p_pairs) AS item(value)
    )
    SELECT requested.project_id, requested.user_id
    FROM requested_pairs AS requested
    JOIN aap_identity_v1.account_profiles AS actor
      ON actor.id = requested.user_id
    CROSS JOIN LATERAL aap_workspace_v1.actor_project_memberships(actor.id)
        AS membership
    WHERE membership.project_id = requested.project_id
      AND actor.is_active IS TRUE
      AND actor.role IN ('super_admin', 'project_admin', 'employee', 'viewer')
      AND membership.member_role IN ('annotator', 'reviewer', 'viewer')
      AND (
          actor.role <> 'viewer'
          OR membership.member_role = 'viewer'
      );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.delivery_member_pairs(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.delivery_reviewer_pairs(
    p_pairs jsonb
)
RETURNS TABLE(project_id uuid, user_id uuid)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_pairs IS NULL
       OR pg_catalog.jsonb_typeof(p_pairs) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Notifications delivery pair array';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(p_pairs) AS item(value)
        WHERE CASE
            WHEN pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'object'
                THEN true
            ELSE pg_catalog.jsonb_object_length(item.value) <> 2
              OR NOT (item.value ?& ARRAY['project_id', 'user_id']::text[])
              OR pg_catalog.jsonb_typeof(item.value->'project_id') IS DISTINCT FROM 'string'
              OR pg_catalog.jsonb_typeof(item.value->'user_id') IS DISTINCT FROM 'string'
              OR (item.value->>'project_id') !~
                 '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
              OR (item.value->>'user_id') !~
                 '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        END
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Notifications delivery pair';
    END IF;

    RETURN QUERY
    WITH requested_pairs AS (
        SELECT DISTINCT
               (item.value->>'project_id')::uuid AS project_id,
               (item.value->>'user_id')::uuid AS user_id
        FROM pg_catalog.jsonb_array_elements(p_pairs) AS item(value)
    )
    SELECT requested.project_id, requested.user_id
    FROM requested_pairs AS requested
    JOIN aap_identity_v1.account_profiles AS actor
      ON actor.id = requested.user_id
    CROSS JOIN LATERAL aap_workspace_v1.actor_project_memberships(actor.id)
        AS membership
    WHERE membership.project_id = requested.project_id
      AND actor.is_active IS TRUE
      AND actor.role IN ('super_admin', 'project_admin', 'employee', 'viewer')
      AND membership.member_role IN ('annotator', 'reviewer', 'viewer')
      AND (
          actor.role <> 'viewer'
          OR membership.member_role = 'viewer'
      )
      AND membership.member_role = 'reviewer';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.delivery_reviewer_pairs(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.delivery_owner_pairs(
    p_pairs jsonb
)
RETURNS TABLE(project_id uuid, user_id uuid)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_pairs IS NULL
       OR pg_catalog.jsonb_typeof(p_pairs) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Notifications delivery pair array';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(p_pairs) AS item(value)
        WHERE CASE
            WHEN pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'object'
                THEN true
            ELSE pg_catalog.jsonb_object_length(item.value) <> 2
              OR NOT (item.value ?& ARRAY['project_id', 'user_id']::text[])
              OR pg_catalog.jsonb_typeof(item.value->'project_id') IS DISTINCT FROM 'string'
              OR pg_catalog.jsonb_typeof(item.value->'user_id') IS DISTINCT FROM 'string'
              OR (item.value->>'project_id') !~
                 '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
              OR (item.value->>'user_id') !~
                 '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        END
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Notifications delivery pair';
    END IF;

    RETURN QUERY
    WITH requested_pairs AS (
        SELECT DISTINCT
               (item.value->>'project_id')::uuid AS project_id,
               (item.value->>'user_id')::uuid AS user_id
        FROM pg_catalog.jsonb_array_elements(p_pairs) AS item(value)
    )
    SELECT requested.project_id, requested.user_id
    FROM requested_pairs AS requested
    JOIN aap_identity_v1.account_profiles AS actor
      ON actor.id = requested.user_id
    CROSS JOIN LATERAL aap_workspace_project_query_v1.project_search_scope(
        actor.id, COALESCE(actor.role, '')
    ) AS project_scope
    WHERE project_scope.project_id = requested.project_id
      AND project_scope.owner_id = actor.id
      AND actor.is_active IS TRUE
      AND actor.role IN ('super_admin', 'project_admin');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.delivery_owner_pairs(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.delivery_super_pairs(
    p_pairs jsonb
)
RETURNS TABLE(project_id uuid, user_id uuid)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_pairs IS NULL
       OR pg_catalog.jsonb_typeof(p_pairs) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Notifications delivery pair array';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(p_pairs) AS item(value)
        WHERE CASE
            WHEN pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'object'
                THEN true
            ELSE pg_catalog.jsonb_object_length(item.value) <> 2
              OR NOT (item.value ?& ARRAY['project_id', 'user_id']::text[])
              OR pg_catalog.jsonb_typeof(item.value->'project_id') IS DISTINCT FROM 'string'
              OR pg_catalog.jsonb_typeof(item.value->'user_id') IS DISTINCT FROM 'string'
              OR (item.value->>'project_id') !~
                 '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
              OR (item.value->>'user_id') !~
                 '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        END
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Notifications delivery pair';
    END IF;

    RETURN QUERY
    WITH requested_pairs AS (
        SELECT DISTINCT
               (item.value->>'project_id')::uuid AS project_id,
               (item.value->>'user_id')::uuid AS user_id
        FROM pg_catalog.jsonb_array_elements(p_pairs) AS item(value)
    )
    SELECT requested.project_id, requested.user_id
    FROM requested_pairs AS requested
    JOIN aap_identity_v1.account_profiles AS actor
      ON actor.id = requested.user_id
    CROSS JOIN LATERAL aap_workspace_project_query_v1.project_search_scope(
        actor.id, COALESCE(actor.role, '')
    ) AS project_scope
    WHERE project_scope.project_id = requested.project_id
      AND actor.is_active IS TRUE
      AND actor.role = 'super_admin';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.delivery_super_pairs(jsonb)
    FROM PUBLIC;
