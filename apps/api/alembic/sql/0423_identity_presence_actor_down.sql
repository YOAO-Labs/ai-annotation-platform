CREATE OR REPLACE FUNCTION aap_identity_accounts_v1._lock_current_actor(
    p_actor jsonb,
    p_expected_key_hash text,
    p_lock_mode text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_field_count integer;
    v_actor_id uuid;
    v_credential_id uuid;
    v_credential_type text;
    v_generation bigint;
    v_expires_at timestamptz;
    v_user_id uuid;
    v_current_hash text;
    v_fence jsonb;
    v_fence_code text;
    v_principal jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;
    IF p_lock_mode NOT IN ('read', 'write_wait', 'write_nowait') THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_actor);
    IF v_field_count <> 10
       OR NOT (p_actor ?& ARRAY[
           'actor_id', 'name', 'email', 'role', 'is_active', 'credential_type',
           'credential_id', 'generation', 'scopes', 'expires_at'
       ])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_actor) AS field(name)
           WHERE field.name NOT IN (
               'actor_id', 'name', 'email', 'role', 'is_active', 'credential_type',
               'credential_id', 'generation', 'scopes', 'expires_at'
           )
       )
       OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
       OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_actor->'name') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
       OR p_actor->>'role' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer')
       OR p_actor->'is_active' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(p_actor->'credential_type') IS DISTINCT FROM 'string'
       OR p_actor->>'credential_type' NOT IN ('jwt', 'api_key')
       OR pg_catalog.jsonb_typeof(p_actor->'credential_id') IS DISTINCT FROM 'string'
       OR (p_actor->>'credential_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;

    v_actor_id := (p_actor->>'actor_id')::uuid;
    v_credential_id := (p_actor->>'credential_id')::uuid;
    v_credential_type := p_actor->>'credential_type';
    IF pg_catalog.char_length(p_actor->>'email') > 255
       OR pg_catalog.char_length(p_actor->>'name') > 100 THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;

    IF v_credential_type = 'jwt' THEN
        IF pg_catalog.jsonb_typeof(p_actor->'generation') IS DISTINCT FROM 'number'
           OR (p_actor->>'generation') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.jsonb_typeof(p_actor->'scopes') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_actor->'expires_at') IS DISTINCT FROM 'string'
           OR (p_actor->>'expires_at') !~* (
               '(z|[+-][0-9]{2}' || pg_catalog.chr(58) || '[0-9]{2})$'
           )
           OR p_expected_key_hash IS NOT NULL THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
        BEGIN
            v_generation := (p_actor->>'generation')::bigint;
            v_expires_at := (p_actor->>'expires_at')::timestamptz;
        EXCEPTION
            WHEN invalid_text_representation OR invalid_datetime_format
                 OR datetime_field_overflow OR numeric_value_out_of_range THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END;
        IF NOT pg_catalog.isfinite(v_expires_at) THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
    ELSE
        IF pg_catalog.jsonb_typeof(p_actor->'generation') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_actor->'scopes') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_actor->'expires_at') NOT IN ('null', 'string')
           OR (pg_catalog.jsonb_typeof(p_actor->'expires_at') = 'string'
               AND (p_actor->>'expires_at') !~* (
                   '(z|[+-][0-9]{2}' || pg_catalog.chr(58) || '[0-9]{2})$'
               ))
           OR p_expected_key_hash IS NULL
           OR pg_catalog.octet_length(p_expected_key_hash) NOT BETWEEN 1 AND 255 THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(p_actor->'scopes') AS scope(value)
            WHERE pg_catalog.jsonb_typeof(scope.value) IS DISTINCT FROM 'string'
        ) THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
        IF pg_catalog.jsonb_typeof(p_actor->'expires_at') = 'string' THEN
            BEGIN
                v_expires_at := (p_actor->>'expires_at')::timestamptz;
            EXCEPTION
                WHEN invalid_text_representation OR invalid_datetime_format
                     OR datetime_field_overflow THEN
                    RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END;
            IF NOT pg_catalog.isfinite(v_expires_at) THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
        END IF;
    END IF;

    BEGIN
        IF p_lock_mode = 'read' THEN
            SELECT account.id INTO v_user_id
            FROM public.users AS account
            WHERE account.id = v_actor_id
            FOR SHARE;
        ELSIF p_lock_mode = 'write_wait' THEN
            SELECT account.id INTO v_user_id
            FROM public.users AS account
            WHERE account.id = v_actor_id
            FOR UPDATE;
        ELSE
            SELECT account.id INTO v_user_id
            FROM public.users AS account
            WHERE account.id = v_actor_id
            FOR UPDATE NOWAIT;
        END IF;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_accounts_v1._failure('account_busy', NULL, NULL);
    END;
    IF NOT FOUND THEN
        RETURN aap_identity_accounts_v1._failure('credential_invalid', NULL, NULL);
    END IF;

    v_fence := aap_identity_v1.lock_current_principal(
        v_actor_id,
        v_credential_type,
        v_credential_id,
        CASE WHEN v_credential_type = 'jwt' THEN v_generation ELSE NULL::bigint END,
        CASE WHEN v_credential_type = 'jwt' THEN v_expires_at ELSE NULL::timestamptz END,
        CASE WHEN v_credential_type = 'jwt' THEN NULL::text[] ELSE ARRAY[]::text[] END
    );
    IF v_fence->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_fence_code := v_fence->'error'->>'code';
        IF v_fence_code IS NULL OR v_fence_code NOT IN (
            'invalid_request', 'credential_invalid', 'credential_revoked',
            'generation_outdated', 'token_expired', 'user_inactive',
            'invalid_account_role', 'credential_busy'
        ) THEN
            v_fence_code := 'credential_invalid';
        END IF;
        RETURN aap_identity_accounts_v1._failure(v_fence_code, NULL, NULL);
    END IF;
    IF v_credential_type = 'api_key' THEN
        SELECT api_key.key_hash INTO v_current_hash
        FROM public.api_keys AS api_key
        WHERE api_key.id = v_credential_id AND api_key.user_id = v_actor_id;
        IF NOT FOUND OR v_current_hash IS DISTINCT FROM p_expected_key_hash THEN
            RETURN aap_identity_accounts_v1._failure('credential_invalid', NULL, NULL);
        END IF;
    END IF;
    v_principal := v_fence->'data';
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-account-fence/v1',
        'ok', true,
        'data', v_principal
    );
END;
$function$;
