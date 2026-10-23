-- Read-only native Identity capability for batched UserBrief hydration.
CREATE SCHEMA aap_identity_briefs_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_briefs_v1 FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_briefs_v1._has_exact_keys(
    p_value jsonb,
    p_keys text[]
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count bigint;
BEGIN
    IF p_value IS NULL
       OR pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'object'
       OR p_keys IS NULL THEN
        RETURN false;
    END IF;

    SELECT pg_catalog.count(*) INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_value) AS field(name);
    IF v_count <> pg_catalog.cardinality(p_keys) THEN
        RETURN false;
    END IF;
    RETURN NOT EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(p_keys) AS expected(name)
        WHERE NOT (p_value ? expected.name)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_briefs_v1._has_exact_keys(
    jsonb, text[]
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_briefs_v1._failure()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-briefs-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_briefs_v1._failure() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_briefs_v1.briefs_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-briefs-policy/v1',
        'policy', 'aap-identity-briefs/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_briefs_v1.briefs_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_briefs_v1.execute_briefs_command(p_command jsonb)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation text;
    v_payload jsonb;
    v_project_id uuid;
    v_user_ids_json jsonb;
    v_user_ids uuid[] := ARRAY[]::uuid[];
    v_invalid_user_id boolean;
    v_users jsonb := '[]'::jsonb;
BEGIN
    IF NOT aap_identity_briefs_v1._has_exact_keys(
        p_command, ARRAY['schema', 'operation', 'payload']::text[]
    ) OR pg_catalog.jsonb_typeof(p_command->'schema') IS DISTINCT FROM 'string'
       OR p_command->>'schema' IS DISTINCT FROM 'aap-identity-briefs-native/v1'
       OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string' THEN
        RETURN aap_identity_briefs_v1._failure();
    END IF;

    v_operation := p_command->>'operation';
    v_payload := p_command->'payload';
    IF v_operation = 'get_user_briefs' THEN
        IF NOT aap_identity_briefs_v1._has_exact_keys(
            v_payload, ARRAY['user_ids']::text[]
        ) THEN
            RETURN aap_identity_briefs_v1._failure();
        END IF;
    ELSIF v_operation = 'get_project_user_briefs' THEN
        IF NOT aap_identity_briefs_v1._has_exact_keys(
            v_payload, ARRAY['project_id', 'user_ids']::text[]
        ) OR pg_catalog.jsonb_typeof(v_payload->'project_id') IS DISTINCT FROM 'string'
           OR ((v_payload->>'project_id') COLLATE "C") !~
              '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN
            RETURN aap_identity_briefs_v1._failure();
        END IF;
        v_project_id := (v_payload->>'project_id')::uuid;
    ELSE
        RETURN aap_identity_briefs_v1._failure();
    END IF;

    v_user_ids_json := v_payload->'user_ids';
    IF pg_catalog.jsonb_typeof(v_user_ids_json) IS DISTINCT FROM 'array' THEN
        RETURN aap_identity_briefs_v1._failure();
    END IF;
    SELECT COALESCE(
        pg_catalog.array_agg((item.value #>> '{}')::uuid),
        ARRAY[]::uuid[]
    )
    INTO v_user_ids
    FROM pg_catalog.jsonb_array_elements(v_user_ids_json) AS item(value)
    WHERE pg_catalog.jsonb_typeof(item.value) = 'string'
      AND ((item.value #>> '{}') COLLATE "C") ~
          '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$';

    SELECT EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(v_user_ids_json) AS item(value)
        WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
           OR ((item.value #>> '{}') COLLATE "C") !~
              '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    ) INTO v_invalid_user_id;
    IF v_invalid_user_id THEN
        RETURN aap_identity_briefs_v1._failure();
    END IF;

    IF pg_catalog.cardinality(v_user_ids) > 0 THEN
        IF v_operation = 'get_project_user_briefs' THEN
            WITH project_roles AS MATERIALIZED (
                SELECT membership.user_id, membership.role
                FROM aap_workspace_v1.identity_brief_project_roles(
                    v_project_id, v_user_ids
                ) AS membership
            )
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'id', account.id,
                        'name', account.name,
                        'email', account.email,
                        'role', CASE
                            WHEN project_roles.role IS NULL OR project_roles.role = ''
                                THEN account.role
                            ELSE project_roles.role
                        END,
                        'avatar_ref', account.avatar_ref
                    ) ORDER BY (account.id::text) COLLATE "C"
                ),
                '[]'::jsonb
            ) INTO v_users
            FROM public.users AS account
            LEFT JOIN project_roles ON project_roles.user_id = account.id
            WHERE account.id = ANY(v_user_ids);
        ELSE
            SELECT COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'id', account.id,
                        'name', account.name,
                        'email', account.email,
                        'role', account.role,
                        'avatar_ref', account.avatar_ref
                    ) ORDER BY (account.id::text) COLLATE "C"
                ),
                '[]'::jsonb
            ) INTO v_users
            FROM public.users AS account
            WHERE account.id = ANY(v_user_ids);
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-briefs-native/v1',
        'ok', true,
        'operation', v_operation,
        'data', pg_catalog.jsonb_build_object(
            'project_id', v_project_id,
            'users', v_users
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_briefs_v1.execute_briefs_command(jsonb)
    FROM PUBLIC;
