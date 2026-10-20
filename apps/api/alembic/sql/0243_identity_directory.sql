-- Native implementation of the public administrator user-directory reads.
CREATE FUNCTION aap_identity_directory_v1._failure(
    p_code text,
    p_status_code integer DEFAULT NULL,
    p_detail text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-directory-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object(
            'code', p_code, 'status_code', p_status_code, 'detail', p_detail
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_directory_v1._failure(text, integer, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_directory_v1._has_exact_keys(
    p_value jsonb,
    p_keys text[]
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_typeof(p_value) = 'object'
       AND (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_value))
           = pg_catalog.cardinality(p_keys)
       AND p_value ?& p_keys
       AND NOT EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_value) AS field(name)
           WHERE NOT (field.name = ANY(p_keys))
       )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_directory_v1._has_exact_keys(jsonb, text[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_directory_v1._strip_unicode_space(p_value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.btrim(
        COALESCE(p_value, ''),
        pg_catalog.chr(9) || pg_catalog.chr(10) || pg_catalog.chr(11)
        || pg_catalog.chr(12) || pg_catalog.chr(13) || pg_catalog.chr(28)
        || pg_catalog.chr(29) || pg_catalog.chr(30) || pg_catalog.chr(31)
        || pg_catalog.chr(32) || pg_catalog.chr(133) || pg_catalog.chr(160)
        || pg_catalog.chr(5760) || pg_catalog.chr(8192) || pg_catalog.chr(8193)
        || pg_catalog.chr(8194) || pg_catalog.chr(8195) || pg_catalog.chr(8196)
        || pg_catalog.chr(8197) || pg_catalog.chr(8198) || pg_catalog.chr(8199)
        || pg_catalog.chr(8200) || pg_catalog.chr(8201) || pg_catalog.chr(8202)
        || pg_catalog.chr(8232) || pg_catalog.chr(8233) || pg_catalog.chr(8239)
        || pg_catalog.chr(8287) || pg_catalog.chr(12288)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_directory_v1._strip_unicode_space(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_directory_v1._valid_payload(
    p_operation text,
    p_payload jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_keys text[];
    v_role jsonb;
    v_status jsonb;
    v_project jsonb;
    v_group jsonb;
    v_search jsonb;
    v_page jsonb;
    v_page_size jsonb;
    v_format jsonb;
BEGIN
    CASE p_operation
        WHEN 'list_users' THEN
            v_keys := ARRAY['role', 'status', 'project_id'];
        WHEN 'query_users' THEN
            v_keys := ARRAY[
                'status', 'project_id', 'group_id', 'role', 'search', 'page', 'page_size'
            ];
        WHEN 'user_stats' THEN
            v_keys := ARRAY['status', 'project_id', 'group_id', 'role', 'search'];
        WHEN 'export_users' THEN
            v_keys := ARRAY[
                'status', 'project_id', 'group_id', 'role', 'search', 'format'
            ];
        ELSE
            RETURN false;
    END CASE;
    IF NOT aap_identity_directory_v1._has_exact_keys(p_payload, v_keys) THEN
        RETURN false;
    END IF;

    v_role := p_payload->'role';
    v_status := p_payload->'status';
    v_project := p_payload->'project_id';
    IF pg_catalog.jsonb_typeof(v_role) NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_status) IS DISTINCT FROM 'string'
       OR p_payload->>'status' NOT IN ('active', 'inactive', 'all')
       OR pg_catalog.jsonb_typeof(v_project) NOT IN ('string', 'null') THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(v_project) = 'string'
       AND (p_payload->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN false;
    END IF;

    IF p_operation <> 'list_users' THEN
        v_group := p_payload->'group_id';
        v_search := p_payload->'search';
        IF pg_catalog.jsonb_typeof(v_group) NOT IN ('string', 'null')
           OR pg_catalog.jsonb_typeof(v_search) NOT IN ('string', 'null')
           OR (pg_catalog.jsonb_typeof(v_group) = 'string'
               AND (p_payload->>'group_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR (pg_catalog.jsonb_typeof(v_search) = 'string'
               AND pg_catalog.char_length(p_payload->>'search') > 255) THEN
            RETURN false;
        END IF;
    END IF;
    IF p_operation = 'query_users' THEN
        v_page := p_payload->'page';
        v_page_size := p_payload->'page_size';
        IF pg_catalog.jsonb_typeof(v_page) IS DISTINCT FROM 'number'
           OR (p_payload->>'page') !~ '^[1-9][0-9]*$'
           OR (p_payload->>'page')::numeric > 9223372036854775807
           OR pg_catalog.jsonb_typeof(v_page_size) IS DISTINCT FROM 'number'
           OR (p_payload->>'page_size') !~ '^[1-9][0-9]*$'
           OR (p_payload->>'page_size')::numeric NOT BETWEEN 1 AND 500 THEN
            RETURN false;
        END IF;
    ELSIF p_operation = 'export_users' THEN
        v_format := p_payload->'format';
        IF pg_catalog.jsonb_typeof(v_format) IS DISTINCT FROM 'string'
           OR p_payload->>'format' NOT IN ('csv', 'json') THEN
            RETURN false;
        END IF;
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_directory_v1._valid_payload(text, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_directory_v1._directory_user(p_user_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', account.id,
        'email', account.email,
        'name', account.name,
        'role', account.role,
        'group_name', account.group_name,
        'group_id', account.group_id,
        'status', account.status,
        'is_active', account.is_active,
        'disabled_kind', account.disabled_kind,
        'disabled_at', account.disabled_at,
        'disabled_by', account.disabled_by,
        'disabled_reason', account.disabled_reason,
        'email_verified_at', account.email_verified_at,
        'last_login_at', account.last_login_at,
        'last_seen_at', account.last_seen_at,
        'password_admin_reset_at', account.password_admin_reset_at,
        'deactivation_requested_at', account.deactivation_requested_at,
        'deactivation_scheduled_at', account.deactivation_scheduled_at,
        'preferences', account.preferences,
        'avatar_ref', account.avatar_ref,
        'created_at', account.created_at
    )
    FROM public.users AS account
    WHERE account.id = p_user_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_directory_v1._directory_user(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_directory_v1._scope_user_ids(
    p_actor_id uuid,
    p_actor_role text,
    p_operation text,
    p_payload jsonb,
    p_apply_filters boolean
)
RETURNS SETOF uuid
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT account.id
    FROM public.users AS account
    WHERE (
        CASE WHEN p_operation = 'list_users' THEN
            CASE WHEN p_actor_role = 'super_admin' THEN
                p_payload->'project_id' = 'null'::jsonb
                OR EXISTS (
                    SELECT 1
                    FROM aap_workspace_v1.identity_directory_project_member_ids(
                        (p_payload->>'project_id')::uuid
                    ) AS project_member(user_id)
                    WHERE project_member.user_id = account.id
                )
            WHEN p_payload->>'role' = 'employee'
                 AND p_payload->>'status' = 'active' THEN
                account.is_active AND account.role = 'employee'
            ELSE
                account.id = p_actor_id OR EXISTS (
                    SELECT 1
                    FROM aap_workspace_v1.identity_directory_owned_member_ids(
                        p_actor_id
                    ) AS owned_member(user_id)
                    WHERE owned_member.user_id = account.id
                )
            END
        ELSE
            CASE WHEN p_payload->'project_id' IS NOT NULL
                       AND p_payload->'project_id' <> 'null'::jsonb THEN
                EXISTS (
                    SELECT 1
                    FROM aap_workspace_v1.identity_directory_project_member_ids(
                        (p_payload->>'project_id')::uuid
                    ) AS project_member(user_id)
                    WHERE project_member.user_id = account.id
                ) AND (
                    p_actor_role = 'super_admin'
                    OR account.id = p_actor_id
                    OR EXISTS (
                        SELECT 1
                        FROM aap_workspace_v1.identity_directory_owned_member_ids(
                            p_actor_id
                        ) AS owned_member(user_id)
                        WHERE owned_member.user_id = account.id
                    )
                )
            WHEN p_actor_role = 'super_admin' THEN true
            ELSE account.id = p_actor_id
                OR EXISTS (
                    SELECT 1
                    FROM aap_workspace_v1.identity_directory_owned_member_ids(
                        p_actor_id
                    ) AS owned_member(user_id)
                    WHERE owned_member.user_id = account.id
                )
                OR (account.is_active AND account.role IN ('employee', 'super_admin'))
            END
        END
    )
      AND (
          NOT p_apply_filters
          OR (p_payload->>'status' = 'all'
              OR (p_payload->>'status' = 'active' AND account.is_active)
              OR (p_payload->>'status' = 'inactive' AND NOT account.is_active))
      )
      AND (
          NOT p_apply_filters
          OR p_payload->'role' = 'null'::jsonb
          OR account.role = p_payload->>'role'
      )
      AND (
          NOT p_apply_filters OR p_operation = 'list_users'
          OR p_payload->'group_id' = 'null'::jsonb
          OR account.group_id = (p_payload->>'group_id')::uuid
      )
      AND (
          NOT p_apply_filters OR p_operation = 'list_users'
          OR p_payload->'search' = 'null'::jsonb
          OR account.email ILIKE '%' || aap_identity_directory_v1._strip_unicode_space(
              p_payload->>'search'
          ) || '%'
          OR account.name ILIKE '%' || aap_identity_directory_v1._strip_unicode_space(
              p_payload->>'search'
          ) || '%'
      )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_directory_v1._scope_user_ids(
    uuid, text, text, jsonb, boolean
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_directory_v1.directory_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-directory-policy/v1',
        'policy', 'aap-identity-directory/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_directory_v1.directory_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_directory_v1.execute_directory_command(p_command jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_operation_id uuid;
    v_actor jsonb;
    v_payload jsonb;
    v_context jsonb;
    v_expected_key_hash text;
    v_fingerprint text;
    v_derivation_id text;
    v_actor_id uuid;
    v_actor_kind text;
    v_credential_id uuid;
    v_generation bigint;
    v_expires_at timestamptz;
    v_principal_reply jsonb;
    v_principal jsonb;
    v_principal_role text;
    v_current_key_hash text;
    v_data jsonb;
    v_users jsonb;
    v_result jsonb;
    v_detail jsonb;
    v_receipt record;
    v_count bigint;
    v_online bigint;
    v_weekly_active bigint;
    v_total bigint;
    v_page bigint;
    v_page_size integer;
    v_pages bigint;
    v_audit_id bigint;
    v_replayed boolean := false;
    v_scope_lost boolean;
BEGIN
    IF NOT aap_identity_directory_v1._has_exact_keys(
        p_command, ARRAY[
            'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
            'request_fingerprint', 'derivation_id', 'audit_context', 'payload'
        ]
    ) OR p_command->>'schema' IS DISTINCT FROM 'aap-identity-directory-native/v1' THEN
        RETURN aap_identity_directory_v1._failure('invalid_request', 400, NULL);
    END IF;
    v_operation := p_command->>'operation';
    IF v_operation NOT IN ('list_users', 'query_users', 'user_stats', 'export_users')
       OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'expected_key_hash') NOT IN ('string', 'null')
       OR (pg_catalog.jsonb_typeof(p_command->'expected_key_hash') = 'string'
           AND pg_catalog.char_length(p_command->>'expected_key_hash') NOT BETWEEN 1 AND 255)
       OR NOT aap_identity_directory_v1._has_exact_keys(
           p_command->'audit_context', ARRAY['method', 'path', 'ip', 'request_id']
       )
       OR NOT aap_audit_v1.valid_request_context(p_command->'audit_context') THEN
        RETURN aap_identity_directory_v1._failure('invalid_request', 400, NULL);
    END IF;
    v_operation_id := (p_command->>'operation_id')::uuid;
    v_actor := p_command->'actor';
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_context := p_command->'audit_context';
    v_payload := p_command->'payload';
    IF NOT aap_identity_directory_v1._valid_payload(v_operation, v_payload) THEN
        RETURN aap_identity_directory_v1._failure('invalid_request', 400, NULL);
    END IF;

    IF NOT aap_identity_directory_v1._has_exact_keys(
        v_actor, ARRAY[
            'actor_id', 'name', 'email', 'role', 'is_active', 'credential_type',
            'credential_id', 'generation', 'scopes', 'expires_at'
        ]
    ) OR pg_catalog.jsonb_typeof(v_actor->'actor_id') IS DISTINCT FROM 'string'
       OR (v_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(v_actor->'name') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_actor->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_actor->'role') IS DISTINCT FROM 'string'
       OR v_actor->>'role' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer')
       OR v_actor->'is_active' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_actor->'credential_type') IS DISTINCT FROM 'string'
       OR v_actor->>'credential_type' NOT IN ('jwt', 'api_key')
       OR pg_catalog.jsonb_typeof(v_actor->'credential_id') IS DISTINCT FROM 'string'
       OR (v_actor->>'credential_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(v_actor->'scopes') NOT IN ('array', 'null')
       OR (pg_catalog.jsonb_typeof(v_actor->'scopes') = 'array' AND EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_array_elements(v_actor->'scopes') AS scope(value)
           WHERE pg_catalog.jsonb_typeof(scope.value) IS DISTINCT FROM 'string'
       ))
       OR pg_catalog.jsonb_typeof(v_actor->'generation') NOT IN ('number', 'null')
       OR (pg_catalog.jsonb_typeof(v_actor->'generation') = 'number'
           AND ((v_actor->>'generation') !~ '^(0|[1-9][0-9]*)$'
                OR (v_actor->>'generation')::numeric > 9223372036854775807))
       OR pg_catalog.jsonb_typeof(v_actor->'expires_at') NOT IN ('string', 'null')
       OR (pg_catalog.jsonb_typeof(v_actor->'expires_at') = 'string'
           AND (v_actor->>'expires_at') !~ '(Z|[+-][0-9]{2}:[0-9]{2})$')
       OR (v_actor->>'credential_type' = 'jwt'
           AND (pg_catalog.jsonb_typeof(v_actor->'generation') <> 'number'
                OR pg_catalog.jsonb_typeof(v_actor->'scopes') <> 'null'
                OR pg_catalog.jsonb_typeof(v_actor->'expires_at') <> 'string'))
       OR (v_actor->>'credential_type' = 'api_key'
           AND pg_catalog.jsonb_typeof(v_actor->'generation') <> 'null')
       OR ((v_actor->>'credential_type' = 'api_key')
           <> (v_expected_key_hash IS NOT NULL)) THEN
        RETURN aap_identity_directory_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_actor_id := (v_actor->>'actor_id')::uuid;
    v_actor_kind := v_actor->>'credential_type';
    v_credential_id := (v_actor->>'credential_id')::uuid;
    IF pg_catalog.jsonb_typeof(v_actor->'generation') = 'number' THEN
        v_generation := (v_actor->>'generation')::bigint;
    END IF;
    IF pg_catalog.jsonb_typeof(v_actor->'expires_at') = 'string' THEN
        BEGIN
            v_expires_at := (v_actor->>'expires_at')::timestamptz;
        EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
                     OR datetime_field_overflow THEN
            RETURN aap_identity_directory_v1._failure('invalid_request', 400, NULL);
        END;
        IF NOT pg_catalog.isfinite(v_expires_at) THEN
            RETURN aap_identity_directory_v1._failure('invalid_request', 400, NULL);
        END IF;
    END IF;
    v_principal_reply := aap_identity_v1.lock_current_principal(
        v_actor_id, v_actor_kind, v_credential_id, v_generation,
        v_expires_at, ARRAY[]::text[]
    );
    IF v_principal_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_principal_reply #>> '{error,code}'
            WHEN 'invalid_request' THEN
                RETURN aap_identity_directory_v1._failure('invalid_request', 400, NULL);
            WHEN 'credential_invalid' THEN
                RETURN aap_identity_directory_v1._failure('credential_invalid', NULL, NULL);
            WHEN 'credential_revoked' THEN
                RETURN aap_identity_directory_v1._failure('credential_revoked', NULL, NULL);
            WHEN 'generation_outdated' THEN
                RETURN aap_identity_directory_v1._failure('generation_outdated', NULL, NULL);
            WHEN 'token_expired' THEN
                RETURN aap_identity_directory_v1._failure('token_expired', NULL, NULL);
            WHEN 'user_inactive' THEN
                RETURN aap_identity_directory_v1._failure('user_inactive', NULL, NULL);
            WHEN 'invalid_account_role' THEN
                RETURN aap_identity_directory_v1._failure('invalid_account_role', NULL, NULL);
            WHEN 'credential_busy' THEN
                RETURN aap_identity_directory_v1._failure('credential_busy', NULL, NULL);
            ELSE
                RETURN aap_identity_directory_v1._failure('unavailable', 503, NULL);
        END CASE;
    END IF;
    v_principal := v_principal_reply->'data';
    v_principal_role := v_principal->>'role';
    IF v_actor_kind = 'api_key' THEN
        BEGIN
            SELECT key.key_hash INTO v_current_key_hash
            FROM public.api_keys AS key
            WHERE key.id = v_credential_id
              AND key.user_id = v_actor_id
              AND key.revoked_at IS NULL
            FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_directory_v1._failure('credential_busy', NULL, NULL);
        END;
        IF NOT FOUND THEN
            RETURN aap_identity_directory_v1._failure('credential_revoked', NULL, NULL);
        END IF;
        IF v_current_key_hash IS DISTINCT FROM v_expected_key_hash THEN
            RETURN aap_identity_directory_v1._failure('credential_invalid', NULL, NULL);
        END IF;
    END IF;
    IF v_principal_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_identity_directory_v1._failure(
            'manager_required', 403, '需要角色权限: super_admin或project_admin'
        );
    END IF;
    IF pg_catalog.jsonb_typeof(v_payload->'role') = 'string'
       AND v_payload->>'role' NOT IN (
           'super_admin', 'project_admin', 'employee', 'viewer'
       ) THEN
        RETURN aap_identity_directory_v1._failure(
            'invalid_platform_role', 400, '非法平台角色: ' || (v_payload->>'role')
        );
    END IF;

    IF v_operation <> 'export_users' THEN
        CASE v_operation
            WHEN 'list_users' THEN
                SELECT COALESCE(
                    pg_catalog.jsonb_agg(
                        aap_identity_directory_v1._directory_user(account.id)
                        ORDER BY account.created_at DESC
                    ), '[]'::jsonb
                ) INTO v_users
                FROM public.users AS account
                WHERE account.id IN (
                    SELECT aap_identity_directory_v1._scope_user_ids(
                        v_actor_id, v_principal_role, v_operation, v_payload, true
                    )
                );
                v_data := pg_catalog.jsonb_build_object('users', v_users);
            WHEN 'query_users' THEN
                v_page := (v_payload->>'page')::bigint;
                v_page_size := (v_payload->>'page_size')::integer;
                WITH scope AS MATERIALIZED (
                    SELECT DISTINCT id
                    FROM aap_identity_directory_v1._scope_user_ids(
                        v_actor_id, v_principal_role, v_operation, v_payload, true
                    ) AS scoped(id)
                ), totals AS MATERIALIZED (
                    SELECT pg_catalog.count(*)::bigint AS total FROM scope
                ), selected AS MATERIALIZED (
                    SELECT account.id, account.created_at, account.role, account.is_active
                    FROM public.users AS account
                    JOIN scope ON scope.id = account.id
                    ORDER BY account.created_at DESC, account.id DESC
                    LIMIT v_page_size
                    OFFSET (
                        SELECT LEAST(
                            (v_page::numeric - 1) * v_page_size::numeric,
                            totals.total::numeric
                        )::bigint
                        FROM totals
                    )
                ), blocked AS MATERIALIZED (
                    SELECT blocked.user_id
                    FROM aap_workspace_v1.identity_directory_lifecycle_blocked(
                        v_actor_id,
                        COALESCE(
                            (SELECT pg_catalog.array_agg(selected.id ORDER BY selected.id)
                             FROM selected),
                            ARRAY[]::uuid[]
                        )
                    ) AS blocked(user_id)
                    WHERE v_principal_role = 'project_admin'
                ), items AS (
                    SELECT COALESCE(
                        pg_catalog.jsonb_agg(
                            aap_identity_directory_v1._directory_user(selected.id)
                            || pg_catalog.jsonb_build_object(
                                'is_managed', CASE WHEN v_principal_role = 'super_admin'
                                    THEN true
                                    ELSE selected.id = v_actor_id
                                        OR EXISTS (
                                            SELECT 1
                                            FROM aap_workspace_v1.identity_directory_owned_member_ids(
                                                v_actor_id
                                            ) AS owned_member(user_id)
                                            WHERE owned_member.user_id = selected.id
                                        )
                                        OR (selected.is_active AND selected.role = 'employee')
                                END,
                                'is_lifecycle_managed', CASE WHEN v_principal_role = 'super_admin'
                                    THEN true
                                    ELSE (
                                        selected.id = v_actor_id
                                        OR EXISTS (
                                            SELECT 1
                                            FROM aap_workspace_v1.identity_directory_owned_member_ids(
                                                v_actor_id
                                            ) AS owned_member(user_id)
                                            WHERE owned_member.user_id = selected.id
                                        )
                                        OR (selected.is_active AND selected.role = 'employee')
                                    ) AND NOT EXISTS (
                                        SELECT 1 FROM blocked
                                        WHERE blocked.user_id = selected.id
                                    )
                                END
                            ) ORDER BY selected.created_at DESC, selected.id DESC
                        ), '[]'::jsonb
                    ) AS value
                    FROM selected
                )
                SELECT pg_catalog.jsonb_build_object(
                    'items', items.value,
                    'total', totals.total,
                    'page', v_page,
                    'page_size', v_page_size,
                    'pages', CASE WHEN totals.total = 0 THEN 0
                        ELSE pg_catalog.ceil(
                            totals.total::numeric / v_page_size::numeric
                        )::bigint END
                ) INTO v_data
                FROM totals CROSS JOIN items;
            WHEN 'user_stats' THEN
                WITH snapshot AS MATERIALIZED (
                    SELECT pg_catalog.statement_timestamp() AS observed_at
                ), scope AS MATERIALIZED (
                    SELECT DISTINCT id
                    FROM aap_identity_directory_v1._scope_user_ids(
                        v_actor_id, v_principal_role, v_operation, v_payload, true
                    ) AS scoped(id)
                )
                SELECT pg_catalog.jsonb_build_object(
                    'total', pg_catalog.count(*)::bigint,
                    'online', pg_catalog.count(*) FILTER (
                        WHERE account.status = 'online'
                    )::bigint,
                    'weekly_active', pg_catalog.count(*) FILTER (
                        WHERE account.last_seen_at >=
                            snapshot.observed_at - INTERVAL '7 days'
                    )::bigint
                ) INTO v_data
                FROM public.users AS account
                JOIN scope ON scope.id = account.id
                CROSS JOIN snapshot;
            ELSE
                RETURN aap_identity_directory_v1._failure('invalid_request', 400, NULL);
        END CASE;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-directory-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', v_data,
            'replayed', false
        );
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-identity-directory-export/v1:' || v_operation_id::text, 0
        )
    ) THEN
        RETURN aap_identity_directory_v1._failure('current_scope_busy', 409, NULL);
    END IF;
    SELECT receipt.operation, receipt.actor_id, receipt.request_fingerprint,
           receipt.derivation_id, receipt.result, receipt.audit_id
    INTO v_receipt
    FROM public.identity_directory_export_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    IF FOUND THEN
        IF v_receipt.operation IS DISTINCT FROM 'export_users'
           OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
           OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
            RETURN aap_identity_directory_v1._failure('operation_conflict', 409, NULL);
        END IF;
        WITH visible AS MATERIALIZED (
            SELECT id
            FROM aap_identity_directory_v1._scope_user_ids(
                v_actor_id, v_principal_role, 'export_users', v_payload, false
            ) AS scoped(id)
        )
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_receipt.result->'users') AS item(value)
            WHERE NOT EXISTS (
                SELECT 1 FROM visible
                WHERE visible.id = (item.value->>'id')::uuid
            )
        ) INTO v_scope_lost;
        IF v_scope_lost THEN
            RETURN aap_identity_directory_v1._failure('operation_conflict', 409, NULL);
        END IF;
        v_data := v_receipt.result;
        v_replayed := true;
    ELSE
        WITH selected AS MATERIALIZED (
            SELECT account.id, account.email, account.name, account.role,
                   account.group_name, account.group_id, account.status, account.created_at
            FROM public.users AS account
            WHERE account.id IN (
                SELECT aap_identity_directory_v1._scope_user_ids(
                    v_actor_id, v_principal_role, 'export_users', v_payload, true
                )
            )
        )
        SELECT pg_catalog.jsonb_build_object(
            'users', COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'id', selected.id,
                        'email', selected.email,
                        'name', selected.name,
                        'role', selected.role,
                        'group_name', selected.group_name,
                        'group_id', selected.group_id,
                        'status', selected.status,
                        'created_at', selected.created_at
                    ) ORDER BY selected.created_at DESC, selected.id DESC
                ), '[]'::jsonb
            ),
            'exported_by', v_principal->'email',
            'exported_at', pg_catalog.statement_timestamp(),
            'request_id', v_context->'request_id'
        ) INTO v_data
        FROM selected;
        v_count := pg_catalog.jsonb_array_length(v_data->'users');
        v_detail := pg_catalog.jsonb_build_object(
            'format', v_payload->'format',
            'count', v_count,
            'actor_email', v_principal->'email',
            'ip', v_context->'ip',
            'request_id', v_context->'request_id'
        );
        v_audit_id := aap_audit_v1.record_identity_directory_export(
            pg_catalog.jsonb_build_object(
                'actor_id', v_actor_id,
                'email', v_principal->'email',
                'role', v_principal->'role'
            ),
            v_context,
            v_detail
        );
        INSERT INTO public.identity_directory_export_receipts (
            operation_id, actor_id, operation, request_fingerprint, derivation_id,
            result, audit_id
        ) VALUES (
            v_operation_id, v_actor_id, 'export_users', v_fingerprint,
            v_derivation_id, v_data, v_audit_id
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-directory-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'principal', v_principal,
        'data', v_data,
        'replayed', v_replayed
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_directory_v1._failure('current_scope_busy', 409, NULL);
    WHEN OTHERS THEN
        RETURN aap_identity_directory_v1._failure('unavailable', 503, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_directory_v1.execute_directory_command(jsonb)
    FROM PUBLIC;
