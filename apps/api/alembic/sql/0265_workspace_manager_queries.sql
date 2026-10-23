-- Extend the existing fixed Project query entry point for manager authorization reads.
CREATE OR REPLACE FUNCTION aap_workspace_project_query_v1.project_query_step(
    p_request jsonb,
    p_action text,
    p_parameters jsonb,
    p_status text,
    p_search text,
    p_type_keys text[],
    p_data_types text[],
    p_member_id uuid,
    p_project_role text,
    p_created_from timestamptz,
    p_created_to timestamptz,
    p_offset bigint,
    p_limit integer
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_reply jsonb;
    v_member jsonb;
    v_assigned_at timestamptz;
    v_updated_at timestamptz;
    v_actor_id uuid;
    v_target_id uuid;
    v_normalized_request jsonb;
    v_facts jsonb;
    v_rows_json text;
BEGIN
    IF p_request->>'operation' IN (
        'get_project_access', 'require_project_manager'
    ) THEN
        IF p_action IS NULL OR p_action NOT IN (
            'get_row', 'actor_status', 'access_member_rows'
        )
           OR NOT aap_workspace_project_query_v1._has_exact_keys(
               p_request,
               ARRAY[
                   'schema', 'request_id', 'operation', 'admitted_actor', 'target_id',
                   'filters_ascii', 'pagination', 'projection_evidence'
               ]::text[]
           )
           OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
           OR p_request->>'schema' IS DISTINCT FROM 'aap-workspace-project-query/v1'
           OR pg_catalog.jsonb_typeof(p_request->'request_id') IS DISTINCT FROM 'string'
           OR (p_request->>'request_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
           OR p_request->>'operation' NOT IN (
               'get_project_access', 'require_project_manager'
           )
           OR NOT aap_workspace_project_query_v1._has_exact_keys(
               p_request->'admitted_actor', ARRAY['id', 'role']::text[]
           )
           OR pg_catalog.jsonb_typeof(p_request #> '{admitted_actor,id}') IS DISTINCT FROM 'string'
           OR (p_request #>> '{admitted_actor,id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request #> '{admitted_actor,role}') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request->'target_id') IS DISTINCT FROM 'string'
           OR (p_request->>'target_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request->'filters_ascii') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_request->'pagination') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_request->'projection_evidence') IS DISTINCT FROM 'null'
           OR p_parameters IS NOT NULL
           OR p_status IS NOT NULL OR p_search IS NOT NULL
           OR p_type_keys IS NOT NULL OR p_data_types IS NOT NULL
           OR p_member_id IS NOT NULL OR p_project_role IS NOT NULL
           OR p_created_from IS NOT NULL OR p_created_to IS NOT NULL
           OR p_offset IS NOT NULL OR p_limit IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace Project access query step';
        END IF;

        v_actor_id := (p_request #>> '{admitted_actor,id}')::uuid;
        v_target_id := (p_request->>'target_id')::uuid;

        IF p_action IN ('get_row', 'actor_status') THEN
            v_normalized_request := pg_catalog.jsonb_set(
                p_request,
                '{operation}',
                pg_catalog.to_jsonb('get_project'::text),
                false
            );
            v_reply := aap_workspace_project_query_v1._project_query_step_impl(
                v_normalized_request,
                p_action,
                NULL::jsonb,
                NULL::text,
                NULL::text,
                NULL::text[],
                NULL::text[],
                NULL::uuid,
                NULL::text,
                NULL::timestamptz,
                NULL::timestamptz,
                NULL::bigint,
                NULL::integer
            );
            v_facts := v_reply->'facts';
        ELSE
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'id', membership.id,
                        'project_id', membership.project_id,
                        'user_id', membership.user_id,
                        'role', membership.role::text,
                        'assigned_by', membership.assigned_by,
                        'assigned_at', pg_catalog.to_jsonb(membership.assigned_at),
                        'version', membership.version,
                        'updated_at', pg_catalog.to_jsonb(membership.updated_at),
                        'weekly_target', membership.weekly_target
                    )
                ),
                '[]'::jsonb
            )::text
            INTO v_rows_json
            FROM public.project_members AS membership
            WHERE membership.project_id = v_target_id
              AND membership.user_id = v_actor_id;
            v_facts := pg_catalog.jsonb_build_object('rows_json', v_rows_json);
        END IF;

        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-project-query/v1',
            'request', p_request,
            'action', p_action,
            'parameters', p_parameters,
            'facts', v_facts
        );
    END IF;

    v_reply := aap_workspace_project_query_v1._project_query_step_impl(
        p_request,
        p_action,
        p_parameters,
        p_status,
        p_search,
        p_type_keys,
        p_data_types,
        p_member_id,
        p_project_role,
        p_created_from,
        p_created_to,
        p_offset,
        p_limit
    );

    v_member := v_reply #> '{facts,member}';
    IF p_action = 'member_access'
       AND pg_catalog.jsonb_typeof(v_member) = 'object' THEN
        -- Keep the legacy Python datetime failure boundary in this statement snapshot.
        SELECT membership.assigned_at, membership.updated_at
        INTO v_assigned_at, v_updated_at
        FROM public.project_members AS membership
        WHERE membership.id = (v_member->>'id')::uuid
          AND membership.project_id = (v_member->>'project_id')::uuid
          AND membership.user_id = (v_member->>'user_id')::uuid;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Project member authority row changed within a stable snapshot';
        END IF;

        IF (
            v_assigned_at IS NOT NULL
            AND pg_catalog.isfinite(v_assigned_at)
            AND (
                v_assigned_at < TIMESTAMPTZ '0001-01-01 00:00:00+00'
                OR v_assigned_at > TIMESTAMPTZ '9999-12-31 23:59:59.999999+00'
            )
        ) OR (
            v_updated_at IS NOT NULL
            AND pg_catalog.isfinite(v_updated_at)
            AND (
                v_updated_at < TIMESTAMPTZ '0001-01-01 00:00:00+00'
                OR v_updated_at > TIMESTAMPTZ '9999-12-31 23:59:59.999999+00'
            )
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22008',
                MESSAGE = 'Project member timestamp is outside the Python datetime range';
        END IF;
    END IF;

    RETURN v_reply;
END;
$function$;
