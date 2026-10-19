-- Native transactional owner for Identity API-key management.
CREATE SCHEMA aap_identity_keys_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_keys_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_key_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    operation text NOT NULL CHECK (operation IN (
        'create_api_key', 'update_api_key', 'rotate_api_key', 'revoke_api_key'
    )),
    payload jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(payload) = 'object'),
    derivation_id text,
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (
        (operation IN ('create_api_key', 'rotate_api_key')
            AND derivation_id IS NOT NULL
            AND derivation_id COLLATE "C" ~ '^[0-9a-f]{64}$')
        OR (operation NOT IN ('create_api_key', 'rotate_api_key')
            AND derivation_id IS NULL)
    )
);
-- aap-statement-break
CREATE INDEX ix_identity_key_operation_receipts_actor
    ON public.identity_key_operation_receipts (actor_id, created_at DESC);
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_key_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_keys_v1.key_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-keys-policy/v1',
        'derivation', 'aap-identity-api-keys/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_keys_v1.key_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_keys_v1.execute_key_command(p_request jsonb)
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
    v_operation_id_text text;
    v_actor jsonb;
    v_actor_id uuid;
    v_credential_id uuid;
    v_credential_type text;
    v_generation bigint;
    v_actor_expiry timestamptz;
    v_expected_hash text;
    v_payload jsonb;
    v_new_key jsonb;
    v_new_key_id uuid;
    v_key_prefix text;
    v_key_hash text;
    v_derivation_id text;
    v_key_id uuid;
    v_name text;
    v_scopes jsonb;
    v_expiry_days integer;
    v_patch jsonb;
    v_patch_name text;
    v_patch_scopes jsonb;
    v_patch_expiry_days integer;
    v_has_name boolean := false;
    v_has_scopes boolean := false;
    v_has_expiry boolean := false;
    v_now timestamptz;
    v_expires_at timestamptz;
    v_principal jsonb;
    v_fence jsonb;
    v_fence_code text;
    v_actor_stored_hash text;
    v_receipt_found boolean := false;
    v_receipt_actor_id uuid;
    v_receipt_operation text;
    v_receipt_payload jsonb;
    v_receipt_derivation_id text;
    v_data jsonb;
    v_items jsonb;
    v_key_record jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-keys-native/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    IF pg_catalog.jsonb_object_length(p_request) <> 7
       OR NOT (p_request ?& ARRAY[
           'schema', 'operation', 'operation_id', 'actor',
           'expected_key_hash', 'payload', 'new_key'
       ])
       OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' <> 'aap-identity-keys-native/v1'
       OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'actor') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_request->'payload') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_request->'new_key') NOT IN ('null', 'object')
       OR pg_catalog.jsonb_typeof(p_request->'expected_key_hash') NOT IN ('null', 'string') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-keys-native/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_operation := p_request->>'operation';
    v_operation_id_text := p_request->>'operation_id';
    v_actor := p_request->'actor';
    v_payload := p_request->'payload';
    v_new_key := p_request->'new_key';
    v_expected_hash := p_request->>'expected_key_hash';
    IF pg_catalog.jsonb_typeof(v_actor) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(v_payload) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(v_new_key) NOT IN ('null', 'object')
       OR pg_catalog.jsonb_typeof(p_request->'expected_key_hash') NOT IN ('null', 'string') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-keys-native/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    IF v_operation NOT IN (
        'list_api_keys', 'create_api_key', 'update_api_key',
        'rotate_api_key', 'revoke_api_key'
    ) OR v_operation_id_text !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_object_length(v_actor) <> 10
       OR NOT (v_actor ?& ARRAY[
           'actor_id', 'name', 'email', 'role', 'is_active', 'credential_type',
           'credential_id', 'generation', 'scopes', 'expires_at'
       ])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(v_actor) AS field(name)
           WHERE field.name NOT IN (
               'actor_id', 'name', 'email', 'role', 'is_active', 'credential_type',
               'credential_id', 'generation', 'scopes', 'expires_at'
           )
       )
       OR pg_catalog.jsonb_typeof(v_actor->'actor_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_actor->'name') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_actor->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_actor->'role') IS DISTINCT FROM 'string'
       OR v_actor->>'role' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer')
       OR v_actor->'is_active' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_actor->'credential_type') IS DISTINCT FROM 'string'
       OR v_actor->>'credential_type' NOT IN ('jwt', 'api_key')
       OR pg_catalog.jsonb_typeof(v_actor->'credential_id') IS DISTINCT FROM 'string'
       OR v_actor->>'actor_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR v_actor->>'credential_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-keys-native/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    IF v_actor->>'credential_type' = 'jwt' THEN
        IF pg_catalog.jsonb_typeof(v_actor->'generation') IS DISTINCT FROM 'number'
           OR (v_actor->>'generation') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.jsonb_typeof(v_actor->'expires_at') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_actor->'scopes') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_request->'expected_key_hash') IS DISTINCT FROM 'null'
           OR (v_actor->>'expires_at') !~* (
               '(z|[+-][0-9]{2}' || pg_catalog.chr(58) || '[0-9]{2})$'
           ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
    ELSE
        IF pg_catalog.jsonb_typeof(v_actor->'generation') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(v_actor->'expires_at') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(v_actor->'scopes') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request->'expected_key_hash') IS DISTINCT FROM 'string'
           OR pg_catalog.octet_length(v_expected_hash) NOT BETWEEN 1 AND 255 THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_actor->'scopes') AS scope(value)
            WHERE pg_catalog.jsonb_typeof(scope.value) IS DISTINCT FROM 'string'
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
    END IF;

    BEGIN
        v_operation_id := v_operation_id_text::uuid;
        v_actor_id := (v_actor->>'actor_id')::uuid;
        v_credential_id := (v_actor->>'credential_id')::uuid;
        v_credential_type := v_actor->>'credential_type';
        IF v_credential_type = 'jwt' THEN
            v_generation := (v_actor->>'generation')::bigint;
            v_actor_expiry := (v_actor->>'expires_at')::timestamptz;
            IF NOT pg_catalog.isfinite(v_actor_expiry) THEN
                RAISE EXCEPTION USING ERRCODE = '22007', MESSAGE = 'invalid actor expiry';
            END IF;
        END IF;
    EXCEPTION
        WHEN invalid_text_representation OR invalid_datetime_format
             OR datetime_field_overflow OR numeric_value_out_of_range THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
    END;

    IF v_operation = 'list_api_keys' THEN
        IF pg_catalog.jsonb_object_length(v_payload) <> 0
           OR pg_catalog.jsonb_typeof(v_new_key) IS DISTINCT FROM 'null' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
    ELSIF v_operation = 'create_api_key' THEN
        IF NOT (v_payload ?& ARRAY['name'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_payload) AS field(name)
               WHERE field.name NOT IN ('name', 'scopes', 'expires_in_days')
           )
           OR pg_catalog.jsonb_typeof(v_payload->'name') IS DISTINCT FROM 'string'
           OR pg_catalog.length(v_payload->>'name') NOT BETWEEN 1 AND 60
           OR pg_catalog.jsonb_typeof(v_new_key) IS DISTINCT FROM 'object' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
        v_name := v_payload->>'name';
        v_scopes := COALESCE(v_payload->'scopes', '[]'::jsonb);
        IF pg_catalog.jsonb_typeof(v_scopes) IS DISTINCT FROM 'array' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_scopes) AS scope(value)
            WHERE pg_catalog.jsonb_typeof(scope.value) IS DISTINCT FROM 'string'
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
        IF v_payload ? 'expires_in_days'
           AND pg_catalog.jsonb_typeof(v_payload->'expires_in_days') <> 'null' THEN
            IF pg_catalog.jsonb_typeof(v_payload->'expires_in_days') IS DISTINCT FROM 'number'
               OR (v_payload->>'expires_in_days') !~ '^[1-9][0-9]*$' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-identity-keys-native/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                );
            END IF;
            BEGIN
                v_expiry_days := (v_payload->>'expires_in_days')::integer;
            EXCEPTION WHEN numeric_value_out_of_range THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-identity-keys-native/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                );
            END;
            IF v_expiry_days NOT BETWEEN 1 AND 3650 THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-identity-keys-native/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                );
            END IF;
        END IF;
    ELSE
        IF pg_catalog.jsonb_typeof(v_payload->'key_id') IS DISTINCT FROM 'string'
           OR (v_payload->>'key_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR (v_operation IN ('rotate_api_key', 'revoke_api_key')
               AND pg_catalog.jsonb_object_length(v_payload) <> 1)
           OR (v_operation = 'update_api_key'
               AND (pg_catalog.jsonb_object_length(v_payload) <> 2
                    OR pg_catalog.jsonb_typeof(v_payload->'patch') IS DISTINCT FROM 'object')) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
        BEGIN
            v_key_id := (v_payload->>'key_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END;
        IF v_operation = 'update_api_key' THEN
            v_patch := v_payload->'patch';
            IF EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_object_keys(v_patch) AS field(name)
                WHERE field.name NOT IN ('name', 'scopes', 'expires_in_days')
            ) THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-identity-keys-native/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                );
            END IF;
            v_has_name := v_patch ? 'name';
            v_has_scopes := v_patch ? 'scopes';
            v_has_expiry := v_patch ? 'expires_in_days';
            IF v_has_name THEN
                IF pg_catalog.jsonb_typeof(v_patch->'name') IS DISTINCT FROM 'string'
                   OR pg_catalog.length(v_patch->>'name') NOT BETWEEN 1 AND 60 THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-identity-keys-native/v1', 'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END IF;
                v_patch_name := v_patch->>'name';
            END IF;
            IF v_has_scopes THEN
                IF pg_catalog.jsonb_typeof(v_patch->'scopes') IS DISTINCT FROM 'array' THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-identity-keys-native/v1', 'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END IF;
                IF EXISTS (
                    SELECT 1 FROM pg_catalog.jsonb_array_elements(v_patch->'scopes') AS scope(value)
                    WHERE pg_catalog.jsonb_typeof(scope.value) IS DISTINCT FROM 'string'
                ) THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-identity-keys-native/v1', 'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END IF;
                v_patch_scopes := v_patch->'scopes';
            END IF;
            IF v_has_expiry
               AND pg_catalog.jsonb_typeof(v_patch->'expires_in_days') <> 'null' THEN
                IF pg_catalog.jsonb_typeof(v_patch->'expires_in_days') IS DISTINCT FROM 'number'
                   OR (v_patch->>'expires_in_days') !~ '^[1-9][0-9]*$' THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-identity-keys-native/v1', 'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END IF;
                BEGIN
                    v_patch_expiry_days := (v_patch->>'expires_in_days')::integer;
                EXCEPTION WHEN numeric_value_out_of_range THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-identity-keys-native/v1', 'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END;
                IF v_patch_expiry_days NOT BETWEEN 1 AND 3650 THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-identity-keys-native/v1', 'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END IF;
            END IF;
        END IF;
        IF v_operation = 'rotate_api_key'
           AND pg_catalog.jsonb_typeof(v_new_key) IS DISTINCT FROM 'object' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
        IF v_operation IN ('update_api_key', 'revoke_api_key')
           AND pg_catalog.jsonb_typeof(v_new_key) IS DISTINCT FROM 'null' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
    END IF;

    IF v_operation IN ('create_api_key', 'rotate_api_key') THEN
        IF pg_catalog.jsonb_object_length(v_new_key) <> 4
           OR NOT (v_new_key ?& ARRAY['key_id', 'key_prefix', 'key_hash', 'derivation_id'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_new_key) AS field(name)
               WHERE field.name NOT IN ('key_id', 'key_prefix', 'key_hash', 'derivation_id')
           )
           OR pg_catalog.jsonb_typeof(v_new_key->'key_id') IS DISTINCT FROM 'string'
           OR (v_new_key->>'key_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_new_key->'key_prefix') IS DISTINCT FROM 'string'
           OR (v_new_key->>'key_prefix') COLLATE "C" !~ '^ak_[A-Za-z0-9_-]{9}$'
           OR pg_catalog.jsonb_typeof(v_new_key->'key_hash') IS DISTINCT FROM 'string'
           OR (v_new_key->>'key_hash') COLLATE "C" !~ '^\$2[aby]\$12\$[./A-Za-z0-9]{53}$'
           OR pg_catalog.jsonb_typeof(v_new_key->'derivation_id') IS DISTINCT FROM 'string'
           OR (v_new_key->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
        BEGIN
            v_new_key_id := (v_new_key->>'key_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END;
        v_key_prefix := v_new_key->>'key_prefix';
        v_key_hash := v_new_key->>'key_hash';
        v_derivation_id := v_new_key->>'derivation_id';
        IF (v_operation = 'create_api_key' AND v_new_key_id <> v_operation_id)
           OR (v_operation = 'rotate_api_key' AND v_new_key_id <> v_key_id) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;
    ELSIF pg_catalog.jsonb_typeof(v_new_key) IS DISTINCT FROM 'null' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-keys-native/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_fence := aap_identity_v1.lock_current_principal(
        v_actor_id,
        v_credential_type,
        v_credential_id,
        v_generation,
        v_actor_expiry,
        CASE WHEN v_credential_type = 'jwt' THEN NULL::text[] ELSE ARRAY[]::text[] END
    );
    IF pg_catalog.jsonb_typeof(v_fence) IS DISTINCT FROM 'object'
       OR v_fence->>'schema' IS DISTINCT FROM 'aap-identity/v1'
       OR v_fence->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_fence_code := v_fence->'error'->>'code';
        IF v_fence_code IS NULL OR v_fence_code NOT IN (
            'invalid_request', 'credential_invalid', 'credential_revoked',
            'generation_outdated', 'token_expired', 'user_inactive',
            'invalid_account_role', 'credential_busy'
        ) THEN
            v_fence_code := 'invalid_request';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-keys-native/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', v_fence_code)
        );
    END IF;
    v_principal := v_fence->'data';
    IF pg_catalog.jsonb_typeof(v_principal) IS DISTINCT FROM 'object'
       OR v_principal->>'actor_id' IS DISTINCT FROM v_actor_id::text THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-keys-native/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    IF v_credential_type = 'api_key' THEN
        SELECT api_key.key_hash INTO v_actor_stored_hash
        FROM public.api_keys AS api_key
        WHERE api_key.id = v_credential_id AND api_key.user_id = v_actor_id;
        IF NOT FOUND OR v_actor_stored_hash IS DISTINCT FROM v_expected_hash THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'credential_invalid')
            );
        END IF;
    END IF;

    IF v_operation = 'list_api_keys' THEN
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', api_key.id,
                    'name', api_key.name,
                    'key_prefix', api_key.key_prefix,
                    'scopes', api_key.scopes,
                    'expires_at', api_key.expires_at,
                    'last_used_at', api_key.last_used_at,
                    'revoked_at', api_key.revoked_at,
                    'created_at', api_key.created_at
                ) ORDER BY api_key.created_at DESC, api_key.id DESC
            ),
            '[]'::jsonb
        ) INTO v_items
        FROM public.api_keys AS api_key
        WHERE api_key.user_id = v_actor_id;
        v_data := pg_catalog.jsonb_build_object('items', v_items);
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-keys-native/v1', 'ok', true,
            'operation', v_operation, 'operation_id', v_operation_id,
            'principal', v_principal, 'data', v_data
        );
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-identity-key-operation/' || v_operation_id::text, 0)
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-keys-native/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'key_busy')
        );
    END IF;

    SELECT receipt.actor_id, receipt.operation, receipt.payload,
           receipt.derivation_id, receipt.result
    INTO v_receipt_actor_id, v_receipt_operation, v_receipt_payload,
         v_receipt_derivation_id, v_data
    FROM public.identity_key_operation_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    v_receipt_found := FOUND;
    IF v_receipt_found THEN
        IF v_receipt_actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt_operation IS DISTINCT FROM v_operation
           OR v_receipt_payload IS DISTINCT FROM v_payload
           OR v_receipt_derivation_id IS DISTINCT FROM v_derivation_id
           OR (v_operation IN ('create_api_key', 'rotate_api_key')
               AND v_data #>> '{key,key_prefix}' IS DISTINCT FROM v_key_prefix) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'operation_conflict')
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-keys-native/v1', 'ok', true,
            'operation', v_operation, 'operation_id', v_operation_id,
            'principal', v_principal, 'data', v_data
        );
    END IF;

    v_now := pg_catalog.clock_timestamp();
    BEGIN
        IF v_operation = 'create_api_key' THEN
            v_expires_at := CASE
                WHEN v_expiry_days IS NULL THEN NULL
                ELSE v_now + (v_expiry_days * INTERVAL '1 day')
            END;
            INSERT INTO public.api_keys (
                id, user_id, name, key_prefix, key_hash, scopes, expires_at
            ) VALUES (
                v_new_key_id, v_actor_id, v_name, v_key_prefix, v_key_hash,
                v_scopes, v_expires_at
            );
            SELECT pg_catalog.jsonb_build_object(
                'id', api_key.id,
                'name', api_key.name,
                'key_prefix', api_key.key_prefix,
                'scopes', api_key.scopes,
                'expires_at', api_key.expires_at,
                'last_used_at', api_key.last_used_at,
                'revoked_at', api_key.revoked_at,
                'created_at', api_key.created_at
            ) INTO v_key_record
            FROM public.api_keys AS api_key
            WHERE api_key.id = v_new_key_id;
            v_data := pg_catalog.jsonb_build_object('key', v_key_record);
        ELSIF v_operation = 'update_api_key' THEN
            BEGIN
                PERFORM api_key.id
                FROM public.api_keys AS api_key
                WHERE api_key.id = v_key_id AND api_key.user_id = v_actor_id
                  AND api_key.revoked_at IS NULL
                FOR UPDATE NOWAIT;
            EXCEPTION
                WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-identity-keys-native/v1', 'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'key_busy')
                    );
            END;
            IF NOT FOUND THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-identity-keys-native/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'key_not_found')
                );
            END IF;
            IF v_has_name OR v_has_scopes OR v_has_expiry THEN
                UPDATE public.api_keys AS api_key
                SET name = CASE WHEN v_has_name THEN v_patch_name ELSE api_key.name END,
                    scopes = CASE WHEN v_has_scopes THEN v_patch_scopes ELSE api_key.scopes END,
                    expires_at = CASE
                        WHEN NOT v_has_expiry THEN api_key.expires_at
                        WHEN pg_catalog.jsonb_typeof(v_patch->'expires_in_days') = 'null' THEN NULL
                        ELSE v_now + (v_patch_expiry_days * INTERVAL '1 day')
                    END
                WHERE api_key.id = v_key_id AND api_key.user_id = v_actor_id;
            END IF;
            SELECT pg_catalog.jsonb_build_object(
                'id', api_key.id,
                'name', api_key.name,
                'key_prefix', api_key.key_prefix,
                'scopes', api_key.scopes,
                'expires_at', api_key.expires_at,
                'last_used_at', api_key.last_used_at,
                'revoked_at', api_key.revoked_at,
                'created_at', api_key.created_at
            ) INTO v_key_record
            FROM public.api_keys AS api_key
            WHERE api_key.id = v_key_id AND api_key.user_id = v_actor_id;
            v_data := pg_catalog.jsonb_build_object('key', v_key_record);
        ELSIF v_operation = 'rotate_api_key' THEN
            BEGIN
                PERFORM 1 FROM public.api_keys AS api_key
                WHERE api_key.id = v_key_id AND api_key.user_id = v_actor_id
                  AND api_key.revoked_at IS NULL
                FOR UPDATE NOWAIT;
            EXCEPTION
                WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-identity-keys-native/v1', 'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'key_busy')
                    );
            END;
            IF NOT FOUND THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-identity-keys-native/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'key_not_found')
                );
            END IF;
            UPDATE public.api_keys AS api_key
            SET key_prefix = v_key_prefix,
                key_hash = v_key_hash,
                last_used_at = NULL
            WHERE api_key.id = v_key_id AND api_key.user_id = v_actor_id;
            SELECT pg_catalog.jsonb_build_object(
                'id', api_key.id,
                'name', api_key.name,
                'key_prefix', api_key.key_prefix,
                'scopes', api_key.scopes,
                'expires_at', api_key.expires_at,
                'last_used_at', api_key.last_used_at,
                'revoked_at', api_key.revoked_at,
                'created_at', api_key.created_at
            ) INTO v_key_record
            FROM public.api_keys AS api_key
            WHERE api_key.id = v_key_id AND api_key.user_id = v_actor_id;
            v_data := pg_catalog.jsonb_build_object('key', v_key_record);
        ELSE
            BEGIN
                PERFORM 1 FROM public.api_keys AS api_key
                WHERE api_key.id = v_key_id AND api_key.user_id = v_actor_id
                  AND api_key.revoked_at IS NULL
                FOR UPDATE NOWAIT;
            EXCEPTION
                WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-identity-keys-native/v1', 'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'key_busy')
                    );
            END;
            IF NOT FOUND THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-identity-keys-native/v1', 'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'key_not_found')
                );
            END IF;
            UPDATE public.api_keys AS api_key
            SET revoked_at = v_now
            WHERE api_key.id = v_key_id AND api_key.user_id = v_actor_id;
            v_data := pg_catalog.jsonb_build_object(
                'key_id', v_key_id, 'revoked', true
            );
        END IF;

        INSERT INTO public.identity_key_operation_receipts (
            operation_id, actor_id, operation, payload, derivation_id, result
        ) VALUES (
            v_operation_id, v_actor_id, v_operation, v_payload,
            v_derivation_id, v_data
        );
    EXCEPTION
        WHEN unique_violation THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-keys-native/v1', 'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'operation_conflict')
            );
    END;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-keys-native/v1', 'ok', true,
        'operation', v_operation, 'operation_id', v_operation_id,
        'principal', v_principal, 'data', v_data
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_keys_v1.execute_key_command(jsonb) FROM PUBLIC;
