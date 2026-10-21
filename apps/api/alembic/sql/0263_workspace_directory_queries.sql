-- Closed native reads for Workspace project membership and mention directory views.
CREATE SCHEMA aap_workspace_directory_query_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_workspace_directory_query_v1 FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.workspace_directory_superadmins()
RETURNS TABLE(id uuid, name text, email text)
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
    SELECT account.id,
           account.name::text,
           account.email::text
    FROM public.users AS account
    WHERE account.role = 'super_admin'
      AND account.is_active IS TRUE
    ORDER BY account.name
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_v1.workspace_directory_superadmins()
    FROM PUBLIC;

-- aap-statement-break
CREATE FUNCTION aap_workspace_directory_query_v1._has_exact_keys(
    p_value jsonb,
    p_keys text[]
)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'object' THEN false
        ELSE (
            SELECT pg_catalog.count(*) = pg_catalog.cardinality(p_keys)
            FROM pg_catalog.jsonb_object_keys(p_value) AS field(name)
        )
        AND p_value ?& p_keys
        AND NOT EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_object_keys(p_value) AS field(name)
            WHERE NOT (field.name = ANY (p_keys))
        )
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_directory_query_v1._has_exact_keys(jsonb, text[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_directory_query_v1.directory_query_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-directory-query/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_directory_query_v1.directory_query_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_directory_query_v1.directory_query_step(
    p_request jsonb,
    p_action text,
    p_parameters jsonb
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
    v_operation text;
    v_actor_id uuid;
    v_target_id uuid;
    v_user_id uuid;
    v_project_request jsonb;
    v_project_reply jsonb;
    v_facts jsonb;
    v_rows_json text;
    v_accounts jsonb;
    v_user record;
BEGIN
    IF p_action IS NULL OR p_action NOT IN (
        'get_row', 'actor_status', 'member_access', 'member_rows',
        'mention_owner', 'mention_superadmins', 'mention_members'
    ) OR NOT aap_workspace_directory_query_v1._has_exact_keys(
        p_request,
        ARRAY['schema', 'request_id', 'operation', 'actor_id', 'target_id']::text[]
    )
       OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' IS DISTINCT FROM 'aap-workspace-directory-query/v1'
       OR pg_catalog.jsonb_typeof(p_request->'request_id') IS DISTINCT FROM 'string'
       OR (p_request->>'request_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
       OR p_request->>'operation' NOT IN ('list_members', 'mention_candidates')
       OR pg_catalog.jsonb_typeof(p_request->'actor_id') IS DISTINCT FROM 'string'
       OR (p_request->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'target_id') IS DISTINCT FROM 'string'
       OR (p_request->>'target_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace directory query request';
    END IF;

    v_operation := p_request->>'operation';
    v_actor_id := (p_request->>'actor_id')::uuid;
    v_target_id := (p_request->>'target_id')::uuid;

    IF NOT (
        (v_operation = 'list_members'
            AND p_action IN ('get_row', 'actor_status', 'member_access', 'member_rows'))
        OR (v_operation = 'mention_candidates'
            AND p_action IN (
                'get_row', 'actor_status', 'member_access', 'mention_owner',
                'mention_superadmins', 'mention_members'
            ))
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Workspace directory action does not match operation';
    END IF;

    IF p_action IN ('get_row', 'actor_status', 'member_access') THEN
        IF p_parameters IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Workspace directory authority step parameters must be null';
        END IF;

        v_project_request := pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-project-query/v1',
            'request_id', p_request->'request_id',
            'operation', 'get_project',
            'admitted_actor', pg_catalog.jsonb_build_object(
                'id', p_request->'actor_id',
                'role', ''
            ),
            'target_id', p_request->'target_id',
            'filters_ascii', NULL,
            'pagination', NULL,
            'projection_evidence', NULL
        );
        v_project_reply := aap_workspace_project_query_v1.project_query_step(
            v_project_request,
            p_action,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL
        );
        v_facts := v_project_reply->'facts';

    ELSIF p_action = 'member_rows' THEN
        IF v_operation <> 'list_members'
           OR NOT aap_workspace_directory_query_v1._has_exact_keys(
               p_parameters, ARRAY['project_id']::text[]
           )
           OR pg_catalog.jsonb_typeof(p_parameters->'project_id') IS DISTINCT FROM 'string'
           OR (p_parameters->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR (p_parameters->>'project_id')::uuid IS DISTINCT FROM v_target_id THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace directory member-row parameters';
        END IF;

        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', member.id,
                    'project_id', member.project_id,
                    'user_id', member.user_id,
                    'role', member.role::text,
                    'assigned_by', member.assigned_by,
                    'assigned_at', pg_catalog.to_jsonb(member.assigned_at),
                    'version', member.version,
                    'updated_at', pg_catalog.to_jsonb(member.updated_at),
                    'weekly_target', member.weekly_target
                )
                ORDER BY member.assigned_at DESC
            ),
            '[]'::jsonb
        )::text
        INTO v_rows_json
        FROM public.project_members AS member
        WHERE member.project_id = v_target_id;
        v_facts := pg_catalog.jsonb_build_object('rows_json', v_rows_json);

    ELSIF p_action = 'mention_owner' THEN
        IF v_operation <> 'mention_candidates'
           OR NOT aap_workspace_directory_query_v1._has_exact_keys(
               p_parameters, ARRAY['user_id']::text[]
           )
           OR pg_catalog.jsonb_typeof(p_parameters->'user_id') IS DISTINCT FROM 'string'
           OR (p_parameters->>'user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace directory owner parameters';
        END IF;
        v_user_id := (p_parameters->>'user_id')::uuid;

        SELECT account.id, account.name, account.email, account.is_active
        INTO v_user
        FROM aap_identity_v1.read_workspace_membership_accounts(
            ARRAY[v_user_id]::uuid[]
        ) AS account
        WHERE account.id = v_user_id;
        IF FOUND THEN
            v_facts := pg_catalog.jsonb_build_object(
                'user', pg_catalog.jsonb_build_object(
                    'id', v_user.id,
                    'name', v_user.name,
                    'email', v_user.email,
                    'is_active', v_user.is_active
                )
            );
        ELSE
            v_facts := pg_catalog.jsonb_build_object('user', NULL);
        END IF;

    ELSIF p_action = 'mention_superadmins' THEN
        IF v_operation <> 'mention_candidates' OR p_parameters IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace directory super-admin parameters';
        END IF;

        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', account.id,
                    'name', account.name,
                    'email', account.email
                )
                ORDER BY account.name
            ),
            '[]'::jsonb
        )
        INTO v_accounts
        FROM aap_identity_v1.workspace_directory_superadmins() AS account;
        v_facts := pg_catalog.jsonb_build_object('accounts', v_accounts);

    ELSIF p_action = 'mention_members' THEN
        IF v_operation <> 'mention_candidates'
           OR NOT aap_workspace_directory_query_v1._has_exact_keys(
               p_parameters, ARRAY['project_id']::text[]
           )
           OR pg_catalog.jsonb_typeof(p_parameters->'project_id') IS DISTINCT FROM 'string'
           OR (p_parameters->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR (p_parameters->>'project_id')::uuid IS DISTINCT FROM v_target_id THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'invalid Workspace directory member parameters';
        END IF;

        WITH selected_members AS MATERIALIZED (
            SELECT member.id,
                   member.project_id,
                   member.user_id,
                   member.role::text AS role,
                   member.assigned_by,
                   member.assigned_at,
                   member.version,
                   member.updated_at,
                   member.weekly_target
            FROM public.project_members AS member
            WHERE member.project_id = v_target_id
        ),
        member_ids AS MATERIALIZED (
            SELECT COALESCE(
                pg_catalog.array_agg(DISTINCT member.user_id),
                ARRAY[]::uuid[]
            ) AS ids
            FROM selected_members AS member
        ),
        accounts AS MATERIALIZED (
            SELECT account.id, account.name, account.email
            FROM member_ids
            CROSS JOIN LATERAL
                aap_identity_v1.read_workspace_membership_accounts(member_ids.ids)
                AS account
        )
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', member.id,
                    'project_id', member.project_id,
                    'user_id', member.user_id,
                    'role', member.role,
                    'assigned_by', member.assigned_by,
                    'assigned_at', pg_catalog.to_jsonb(member.assigned_at),
                    'version', member.version,
                    'updated_at', pg_catalog.to_jsonb(member.updated_at),
                    'weekly_target', member.weekly_target,
                    'user_name', account.name,
                    'user_email', account.email
                )
                ORDER BY member.assigned_at DESC
            ),
            '[]'::jsonb
        )::text
        INTO v_rows_json
        FROM selected_members AS member
        JOIN accounts AS account ON account.id = member.user_id;
        v_facts := pg_catalog.jsonb_build_object('rows_json', v_rows_json);
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-directory-query/v1',
        'request', p_request,
        'action', p_action,
        'parameters', p_parameters,
        'facts', v_facts
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_directory_query_v1.directory_query_step(jsonb, text, jsonb)
    FROM PUBLIC;
