-- Native transactional ownership for self-account commands and presence.
CREATE SCHEMA aap_identity_accounts_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_accounts_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_account_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    operation text NOT NULL CHECK (operation IN (
        'update_name', 'patch_preferences', 'change_password', 'set_avatar_ref'
    )),
    request_scope jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(request_scope) = 'object'
    ),
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    audit_id bigint,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_identity_account_operation_receipts_actor
    ON public.identity_account_operation_receipts (actor_id, created_at DESC);
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_account_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_accounts_v1._failure(
    p_code text,
    p_status_code smallint DEFAULT NULL,
    p_detail jsonb DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-accounts-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object(
            'code', p_code,
            'status_code', p_status_code,
            'detail', p_detail
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_accounts_v1._failure(text, smallint, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_accounts_v1._account_record(p_actor_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
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
    WHERE account.id = p_actor_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_accounts_v1._account_record(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_accounts_v1._lock_current_actor(
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
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_accounts_v1._lock_current_actor(jsonb, text, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_identity_account_event(
    p_event text,
    p_actor jsonb,
    p_context jsonb,
    p_detail jsonb
)
RETURNS bigint
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
    v_actor_email text;
    v_actor_role text;
    v_action text;
    v_status_code smallint;
    v_audit_id bigint;
    v_bytes bigint;
    v_avatar_ref text;
    v_kind text;
BEGIN
    IF p_event IS NULL OR p_event NOT IN (
        'update_name', 'change_password', 'set_avatar_ref'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid account audit event';
    END IF;
    IF pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid account audit context';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_context);
    IF v_field_count <> 4
       OR NOT (p_context ?& ARRAY['method', 'path', 'ip', 'request_id'])
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid account audit context';
    END IF;
    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid account audit actor';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_actor);
    IF v_field_count <> 3
       OR NOT (p_actor ?& ARRAY['actor_id', 'email', 'role'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_actor) AS field(name)
           WHERE field.name NOT IN ('actor_id', 'email', 'role')
       )
       OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
       OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_actor->>'email') > 255
       OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
       OR p_actor->>'role' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid account audit actor';
    END IF;
    BEGIN
        v_actor_id := (p_actor->>'actor_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid account audit actor';
    END;
    v_actor_email := p_actor->>'email';
    v_actor_role := p_actor->>'role';

    IF p_event = 'update_name' THEN
        IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid name audit detail';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF v_field_count <> 2
           OR NOT (p_detail ?& ARRAY['old_name', 'new_name'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(p_detail) AS field(name)
               WHERE field.name NOT IN ('old_name', 'new_name')
           )
           OR pg_catalog.jsonb_typeof(p_detail->'old_name') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_detail->>'old_name') > 100
           OR pg_catalog.jsonb_typeof(p_detail->'new_name') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_detail->>'new_name') NOT BETWEEN 1 AND 100
           OR p_detail->>'new_name' <> pg_catalog.btrim(p_detail->>'new_name')
           OR p_detail->>'old_name' IS NOT DISTINCT FROM p_detail->>'new_name' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid name audit detail';
        END IF;
        v_action := 'user.profile_update';
        v_status_code := 200;
    ELSIF p_event = 'change_password' THEN
        IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid password audit detail';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF v_field_count <> 1
           OR NOT (p_detail ? 'email')
           OR pg_catalog.jsonb_typeof(p_detail->'email') IS DISTINCT FROM 'string'
           OR p_detail->>'email' IS DISTINCT FROM v_actor_email THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid password audit detail';
        END IF;
        v_action := 'user.password_change';
        v_status_code := 204;
    ELSE
        IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid avatar audit detail';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF NOT (p_detail ?& ARRAY['field', 'avatar_ref', 'kind'])
           OR p_detail->>'field' IS DISTINCT FROM 'avatar'
           OR pg_catalog.jsonb_typeof(p_detail->'kind') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid avatar audit detail';
        END IF;
        v_kind := p_detail->>'kind';
        IF v_kind = 'upload' THEN
            IF v_field_count <> 4
               OR NOT (p_detail ? 'bytes')
               OR pg_catalog.jsonb_typeof(p_detail->'avatar_ref') IS DISTINCT FROM 'string'
               OR (p_detail->>'avatar_ref') COLLATE "C" !~ '^upload:[0-9a-f]{32}$'
               OR pg_catalog.jsonb_typeof(p_detail->'bytes') IS DISTINCT FROM 'number'
               OR (p_detail->>'bytes') !~ '^(0|[1-9][0-9]*)$' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid avatar audit detail';
            END IF;
            BEGIN
                v_bytes := (p_detail->>'bytes')::bigint;
            EXCEPTION WHEN numeric_value_out_of_range THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid avatar audit detail';
            END;
            v_avatar_ref := p_detail->>'avatar_ref';
        ELSIF v_kind = 'preset_or_clear' THEN
            IF v_field_count <> 3
               OR pg_catalog.jsonb_typeof(p_detail->'avatar_ref') NOT IN ('null', 'string')
               OR (pg_catalog.jsonb_typeof(p_detail->'avatar_ref') = 'string'
                   AND (p_detail->>'avatar_ref') COLLATE "C" !~ '^preset:[a-z0-9-]{1,40}$') THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid avatar audit detail';
            END IF;
            v_avatar_ref := p_detail->>'avatar_ref';
        ELSE
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid avatar audit detail';
        END IF;
        v_action := 'user.profile_update';
        v_status_code := 200;
    END IF;

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        v_actor_id, v_actor_email, v_actor_role, v_action, 'user', v_actor_id::text,
        p_context->>'method', p_context->>'path', v_status_code,
        NULLIF(p_context->>'ip', '')::pg_catalog.inet,
        p_context->>'request_id', p_detail
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_account_event(
    text, jsonb, jsonb, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_accounts_v1.account_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-accounts-policy/v1',
        'policy', 'aap-identity-accounts/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_accounts_v1.account_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_accounts_v1.prepare_account_command(p_request jsonb)
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
    v_operation text;
    v_operation_id uuid;
    v_actor jsonb;
    v_expected_key_hash text;
    v_fingerprint text;
    v_derivation_id text;
    v_context jsonb;
    v_actor_id uuid;
    v_lock_mode text;
    v_fence jsonb;
    v_principal jsonb;
    v_account jsonb;
    v_password_hash text;
    v_request_scope jsonb;
    v_receipt_actor_id uuid;
    v_receipt_operation text;
    v_receipt_scope jsonb;
    v_receipt_result jsonb;
    v_replay jsonb;
    v_receipt_found boolean := false;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_field_count <> 8
       OR NOT (p_request ?& ARRAY[
           'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
           'request_fingerprint', 'derivation_id', 'audit_context'
       ])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
           WHERE field.name NOT IN (
               'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
               'request_fingerprint', 'derivation_id', 'audit_context'
           )
       )
       OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' <> 'aap-identity-accounts-native/v1'
       OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
       OR p_request->>'operation' NOT IN (
           'read_self_account', 'get_preferences', 'heartbeat', 'update_name',
           'patch_preferences', 'change_password', 'set_avatar_ref'
       )
       OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
       OR (p_request->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'actor') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_request->'expected_key_hash') NOT IN ('null', 'string')
       OR pg_catalog.jsonb_typeof(p_request->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_request->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_request->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_request->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_request->'audit_context') IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;

    v_operation := p_request->>'operation';
    BEGIN
        v_operation_id := (p_request->>'operation_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END;
    v_actor := p_request->'actor';
    v_expected_key_hash := p_request->>'expected_key_hash';
    v_fingerprint := p_request->>'request_fingerprint';
    v_derivation_id := p_request->>'derivation_id';
    v_context := p_request->'audit_context';

    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(v_context);
    IF v_field_count <> 4
       OR NOT (v_context ?& ARRAY['method', 'path', 'ip', 'request_id'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_each(v_context) AS field(key, value)
           WHERE pg_catalog.jsonb_typeof(field.value) NOT IN ('string', 'null')
       )
       OR NOT aap_audit_v1.valid_request_context(v_context) THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;

    v_lock_mode := CASE v_operation
        WHEN 'read_self_account' THEN 'read'
        WHEN 'get_preferences' THEN 'read'
        WHEN 'patch_preferences' THEN 'write_wait'
        WHEN 'heartbeat' THEN 'write_nowait'
        ELSE 'write_nowait'
    END;
    v_fence := aap_identity_accounts_v1._lock_current_actor(
        v_actor, v_expected_key_hash, v_lock_mode
    );
    IF v_fence->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_fence;
    END IF;
    v_principal := v_fence->'data';
    v_actor_id := (v_principal->>'actor_id')::uuid;
    v_account := aap_identity_accounts_v1._account_record(v_actor_id);
    IF v_account IS NULL THEN
        RETURN aap_identity_accounts_v1._failure('credential_invalid', NULL, NULL);
    END IF;

    IF v_operation IN (
        'update_name', 'patch_preferences', 'change_password', 'set_avatar_ref'
    ) THEN
        v_request_scope := pg_catalog.jsonb_build_object(
            'actor_id', v_actor_id,
            'operation', v_operation,
            'request_fingerprint', v_fingerprint,
            'derivation_id', v_derivation_id
        );
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(
                'aap-identity-account-operation/' || v_operation_id::text, 0
            )
        ) THEN
            RETURN aap_identity_accounts_v1._failure('credential_busy', NULL, NULL);
        END IF;
        SELECT receipt.actor_id, receipt.operation, receipt.request_scope, receipt.result
        INTO v_receipt_actor_id, v_receipt_operation, v_receipt_scope, v_receipt_result
        FROM public.identity_account_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        v_receipt_found := FOUND;
        IF v_receipt_found THEN
            IF v_receipt_actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt_operation IS DISTINCT FROM v_operation
               OR v_receipt_scope IS DISTINCT FROM v_request_scope THEN
                RETURN aap_identity_accounts_v1._failure(
                    'operation_conflict', NULL, NULL
                );
            END IF;
            v_replay := v_receipt_result;
        END IF;
    END IF;

    IF v_operation = 'change_password' AND v_replay IS NULL THEN
        SELECT account.password_hash INTO v_password_hash
        FROM public.users AS account
        WHERE account.id = v_actor_id;
        IF NOT FOUND OR v_password_hash IS NULL
           OR pg_catalog.octet_length(v_password_hash) NOT BETWEEN 1 AND 255 THEN
            RETURN aap_identity_accounts_v1._failure('credential_invalid', NULL, NULL);
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-accounts-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'principal', v_principal,
        'data', pg_catalog.jsonb_build_object(
            'account', v_account,
            'password_hash', v_password_hash,
            'replay', v_replay
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_accounts_v1.prepare_account_command(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_accounts_v1.apply_account_command(p_request jsonb)
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
    v_operation text;
    v_operation_id uuid;
    v_actor jsonb;
    v_expected_key_hash text;
    v_fingerprint text;
    v_derivation_id text;
    v_context jsonb;
    v_base jsonb;
    v_new_value jsonb;
    v_response_preferences jsonb;
    v_actor_id uuid;
    v_lock_mode text;
    v_fence jsonb;
    v_principal jsonb;
    v_request_scope jsonb;
    v_receipt_actor_id uuid;
    v_receipt_operation text;
    v_receipt_scope jsonb;
    v_receipt_result jsonb;
    v_receipt_found boolean := false;
    v_user_name text;
    v_user_email text;
    v_user_role text;
    v_user_password_hash text;
    v_user_avatar_ref text;
    v_user_preferences jsonb;
    v_old_name text;
    v_new_name text;
    v_new_password_hash text;
    v_new_avatar_ref text;
    v_avatar_kind text;
    v_avatar_bytes bigint;
    v_changed boolean;
    v_account jsonb;
    v_data jsonb;
    v_audit_actor jsonb;
    v_audit_event text;
    v_audit_detail jsonb;
    v_audit_id bigint;
    v_now timestamptz;
    v_constraint_name text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_field_count <> 11
       OR NOT (p_request ?& ARRAY[
           'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
           'request_fingerprint', 'derivation_id', 'audit_context', 'base_value',
           'new_value', 'response_preferences'
       ])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
           WHERE field.name NOT IN (
               'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
               'request_fingerprint', 'derivation_id', 'audit_context', 'base_value',
               'new_value', 'response_preferences'
           )
       )
       OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' <> 'aap-identity-accounts-native/v1'
       OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
       OR p_request->>'operation' NOT IN (
           'read_self_account', 'get_preferences', 'heartbeat', 'update_name',
           'patch_preferences', 'change_password', 'set_avatar_ref'
       )
       OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
       OR (p_request->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'actor') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_request->'expected_key_hash') NOT IN ('null', 'string')
       OR pg_catalog.jsonb_typeof(p_request->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_request->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_request->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_request->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_request->'audit_context') IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;

    v_operation := p_request->>'operation';
    BEGIN
        v_operation_id := (p_request->>'operation_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END;
    v_actor := p_request->'actor';
    v_expected_key_hash := p_request->>'expected_key_hash';
    v_fingerprint := p_request->>'request_fingerprint';
    v_derivation_id := p_request->>'derivation_id';
    v_context := p_request->'audit_context';
    v_base := p_request->'base_value';
    v_new_value := p_request->'new_value';
    v_response_preferences := p_request->'response_preferences';

    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(v_context);
    IF v_field_count <> 4
       OR NOT (v_context ?& ARRAY['method', 'path', 'ip', 'request_id'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_each(v_context) AS field(key, value)
           WHERE pg_catalog.jsonb_typeof(field.value) NOT IN ('string', 'null')
       )
       OR NOT aap_audit_v1.valid_request_context(v_context) THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;

    IF v_operation NOT IN (
        'heartbeat', 'update_name', 'patch_preferences', 'change_password',
        'set_avatar_ref'
    ) THEN
        RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
    END IF;
    IF v_operation = 'heartbeat' THEN
        IF pg_catalog.jsonb_typeof(v_base) IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(v_new_value) IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(v_response_preferences) IS DISTINCT FROM 'null' THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
    ELSIF v_operation = 'update_name' THEN
        IF pg_catalog.jsonb_typeof(v_base) IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_base #>> '{}') > 100
           OR pg_catalog.jsonb_typeof(v_new_value) IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_new_value #>> '{}') NOT BETWEEN 1 AND 100
           OR (v_new_value #>> '{}') <> pg_catalog.btrim(v_new_value #>> '{}')
           OR pg_catalog.jsonb_typeof(v_response_preferences) IS DISTINCT FROM 'null' THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
    ELSIF v_operation = 'patch_preferences' THEN
        IF pg_catalog.jsonb_typeof(v_base) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_new_value) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_response_preferences) IS DISTINCT FROM 'object' THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
    ELSIF v_operation = 'change_password' THEN
        IF pg_catalog.jsonb_typeof(v_base) IS DISTINCT FROM 'string'
           OR pg_catalog.octet_length(v_base #>> '{}') NOT BETWEEN 1 AND 255
           OR pg_catalog.jsonb_typeof(v_new_value) IS DISTINCT FROM 'string'
           OR (v_new_value #>> '{}') COLLATE "C" !~ '^\$2[aby]\$(0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}$'
           OR pg_catalog.jsonb_typeof(v_response_preferences) IS DISTINCT FROM 'null' THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
    ELSE
        IF pg_catalog.jsonb_typeof(v_base) NOT IN ('null', 'string')
           OR pg_catalog.jsonb_typeof(v_new_value) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_response_preferences) IS DISTINCT FROM 'null' THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(v_new_value);
        IF v_field_count <> 3
           OR NOT (v_new_value ?& ARRAY['avatar_ref', 'kind', 'bytes'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_new_value) AS field(name)
               WHERE field.name NOT IN ('avatar_ref', 'kind', 'bytes')
           )
           OR pg_catalog.jsonb_typeof(v_new_value->'avatar_ref') NOT IN ('null', 'string')
           OR pg_catalog.jsonb_typeof(v_new_value->'kind') IS DISTINCT FROM 'string'
           OR v_new_value->>'kind' NOT IN ('upload', 'preset_or_clear')
           OR pg_catalog.jsonb_typeof(v_new_value->'bytes') NOT IN ('null', 'number')
           OR pg_catalog.char_length(v_new_value->>'avatar_ref') > 80 THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
        v_avatar_kind := v_new_value->>'kind';
        IF v_avatar_kind = 'upload' THEN
            IF pg_catalog.jsonb_typeof(v_new_value->'avatar_ref') IS DISTINCT FROM 'string'
               OR (v_new_value->>'avatar_ref') COLLATE "C" !~ '^upload:[0-9a-f]{32}$'
               OR pg_catalog.jsonb_typeof(v_new_value->'bytes') IS DISTINCT FROM 'number'
               OR (v_new_value->>'bytes') !~ '^(0|[1-9][0-9]*)$' THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
            BEGIN
                v_avatar_bytes := (v_new_value->>'bytes')::bigint;
            EXCEPTION WHEN numeric_value_out_of_range THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END;
        ELSE
            IF (pg_catalog.jsonb_typeof(v_new_value->'avatar_ref') = 'string'
                AND (v_new_value->>'avatar_ref') COLLATE "C" !~ '^preset:[a-z0-9-]{1,40}$')
               OR pg_catalog.jsonb_typeof(v_new_value->'bytes') IS DISTINCT FROM 'null' THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
        END IF;
        v_new_avatar_ref := v_new_value->>'avatar_ref';
    END IF;

    v_lock_mode := CASE v_operation
        WHEN 'patch_preferences' THEN 'write_wait'
        WHEN 'heartbeat' THEN 'write_nowait'
        ELSE 'write_nowait'
    END;
    v_fence := aap_identity_accounts_v1._lock_current_actor(
        v_actor, v_expected_key_hash, v_lock_mode
    );
    IF v_fence->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_fence;
    END IF;
    v_principal := v_fence->'data';
    v_actor_id := (v_principal->>'actor_id')::uuid;

    SELECT account.name, account.email, account.role, account.password_hash,
           account.avatar_ref, account.preferences
    INTO v_user_name, v_user_email, v_user_role, v_user_password_hash,
         v_user_avatar_ref, v_user_preferences
    FROM public.users AS account
    WHERE account.id = v_actor_id;
    IF NOT FOUND THEN
        RETURN aap_identity_accounts_v1._failure('credential_invalid', NULL, NULL);
    END IF;

    IF v_operation = 'heartbeat' THEN
        v_now := pg_catalog.clock_timestamp();
        UPDATE public.users AS account
        SET last_seen_at = v_now, status = 'online'
        WHERE account.id = v_actor_id;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-accounts-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', pg_catalog.jsonb_build_object(
                'account', NULL, 'preferences', NULL,
                'previous_avatar_ref', NULL, 'changed', NULL
            )
        );
    END IF;

    v_request_scope := pg_catalog.jsonb_build_object(
        'actor_id', v_actor_id,
        'operation', v_operation,
        'request_fingerprint', v_fingerprint,
        'derivation_id', v_derivation_id
    );
    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-identity-account-operation/' || v_operation_id::text, 0
        )
    ) THEN
        RETURN aap_identity_accounts_v1._failure('credential_busy', NULL, NULL);
    END IF;
    SELECT receipt.actor_id, receipt.operation, receipt.request_scope, receipt.result
    INTO v_receipt_actor_id, v_receipt_operation, v_receipt_scope, v_receipt_result
    FROM public.identity_account_operation_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    v_receipt_found := FOUND;
    IF v_receipt_found THEN
        IF v_receipt_actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt_operation IS DISTINCT FROM v_operation
           OR v_receipt_scope IS DISTINCT FROM v_request_scope THEN
            RETURN aap_identity_accounts_v1._failure('operation_conflict', NULL, NULL);
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-accounts-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', v_receipt_result
        );
    END IF;

    BEGIN
        IF v_operation = 'update_name' THEN
            v_old_name := v_user_name;
            v_new_name := v_new_value #>> '{}';
            IF (to_jsonb(v_old_name) IS DISTINCT FROM v_base) THEN
                RETURN aap_identity_accounts_v1._failure('account_busy', NULL, NULL);
            END IF;
            v_changed := v_old_name IS DISTINCT FROM v_new_name;
            IF v_changed THEN
                UPDATE public.users AS account
                SET name = v_new_name
                WHERE account.id = v_actor_id;
                v_principal := v_principal || pg_catalog.jsonb_build_object(
                    'name', v_new_name
                );
                v_audit_actor := pg_catalog.jsonb_build_object(
                    'actor_id', v_actor_id, 'email', v_user_email, 'role', v_user_role
                );
                v_audit_event := 'update_name';
                v_audit_detail := pg_catalog.jsonb_build_object(
                    'old_name', v_old_name, 'new_name', v_new_name
                );
                v_audit_id := aap_audit_v1.record_identity_account_event(
                    v_audit_event, v_audit_actor, v_context, v_audit_detail
                );
            END IF;
            v_account := aap_identity_accounts_v1._account_record(v_actor_id);
            v_data := pg_catalog.jsonb_build_object(
                'account', v_account,
                'preferences', NULL,
                'previous_avatar_ref', NULL,
                'changed', v_changed
            );
        ELSIF v_operation = 'patch_preferences' THEN
            IF v_user_preferences IS DISTINCT FROM v_base THEN
                RETURN aap_identity_accounts_v1._failure('account_busy', NULL, NULL);
            END IF;
            UPDATE public.users AS account
            SET preferences = v_new_value
            WHERE account.id = v_actor_id;
            v_data := pg_catalog.jsonb_build_object(
                'account', NULL,
                'preferences', v_response_preferences,
                'previous_avatar_ref', NULL,
                'changed', NULL
            );
        ELSIF v_operation = 'change_password' THEN
            IF to_jsonb(v_user_password_hash) IS DISTINCT FROM v_base THEN
                RETURN aap_identity_accounts_v1._failure('account_busy', NULL, NULL);
            END IF;
            v_new_password_hash := v_new_value #>> '{}';
            UPDATE public.users AS account
            SET password_hash = v_new_password_hash,
                password_admin_reset_at = NULL
            WHERE account.id = v_actor_id;
            v_audit_actor := pg_catalog.jsonb_build_object(
                'actor_id', v_actor_id, 'email', v_user_email, 'role', v_user_role
            );
            v_audit_event := 'change_password';
            v_audit_detail := pg_catalog.jsonb_build_object('email', v_user_email);
            v_audit_id := aap_audit_v1.record_identity_account_event(
                v_audit_event, v_audit_actor, v_context, v_audit_detail
            );
            v_data := pg_catalog.jsonb_build_object(
                'account', NULL,
                'preferences', NULL,
                'previous_avatar_ref', NULL,
                'changed', NULL
            );
        ELSE
            IF (CASE
                    WHEN v_user_avatar_ref IS NULL THEN 'null'::jsonb
                    ELSE to_jsonb(v_user_avatar_ref)
                END) IS DISTINCT FROM v_base THEN
                RETURN aap_identity_accounts_v1._failure('account_busy', NULL, NULL);
            END IF;
            v_changed := v_user_avatar_ref IS DISTINCT FROM v_new_avatar_ref;
            IF v_changed THEN
                UPDATE public.users AS account
                SET avatar_ref = v_new_avatar_ref
                WHERE account.id = v_actor_id;
                v_audit_actor := pg_catalog.jsonb_build_object(
                    'actor_id', v_actor_id, 'email', v_user_email, 'role', v_user_role
                );
                v_audit_event := 'set_avatar_ref';
                IF v_avatar_kind = 'upload' THEN
                    v_audit_detail := pg_catalog.jsonb_build_object(
                        'field', 'avatar', 'avatar_ref', v_new_avatar_ref,
                        'kind', v_avatar_kind, 'bytes', v_avatar_bytes
                    );
                ELSE
                    v_audit_detail := pg_catalog.jsonb_build_object(
                        'field', 'avatar', 'avatar_ref', v_new_avatar_ref,
                        'kind', v_avatar_kind
                    );
                END IF;
                v_audit_id := aap_audit_v1.record_identity_account_event(
                    v_audit_event, v_audit_actor, v_context, v_audit_detail
                );
            END IF;
            v_account := aap_identity_accounts_v1._account_record(v_actor_id);
            v_data := pg_catalog.jsonb_build_object(
                'account', v_account,
                'preferences', NULL,
                'previous_avatar_ref', CASE
                    WHEN v_changed THEN v_user_avatar_ref ELSE NULL::text
                END,
                'changed', v_changed
            );
        END IF;

        INSERT INTO public.identity_account_operation_receipts (
            operation_id, actor_id, operation, request_scope, result, audit_id
        ) VALUES (
            v_operation_id, v_actor_id, v_operation, v_request_scope, v_data, v_audit_id
        );
    EXCEPTION WHEN unique_violation THEN
        GET STACKED DIAGNOSTICS v_constraint_name = CONSTRAINT_NAME;
        IF v_constraint_name = 'identity_account_operation_receipts_pkey' THEN
            RETURN aap_identity_accounts_v1._failure('operation_conflict', NULL, NULL);
        END IF;
        RAISE;
    END;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-accounts-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'principal', v_principal,
        'data', v_data
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_accounts_v1.apply_account_command(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_accounts_v1.mark_inactive_accounts_offline(
    p_cutoff timestamptz
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_affected bigint;
BEGIN
    IF p_cutoff IS NULL OR NOT pg_catalog.isfinite(p_cutoff) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid presence cutoff';
    END IF;
    UPDATE public.users AS account
    SET status = 'offline'
    WHERE account.status = 'online'
      AND (account.last_seen_at IS NULL OR account.last_seen_at < p_cutoff);
    GET DIAGNOSTICS v_affected = ROW_COUNT;
    RETURN v_affected;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_accounts_v1.mark_inactive_accounts_offline(
    timestamptz
) FROM PUBLIC;
