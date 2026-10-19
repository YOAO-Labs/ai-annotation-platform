-- Native session commands and the Audit-owned append-only event capability.
CREATE SCHEMA aap_identity_sessions_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_identity_sessions_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.identity_session_operation_receipts (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    operation text NOT NULL CHECK (operation IN (
        'login', 'refresh', 'logout', 'logout_all'
    )),
    request_scope jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(request_scope) = 'object'
    ),
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    audit_id bigint,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_identity_session_operation_receipts_actor
    ON public.identity_session_operation_receipts (actor_id, created_at DESC);
-- aap-statement-break
REVOKE ALL ON TABLE public.identity_session_operation_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_sessions_v1._failure(
    p_code text,
    p_failed_count bigint DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-sessions-native/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object(
            'code', p_code,
            'failed_count', p_failed_count
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_sessions_v1._failure(text, bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_identity_session_event(
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
    v_detail_email text;
    v_user_agent text;
    v_failed_count bigint;
    v_status_code smallint;
    v_action text;
    v_target_id text;
    v_audit_id bigint;
    v_timestamp_seconds bigint;
BEGIN
    IF p_event IS NULL OR p_event NOT IN (
        'login_success', 'login_invalid', 'login_inactive', 'login_unverified',
        'refresh', 'logout', 'logout_all'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid session audit event';
    END IF;
    IF pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid session audit context';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_context);
    IF v_field_count <> 4
       OR NOT (p_context ?& ARRAY['method', 'path', 'ip', 'request_id'])
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid session audit context';
    END IF;

    IF p_actor IS NULL THEN
        IF p_event <> 'login_invalid' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'session audit actor required';
        END IF;
    ELSE
        IF pg_catalog.jsonb_typeof(p_actor) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid session audit actor';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(p_actor);
        IF v_field_count <> 3
           OR NOT (p_actor ?& ARRAY['actor_id', 'email', 'role'])
           OR pg_catalog.jsonb_typeof(p_actor->'actor_id') IS DISTINCT FROM 'string'
           OR (p_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_actor->'email') IS DISTINCT FROM 'string'
           OR pg_catalog.length(p_actor->>'email') NOT BETWEEN 0 AND 255
           OR pg_catalog.jsonb_typeof(p_actor->'role') IS DISTINCT FROM 'string'
           OR p_actor->>'role' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid session audit actor';
        END IF;
        BEGIN
            v_actor_id := (p_actor->>'actor_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid session audit actor';
        END;
        v_actor_email := p_actor->>'email';
        v_actor_role := p_actor->>'role';
    END IF;

    IF p_event = 'login_invalid' THEN
        IF p_actor IS NOT NULL OR pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid failed login audit';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF v_field_count <> 4
           OR NOT (p_detail ?& ARRAY['email', 'result', 'user_agent', 'ip_failed_count'])
           OR pg_catalog.jsonb_typeof(p_detail->'email') IS DISTINCT FROM 'string'
           OR pg_catalog.length(p_detail->>'email') NOT BETWEEN 0 AND 16384
           OR p_detail->>'result' IS DISTINCT FROM 'invalid_credentials'
           OR pg_catalog.jsonb_typeof(p_detail->'user_agent') IS DISTINCT FROM 'string'
           OR pg_catalog.length(p_detail->>'user_agent') NOT BETWEEN 0 AND 256
           OR pg_catalog.jsonb_typeof(p_detail->'ip_failed_count') IS DISTINCT FROM 'number'
           OR (p_detail->>'ip_failed_count') !~ '^(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid failed login audit';
        END IF;
        BEGIN
            v_failed_count := (p_detail->>'ip_failed_count')::bigint;
        EXCEPTION WHEN numeric_value_out_of_range THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid failed login audit';
        END;
        v_detail_email := p_detail->>'email';
        v_user_agent := p_detail->>'user_agent';
        v_target_id := pg_catalog.left(v_detail_email, 64);
        v_action := 'auth.login';
        v_status_code := 401;
    ELSIF p_event IN ('login_success', 'login_inactive', 'login_unverified') THEN
        IF p_actor IS NULL OR pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid login audit';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF v_field_count <> 2
           OR NOT (p_detail ?& ARRAY['email', 'result'])
           OR pg_catalog.jsonb_typeof(p_detail->'email') IS DISTINCT FROM 'string'
           OR pg_catalog.length(p_detail->>'email') NOT BETWEEN 0 AND 255
           OR p_detail->>'email' IS DISTINCT FROM v_actor_email
           OR p_detail->>'result' IS DISTINCT FROM (CASE p_event
               WHEN 'login_success' THEN 'success'
               WHEN 'login_inactive' THEN 'deactivated'
               ELSE 'email_not_verified'
           END) THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid login audit';
        END IF;
        v_target_id := v_actor_id::text;
        v_action := 'auth.login';
        v_status_code := CASE p_event
            WHEN 'login_success' THEN 200
            WHEN 'login_inactive' THEN 403
            ELSE 400
        END;
    ELSIF p_event = 'refresh' THEN
        IF p_actor IS NULL OR pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid refresh audit';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF v_field_count <> 2
           OR NOT (p_detail ?& ARRAY['old_jti', 'expired_seconds_ago'])
           OR pg_catalog.jsonb_typeof(p_detail->'old_jti') IS DISTINCT FROM 'string'
           OR (p_detail->>'old_jti') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_detail->'expired_seconds_ago') IS DISTINCT FROM 'number'
           OR (p_detail->>'expired_seconds_ago') !~ '^-?(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid refresh audit';
        END IF;
        BEGIN
            v_timestamp_seconds := (p_detail->>'expired_seconds_ago')::bigint;
        EXCEPTION WHEN numeric_value_out_of_range THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid refresh audit';
        END;
        v_target_id := v_actor_id::text;
        v_action := 'auth.token_refresh';
        v_status_code := 200;
    ELSIF p_event = 'logout' THEN
        IF p_actor IS NULL OR (p_detail IS NOT NULL
           AND pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'null') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid logout audit';
        END IF;
        v_target_id := v_actor_id::text;
        v_action := 'auth.logout';
        v_status_code := 204;
        p_detail := NULL;
    ELSE
        IF p_actor IS NULL OR pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid logout-all audit';
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(p_detail);
        IF v_field_count <> 1
           OR NOT (p_detail ? 'new_generation')
           OR pg_catalog.jsonb_typeof(p_detail->'new_generation') IS DISTINCT FROM 'number'
           OR (p_detail->>'new_generation') !~ '^[1-9][0-9]*$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid logout-all audit';
        END IF;
        BEGIN
            v_timestamp_seconds := (p_detail->>'new_generation')::bigint;
        EXCEPTION WHEN numeric_value_out_of_range THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid logout-all audit';
        END;
        v_target_id := v_actor_id::text;
        v_action := 'auth.logout_all';
        v_status_code := 200;
    END IF;

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        v_actor_id, v_actor_email, v_actor_role, v_action, 'user', v_target_id,
        p_context->>'method', p_context->>'path', v_status_code,
        NULLIF(p_context->>'ip', '')::pg_catalog.inet,
        p_context->>'request_id', p_detail
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_session_event(
    text, jsonb, jsonb, jsonb
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_sessions_v1.session_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-sessions-policy/v1',
        'issuer', 'aap-identity/v1',
        'algorithm', 'HS256',
        'refresh_grace_seconds', 604800
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_sessions_v1.session_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_sessions_v1.read_login_candidate(p_email text)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item jsonb;
BEGIN
    IF p_email IS NULL OR pg_catalog.char_length(p_email) NOT BETWEEN 0 AND 16384 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid login candidate request';
    END IF;
    SELECT pg_catalog.jsonb_build_object(
        'actor_id', actor.id,
        'password_hash', actor.password_hash
    ) INTO v_item
    FROM public.users AS actor
    WHERE actor.email = p_email;
    IF FOUND AND (
        v_item->>'password_hash' IS NULL
        OR pg_catalog.char_length(v_item->>'password_hash') NOT BETWEEN 1 AND 255
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid stored login credential';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-login-candidate/v1',
        'item', v_item
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_sessions_v1.read_login_candidate(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_sessions_v1.execute_session_command(p_request jsonb)
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
    v_actor_id uuid;
    v_credential_id uuid;
    v_credential_type text;
    v_actor_generation bigint;
    v_actor_expiry timestamptz;
    v_expected_key_hash text;
    v_login jsonb;
    v_login_actor_id uuid;
    v_login_actor_id_text text;
    v_expected_password_hash text;
    v_password_matches boolean;
    v_failed_count bigint;
    v_policy jsonb;
    v_expire_minutes integer;
    v_email_verification_required boolean;
    v_derivation_id text;
    v_request_fingerprint text;
    v_audit_context jsonb;
    v_user_agent text;
    v_now timestamptz;
    v_user_found boolean := false;
    v_user_email text;
    v_user_name text;
    v_user_role text;
    v_user_active boolean;
    v_user_generation bigint;
    v_user_email_verified_at timestamptz;
    v_user_password_hash text;
    v_auth_reply jsonb;
    v_auth_data jsonb;
    v_auth_code text;
    v_constraint_name text;
    v_current_principal jsonb;
    v_audit_actor jsonb;
    v_audit_event text;
    v_audit_detail jsonb;
    v_audit_id bigint;
    v_receipt_found boolean := false;
    v_receipt_actor_id uuid;
    v_receipt_operation text;
    v_receipt_scope jsonb;
    v_receipt_result jsonb;
    v_request_scope jsonb;
    v_data jsonb;
    v_claims jsonb;
    v_claim_exp bigint;
    v_claim_generation bigint;
    v_new_generation bigint;
    v_stored_key_hash text;
    v_key_expiry timestamptz;
    v_key_scopes jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_field_count <> 10
       OR NOT (p_request ?& ARRAY[
           'schema', 'operation', 'operation_id', 'actor', 'expected_key_hash',
           'login', 'policy', 'request_fingerprint', 'audit_context', 'user_agent'
       ])
       OR pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' <> 'aap-identity-sessions-native/v1'
       OR pg_catalog.jsonb_typeof(p_request->'operation') IS DISTINCT FROM 'string'
       OR p_request->>'operation' NOT IN ('login', 'refresh', 'logout', 'logout_all')
       OR pg_catalog.jsonb_typeof(p_request->'operation_id') IS DISTINCT FROM 'string'
       OR (p_request->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'actor') NOT IN ('null', 'object')
       OR pg_catalog.jsonb_typeof(p_request->'expected_key_hash') NOT IN ('null', 'string')
       OR pg_catalog.jsonb_typeof(p_request->'login') NOT IN ('null', 'object')
       OR pg_catalog.jsonb_typeof(p_request->'policy') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_request->'request_fingerprint') NOT IN ('null', 'string')
       OR pg_catalog.jsonb_typeof(p_request->'audit_context') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_request->'user_agent') IS DISTINCT FROM 'string' THEN
        RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
    END IF;

    v_operation := p_request->>'operation';
    v_operation_id := (p_request->>'operation_id')::uuid;
    v_actor := p_request->'actor';
    v_expected_key_hash := p_request->>'expected_key_hash';
    v_login := p_request->'login';
    v_policy := p_request->'policy';
    v_request_fingerprint := p_request->>'request_fingerprint';
    v_audit_context := p_request->'audit_context';
    v_user_agent := p_request->>'user_agent';

    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(v_policy);
    IF v_field_count <> 3
       OR NOT (v_policy ?& ARRAY['expire_minutes', 'email_verification_required', 'derivation_id'])
       OR pg_catalog.jsonb_typeof(v_policy->'expire_minutes') IS DISTINCT FROM 'number'
       OR (v_policy->>'expire_minutes') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(v_policy->'email_verification_required') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_policy->'derivation_id') IS DISTINCT FROM 'string'
       OR (v_policy->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.char_length(v_user_agent) NOT BETWEEN 0 AND 256 THEN
        RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
    END IF;
    BEGIN
        v_expire_minutes := (v_policy->>'expire_minutes')::integer;
    EXCEPTION WHEN numeric_value_out_of_range THEN
        RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
    END;
    IF v_expire_minutes NOT BETWEEN 1 AND 525600 THEN
        RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
    END IF;
    v_email_verification_required := (v_policy->>'email_verification_required')::boolean;
    v_derivation_id := v_policy->>'derivation_id';

    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(v_audit_context);
    IF v_field_count <> 4
       OR NOT (v_audit_context ?& ARRAY['method', 'path', 'ip', 'request_id'])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_each(v_audit_context) AS item(key, value)
           WHERE pg_catalog.jsonb_typeof(item.value) NOT IN ('string', 'null')
       )
       OR pg_catalog.char_length(v_audit_context->>'method') > 8
       OR pg_catalog.char_length(v_audit_context->>'path') > 256
       OR pg_catalog.char_length(v_audit_context->>'ip') > 64
       OR pg_catalog.char_length(v_audit_context->>'request_id') > 36 THEN
        RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
    END IF;
    BEGIN
        PERFORM NULLIF(v_audit_context->>'ip', '')::pg_catalog.inet;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
    END;

    IF v_operation = 'login' THEN
        IF pg_catalog.jsonb_typeof(v_actor) IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_request->'expected_key_hash') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(v_login) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request->'request_fingerprint') IS DISTINCT FROM 'string'
           OR (v_request_fingerprint COLLATE "C") !~ '^[0-9a-f]{64}$' THEN
            RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(v_login);
        IF v_field_count <> 5
           OR NOT (v_login ?& ARRAY[
               'email', 'actor_id', 'expected_password_hash', 'password_matches',
               'failed_count'
           ])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_login) AS field(name)
               WHERE field.name NOT IN (
                   'email', 'actor_id', 'expected_password_hash', 'password_matches',
                   'failed_count'
               )
           )
           OR pg_catalog.jsonb_typeof(v_login->'email') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_login->>'email') NOT BETWEEN 0 AND 16384
           OR pg_catalog.jsonb_typeof(v_login->'actor_id') NOT IN ('null', 'string')
           OR pg_catalog.jsonb_typeof(v_login->'expected_password_hash') NOT IN ('null', 'string')
           OR pg_catalog.jsonb_typeof(v_login->'password_matches') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_login->'failed_count') IS DISTINCT FROM 'number'
           OR (v_login->>'failed_count') !~ '^(0|[1-9][0-9]*)$' THEN
            RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
        END IF;
        v_login_actor_id_text := v_login->>'actor_id';
        v_expected_password_hash := v_login->>'expected_password_hash';
        v_password_matches := (v_login->>'password_matches')::boolean;
        BEGIN
            v_failed_count := (v_login->>'failed_count')::bigint;
            IF v_login_actor_id_text IS NOT NULL THEN
                IF v_login_actor_id_text !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                    RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
                END IF;
                v_login_actor_id := v_login_actor_id_text::uuid;
            END IF;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
        END;
        IF (v_login_actor_id IS NULL AND (
                v_expected_password_hash IS NOT NULL OR v_password_matches
            )) OR (v_login_actor_id IS NOT NULL AND (
                v_expected_password_hash IS NULL
                OR pg_catalog.octet_length(v_expected_password_hash) NOT BETWEEN 1 AND 255
            )) THEN
            RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
        END IF;
    ELSE
        IF pg_catalog.jsonb_typeof(v_actor) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_login) IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_request->'request_fingerprint') IS DISTINCT FROM 'null' THEN
            RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_field_count
        FROM pg_catalog.jsonb_object_keys(v_actor);
        IF v_field_count <> 10
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
           OR (v_actor->>'actor_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_actor->'name') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_actor->'email') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_actor->'role') IS DISTINCT FROM 'string'
           OR v_actor->>'role' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer')
           OR v_actor->'is_active' IS DISTINCT FROM 'true'::jsonb
           OR pg_catalog.jsonb_typeof(v_actor->'credential_type') IS DISTINCT FROM 'string'
           OR v_actor->>'credential_type' NOT IN ('jwt', 'api_key')
           OR pg_catalog.jsonb_typeof(v_actor->'credential_id') IS DISTINCT FROM 'string'
           OR (v_actor->>'credential_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
        END IF;
        v_credential_type := v_actor->>'credential_type';
        IF pg_catalog.jsonb_typeof(p_request->'expected_key_hash') IS DISTINCT FROM
           (CASE WHEN v_credential_type = 'api_key' THEN 'string' ELSE 'null' END) THEN
            RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
        END IF;
        IF v_credential_type = 'jwt' THEN
            IF pg_catalog.jsonb_typeof(v_actor->'generation') IS DISTINCT FROM 'number'
               OR (v_actor->>'generation') !~ '^(0|[1-9][0-9]*)$'
               OR pg_catalog.jsonb_typeof(v_actor->'scopes') IS DISTINCT FROM 'null'
               OR pg_catalog.jsonb_typeof(v_actor->'expires_at') IS DISTINCT FROM 'string'
               OR (v_actor->>'expires_at') !~* (
                   '(z|[+-][0-9]{2}' || pg_catalog.chr(58) || '[0-9]{2})$'
               ) THEN
                RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
            END IF;
        ELSE
            IF pg_catalog.jsonb_typeof(v_actor->'generation') IS DISTINCT FROM 'null'
               OR pg_catalog.jsonb_typeof(v_actor->'scopes') IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_typeof(v_actor->'expires_at') NOT IN ('null', 'string')
               OR (pg_catalog.jsonb_typeof(v_actor->'expires_at') = 'string'
                   AND (v_actor->>'expires_at') !~* (
                       '(z|[+-][0-9]{2}' || pg_catalog.chr(58) || '[0-9]{2})$'
                   ))
               OR pg_catalog.octet_length(v_expected_key_hash) NOT BETWEEN 1 AND 255 THEN
                RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(v_actor->'scopes') AS scope(value)
                WHERE pg_catalog.jsonb_typeof(scope.value) IS DISTINCT FROM 'string'
            ) THEN
                RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
            END IF;
        END IF;
    END IF;

    -- Values already passed strict JSON type and shape checks above.
    BEGIN
    v_now := pg_catalog.clock_timestamp();
    IF v_operation = 'login' THEN
        IF v_login_actor_id IS NULL THEN
            v_user_found := false;
        ELSE
            BEGIN
                SELECT actor.id, actor.email, actor.name, actor.role, actor.is_active,
                       actor.credential_generation, actor.email_verified_at,
                       actor.password_hash
                INTO v_actor_id, v_user_email, v_user_name, v_user_role, v_user_active,
                     v_user_generation, v_user_email_verified_at,
                     v_user_password_hash
                FROM public.users AS actor
                WHERE actor.id = v_login_actor_id
                FOR UPDATE NOWAIT;
            EXCEPTION
                WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                    RETURN aap_identity_sessions_v1._failure('account_busy', NULL);
            END;
            v_user_found := FOUND;
            IF NOT v_user_found
               OR v_user_email IS DISTINCT FROM v_login->>'email'
               OR v_user_password_hash IS DISTINCT FROM v_expected_password_hash THEN
                RETURN aap_identity_sessions_v1._failure('account_busy', NULL);
            END IF;
        END IF;

        IF NOT v_password_matches THEN
            v_audit_detail := pg_catalog.jsonb_build_object(
                'email', v_login->>'email',
                'result', 'invalid_credentials',
                'user_agent', v_user_agent,
                'ip_failed_count', v_failed_count
            );
            PERFORM aap_audit_v1.record_identity_session_event(
                'login_invalid', NULL::jsonb, v_audit_context, v_audit_detail
            );
            RETURN aap_identity_sessions_v1._failure('invalid_credentials', v_failed_count);
        END IF;
        IF NOT v_user_found THEN
            RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
        END IF;
        IF NOT v_user_active THEN
            v_audit_actor := pg_catalog.jsonb_build_object(
                'actor_id', v_actor_id, 'email', v_user_email, 'role', v_user_role
            );
            v_audit_detail := pg_catalog.jsonb_build_object(
                'email', v_user_email, 'result', 'deactivated'
            );
            PERFORM aap_audit_v1.record_identity_session_event(
                'login_inactive', v_audit_actor, v_audit_context, v_audit_detail
            );
            RETURN aap_identity_sessions_v1._failure('user_inactive', NULL);
        END IF;
        IF v_user_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
            RETURN aap_identity_sessions_v1._failure('invalid_account_role', NULL);
        END IF;
        IF v_email_verification_required AND v_user_email_verified_at IS NULL THEN
            v_audit_actor := pg_catalog.jsonb_build_object(
                'actor_id', v_actor_id, 'email', v_user_email, 'role', v_user_role
            );
            v_audit_detail := pg_catalog.jsonb_build_object(
                'email', v_user_email, 'result', 'email_not_verified'
            );
            PERFORM aap_audit_v1.record_identity_session_event(
                'login_unverified', v_audit_actor, v_audit_context, v_audit_detail
            );
            RETURN aap_identity_sessions_v1._failure('email_not_verified', NULL);
        END IF;
        v_actor_id := v_login_actor_id;
        v_request_scope := pg_catalog.jsonb_build_object(
            'actor_id', v_actor_id,
            'operation', v_operation,
            'request_fingerprint', v_request_fingerprint,
            'policy', v_policy
        );
    ELSE
        IF v_operation IN ('refresh', 'logout') AND v_credential_type <> 'jwt' THEN
            RETURN aap_identity_sessions_v1._failure('invalid_token', NULL);
        END IF;
        v_actor_id := (v_actor->>'actor_id')::uuid;
        v_credential_id := (v_actor->>'credential_id')::uuid;
        BEGIN
            SELECT actor.id, actor.email, actor.name, actor.role, actor.is_active,
                   actor.credential_generation, actor.email_verified_at,
                   actor.password_hash
            INTO v_actor_id, v_user_email, v_user_name, v_user_role, v_user_active,
                 v_user_generation, v_user_email_verified_at,
                 v_user_password_hash
            FROM public.users AS actor
            WHERE actor.id = v_actor_id
            FOR UPDATE NOWAIT;
        EXCEPTION
            WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_identity_sessions_v1._failure('account_busy', NULL);
        END;
        IF NOT FOUND THEN
            RETURN aap_identity_sessions_v1._failure('credential_invalid', NULL);
        END IF;
        IF NOT v_user_active THEN
            RETURN aap_identity_sessions_v1._failure('user_inactive', NULL);
        END IF;
        IF v_user_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
            RETURN aap_identity_sessions_v1._failure('invalid_account_role', NULL);
        END IF;

        IF v_credential_type = 'jwt' THEN
            BEGIN
                v_actor_generation := (v_actor->>'generation')::bigint;
                v_actor_expiry := (v_actor->>'expires_at')::timestamptz;
            EXCEPTION
                WHEN invalid_text_representation OR invalid_datetime_format
                     OR datetime_field_overflow OR numeric_value_out_of_range THEN
                    RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
            END;
            IF NOT pg_catalog.isfinite(v_actor_expiry) THEN
                RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
            END IF;
            IF v_operation = 'logout'
               AND NOT pg_catalog.pg_try_advisory_xact_lock(
                   pg_catalog.hashtextextended(
                       'aap-identity-jti' || pg_catalog.chr(58) || v_credential_id::text, 0
                   )
               ) THEN
                RETURN aap_identity_sessions_v1._failure('credential_busy', NULL);
            ELSIF v_operation <> 'logout'
               AND NOT pg_catalog.pg_try_advisory_xact_lock_shared(
                   pg_catalog.hashtextextended(
                       'aap-identity-jti' || pg_catalog.chr(58) || v_credential_id::text, 0
                   )
               ) THEN
                RETURN aap_identity_sessions_v1._failure('credential_busy', NULL);
            END IF;
            v_auth_reply := aap_identity_v1.read_jwt_principal(
                v_actor_id, v_credential_id, v_actor_generation, v_actor_expiry,
                v_operation = 'refresh'
            );
            IF v_auth_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                v_auth_code := v_auth_reply->'error'->>'code';
                IF v_auth_code IS NULL OR v_auth_code NOT IN (
                    'invalid_request', 'credential_invalid', 'credential_revoked',
                    'generation_outdated', 'token_expired', 'user_inactive',
                    'invalid_account_role', 'credential_busy'
                ) THEN
                    v_auth_code := 'invalid_token';
                END IF;
                RETURN aap_identity_sessions_v1._failure(v_auth_code, NULL);
            END IF;
            v_current_principal := v_auth_reply->'data';
            v_claim_generation := v_user_generation;
        ELSE
            IF pg_catalog.jsonb_typeof(v_actor->'expires_at') = 'string' THEN
                BEGIN
                    v_actor_expiry := (v_actor->>'expires_at')::timestamptz;
                EXCEPTION
                    WHEN invalid_text_representation OR invalid_datetime_format
                         OR datetime_field_overflow THEN
                        RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
                END;
                IF NOT pg_catalog.isfinite(v_actor_expiry) THEN
                    RETURN aap_identity_sessions_v1._failure('invalid_request', NULL);
                END IF;
            END IF;
            v_auth_reply := aap_identity_v1.lock_current_principal(
                v_actor_id, 'api_key', v_credential_id,
                NULL, NULL, ARRAY[]::text[]
            );
            IF v_auth_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                v_auth_code := v_auth_reply->'error'->>'code';
                IF v_auth_code IS NULL OR v_auth_code NOT IN (
                    'invalid_request', 'credential_invalid', 'credential_revoked',
                    'generation_outdated', 'token_expired', 'user_inactive',
                    'invalid_account_role', 'credential_busy'
                ) THEN
                    v_auth_code := 'credential_invalid';
                END IF;
                RETURN aap_identity_sessions_v1._failure(v_auth_code, NULL);
            END IF;
            SELECT api_key.key_hash, api_key.scopes, api_key.expires_at
            INTO v_stored_key_hash, v_key_scopes, v_key_expiry
            FROM public.api_keys AS api_key
            WHERE api_key.id = v_credential_id AND api_key.user_id = v_actor_id;
            IF NOT FOUND OR v_stored_key_hash IS DISTINCT FROM v_expected_key_hash THEN
                RETURN aap_identity_sessions_v1._failure('credential_invalid', NULL);
            END IF;
            v_current_principal := v_auth_reply->'data';
        END IF;
        v_request_scope := pg_catalog.jsonb_build_object(
            'actor_id', v_actor_id,
            'operation', v_operation,
            'request_fingerprint', NULL,
            'policy', v_policy
        );
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-identity-session-operation/' || v_operation_id::text, 0)
    ) THEN
        RETURN aap_identity_sessions_v1._failure('credential_busy', NULL);
    END IF;
    SELECT receipt.actor_id, receipt.operation, receipt.request_scope, receipt.result
    INTO v_receipt_actor_id, v_receipt_operation, v_receipt_scope, v_receipt_result
    FROM public.identity_session_operation_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    v_receipt_found := FOUND;
    IF v_receipt_found THEN
        IF v_receipt_actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt_operation IS DISTINCT FROM v_operation
           OR v_receipt_scope IS DISTINCT FROM v_request_scope THEN
            RETURN aap_identity_sessions_v1._failure('operation_conflict', NULL);
        END IF;
        v_data := v_receipt_result;
        IF v_operation = 'login' THEN
            v_claims := v_data->'claims';
            v_current_principal := pg_catalog.jsonb_build_object(
                'actor_id', v_actor_id,
                'name', v_user_name,
                'email', v_user_email,
                'role', v_user_role,
                'is_active', true,
                'credential_type', 'jwt',
                'credential_id', v_claims->>'jti',
                'generation', (v_claims->>'gen')::bigint,
                'scopes', NULL,
                'expires_at', pg_catalog.to_timestamp(
                    ((v_claims->>'exp')::bigint)::double precision
                )
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-sessions-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_current_principal,
            'data', v_data
        );
    END IF;

    v_now := pg_catalog.clock_timestamp();
    IF v_operation = 'login' THEN
        v_claim_generation := v_user_generation;
        v_claim_exp := pg_catalog.floor(
            pg_catalog.date_part('epoch', v_now)
        )::bigint + (v_expire_minutes::bigint * 60);
        v_claims := pg_catalog.jsonb_build_object(
            'sub', v_actor_id,
            'role', v_user_role,
            'exp', v_claim_exp,
            'jti', v_operation_id,
            'gen', v_claim_generation,
            'iss', 'aap-identity/v1'
        );
        v_current_principal := pg_catalog.jsonb_build_object(
            'actor_id', v_actor_id,
            'name', v_user_name,
            'email', v_user_email,
            'role', v_user_role,
            'is_active', true,
            'credential_type', 'jwt',
            'credential_id', v_operation_id,
            'generation', v_claim_generation,
            'scopes', NULL,
            'expires_at', pg_catalog.to_timestamp(v_claim_exp::double precision)
        );
        UPDATE public.users AS actor
        SET last_login_at = v_now,
            last_seen_at = v_now,
            status = 'online'
        WHERE actor.id = v_actor_id;
        v_audit_actor := pg_catalog.jsonb_build_object(
            'actor_id', v_actor_id, 'email', v_user_email, 'role', v_user_role
        );
        v_audit_event := 'login_success';
        v_audit_detail := pg_catalog.jsonb_build_object(
            'email', v_user_email, 'result', 'success'
        );
    ELSIF v_operation = 'refresh' THEN
        v_claim_exp := pg_catalog.floor(
            pg_catalog.date_part('epoch', v_now)
        )::bigint + (v_expire_minutes::bigint * 60);
        v_claims := pg_catalog.jsonb_build_object(
            'sub', v_actor_id,
            'role', v_user_role,
            'exp', v_claim_exp,
            'jti', v_operation_id,
            'gen', v_claim_generation,
            'iss', 'aap-identity/v1'
        );
        UPDATE public.users AS actor
        SET last_seen_at = v_now
        WHERE actor.id = v_actor_id;
        v_audit_actor := pg_catalog.jsonb_build_object(
            'actor_id', v_actor_id, 'email', v_user_email, 'role', v_user_role
        );
        v_audit_event := 'refresh';
        v_audit_detail := pg_catalog.jsonb_build_object(
            'old_jti', v_credential_id,
            'expired_seconds_ago', pg_catalog.trunc(
                pg_catalog.date_part('epoch', v_now - v_actor_expiry)
            )::bigint
        );
    ELSIF v_operation = 'logout' THEN
        IF NOT aap_identity_v1.revoke_jti(
            v_credential_id, v_actor_expiry + INTERVAL '604800 seconds'
        ) THEN
            RETURN aap_identity_sessions_v1._failure('invalid_token', NULL);
        END IF;
        UPDATE public.users AS actor SET status = 'offline' WHERE actor.id = v_actor_id;
        v_audit_actor := pg_catalog.jsonb_build_object(
            'actor_id', v_actor_id, 'email', v_user_email, 'role', v_user_role
        );
        v_audit_event := 'logout';
        v_audit_detail := NULL;
    ELSE
        v_new_generation := aap_identity_v1.advance_credential_generation(v_actor_id);
        UPDATE public.users AS actor SET status = 'offline' WHERE actor.id = v_actor_id;
        v_claim_exp := pg_catalog.floor(
            pg_catalog.date_part('epoch', v_now)
        )::bigint + (v_expire_minutes::bigint * 60);
        v_claims := pg_catalog.jsonb_build_object(
            'sub', v_actor_id,
            'role', v_user_role,
            'exp', v_claim_exp,
            'jti', v_operation_id,
            'gen', v_new_generation,
            'iss', 'aap-identity/v1'
        );
        v_audit_actor := pg_catalog.jsonb_build_object(
            'actor_id', v_actor_id, 'email', v_user_email, 'role', v_user_role
        );
        v_audit_event := 'logout_all';
        v_audit_detail := pg_catalog.jsonb_build_object('new_generation', v_new_generation);
    END IF;
    v_data := pg_catalog.jsonb_build_object('claims', v_claims);
    v_audit_id := aap_audit_v1.record_identity_session_event(
        v_audit_event, v_audit_actor, v_audit_context, v_audit_detail
    );

    INSERT INTO public.identity_session_operation_receipts (
        operation_id, actor_id, operation, request_scope, result, audit_id
    ) VALUES (
        v_operation_id, v_actor_id, v_operation, v_request_scope, v_data, v_audit_id
    );
    EXCEPTION WHEN unique_violation THEN
        GET STACKED DIAGNOSTICS v_constraint_name = CONSTRAINT_NAME;
        IF v_constraint_name = 'identity_session_operation_receipts_pkey' THEN
            RETURN aap_identity_sessions_v1._failure('operation_conflict', NULL);
        END IF;
        RAISE;
    END;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-sessions-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'principal', v_current_principal,
        'data', v_data
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_identity_sessions_v1.execute_session_command(jsonb) FROM PUBLIC;
