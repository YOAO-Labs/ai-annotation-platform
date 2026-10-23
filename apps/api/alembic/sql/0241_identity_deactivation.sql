-- Native transactional ownership for self-deactivation, due processing and the
-- shared platform-super-admin invariant fence.
CREATE SCHEMA aap_identity_deactivation_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_deactivation_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_deactivation_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    operation text NOT NULL CHECK (
        operation IN ('request_self_deactivation', 'cancel_self_deactivation')
    ),
    request_fingerprint text NOT NULL CHECK (
        request_fingerprint COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    derivation_id text NOT NULL CHECK (derivation_id COLLATE "C" ~ '^[0-9a-f]{64}$'),
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    audit_id bigint,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_identity_deactivation_operation_receipts_actor
    ON public.identity_deactivation_operation_receipts (actor_id, created_at DESC);
-- aap-statement-break
COMMENT ON TABLE public.identity_deactivation_operation_receipts IS
    'Frozen self-deactivation command results; operation IDs are never retargeted.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_deactivation_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_deactivation_due_receipts (
    operation_id uuid PRIMARY KEY,
    protocol text NOT NULL CHECK (protocol = 'aap-identity-deactivation-due-native/v1'),
    requested_limit smallint NOT NULL CHECK (requested_limit BETWEEN 1 AND 500),
    processed smallint NOT NULL CHECK (processed >= 0),
    examined smallint NOT NULL CHECK (examined BETWEEN 0 AND requested_limit),
    blocked_last_super_admin smallint NOT NULL CHECK (
        blocked_last_super_admin >= 0
        AND processed + blocked_last_super_admin = examined
    ),
    has_more boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
COMMENT ON TABLE public.identity_deactivation_due_receipts IS
    'Frozen, bounded due-processing pages, including zero-progress pages.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_deactivation_due_receipts FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_deactivation_requests (
    request_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    requested_at timestamptz,
    scheduled_at timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'cancelled', 'completed')),
    origin text NOT NULL CHECK (origin IN ('native', 'legacy')),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE UNIQUE INDEX ux_identity_deactivation_requests_pending_user
    ON public.identity_deactivation_requests (user_id) WHERE state = 'pending';
-- aap-statement-break
CREATE INDEX ix_identity_deactivation_requests_history
    ON public.identity_deactivation_requests (user_id, created_at DESC);
-- aap-statement-break
COMMENT ON TABLE public.identity_deactivation_requests IS
    'Correlation ledger for accepted and legacy requests; User fields remain authoritative.';
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_deactivation_requests FROM PUBLIC;
-- aap-statement-break
INSERT INTO public.identity_deactivation_requests (
    request_id, user_id, requested_at, scheduled_at, state, origin
)
SELECT pg_catalog.gen_random_uuid(), account.id,
       account.deactivation_requested_at, account.deactivation_scheduled_at,
       'pending', 'legacy'
FROM public.users AS account
WHERE account.deactivation_scheduled_at IS NOT NULL
ON CONFLICT DO NOTHING;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.lock_super_admin_invariant()
RETURNS boolean
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-identity-super-admin-invariant/v1', 0)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.lock_super_admin_invariant() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.super_admin_status(p_excluded_user uuid)
RETURNS jsonb
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-super-admin-status/v1',
        'other_active', pg_catalog.count(*) FILTER (
            WHERE account.is_active
        )::integer,
        'other_unreserved', pg_catalog.count(*) FILTER (
            WHERE account.is_active
              AND account.deactivation_requested_at IS NULL
              AND account.deactivation_scheduled_at IS NULL
        )::integer
    )
    FROM public.users AS account
    WHERE account.role = 'super_admin'
      AND (p_excluded_user IS NULL OR account.id <> p_excluded_user)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_v1.super_admin_status(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_identity_deactivation_event(
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
    v_count integer;
    v_actor_id uuid;
    v_actor_email text;
    v_actor_role text;
    v_action text;
    v_detail_json jsonb;
    v_scheduled_at timestamptz;
    v_audit_id bigint;
    v_prior_update_setting text;
    v_ip inet;
BEGIN
    IF p_event IS NULL OR p_event NOT IN (
        'user.deactivation_request', 'user.deactivation_cancel',
        'user.deactivation_approve'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation audit event';
    END IF;
    IF pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation audit context';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_context);
    IF v_count <> 4 OR NOT (p_context ?& ARRAY['method', 'path', 'ip', 'request_id']) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation audit context';
    END IF;
    v_ip := NULLIF(p_context->>'ip', '')::inet;
    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation audit actor';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_actor);
    IF v_count <> 3 OR NOT (p_actor ?& ARRAY['actor_id', 'email', 'role'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_actor) AS field(name)
           WHERE field.name NOT IN ('actor_id', 'email', 'role')
       ) OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
       OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_actor->>'email') NOT BETWEEN 1 AND 255
       OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
       OR p_actor->>'role' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation audit actor';
    END IF;
    v_actor_id := (p_actor->>'actor_id')::uuid;
    v_actor_email := p_actor->>'email';
    v_actor_role := p_actor->>'role';

    IF p_event = 'user.deactivation_request' THEN
        SELECT pg_catalog.count(*)::integer INTO v_count
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object'
           OR v_count <> 2 OR NOT (p_detail ?& ARRAY['scheduled_at', 'reason_present'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(p_detail) AS field(name)
               WHERE field.name NOT IN ('scheduled_at', 'reason_present')
           ) OR pg_catalog.jsonb_typeof(p_detail->'scheduled_at') IS DISTINCT FROM 'string'
           OR (p_detail->>'scheduled_at') !~* (
               '(z|[+-][0-9]{2}' || pg_catalog.chr(58) || '[0-9]{2})$'
           ) OR pg_catalog.jsonb_typeof(p_detail->'reason_present') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation request detail';
        END IF;
        BEGIN
            v_scheduled_at := (p_detail->>'scheduled_at')::timestamptz;
        EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
                     OR datetime_field_overflow THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation request schedule';
        END;
        IF NOT pg_catalog.isfinite(v_scheduled_at) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation request schedule';
        END IF;
        v_action := 'user.deactivation_request';
        v_detail_json := p_detail;
    ELSIF p_event = 'user.deactivation_cancel' THEN
        IF p_detail IS NOT NULL AND (
            pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object'
            OR p_detail <> '{}'::jsonb
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation cancel detail';
        END IF;
        v_action := 'user.deactivation_cancel';
        v_detail_json := NULL;
    ELSE
        SELECT pg_catalog.count(*)::integer INTO v_count
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object'
           OR v_count <> 4
           OR NOT (p_detail ?& ARRAY['email', 'role', 'scheduled_at', 'auto'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(p_detail) AS field(name)
               WHERE field.name NOT IN ('email', 'role', 'scheduled_at', 'auto')
           ) OR p_detail->>'email' IS DISTINCT FROM v_actor_email
           OR p_detail->>'role' IS DISTINCT FROM v_actor_role
           OR pg_catalog.jsonb_typeof(p_detail->'auto') IS DISTINCT FROM 'boolean'
           OR (p_detail->>'auto')::boolean IS DISTINCT FROM true
           OR pg_catalog.jsonb_typeof(p_detail->'scheduled_at') NOT IN ('string', 'null')
           OR (pg_catalog.jsonb_typeof(p_detail->'scheduled_at') = 'string'
               AND (p_detail->>'scheduled_at') !~* (
                   '(z|[+-][0-9]{2}' || pg_catalog.chr(58) || '[0-9]{2})$'
               )) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation approval detail';
        END IF;
        IF pg_catalog.jsonb_typeof(p_detail->'scheduled_at') = 'string' THEN
            BEGIN
                v_scheduled_at := (p_detail->>'scheduled_at')::timestamptz;
            EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
                         OR datetime_field_overflow THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation approval schedule';
            END;
            IF NOT pg_catalog.isfinite(v_scheduled_at) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation approval schedule';
            END IF;
        END IF;
        v_action := 'user.deactivation_approve';
        v_detail_json := p_detail;
    END IF;

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        v_actor_id, v_actor_email, v_actor_role, v_action, 'user', v_actor_id::text,
        p_context->>'method', p_context->>'path', 200,
        v_ip, p_context->>'request_id', v_detail_json
    ) RETURNING id INTO v_audit_id;

    IF p_event = 'user.deactivation_approve' THEN
        v_prior_update_setting := current_setting('app.allow_audit_update', true);
        PERFORM pg_catalog.set_config('app.allow_audit_update', 'true', true);
        UPDATE public.audit_logs
        SET actor_email = NULL, actor_role = NULL
        WHERE actor_id = v_actor_id;
        PERFORM pg_catalog.set_config(
            'app.allow_audit_update', COALESCE(v_prior_update_setting, ''), true
        );
    END IF;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_deactivation_event(
    text, jsonb, jsonb, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deactivation_v1._failure(
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
        'schema', 'aap-identity-deactivation-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object(
            'code', p_code, 'status_code', p_status_code, 'detail', p_detail
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_deactivation_v1._failure(text, smallint, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deactivation_v1._due_failure(p_code text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-deactivation-due-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_deactivation_v1._due_failure(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deactivation_v1._strip_unicode_space(p_value text)
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
REVOKE ALL ON FUNCTION aap_identity_deactivation_v1._strip_unicode_space(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deactivation_v1._account_record(p_user_id uuid)
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
REVOKE ALL ON FUNCTION aap_identity_deactivation_v1._account_record(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deactivation_v1._actor(
    p_actor jsonb,
    p_expected_key_hash text
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
    v_count integer;
    v_actor_id uuid;
    v_credential_id uuid;
    v_kind text;
    v_generation bigint;
    v_expiry timestamptz;
    v_user record;
    v_key_hash text;
    v_scopes jsonb;
    v_key_expiry timestamptz;
BEGIN
    IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_deactivation_v1._failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_actor);
    IF v_count <> 10 OR NOT (p_actor ?& ARRAY[
        'actor_id', 'name', 'email', 'role', 'is_active', 'credential_type',
        'credential_id', 'generation', 'scopes', 'expires_at'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_actor) AS field(name)
        WHERE field.name NOT IN (
            'actor_id', 'name', 'email', 'role', 'is_active', 'credential_type',
            'credential_id', 'generation', 'scopes', 'expires_at'
        )
    ) OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
      OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      OR pg_catalog.jsonb_typeof(p_actor->'name') IS DISTINCT FROM 'string'
      OR pg_catalog.char_length(p_actor->>'name') > 100
      OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
      OR pg_catalog.char_length(p_actor->>'email') > 255
      OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
      OR p_actor->>'role' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer')
      OR p_actor->'is_active' IS DISTINCT FROM 'true'::jsonb
      OR pg_catalog.jsonb_typeof(p_actor->'credential_type') IS DISTINCT FROM 'string'
      OR p_actor->>'credential_type' NOT IN ('jwt', 'api_key')
      OR pg_catalog.jsonb_typeof(p_actor->'credential_id') IS DISTINCT FROM 'string'
      OR (p_actor->>'credential_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_identity_deactivation_v1._failure('invalid_request');
    END IF;
    BEGIN
        v_actor_id := (p_actor->>'actor_id')::uuid;
        v_credential_id := (p_actor->>'credential_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_deactivation_v1._failure('invalid_request');
    END;
    v_kind := p_actor->>'credential_type';
    IF v_kind = 'jwt' THEN
        IF pg_catalog.jsonb_typeof(p_actor->'generation') IS DISTINCT FROM 'number'
           OR (p_actor->>'generation') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.jsonb_typeof(p_actor->'scopes') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_actor->'expires_at') IS DISTINCT FROM 'string'
           OR (p_actor->>'expires_at') !~* (
               '(z|[+-][0-9]{2}' || pg_catalog.chr(58) || '[0-9]{2})$'
           ) OR p_expected_key_hash IS NOT NULL THEN
            RETURN aap_identity_deactivation_v1._failure('invalid_request');
        END IF;
        BEGIN
            v_generation := (p_actor->>'generation')::bigint;
            v_expiry := (p_actor->>'expires_at')::timestamptz;
        EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
                     OR datetime_field_overflow OR numeric_value_out_of_range THEN
            RETURN aap_identity_deactivation_v1._failure('invalid_request');
        END;
        IF v_generation < 0 OR NOT pg_catalog.isfinite(v_expiry) THEN
            RETURN aap_identity_deactivation_v1._failure('invalid_request');
        END IF;
    ELSE
        IF pg_catalog.jsonb_typeof(p_actor->'generation') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_actor->'scopes') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_actor->'expires_at') NOT IN ('null', 'string')
           OR p_expected_key_hash IS NULL
           OR p_expected_key_hash COLLATE "C" !~ '^[0-9a-f]{64}$'
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_array_elements(p_actor->'scopes') AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
           ) OR (pg_catalog.jsonb_typeof(p_actor->'expires_at') = 'string'
               AND (p_actor->>'expires_at') !~* (
                   '(z|[+-][0-9]{2}' || pg_catalog.chr(58) || '[0-9]{2})$'
               )) THEN
            RETURN aap_identity_deactivation_v1._failure('invalid_request');
        END IF;
        IF pg_catalog.jsonb_typeof(p_actor->'expires_at') = 'string' THEN
            BEGIN
                v_expiry := (p_actor->>'expires_at')::timestamptz;
            EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
                         OR datetime_field_overflow THEN
                RETURN aap_identity_deactivation_v1._failure('invalid_request');
            END;
            IF NOT pg_catalog.isfinite(v_expiry) THEN
                RETURN aap_identity_deactivation_v1._failure('invalid_request');
            END IF;
        END IF;
    END IF;

    -- The caller already owns the global super-admin invariant fence. Lock the
    -- User first; only then inspect JWT/API-key state, preserving auth writers'
    -- User -> credential lock order.
    BEGIN
        SELECT account.id, account.name, account.email, account.role,
               account.is_active, account.credential_generation
        INTO v_user
        FROM public.users AS account
        WHERE account.id = v_actor_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_deactivation_v1._failure('account_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_identity_deactivation_v1._failure('credential_invalid');
    END IF;
    IF NOT v_user.is_active THEN
        RETURN aap_identity_deactivation_v1._failure('user_inactive');
    END IF;
    IF v_user.role IS NULL
       OR v_user.role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN aap_identity_deactivation_v1._failure('invalid_account_role');
    END IF;

    IF v_kind = 'jwt' THEN
        IF v_user.credential_generation IS DISTINCT FROM v_generation THEN
            RETURN aap_identity_deactivation_v1._failure('generation_outdated');
        END IF;
        IF v_expiry <= pg_catalog.clock_timestamp() THEN
            RETURN aap_identity_deactivation_v1._failure('token_expired');
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock_shared(
            pg_catalog.hashtextextended('aap-identity-jti:' || v_credential_id::text, 0)
        ) THEN
            RETURN aap_identity_deactivation_v1._failure('credential_busy');
        END IF;
        IF aap_identity_v1.is_jti_revoked(v_credential_id) THEN
            RETURN aap_identity_deactivation_v1._failure('credential_revoked');
        END IF;
    ELSE
        BEGIN
            SELECT key.key_hash, key.scopes, key.expires_at
            INTO v_key_hash, v_scopes, v_key_expiry
            FROM public.api_keys AS key
            WHERE key.id = v_credential_id AND key.user_id = v_actor_id
              AND key.revoked_at IS NULL
            FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_deactivation_v1._failure('credential_busy');
        END;
        IF NOT FOUND THEN
            RETURN aap_identity_deactivation_v1._failure('credential_revoked');
        END IF;
        IF v_key_hash IS DISTINCT FROM p_expected_key_hash
           OR v_scopes IS DISTINCT FROM p_actor->'scopes'
           OR v_key_expiry IS DISTINCT FROM v_expiry THEN
            RETURN aap_identity_deactivation_v1._failure('credential_invalid');
        END IF;
        IF v_key_expiry IS NOT NULL AND v_key_expiry <= pg_catalog.clock_timestamp() THEN
            RETURN aap_identity_deactivation_v1._failure('credential_revoked');
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', v_user.id, 'name', v_user.name, 'email', v_user.email,
            'role', v_user.role, 'is_active', true, 'credential_type', v_kind,
            'credential_id', v_credential_id,
            'generation', CASE WHEN v_kind = 'jwt' THEN v_user.credential_generation END,
            'scopes', CASE WHEN v_kind = 'api_key' THEN v_scopes END,
            'expires_at', CASE WHEN v_kind = 'jwt' THEN v_expiry ELSE v_key_expiry END
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_deactivation_v1._actor(jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deactivation_v1._reconcile_request(
    p_user_id uuid,
    p_requested_at timestamptz,
    p_scheduled_at timestamptz
)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_request_id uuid;
    v_requested_at timestamptz;
    v_scheduled_at timestamptz;
BEGIN
    IF p_user_id IS NULL OR p_scheduled_at IS NULL OR NOT pg_catalog.isfinite(p_scheduled_at)
       OR (p_requested_at IS NOT NULL AND NOT pg_catalog.isfinite(p_requested_at)) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid deactivation correlation';
    END IF;
    SELECT request.request_id, request.requested_at, request.scheduled_at
    INTO v_request_id, v_requested_at, v_scheduled_at
    FROM public.identity_deactivation_requests AS request
    WHERE request.user_id = p_user_id AND request.state = 'pending'
    FOR UPDATE;
    IF FOUND THEN
        IF v_requested_at IS NOT DISTINCT FROM p_requested_at
           AND v_scheduled_at IS NOT DISTINCT FROM p_scheduled_at THEN
            RETURN v_request_id;
        END IF;
        UPDATE public.identity_deactivation_requests
        SET state = 'cancelled', updated_at = pg_catalog.clock_timestamp()
        WHERE request_id = v_request_id;
    END IF;
    v_request_id := pg_catalog.gen_random_uuid();
    INSERT INTO public.identity_deactivation_requests (
        request_id, user_id, requested_at, scheduled_at, state, origin
    ) VALUES (
        v_request_id, p_user_id, p_requested_at, p_scheduled_at, 'pending', 'legacy'
    );
    RETURN v_request_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_deactivation_v1._reconcile_request(
    uuid, timestamptz, timestamptz
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deactivation_v1.deactivation_policy()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-deactivation-policy/v1',
        'policy', 'aap-identity-deactivation/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_deactivation_v1.deactivation_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deactivation_v1.due_policy()
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-deactivation-due-policy/v1',
        'policy', 'aap-identity-deactivation-due/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_deactivation_v1.due_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deactivation_v1.execute_deactivation_command(p_command jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_count integer;
    v_operation text;
    v_operation_id uuid;
    v_fingerprint text;
    v_derivation_id text;
    v_actor_id uuid;
    v_actor jsonb;
    v_actor_result jsonb;
    v_principal jsonb;
    v_expected_key_hash text;
    v_context jsonb;
    v_payload jsonb;
    v_reason text;
    v_user record;
    v_status jsonb;
    v_message text;
    v_now timestamptz;
    v_scheduled_at timestamptz;
    v_receipt record;
    v_result jsonb;
    v_account jsonb;
    v_request_id uuid;
    v_audit_id bigint;
    v_receivers uuid[];
BEGIN
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_deactivation_v1._failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_command);
    IF v_count <> 9 OR NOT (p_command ?& ARRAY[
        'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
        'request_fingerprint', 'derivation_id', 'audit_context', 'payload'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_command) AS field(name)
        WHERE field.name NOT IN (
            'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
            'request_fingerprint', 'derivation_id', 'audit_context', 'payload'
        )
    ) OR p_command->>'schema' IS DISTINCT FROM 'aap-identity-deactivation-native/v1'
       OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'audit_context') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_command->'payload') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_command->'expected_key_hash') NOT IN ('string', 'null') THEN
        RETURN aap_identity_deactivation_v1._failure('invalid_request');
    END IF;
    v_operation := p_command->>'operation';
    IF v_operation NOT IN ('request_self_deactivation', 'cancel_self_deactivation')
       OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN aap_identity_deactivation_v1._failure('invalid_request');
    END IF;
    BEGIN
        v_operation_id := (p_command->>'operation_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_deactivation_v1._failure('invalid_request');
    END;
    v_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_actor := p_command->'actor';
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_context := p_command->'audit_context';
    v_payload := p_command->'payload';

    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(v_context);
    IF v_count <> 4 OR NOT (v_context ?& ARRAY['method', 'path', 'ip', 'request_id'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(v_context) AS field(name)
           WHERE field.name NOT IN ('method', 'path', 'ip', 'request_id')
       ) OR pg_catalog.jsonb_typeof(v_context->'method') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_context->'path') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_context->'ip') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_context->'request_id') NOT IN ('string', 'null')
       OR NOT aap_audit_v1.valid_request_context(v_context) THEN
        RETURN aap_identity_deactivation_v1._failure('invalid_request');
    END IF;
    IF v_operation = 'request_self_deactivation' THEN
        SELECT pg_catalog.count(*)::integer INTO v_count
        FROM pg_catalog.jsonb_object_keys(v_payload);
        IF v_count <> 1 OR NOT (v_payload ? 'reason')
           OR pg_catalog.jsonb_typeof(v_payload->'reason') NOT IN ('string', 'null')
           OR (pg_catalog.jsonb_typeof(v_payload->'reason') = 'string'
               AND pg_catalog.char_length(v_payload->>'reason') > 500) THEN
            RETURN aap_identity_deactivation_v1._failure('invalid_request');
        END IF;
        v_reason := aap_identity_deactivation_v1._strip_unicode_space(v_payload->>'reason');
        IF v_reason = '' THEN v_reason := NULL; END IF;
    ELSE
        SELECT pg_catalog.count(*)::integer INTO v_count
        FROM pg_catalog.jsonb_object_keys(v_payload);
        IF v_count <> 0 THEN
            RETURN aap_identity_deactivation_v1._failure('invalid_request');
        END IF;
    END IF;

    IF NOT aap_identity_v1.lock_super_admin_invariant() THEN
        RETURN aap_identity_deactivation_v1._failure('invariant_busy');
    END IF;
    v_actor_result := aap_identity_deactivation_v1._actor(v_actor, v_expected_key_hash);
    IF v_actor_result->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor_result;
    END IF;
    v_principal := v_actor_result->'data';
    v_actor_id := (v_principal->>'actor_id')::uuid;
    SELECT account.deactivation_requested_at, account.deactivation_reason,
           account.deactivation_scheduled_at
    INTO v_user
    FROM public.users AS account
    WHERE account.id = v_actor_id;

    SELECT receipt.actor_id, receipt.operation, receipt.request_fingerprint,
           receipt.derivation_id, receipt.result
    INTO v_receipt
    FROM public.identity_deactivation_operation_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    IF FOUND THEN
        IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.operation IS DISTINCT FROM v_operation
           OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
           OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
            RETURN aap_identity_deactivation_v1._failure('operation_conflict');
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-deactivation-native/v1', 'ok', true,
            'operation', v_operation, 'operation_id', v_operation_id,
            'principal', v_principal, 'data', v_receipt.result
        );
    END IF;
    IF v_operation = 'request_self_deactivation' AND EXISTS (
        SELECT 1 FROM public.identity_deactivation_requests AS request
        WHERE request.request_id = v_operation_id
    ) THEN
        RETURN aap_identity_deactivation_v1._failure('operation_conflict');
    END IF;

    IF v_operation = 'request_self_deactivation' THEN
        IF v_user.deactivation_requested_at IS NOT NULL
           OR v_user.deactivation_scheduled_at IS NOT NULL THEN
            RETURN aap_identity_deactivation_v1._failure(
                'policy_rejected', 400::smallint,
                pg_catalog.to_jsonb('已有进行中的注销申请'::text)
            );
        END IF;
        IF v_principal->>'role' = 'super_admin' THEN
            v_status := aap_identity_v1.super_admin_status(v_actor_id);
            IF (v_status->>'other_active')::integer = 0 THEN
                v_message := '您是最后一名超级管理员，请先指定继任者再申请注销';
                RETURN aap_identity_deactivation_v1._failure(
                    'policy_rejected', 400::smallint, pg_catalog.to_jsonb(v_message)
                );
            ELSIF (v_status->>'other_unreserved')::integer = 0 THEN
                v_message := '其余超级管理员均已申请注销，请先让其他管理员撤销申请或指定继任者';
                RETURN aap_identity_deactivation_v1._failure(
                    'policy_rejected', 400::smallint, pg_catalog.to_jsonb(v_message)
                );
            END IF;
        END IF;

        v_now := pg_catalog.clock_timestamp();
        v_scheduled_at := v_now + interval '7 days';
        UPDATE public.identity_deactivation_requests
        SET state = 'cancelled', updated_at = v_now
        WHERE user_id = v_actor_id AND state = 'pending';
        UPDATE public.users
        SET deactivation_requested_at = v_now,
            deactivation_reason = v_reason,
            deactivation_scheduled_at = v_scheduled_at
        WHERE id = v_actor_id;
        INSERT INTO public.identity_deactivation_requests (
            request_id, user_id, requested_at, scheduled_at, state, origin, created_at, updated_at
        ) VALUES (
            v_operation_id, v_actor_id, v_now, v_scheduled_at, 'pending', 'native', v_now, v_now
        );
        v_audit_id := aap_audit_v1.record_identity_deactivation_event(
            'user.deactivation_request',
            pg_catalog.jsonb_build_object(
                'actor_id', v_actor_id, 'email', v_principal->>'email',
                'role', v_principal->>'role'
            ),
            v_context,
            pg_catalog.jsonb_build_object(
                'scheduled_at', v_scheduled_at, 'reason_present', v_reason IS NOT NULL
            )
        );
        SELECT COALESCE(
            pg_catalog.array_agg(account.id ORDER BY account.id), ARRAY[]::uuid[]
        ) INTO v_receivers
        FROM public.users AS account
        WHERE account.is_active AND account.role = 'super_admin';
        PERFORM aap_notifications_v1.create_identity_deactivation_notifications(
            v_operation_id,
            'user.deactivation_requested',
            pg_catalog.jsonb_build_object(
                'user_id', v_actor_id, 'email', v_principal->>'email',
                'name', v_principal->>'name', 'role', v_principal->>'role',
                'scheduled_at', v_scheduled_at
            ),
            v_receivers
        );
    ELSE
        IF v_user.deactivation_requested_at IS NULL
           AND v_user.deactivation_scheduled_at IS NULL THEN
            RETURN aap_identity_deactivation_v1._failure(
                'policy_rejected', 400::smallint,
                pg_catalog.to_jsonb('当前无待生效的注销申请'::text)
            );
        END IF;
        v_request_id := aap_identity_deactivation_v1._reconcile_request(
            v_actor_id, v_user.deactivation_requested_at,
            COALESCE(v_user.deactivation_scheduled_at, pg_catalog.clock_timestamp())
        );
        UPDATE public.users
        SET deactivation_requested_at = NULL,
            deactivation_reason = NULL,
            deactivation_scheduled_at = NULL
        WHERE id = v_actor_id;
        UPDATE public.identity_deactivation_requests
        SET state = 'cancelled', updated_at = pg_catalog.clock_timestamp()
        WHERE request_id = v_request_id AND state = 'pending';
        v_audit_id := aap_audit_v1.record_identity_deactivation_event(
            'user.deactivation_cancel',
            pg_catalog.jsonb_build_object(
                'actor_id', v_actor_id, 'email', v_principal->>'email',
                'role', v_principal->>'role'
            ),
            v_context,
            NULL
        );
    END IF;

    v_account := aap_identity_deactivation_v1._account_record(v_actor_id);
    v_result := pg_catalog.jsonb_build_object('account', v_account);
    INSERT INTO public.identity_deactivation_operation_receipts (
        operation_id, actor_id, operation, request_fingerprint,
        derivation_id, result, audit_id
    ) VALUES (
        v_operation_id, v_actor_id, v_operation, v_fingerprint,
        v_derivation_id, v_result, v_audit_id
    );
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-deactivation-native/v1', 'ok', true,
        'operation', v_operation, 'operation_id', v_operation_id,
        'principal', v_principal, 'data', v_result
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_deactivation_v1.execute_deactivation_command(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_deactivation_v1.process_due_deactivations(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_count integer;
    v_operation_id uuid;
    v_limit integer;
    v_now timestamptz;
    v_receipt record;
    v_due record;
    v_status jsonb;
    v_request_id uuid;
    v_audit_id bigint;
    v_receivers uuid[];
    v_processed integer := 0;
    v_examined integer := 0;
    v_blocked integer := 0;
    v_has_more boolean := false;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_deactivation_v1._due_failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_count <> 3 OR NOT (p_request ?& ARRAY['schema', 'operation_id', 'limit'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
           WHERE field.name NOT IN ('schema', 'operation_id', 'limit')
       ) OR p_request->>'schema' IS DISTINCT FROM 'aap-identity-deactivation-due-native/v1'
       OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
       OR (p_request->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'limit') IS DISTINCT FROM 'number'
       OR (p_request->>'limit') !~ '^(0|[1-9][0-9]*)$' THEN
        RETURN aap_identity_deactivation_v1._due_failure('invalid_request');
    END IF;
    BEGIN
        v_operation_id := (p_request->>'operation_id')::uuid;
        v_limit := (p_request->>'limit')::integer;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_identity_deactivation_v1._due_failure('invalid_request');
    END;
    IF v_limit NOT BETWEEN 1 AND 500 THEN
        RETURN aap_identity_deactivation_v1._due_failure('invalid_request');
    END IF;
    IF NOT aap_identity_v1.lock_super_admin_invariant() THEN
        RETURN aap_identity_deactivation_v1._due_failure('invariant_busy');
    END IF;

    SELECT receipt.protocol, receipt.requested_limit, receipt.processed,
           receipt.examined, receipt.blocked_last_super_admin, receipt.has_more
    INTO v_receipt
    FROM public.identity_deactivation_due_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    IF FOUND THEN
        IF v_receipt.protocol IS DISTINCT FROM 'aap-identity-deactivation-due-native/v1'
           OR v_receipt.requested_limit IS DISTINCT FROM v_limit THEN
            RETURN aap_identity_deactivation_v1._due_failure('operation_conflict');
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-deactivation-due-native/v1', 'ok', true,
            'operation_id', v_operation_id,
            'processed', v_receipt.processed, 'examined', v_receipt.examined,
            'blocked_last_super_admin', v_receipt.blocked_last_super_admin,
            'has_more', v_receipt.has_more
        );
    END IF;

    -- DB time is chosen only after the shared invariant fence is held.
    v_now := pg_catalog.clock_timestamp();
    FOR v_due IN
        SELECT account.id, account.email, account.name, account.role,
               account.deactivation_requested_at, account.deactivation_reason,
               account.deactivation_scheduled_at
        FROM public.users AS account
        WHERE account.is_active
          AND account.deactivation_scheduled_at IS NOT NULL
          AND account.deactivation_scheduled_at <= v_now
        ORDER BY account.id
        LIMIT v_limit
        FOR UPDATE OF account SKIP LOCKED
    LOOP
        v_examined := v_examined + 1;
        v_request_id := aap_identity_deactivation_v1._reconcile_request(
            v_due.id, v_due.deactivation_requested_at, v_due.deactivation_scheduled_at
        );
        IF v_due.role = 'super_admin' THEN
            v_status := aap_identity_v1.super_admin_status(v_due.id);
            IF (v_status->>'other_active')::integer = 0 THEN
                v_blocked := v_blocked + 1;
                CONTINUE;
            END IF;
        END IF;

        UPDATE public.users
        SET is_active = false,
            status = 'offline',
            disabled_kind = 'deleted',
            disabled_at = v_now,
            disabled_by = v_due.id,
            disabled_reason = COALESCE(v_due.deactivation_reason, '自助注销申请到期'),
            deactivation_requested_at = NULL,
            deactivation_reason = NULL,
            deactivation_scheduled_at = NULL
        WHERE id = v_due.id;
        v_audit_id := aap_audit_v1.record_identity_deactivation_event(
            'user.deactivation_approve',
            pg_catalog.jsonb_build_object(
                'actor_id', v_due.id, 'email', v_due.email, 'role', v_due.role
            ),
            pg_catalog.jsonb_build_object(
                'method', NULL, 'path', NULL, 'ip', NULL, 'request_id', NULL
            ),
            pg_catalog.jsonb_build_object(
                'email', v_due.email, 'role', v_due.role,
                'scheduled_at', v_due.deactivation_scheduled_at, 'auto', true
            )
        );
        UPDATE public.identity_deactivation_requests
        SET state = 'completed', updated_at = v_now
        WHERE request_id = v_request_id AND state = 'pending';
        SELECT COALESCE(
            pg_catalog.array_agg(account.id ORDER BY account.id), ARRAY[]::uuid[]
        ) INTO v_receivers
        FROM public.users AS account
        WHERE account.is_active AND account.role = 'super_admin'
          AND account.id <> v_due.id;
        PERFORM aap_notifications_v1.create_identity_deactivation_notifications(
            v_request_id,
            'user.deactivation_completed',
            pg_catalog.jsonb_build_object(
                'user_id', v_due.id, 'email', v_due.email, 'name', v_due.name
            ),
            v_receivers
        );
        v_processed := v_processed + 1;
    END LOOP;

    SELECT EXISTS (
        SELECT 1 FROM public.users AS account
        WHERE account.is_active
          AND account.deactivation_scheduled_at IS NOT NULL
          AND account.deactivation_scheduled_at <= pg_catalog.clock_timestamp()
          AND (
              account.role <> 'super_admin'
              OR EXISTS (
                  SELECT 1 FROM public.users AS other_admin
                  WHERE other_admin.is_active
                    AND other_admin.role = 'super_admin'
                    AND other_admin.id <> account.id
              )
          )
    ) INTO v_has_more;
    INSERT INTO public.identity_deactivation_due_receipts (
        operation_id, protocol, requested_limit, processed, examined,
        blocked_last_super_admin, has_more
    ) VALUES (
        v_operation_id, 'aap-identity-deactivation-due-native/v1', v_limit,
        v_processed, v_examined, v_blocked, v_has_more
    );
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-deactivation-due-native/v1', 'ok', true,
        'operation_id', v_operation_id,
        'processed', v_processed, 'examined', v_examined,
        'blocked_last_super_admin', v_blocked, 'has_more', v_has_more
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_deactivation_v1.process_due_deactivations(jsonb)
    FROM PUBLIC;
