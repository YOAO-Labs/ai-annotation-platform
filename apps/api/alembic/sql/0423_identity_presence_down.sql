-- Restore the original account columns before restoring frozen functions.
ALTER TABLE public.users
    ADD COLUMN status varchar(20) NOT NULL DEFAULT 'offline',
    ADD COLUMN last_seen_at timestamptz;
-- aap-statement-break
UPDATE public.users AS account
SET status = presence.status, last_seen_at = presence.last_seen_at
FROM public.identity_account_presence AS presence WHERE presence.user_id = account.id;
-- aap-statement-break
CREATE INDEX ix_users_last_seen_at ON public.users(last_seen_at) WHERE last_seen_at IS NOT NULL;
-- aap-statement-break
DO $owner$
DECLARE owner_name name;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO STRICT owner_name
    FROM pg_catalog.pg_proc
    WHERE oid = 'aap_identity_accounts_v1._lock_current_actor(jsonb,text,text)'::regprocedure;
    EXECUTE pg_catalog.format(
        'GRANT SELECT (status, last_seen_at), UPDATE (status, last_seen_at), INSERT (status) ON public.users TO %I', owner_name
    );
END;
$owner$;
-- aap-statement-break
CREATE OR REPLACE VIEW aap_identity_v1.account_profiles
WITH (security_barrier = true)
AS
SELECT
    account.id,
    account.name,
    account.email,
    account.role,
    account.is_active,
    account.status,
    account.avatar_ref
FROM public.users AS account;
-- aap-statement-break
CREATE OR REPLACE VIEW aap_identity_fixture_v1.account_profiles
WITH (security_barrier = true)
AS
SELECT
    account.id,
    account.email,
    account.name,
    account.role,
    account.group_name,
    account.group_id,
    account.status,
    account.is_active,
    account.disabled_kind,
    account.disabled_at,
    account.disabled_by,
    account.disabled_reason,
    account.email_verified_at,
    account.last_login_at,
    account.last_seen_at,
    account.password_admin_reset_at,
    account.deactivation_requested_at,
    account.deactivation_scheduled_at,
    account.preferences,
    account.avatar_ref,
    account.created_at
FROM public.users AS account
WHERE pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) = '_test'
   OR pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) = '_e2e';
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_identity_sessions_v1.execute_session_command(p_request jsonb)
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
CREATE OR REPLACE FUNCTION aap_identity_accounts_v1._account_record(p_actor_id uuid)
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
CREATE OR REPLACE FUNCTION aap_identity_accounts_v1.prepare_account_command(p_request jsonb)
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
CREATE OR REPLACE FUNCTION aap_identity_accounts_v1.apply_account_command(p_request jsonb)
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
    v_expected_avatar_given boolean;
    v_upload_token text;
    v_receipt jsonb;
    v_upload_owner uuid;
    v_upload_sha256 text;
    v_upload_size bigint;
    v_upload_version text;
    v_upload_storage_ref jsonb;
    v_upload_state text;
    v_maintenance_revision bigint;
    v_location_id uuid;
    v_gate jsonb;
    v_key_proof jsonb;
    v_key_id uuid;
    v_proof_expires_at timestamptz;
    v_fact_scopes text[];
    v_fact_expires_at timestamptz;
    v_fact_revision text;
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
        IF NOT (v_new_value ?& ARRAY['avatar_ref', 'kind', 'bytes'])
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(v_new_value) AS field(name)
               WHERE field.name NOT IN (
                   'avatar_ref', 'kind', 'bytes', 'expected_previous_avatar_ref',
                   'receipt', 'accepted_api_key_proof'
               )
           )
           OR pg_catalog.jsonb_typeof(v_new_value->'avatar_ref') NOT IN ('null', 'string')
           OR pg_catalog.jsonb_typeof(v_new_value->'kind') IS DISTINCT FROM 'string'
           OR v_new_value->>'kind' NOT IN ('upload', 'preset_or_clear')
           OR pg_catalog.jsonb_typeof(v_new_value->'bytes') NOT IN ('null', 'number')
           OR pg_catalog.char_length(v_new_value->>'avatar_ref') > 80
           OR pg_catalog.char_length(v_new_value->>'expected_previous_avatar_ref') > 80 THEN
            RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
        END IF;
        v_expected_avatar_given := v_new_value ? 'expected_previous_avatar_ref';
        IF v_expected_avatar_given THEN
            IF pg_catalog.jsonb_typeof(v_new_value->'expected_previous_avatar_ref')
                   NOT IN ('null', 'string')
               OR (
                   pg_catalog.jsonb_typeof(v_new_value->'expected_previous_avatar_ref')
                       = 'string'
                   AND (v_new_value->>'expected_previous_avatar_ref') COLLATE "C"
                       !~ '^(preset:[a-z0-9-]{1,40}|upload:[0-9a-f]{32})$'
               ) THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
        END IF;
        -- The proof is optional on the wire (JWT commands omit it) but its
        -- shape is closed when present. Values are compared with the current
        -- key facts later, inside this same owner transaction.
        v_key_proof := v_new_value->'accepted_api_key_proof';
        IF v_new_value ? 'accepted_api_key_proof'
           AND pg_catalog.jsonb_typeof(v_key_proof) IS DISTINCT FROM 'null' THEN
            IF pg_catalog.jsonb_typeof(v_key_proof) IS DISTINCT FROM 'object'
               OR NOT (v_key_proof ?& ARRAY['scopes', 'expires_at', 'revision'])
               OR (v_key_proof - ARRAY['scopes', 'expires_at', 'revision'])
                   <> '{}'::jsonb
               OR pg_catalog.jsonb_typeof(v_key_proof->'scopes')
                   IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_typeof(v_key_proof->'revision')
                   IS DISTINCT FROM 'string'
               OR (v_key_proof->>'revision') COLLATE "C"
                   !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(v_key_proof->'expires_at')
                   NOT IN ('null', 'string')
               OR (
                   pg_catalog.jsonb_typeof(v_key_proof->'expires_at') = 'string'
                   AND (v_key_proof->>'expires_at') !~* (
                       '(z|[+-][0-9]{2}' || pg_catalog.chr(58) || '[0-9]{2})$'
                   )
               )
               OR EXISTS (
                   SELECT 1
                   FROM pg_catalog.jsonb_array_elements(v_key_proof->'scopes')
                       AS scope(value)
                   WHERE pg_catalog.jsonb_typeof(scope.value)
                       IS DISTINCT FROM 'string'
                      OR pg_catalog.char_length(scope.value #>> '{}')
                          NOT BETWEEN 1 AND 128
               ) THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
            IF pg_catalog.jsonb_typeof(v_key_proof->'expires_at') = 'string' THEN
                BEGIN
                    v_proof_expires_at := (v_key_proof->>'expires_at')::timestamptz;
                EXCEPTION
                    WHEN invalid_text_representation OR invalid_datetime_format
                         OR datetime_field_overflow OR numeric_value_out_of_range THEN
                        RETURN aap_identity_accounts_v1._failure(
                            'invalid_request', NULL, NULL
                        );
                END;
                IF NOT pg_catalog.isfinite(v_proof_expires_at) THEN
                    RETURN aap_identity_accounts_v1._failure(
                        'invalid_request', NULL, NULL
                    );
                END IF;
            END IF;
        END IF;
        v_avatar_kind := v_new_value->>'kind';
        v_new_avatar_ref := v_new_value->>'avatar_ref';
        IF v_avatar_kind = 'upload' THEN
            IF pg_catalog.jsonb_typeof(v_new_value->'avatar_ref') IS DISTINCT FROM 'string'
               OR (v_new_value->>'avatar_ref') COLLATE "C" !~ '^upload:[0-9a-f]{32}$'
               OR pg_catalog.jsonb_typeof(v_new_value->'bytes') IS DISTINCT FROM 'number'
               OR (v_new_value->>'bytes') !~ '^(0|[1-9][0-9]*)$'
               OR NOT v_expected_avatar_given
               OR pg_catalog.jsonb_typeof(v_new_value->'expected_previous_avatar_ref')
                   IS DISTINCT FROM COALESCE(
                       pg_catalog.jsonb_typeof(v_base), 'null'::text
                   )
               OR v_base IS DISTINCT FROM v_new_value->'expected_previous_avatar_ref'
               OR pg_catalog.jsonb_typeof(v_new_value->'receipt') IS DISTINCT FROM 'object' THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
            BEGIN
                v_avatar_bytes := (v_new_value->>'bytes')::bigint;
            EXCEPTION WHEN numeric_value_out_of_range THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END;
            v_receipt := v_new_value->'receipt';
            v_upload_token := v_receipt->>'token';
            SELECT pg_catalog.count(*)::integer INTO v_field_count
            FROM pg_catalog.jsonb_object_keys(v_receipt);
            IF v_field_count <> 6
               OR NOT (v_receipt ?& ARRAY[
                   'token', 'sha256', 'size_bytes', 'object_version_id',
                   'maintenance_revision', 'storage_ref'
               ])
               OR EXISTS (
                   SELECT 1 FROM pg_catalog.jsonb_object_keys(v_receipt) AS field(name)
                   WHERE field.name NOT IN (
                       'token', 'sha256', 'size_bytes', 'object_version_id',
                       'maintenance_revision', 'storage_ref'
                   )
               )
               OR pg_catalog.jsonb_typeof(v_receipt->'token') IS DISTINCT FROM 'string'
               OR (v_receipt->>'token') COLLATE "C" !~ '^[0-9a-f]{32}$'
               OR v_upload_token IS DISTINCT FROM pg_catalog.substr(v_new_avatar_ref, 8)
               OR pg_catalog.jsonb_typeof(v_receipt->'sha256') IS DISTINCT FROM 'string'
               OR (v_receipt->>'sha256') COLLATE "C" !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(v_receipt->'size_bytes') IS DISTINCT FROM 'number'
               OR (v_receipt->>'size_bytes') !~ '^(0|[1-9][0-9]*)$'
               OR pg_catalog.char_length(v_receipt->>'size_bytes') > 7
               OR (v_receipt->>'size_bytes')::bigint <> v_avatar_bytes
               OR pg_catalog.jsonb_typeof(v_receipt->'object_version_id')
                   NOT IN ('null', 'string')
               OR pg_catalog.char_length(v_receipt->>'object_version_id') > 1024
               OR pg_catalog.jsonb_typeof(v_receipt->'maintenance_revision')
                   IS DISTINCT FROM 'number'
               OR (v_receipt->>'maintenance_revision') !~ '^[1-9][0-9]{0,9}$'
               OR pg_catalog.jsonb_typeof(v_receipt->'storage_ref') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_receipt->'storage_ref'->'location_id')
                   IS DISTINCT FROM 'string'
               OR (v_receipt->'storage_ref'->>'location_id') COLLATE "C"
                   !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
            v_maintenance_revision := (v_receipt->>'maintenance_revision')::bigint;
            v_location_id := (v_receipt->'storage_ref'->>'location_id')::uuid;
        ELSE
            IF (
                v_new_value ? 'receipt'
                AND pg_catalog.jsonb_typeof(v_new_value->'receipt')
                    IS DISTINCT FROM 'null'
            )
               OR pg_catalog.jsonb_typeof(v_new_value->'bytes') IS DISTINCT FROM 'null'
               OR (pg_catalog.jsonb_typeof(v_new_value->'avatar_ref') = 'string'
                   AND (v_new_value->>'avatar_ref') COLLATE "C"
                       !~ '^preset:[a-z0-9-]{1,40}$') THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
            IF v_expected_avatar_given
               AND pg_catalog.jsonb_typeof(v_new_value->'expected_previous_avatar_ref')
                   = 'string'
               AND v_base IS DISTINCT FROM
                   v_new_value->'expected_previous_avatar_ref' THEN
                RETURN aap_identity_accounts_v1._failure('invalid_request', NULL, NULL);
            END IF;
        END IF;
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
            -- Re-read the accepted key inside this transaction and require the
            -- exact frozen proof the caller captured before image work and
            -- object I/O. The fact helper takes the key's SHARE fence, returns
            -- no row after revocation/expiry, and returns a verifier digest
            -- (never the verifier). A JWT instead carries its generation and
            -- expiry in the fenced actor checked above.
            IF v_principal->>'credential_type' = 'api_key' THEN
                IF pg_catalog.jsonb_typeof(
                       v_new_value->'accepted_api_key_proof'
                   ) IS DISTINCT FROM 'object' THEN
                    RETURN aap_identity_accounts_v1._failure(
                        'credential_invalid', NULL, NULL
                    );
                END IF;
                v_key_id := (v_principal->>'credential_id')::uuid;
                SELECT facts.scopes, facts.expires_at, facts.revision
                INTO v_fact_scopes, v_fact_expires_at, v_fact_revision
                FROM aap_identity_v1.read_accepted_api_key_facts(
                    v_actor_id, v_key_id
                ) AS facts;
                IF NOT FOUND
                   OR pg_catalog.to_jsonb(v_fact_scopes) IS DISTINCT FROM
                       v_new_value->'accepted_api_key_proof'->'scopes'
                   OR v_fact_revision IS DISTINCT FROM
                       v_new_value->'accepted_api_key_proof'->>'revision'
                   OR v_fact_expires_at IS DISTINCT FROM
                       (CASE
                           WHEN pg_catalog.jsonb_typeof(
                               v_new_value->'accepted_api_key_proof'->'expires_at'
                           ) = 'null' THEN NULL::timestamptz
                           ELSE v_proof_expires_at
                       END) THEN
                    RETURN aap_identity_accounts_v1._failure(
                        'credential_invalid', NULL, NULL
                    );
                END IF;
            ELSIF COALESCE(
                pg_catalog.jsonb_typeof(v_new_value->'accepted_api_key_proof'),
                'null'
            ) IS DISTINCT FROM 'null' THEN
                RETURN aap_identity_accounts_v1._failure(
                    'invalid_request', NULL, NULL
                );
            END IF;
            IF (CASE
                    WHEN v_user_avatar_ref IS NULL THEN 'null'::jsonb
                    ELSE to_jsonb(v_user_avatar_ref)
                END) IS DISTINCT FROM v_base THEN
                RETURN aap_identity_accounts_v1._failure(
                    CASE
                        WHEN v_avatar_kind = 'upload'
                        THEN 'avatar_reference_changed'
                        ELSE 'account_busy'
                    END,
                    NULL, NULL
                );
            END IF;
            v_changed := v_user_avatar_ref IS DISTINCT FROM v_new_avatar_ref;
            IF v_avatar_kind = 'upload' THEN
                -- Reacquire the Asset-owned admission gate at the exact revision
                -- captured by object I/O. The FOR SHARE lock is held until this
                -- owner transaction commits, so a maintenance transition cannot
                -- slip between admission and users.avatar_ref publication.
                v_gate := aap_assets_v1.lock_publication_storage_gate(
                    v_location_id, v_maintenance_revision
                );
                IF v_gate->>'ok' IS DISTINCT FROM 'true' THEN
                    RETURN aap_identity_accounts_v1._failure(
                        v_gate->>'code', NULL, NULL
                    );
                END IF;
                SELECT upload.owner_id, upload.sha256, upload.size_bytes,
                       upload.object_version_id, upload.storage_ref, upload.state
                INTO v_upload_owner, v_upload_sha256, v_upload_size,
                     v_upload_version, v_upload_storage_ref, v_upload_state
                FROM public.avatar_uploads AS upload
                WHERE upload.token = v_upload_token
                FOR UPDATE;
                IF NOT FOUND
                   OR v_upload_owner IS DISTINCT FROM v_actor_id
                   OR v_upload_state IS DISTINCT FROM 'pending'
                   OR v_upload_sha256 IS DISTINCT FROM (v_receipt->>'sha256')
                   OR v_upload_size IS DISTINCT FROM v_avatar_bytes
                   OR v_upload_version IS DISTINCT FROM (
                       CASE
                           WHEN pg_catalog.jsonb_typeof(
                               v_receipt->'object_version_id'
                           ) = 'null' THEN NULL
                           ELSE v_receipt->>'object_version_id'
                       END
                   )
                   OR v_upload_storage_ref IS DISTINCT FROM v_receipt->'storage_ref'
                   OR NOT v_changed THEN
                    RETURN aap_identity_accounts_v1._failure(
                        'avatar_receipt_invalid', NULL, NULL
                    );
                END IF;
                UPDATE public.avatar_uploads AS upload
                SET state = 'ready'
                WHERE upload.token = v_upload_token
                  AND upload.owner_id = v_actor_id
                  AND upload.state = 'pending';
            END IF;
            IF v_changed THEN
                IF v_user_avatar_ref IS NOT NULL
                   AND v_user_avatar_ref COLLATE "C"
                       ~ '^upload:[0-9a-f]{32}$' THEN
                    UPDATE public.avatar_uploads AS upload
                    SET state = 'retired', retired_at = pg_catalog.clock_timestamp()
                    WHERE upload.token = pg_catalog.substr(v_user_avatar_ref, 8)
                      AND upload.owner_id = v_actor_id
                      AND upload.state = 'ready';
                END IF;
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
CREATE OR REPLACE FUNCTION aap_identity_accounts_v1.mark_inactive_accounts_offline(
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
CREATE OR REPLACE FUNCTION aap_identity_recovery_v1.apply_recovery_command(p_request jsonb)
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
    v_prepare_request jsonb;
    v_prepared jsonb;
    v_data jsonb;
    v_policy jsonb;
    v_args jsonb;
    v_password_hash text;
    v_new_token_hash text;
    v_ciphertext text;
    v_encryption_id text;
    v_actor_id uuid;
    v_actor jsonb;
    v_account jsonb;
    v_claims jsonb;
    v_result jsonb;
    v_receipt_result jsonb;
    v_completed boolean := false;
    v_password_changed boolean := false;
    v_email_verified_changed boolean := false;
    v_audit_id bigint;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_now_epoch bigint;
    v_exp_epoch bigint;
    v_token_id uuid;
    v_token_expires timestamptz;
    v_email text;
    v_role text;
    v_name text;
    v_verification_required boolean;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_count <> 12 OR NOT (p_request ?& ARRAY[
        'schema', 'operation', 'operation_id', 'request_fingerprint',
        'derivation_id', 'audit_context', 'config', 'args', 'password_hash',
        'token_hash', 'token_ciphertext', 'encryption_id'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
        WHERE field.name NOT IN (
            'schema', 'operation', 'operation_id', 'request_fingerprint',
            'derivation_id', 'audit_context', 'config', 'args', 'password_hash',
            'token_hash', 'token_ciphertext', 'encryption_id'
        )
    ) OR pg_catalog.jsonb_typeof(p_request->'password_hash') NOT IN ('null', 'string')
       OR pg_catalog.jsonb_typeof(p_request->'token_hash') NOT IN ('null', 'string')
       OR pg_catalog.jsonb_typeof(p_request->'token_ciphertext') NOT IN ('null', 'string')
       OR pg_catalog.jsonb_typeof(p_request->'encryption_id') NOT IN ('null', 'string') THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END IF;
    v_operation := p_request->>'operation';
    BEGIN
        v_operation_id := (p_request->>'operation_id')::uuid;
    EXCEPTION WHEN invalid_text_representation OR null_value_not_allowed THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END;
    v_password_hash := p_request->>'password_hash';
    v_new_token_hash := p_request->>'token_hash';
    v_ciphertext := p_request->>'token_ciphertext';
    v_encryption_id := p_request->>'encryption_id';
    IF v_password_hash IS NOT NULL AND (
           pg_catalog.char_length(v_password_hash) <> 60
           OR v_password_hash COLLATE "C" !~ '^\$2[aby]\$[0-9]{2}\$[A-Za-z0-9./]{53}$'
       )
       OR v_new_token_hash IS NOT NULL AND v_new_token_hash COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR v_ciphertext IS NOT NULL AND pg_catalog.char_length(v_ciphertext) NOT BETWEEN 1 AND 4096
       OR v_encryption_id IS NOT NULL AND v_encryption_id COLLATE "C" !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END IF;

    v_prepare_request := p_request - ARRAY[
        'password_hash', 'token_hash', 'token_ciphertext', 'encryption_id'
    ];
    v_prepared := aap_identity_recovery_v1.prepare_recovery_command(v_prepare_request);
    IF v_prepared->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_prepared;
    END IF;
    v_data := v_prepared->'data';
    v_now := pg_catalog.clock_timestamp();
    v_policy := v_data->'policy';
    v_args := v_prepare_request->'args';
    IF v_operation IN ('consume_password_reset', 'consume_email_verification')
       AND v_new_token_hash IS DISTINCT FROM v_args->>'token_hash' THEN
        RETURN aap_identity_recovery_v1._failure('invalid_request');
    END IF;
    v_actor := v_data->'actor';
    v_actor_id := CASE
        WHEN pg_catalog.jsonb_typeof(v_data->'actor') = 'object'
        THEN (v_data->'actor'->>'id')::uuid
        ELSE NULL
    END;

    IF pg_catalog.jsonb_typeof(v_data->'replay') = 'object' THEN
        IF v_operation = 'register_open' THEN
            IF v_new_token_hash IS NOT NULL OR v_ciphertext IS NOT NULL
               OR v_encryption_id IS NOT NULL THEN
                RETURN aap_identity_recovery_v1._failure('operation_conflict');
            END IF;
            IF pg_catalog.jsonb_typeof(v_data->'replay'->'claims') = 'object' THEN
                IF v_password_hash IS NULL
                   OR v_password_hash IS DISTINCT FROM v_data->>'password_hash' THEN
                    RETURN aap_identity_recovery_v1._failure('operation_conflict');
                END IF;
            ELSIF v_password_hash IS NOT NULL THEN
                RETURN aap_identity_recovery_v1._failure('operation_conflict');
            END IF;
        ELSIF v_password_hash IS NOT NULL
           OR (v_operation NOT IN ('consume_password_reset', 'consume_email_verification')
               AND v_new_token_hash IS NOT NULL)
           OR v_ciphertext IS NOT NULL OR v_encryption_id IS NOT NULL THEN
            RETURN aap_identity_recovery_v1._failure('operation_conflict');
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-recovery-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'data', pg_catalog.jsonb_build_object(
                'result', v_data->'replay',
                'actor', v_data->'actor',
                'replayed', true,
                'password_changed', false,
                'email_verified_changed', false
            )
        );
    END IF;

    IF v_operation = 'register_open' THEN
        v_verification_required := (v_policy->>'email_verification_required')::boolean;
        IF NOT (v_policy->>'allow_open_registration')::boolean THEN
            RETURN aap_identity_recovery_v1._failure('registration_disabled');
        END IF;
        IF v_password_hash IS NULL THEN
            RETURN aap_identity_recovery_v1._failure('invalid_request');
        END IF;
        IF v_verification_required THEN
            IF v_new_token_hash IS NULL OR v_ciphertext IS NULL OR v_encryption_id IS NULL THEN
                RETURN aap_identity_recovery_v1._failure('invalid_request');
            END IF;
        ELSIF v_new_token_hash IS NOT NULL OR v_ciphertext IS NOT NULL OR v_encryption_id IS NOT NULL THEN
            RETURN aap_identity_recovery_v1._failure('invalid_request');
        END IF;
        v_email := pg_catalog.lower(pg_catalog.btrim(v_args->>'email'));
        v_name := v_args->>'name';
        v_now := pg_catalog.clock_timestamp();
        INSERT INTO public.users (
            id, email, name, password_hash, credential_generation, role,
            group_name, group_id, status, is_active, email_verified_at
        ) VALUES (
            v_operation_id, v_email, v_name, v_password_hash, 0, 'viewer',
            NULL, NULL, CASE WHEN v_verification_required THEN 'offline' ELSE 'online' END,
            true, CASE WHEN v_verification_required THEN NULL ELSE v_now END
        ) ON CONFLICT (email) DO NOTHING
        RETURNING id INTO v_actor_id;
        IF NOT FOUND THEN
            RETURN aap_identity_recovery_v1._failure('email_exists');
        END IF;
        IF v_verification_required THEN
            v_token_id := v_operation_id;
            v_token_expires := v_now + pg_catalog.make_interval(secs => 86400);
            INSERT INTO public.email_verification_tokens (
                id, user_id, token_hash, issued_email, expires_at
            ) VALUES (
                v_token_id, v_actor_id, v_new_token_hash, v_email, v_token_expires
            );
            INSERT INTO public.identity_credential_mail_outbox (
                id, actor_id, challenge_id, purpose, email, frontend_base_url,
                token_hash, token_ciphertext, encryption_id, expires_at
            ) VALUES (
                v_operation_id, v_actor_id, v_token_id, 'email_verification', v_email,
                v_policy->>'frontend_base_url', v_new_token_hash, v_ciphertext,
                v_encryption_id, v_token_expires
            );
        END IF;
        v_account := aap_identity_accounts_v1._account_record(v_actor_id);
        v_actor := aap_identity_recovery_v1._actor_record(v_actor_id);
        IF NOT v_verification_required THEN
            v_now_epoch := floor(extract(epoch FROM v_now))::bigint;
            v_exp_epoch := v_now_epoch + (v_policy->>'expire_minutes')::bigint * 60;
            v_claims := pg_catalog.jsonb_build_object(
                'sub', v_actor_id,
                'role', 'viewer',
                'exp', v_exp_epoch,
                'jti', v_operation_id,
                'gen', 0,
                'iss', 'aap-identity/v1'
            );
        END IF;
        v_result := pg_catalog.jsonb_build_object(
            'account', v_account,
            'claims', v_claims,
            'email_verification_required', v_verification_required,
            'completed', NULL
        );
        v_audit_id := aap_audit_v1.record_identity_recovery_event(
            'register_open',
            pg_catalog.jsonb_build_object('actor_id', v_actor_id, 'email', v_email, 'role', 'viewer'),
            p_request->'audit_context',
            pg_catalog.jsonb_build_object(
                'email', v_email, 'role', 'viewer', 'method', 'open_registration',
                'email_verification_required', v_verification_required
            )
        );
    ELSIF v_operation IN ('request_password_reset', 'resend_email_verification') THEN
        IF v_data->>'eligible' = 'true' THEN
            IF v_actor_id IS NULL OR v_new_token_hash IS NULL
               OR v_ciphertext IS NULL OR v_encryption_id IS NULL THEN
                RETURN aap_identity_recovery_v1._failure('invalid_request');
            END IF;
            v_token_id := v_operation_id;
            v_email := v_data->'actor'->>'email';
            IF v_operation = 'request_password_reset' THEN
                v_token_expires := v_now + pg_catalog.make_interval(secs => 3600);
                INSERT INTO public.password_reset_tokens (
                    id, user_id, token_hash, issued_email, expires_at
                ) VALUES (
                    v_token_id, v_actor_id, v_new_token_hash, v_email, v_token_expires
                );
                INSERT INTO public.identity_credential_mail_outbox (
                    id, actor_id, challenge_id, purpose, email, frontend_base_url,
                    token_hash, token_ciphertext, encryption_id, expires_at
                ) VALUES (
                    v_operation_id, v_actor_id, v_token_id, 'password_reset', v_email,
                    v_policy->>'frontend_base_url', v_new_token_hash, v_ciphertext,
                    v_encryption_id, v_token_expires
                );
            ELSE
                v_token_expires := v_now + pg_catalog.make_interval(secs => 86400);
                INSERT INTO public.email_verification_tokens (
                    id, user_id, token_hash, issued_email, expires_at
                ) VALUES (
                    v_token_id, v_actor_id, v_new_token_hash, v_email, v_token_expires
                );
                INSERT INTO public.identity_credential_mail_outbox (
                    id, actor_id, challenge_id, purpose, email, frontend_base_url,
                    token_hash, token_ciphertext, encryption_id, expires_at
                ) VALUES (
                    v_operation_id, v_actor_id, v_token_id, 'email_verification', v_email,
                    v_policy->>'frontend_base_url', v_new_token_hash, v_ciphertext,
                    v_encryption_id, v_token_expires
                );
            END IF;
        ELSIF v_new_token_hash IS NOT NULL OR v_ciphertext IS NOT NULL OR v_encryption_id IS NOT NULL THEN
            RETURN aap_identity_recovery_v1._failure('invalid_request');
        END IF;
        v_result := pg_catalog.jsonb_build_object(
            'account', NULL, 'claims', NULL,
            'email_verification_required', NULL, 'completed', NULL
        );
    ELSIF v_operation = 'consume_password_reset' THEN
        IF v_actor_id IS NULL OR pg_catalog.jsonb_typeof(v_data->'token') IS DISTINCT FROM 'object'
           OR v_ciphertext IS NOT NULL OR v_encryption_id IS NOT NULL THEN
            RETURN aap_identity_recovery_v1._failure('invalid_request');
        END IF;
        IF (v_data->'actor'->>'is_active')::boolean THEN
            IF v_password_hash IS NULL THEN
                RETURN aap_identity_recovery_v1._failure('invalid_request');
            END IF;
            IF (v_data->'actor'->>'credential_generation')::bigint = 9223372036854775807 THEN
                RETURN aap_identity_recovery_v1._failure('operation_conflict');
            END IF;
            UPDATE public.password_reset_tokens AS token
            SET used_at = v_now
            WHERE token.id = (v_data->'token'->>'id')::uuid
              AND token.user_id = v_actor_id AND token.used_at IS NULL
              AND token.expires_at > v_now
              AND token.issued_email = v_data->'actor'->>'email';
            IF NOT FOUND THEN
                RETURN aap_identity_recovery_v1._failure('invalid_request');
            END IF;
            UPDATE public.users AS account
            SET password_hash = v_password_hash,
                password_admin_reset_at = NULL,
                credential_generation = account.credential_generation + 1
            WHERE account.id = v_actor_id
              AND account.is_active
              AND account.credential_generation < 9223372036854775807;
            IF NOT FOUND THEN
                RETURN aap_identity_recovery_v1._failure('operation_conflict');
            END IF;
            v_actor := aap_identity_recovery_v1._actor_record(v_actor_id);
            v_audit_id := aap_audit_v1.record_identity_recovery_event(
                'consume_password_reset',
                pg_catalog.jsonb_build_object(
                    'actor_id', v_actor_id,
                    'email', v_data->'actor'->>'email',
                    'role', v_data->'actor'->>'role'
                ),
                p_request->'audit_context',
                pg_catalog.jsonb_build_object('method', 'reset_token')
            );
            v_completed := true;
            v_password_changed := true;
        ELSE
            IF v_password_hash IS NOT NULL THEN
                RETURN aap_identity_recovery_v1._failure('invalid_request');
            END IF;
            UPDATE public.password_reset_tokens AS token
            SET used_at = v_now
            WHERE token.id = (v_data->'token'->>'id')::uuid
              AND token.user_id = v_actor_id AND token.used_at IS NULL
              AND token.expires_at > v_now
              AND token.issued_email = v_data->'actor'->>'email';
            IF NOT FOUND THEN
                RETURN aap_identity_recovery_v1._failure('invalid_request');
            END IF;
            v_actor := v_data->'actor';
        END IF;
        v_result := pg_catalog.jsonb_build_object(
            'account', NULL, 'claims', NULL,
            'email_verification_required', NULL, 'completed', v_completed
        );
    ELSE
        IF v_actor_id IS NULL OR pg_catalog.jsonb_typeof(v_data->'token') IS DISTINCT FROM 'object'
           OR v_password_hash IS NOT NULL
           OR v_ciphertext IS NOT NULL OR v_encryption_id IS NOT NULL THEN
            RETURN aap_identity_recovery_v1._failure('invalid_request');
        END IF;
        UPDATE public.email_verification_tokens AS token
        SET used_at = v_now
        WHERE token.id = (v_data->'token'->>'id')::uuid
          AND token.user_id = v_actor_id AND token.used_at IS NULL
          AND token.expires_at > v_now
          AND token.issued_email = v_data->'actor'->>'email';
        IF NOT FOUND THEN
            RETURN aap_identity_recovery_v1._failure('invalid_request');
        END IF;
        UPDATE public.users AS account
        SET email_verified_at = v_now
        WHERE account.id = v_actor_id AND account.email_verified_at IS NULL;
        v_email_verified_changed := FOUND;
        v_actor := aap_identity_recovery_v1._actor_record(v_actor_id);
        v_audit_id := aap_audit_v1.record_identity_recovery_event(
            'consume_email_verification',
            pg_catalog.jsonb_build_object(
                'actor_id', v_actor_id,
                'email', v_data->'actor'->>'email',
                'role', v_data->'actor'->>'role'
            ),
            p_request->'audit_context',
            pg_catalog.jsonb_build_object(
                'email', v_data->'actor'->>'email', 'result', 'email_verified'
            )
        );
        v_completed := true;
        v_result := pg_catalog.jsonb_build_object(
            'account', NULL, 'claims', NULL,
            'email_verification_required', NULL, 'completed', true
        );
    END IF;

    v_receipt_result := v_result;
    IF v_operation = 'register_open' THEN
        v_receipt_result := jsonb_set(
            v_result,
            '{account}',
            v_account - ARRAY['email', 'name'],
            true
        );
    END IF;
    INSERT INTO public.identity_recovery_operation_receipts (
        operation_id, operation, actor_id, request_fingerprint,
        derivation_id, result, audit_id
    ) VALUES (
        v_operation_id, v_operation, v_actor_id,
        p_request->>'request_fingerprint', p_request->>'derivation_id',
        v_receipt_result, v_audit_id
    );
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-recovery-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'data', pg_catalog.jsonb_build_object(
            'result', v_result,
            'actor', v_actor,
            'replayed', false,
            'password_changed', v_password_changed,
            'email_verified_changed', v_email_verified_changed
        )
    );
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_identity_groups_v1._account_record(p_user_id uuid)
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
    WHERE account.id = p_user_id
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_identity_deactivation_v1._account_record(p_user_id uuid)
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
CREATE OR REPLACE FUNCTION aap_identity_deactivation_v1.process_due_deactivations(p_request jsonb)
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
CREATE OR REPLACE FUNCTION aap_identity_invitations_v1._account_record(p_user_id uuid)
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
CREATE OR REPLACE FUNCTION aap_identity_invitations_v1.apply_invitation_command(p_command jsonb)
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
    v_actor_input jsonb;
    v_expected_key_hash text;
    v_context jsonb;
    v_config jsonb;
    v_args jsonb;
    v_derived jsonb;
    v_policy jsonb;
    v_actor_parse jsonb;
    v_actor_lock jsonb;
    v_principal jsonb;
    v_actor_native jsonb;
    v_actor_id uuid;
    v_actor_role text;
    v_allow_null boolean;
    v_user_ids uuid[] := ARRAY[]::uuid[];
    v_initial record;
    v_invitation record;
    v_owner record;
    v_inviter record;
    v_target record;
    v_project jsonb;
    v_workspace jsonb;
    v_project_id uuid;
    v_owner_id uuid;
    v_target_id uuid;
    v_snapshot_target_id uuid;
    v_project_role text;
    v_group jsonb;
    v_password_hash text;
    v_claims jsonb;
    v_group_id uuid;
    v_group_name text;
    v_user_id uuid;
    v_invitation_id uuid;
    v_mail_hash text;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_token text;
    v_status text;
    v_receipt record;
    v_outbox record;
    v_result jsonb;
    v_data jsonb;
    v_acceptance jsonb;
    v_batch jsonb;
    v_batch_item jsonb;
    v_scope_project record;
    v_batch_result jsonb;
    v_batch_snapshot jsonb;
    v_mail_context jsonb;
    v_audit_id bigint;
    v_detail jsonb;
    v_member_reply jsonb;
    v_active_count bigint;
    v_replayed boolean := false;
    v_operation_lock boolean;
BEGIN
    SELECT account.id,account.name,account.role,account.is_active INTO v_owner
    FROM public.users AS account WHERE false;
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(p_command);
    IF v_count <> 11 OR NOT (p_command ?& ARRAY[
        'schema','operation','operation_id','actor','expected_key_hash',
        'request_fingerprint','derivation_id','audit_context','config','args','derived'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_command) AS field(name)
        WHERE field.name NOT IN (
            'schema','operation','operation_id','actor','expected_key_hash',
            'request_fingerprint','derivation_id','audit_context','config','args','derived'
        )
    ) OR p_command->>'schema' IS DISTINCT FROM 'aap-identity-invitations-native/v1'
      OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
      OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
      OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
      OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
      OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
      OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
      OR pg_catalog.jsonb_typeof(p_command->'actor') NOT IN ('object','null')
      OR pg_catalog.jsonb_typeof(p_command->'expected_key_hash') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(p_command->'audit_context') IS DISTINCT FROM 'object'
      OR pg_catalog.jsonb_typeof(p_command->'config') IS DISTINCT FROM 'object'
      OR pg_catalog.jsonb_typeof(p_command->'args') IS DISTINCT FROM 'object'
      OR pg_catalog.jsonb_typeof(p_command->'derived') IS DISTINCT FROM 'object' THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    v_operation := p_command->>'operation';
    IF v_operation NOT IN (
        'create_invitation','apply_invitation_batch','revoke_invitation',
        'resend_invitation','send_invitation_email','register_invitation','accept_invitation'
    ) OR NOT aap_identity_invitations_v1._args_valid(v_operation,p_command->'args') THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    BEGIN v_operation_id := (p_command->>'operation_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END;
    v_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_actor_input := p_command->'actor';
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_context := p_command->'audit_context';
    v_config := p_command->'config';
    v_args := p_command->'args';
    v_derived := p_command->'derived';
    SELECT pg_catalog.count(*)::integer INTO v_count
    FROM pg_catalog.jsonb_object_keys(v_derived);
    IF v_count <> 9 OR NOT (v_derived ?& ARRAY[
        'password_hash','claims','token','batch_tokens','mail_token_hash',
        'mail_token_ciphertext','mail_encryption_id','frontend_base_url','mail_frontend_url'
    ]) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(v_derived) AS field(name)
        WHERE field.name NOT IN (
            'password_hash','claims','token','batch_tokens','mail_token_hash',
            'mail_token_ciphertext','mail_encryption_id','frontend_base_url','mail_frontend_url'
        )
    ) OR pg_catalog.jsonb_typeof(v_derived->'password_hash') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'claims') NOT IN ('object','null')
      OR pg_catalog.jsonb_typeof(v_derived->'token') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'batch_tokens') NOT IN ('array','null')
      OR pg_catalog.jsonb_typeof(v_derived->'mail_token_hash') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'mail_token_ciphertext') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'mail_encryption_id') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'frontend_base_url') NOT IN ('string','null')
      OR pg_catalog.jsonb_typeof(v_derived->'mail_frontend_url') NOT IN ('string','null') THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    IF (pg_catalog.jsonb_typeof(v_derived->'password_hash') = 'string'
        AND ((v_derived->>'password_hash') !~ '^\$2[aby]\$12\$[./A-Za-z0-9]{53}$'
             OR pg_catalog.char_length(v_derived->>'password_hash') <> 60))
       OR (pg_catalog.jsonb_typeof(v_derived->'token') = 'string'
           AND pg_catalog.char_length(v_derived->>'token') NOT BETWEEN 1 AND 64)
       OR (pg_catalog.jsonb_typeof(v_derived->'mail_token_hash') = 'string'
           AND (v_derived->>'mail_token_hash') COLLATE "C" !~ '^[0-9a-f]{64}$')
       OR (pg_catalog.jsonb_typeof(v_derived->'mail_encryption_id') = 'string'
           AND (v_derived->>'mail_encryption_id') COLLATE "C" !~ '^[0-9a-f]{64}$')
       OR (pg_catalog.jsonb_typeof(v_derived->'mail_token_ciphertext') = 'string'
           AND pg_catalog.char_length(v_derived->>'mail_token_ciphertext') NOT BETWEEN 1 AND 4096)
       OR (pg_catalog.jsonb_typeof(v_derived->'frontend_base_url') = 'string'
           AND pg_catalog.char_length(v_derived->>'frontend_base_url') < 1)
       OR (pg_catalog.jsonb_typeof(v_derived->'mail_frontend_url') = 'string'
           AND pg_catalog.char_length(v_derived->>'mail_frontend_url') < 1) THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    IF v_operation IN ('create_invitation','resend_invitation') THEN
        IF v_derived->'token' IS NULL OR v_derived->'token' = 'null'::jsonb
           OR v_derived->'frontend_base_url' IS NULL OR v_derived->'frontend_base_url' = 'null'::jsonb
           OR EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                      WHERE pair.key NOT IN ('token','frontend_base_url') AND pair.value <> 'null'::jsonb) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    ELSIF v_operation = 'register_invitation' THEN
        IF v_derived->'password_hash' = 'null'::jsonb OR v_derived->'claims' = 'null'::jsonb
           OR EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                      WHERE pair.key NOT IN ('password_hash','claims') AND pair.value <> 'null'::jsonb) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    ELSIF v_operation = 'send_invitation_email' THEN
        IF v_derived->'mail_token_hash' = 'null'::jsonb
           OR v_derived->'mail_token_ciphertext' = 'null'::jsonb
           OR v_derived->'mail_encryption_id' = 'null'::jsonb
           OR v_derived->'mail_frontend_url' = 'null'::jsonb
           OR EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                      WHERE pair.key NOT IN (
                          'mail_token_hash','mail_token_ciphertext','mail_encryption_id','mail_frontend_url'
                      ) AND pair.value <> 'null'::jsonb) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    ELSIF v_operation = 'apply_invitation_batch' THEN
        IF v_derived->'batch_tokens' = 'null'::jsonb
           OR v_derived->'frontend_base_url' = 'null'::jsonb
           OR EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                      WHERE pair.key NOT IN ('batch_tokens','frontend_base_url') AND pair.value <> 'null'::jsonb) THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END IF;
    ELSIF EXISTS (SELECT 1 FROM jsonb_each(v_derived) AS pair(key,value)
                  WHERE pair.value <> 'null'::jsonb) THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    v_allow_null := v_operation = 'register_invitation';
    v_actor_parse := aap_identity_invitations_v1._actor(
        v_actor_input,v_expected_key_hash,v_allow_null,false
    );
    IF v_actor_parse->'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_actor_parse; END IF;
    IF v_actor_input IS NOT NULL AND pg_catalog.jsonb_typeof(v_actor_input) <> 'null' THEN
        BEGIN v_actor_id := (v_actor_input->>'actor_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_identity_invitations_v1._failure('invalid_request');
        END;
    ELSIF NOT v_allow_null THEN
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;

    IF v_operation = 'create_invitation' THEN
        IF pg_catalog.jsonb_typeof(v_args->'project_id') = 'string' THEN
            v_project_id := (v_args->>'project_id')::uuid;
            v_project := aap_identity_invitations_v1._project_record(v_project_id);
            IF v_project IS NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_not_found','目标项目不存在'
                );
            END IF;
            v_owner_id := (v_project->>'owner_id')::uuid;
        END IF;
        SELECT account.id INTO v_target_id FROM public.users AS account
        WHERE account.email = v_args->>'email' AND account.is_active
        ORDER BY account.id LIMIT 1;
        v_snapshot_target_id := v_target_id;
        v_user_ids := ARRAY[v_actor_id,v_owner_id,v_target_id];
    ELSIF v_operation = 'apply_invitation_batch' THEN
        v_batch_snapshot := aap_identity_invitations_v1._batch_snapshot(
            v_actor_id,v_args->'items'
        );
        SELECT COALESCE(
            pg_catalog.array_agg((item.value #>> '{}')::uuid), ARRAY[]::uuid[]
        ) INTO v_user_ids
        FROM pg_catalog.jsonb_array_elements(v_batch_snapshot->'user_ids') AS item(value);
    ELSIF v_operation IN ('revoke_invitation','resend_invitation','send_invitation_email') THEN
        SELECT invitation.* INTO v_initial FROM public.user_invitations AS invitation
        WHERE invitation.id = (v_args->>'invitation_id')::uuid;
        IF NOT FOUND THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        IF v_operation = 'send_invitation_email' AND v_initial.project_id IS NOT NULL THEN
            v_project_id := v_initial.project_id;
            v_project := aap_identity_invitations_v1._project_record(v_project_id);
            IF v_project IS NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目已删除，无法接受邀请'
                );
            END IF;
            v_owner_id := (v_project->>'owner_id')::uuid;
        END IF;
        v_user_ids := ARRAY[v_actor_id,v_owner_id,
            CASE WHEN v_operation = 'send_invitation_email' THEN v_initial.invited_by END];
    ELSIF v_operation IN ('register_invitation','accept_invitation') THEN
        v_token := v_args->>'invitation_token';
        SELECT invitation.* INTO v_initial FROM public.user_invitations AS invitation
        WHERE invitation.token = v_token;
        IF NOT FOUND THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请链接无效'
            );
        END IF;
        v_project_id := v_initial.project_id;
        v_project := aap_identity_invitations_v1._project_record(v_project_id);
        IF v_project_id IS NOT NULL AND v_project IS NULL THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_owner_unavailable','目标项目已删除，无法接受邀请'
            );
        END IF;
        v_owner_id := (v_project->>'owner_id')::uuid;
        IF v_operation = 'register_invitation' THEN
            SELECT account.id INTO v_target_id FROM public.users AS account
            WHERE account.id = v_operation_id;
        ELSE
            v_target_id := v_actor_id;
        END IF;
        v_user_ids := ARRAY[v_initial.invited_by,v_owner_id,v_target_id,v_actor_id];
    ELSE
        RETURN aap_identity_invitations_v1._failure('invalid_request');
    END IF;
    v_user_ids := ARRAY(SELECT DISTINCT id FROM unnest(v_user_ids) AS users(id)
                        WHERE id IS NOT NULL ORDER BY id);
    IF NOT aap_identity_invitations_v1._lock_users(v_user_ids) THEN
        RETURN aap_identity_invitations_v1._failure('current_scope_busy');
    END IF;
    v_actor_lock := aap_identity_invitations_v1._actor(
        v_actor_input,v_expected_key_hash,v_allow_null,true
    );
    IF v_actor_lock->'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_actor_lock; END IF;
    v_principal := v_actor_lock->'principal';
    v_actor_native := v_actor_lock->'actor';
    v_actor_role := v_actor_native->>'role';
    IF v_actor_id IS NOT NULL AND v_operation <> 'accept_invitation'
       AND v_actor_role NOT IN ('super_admin','project_admin') THEN
        RETURN aap_identity_invitations_v1._failure(
            'policy_rejected','manager_required','当前账号不再具备邀请权限'
        );
    END IF;
    v_policy := aap_identity_invitations_v1._policy(v_config);

    IF v_operation = 'apply_invitation_batch' THEN
        IF v_actor_role NOT IN ('super_admin','project_admin') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','manager_required','当前账号不再具备邀请权限'
            );
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text,0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        BEGIN
            PERFORM account.id FROM public.users AS account
            WHERE account.id = v_actor_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        SELECT * INTO v_receipt
        FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_result := v_receipt.result;
            -- A batch receipt is not a durable permission grant. Recheck every
            -- successful Project scope and referenced invitation before
            -- returning the frozen response to a project administrator.
            FOR v_scope_project IN
                SELECT DISTINCT
                    (argument.value->>'project_id')::uuid AS project_id,
                    (v_batch_snapshot #>> ARRAY[
                        'owners', argument.value->>'index'
                    ])::uuid AS owner_id
                FROM pg_catalog.jsonb_array_elements(v_args->'items') AS argument(value)
                JOIN pg_catalog.jsonb_array_elements(v_result->'items') AS prior(value)
                  ON prior.value->>'index' = argument.value->>'index'
                WHERE prior.value->'ok' = 'true'::jsonb
                  AND pg_catalog.jsonb_typeof(argument.value->'project_id') = 'string'
                ORDER BY project_id
            LOOP
                IF v_scope_project.owner_id IS NULL THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                BEGIN
                    v_workspace := aap_workspace_v1.lock_invitation_project(
                        v_scope_project.project_id, v_scope_project.owner_id
                    );
                EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END;
                IF v_workspace->>'schema' IS DISTINCT FROM 'aap-workspace-invitation-project/v1'
                   OR v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                IF v_actor_role = 'project_admin'
                   AND v_scope_project.owner_id IS DISTINCT FROM v_actor_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
            END LOOP;
            FOR v_batch_item IN
                SELECT prior.value
                FROM pg_catalog.jsonb_array_elements(v_result->'items') AS prior(value)
                WHERE prior.value->'ok' = 'true'::jsonb
                ORDER BY (prior.value->>'invitation_id')::uuid
            LOOP
                BEGIN
                    SELECT invitation.* INTO v_invitation
                    FROM public.user_invitations AS invitation
                    WHERE invitation.id = (v_batch_item->>'invitation_id')::uuid
                      AND invitation.invited_by = v_actor_id
                      AND invitation.email = v_batch_item->>'email'
                      AND invitation.project_id IS NOT DISTINCT FROM
                          CASE WHEN pg_catalog.jsonb_typeof(v_batch_item->'project_id') = 'null'
                              THEN NULL ELSE (v_batch_item->>'project_id')::uuid END
                    FOR KEY SHARE NOWAIT;
                EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END;
                IF NOT FOUND THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
            END LOOP;
            v_replayed := true;
        ELSE
            v_result := aap_identity_invitations_v1._batch_effects(
                false,v_actor_id,v_actor_role,v_principal->>'email',v_context,
                v_policy,v_args->'items',v_derived->'batch_tokens',v_batch_snapshot
            );
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    v_operation_lock := pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text,0)
    );
    IF NOT v_operation_lock THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;

    IF v_operation = 'create_invitation' THEN
        IF v_args->>'role' NOT IN ('super_admin','project_admin','employee','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invalid_platform_role','非法角色: ' || (v_args->>'role')
            );
        END IF;
        IF v_project_id IS NULL AND v_args->'project_member_role' <> 'null'::jsonb THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','account_project_role','账号级邀请不得包含项目职责'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_args->>'project_member_role' NOT IN ('annotator','reviewer','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invalid_project_role',
                '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_args->>'role' = 'viewer'
           AND v_args->>'project_member_role' <> 'viewer' THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','platform_project_role_incompatible',
                '账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_args->>'role' IN ('super_admin','project_admin') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','platform_project_role_incompatible','账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目'
            );
        END IF;
        IF v_actor_role = 'project_admin' AND v_args->>'role' NOT IN ('employee','viewer') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_admin_role_forbidden','项目管理员仅能邀请员工或观察者'
            );
        END IF;
        BEGIN
            PERFORM account.id FROM public.users AS account
            WHERE account.id = v_actor_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(pg_catalog.hashtextextended(v_args->>'email',0)) THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END IF;
        IF v_project_id IS NOT NULL THEN
            v_workspace := aap_workspace_v1.lock_invitation_project(v_project_id,v_owner_id);
            IF v_workspace #>> '{error,code}' = 'project_missing' THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_not_found','目标项目不存在'
                );
            ELSIF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END IF;
            SELECT account.id,account.role,account.is_active INTO v_owner
            FROM public.users AS account WHERE account.id = v_owner_id;
            IF NOT FOUND OR NOT v_owner.is_active
               OR v_owner.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目当前没有可用的负责人'
                );
            END IF;
            IF v_actor_role = 'project_admin' AND v_owner_id <> v_actor_id THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_admin_scope','邀请人已不再管理目标项目'
                );
            END IF;
        END IF;
        SELECT * INTO v_receipt FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation <> v_operation OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_invitation_id := (v_receipt.result #>> '{invitation,id}')::uuid;
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_invitation_id AND invitation.invited_by = v_actor_id
              AND invitation.email = v_args->>'email'
            FOR KEY SHARE NOWAIT;
            IF NOT FOUND OR v_invitation.project_id IS DISTINCT FROM v_project_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_result := v_receipt.result;
            v_replayed := true;
        ELSE
            IF aap_identity_invitations_v1.invitation_quota_reached(
                v_actor_id,v_now,(v_policy->>'max_invitations_per_day')::integer
            ) THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','daily_quota',
                    '每位管理员滚动 24 小时最多邀请 ' ||
                    (v_policy->>'max_invitations_per_day') || ' 人，请稍后再试'
                );
            END IF;
            SELECT account.id,account.role,account.is_active INTO v_target
            FROM public.users AS account
            WHERE account.email = v_args->>'email' AND account.is_active
            ORDER BY account.id LIMIT 1;
            IF FOUND THEN
                IF v_target.id IS DISTINCT FROM v_snapshot_target_id THEN
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END IF;
                v_target_id := v_target.id;
                IF v_project_id IS NULL THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','active_email_exists',
                        '邮箱 ' || (v_args->>'email') || ' 已注册，请选择目标项目后邀请该用户加入'
                    );
                END IF;
                IF v_target.role NOT IN ('employee','viewer')
                   OR (v_target.role = 'viewer' AND v_args->>'project_member_role' <> 'viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','incompatible_account_role',
                        aap_identity_invitations_v1._incompatible_role_detail(
                            v_target.role,v_args->>'project_member_role'
                        )
                    );
                END IF;
                IF aap_workspace_v1.read_invitation_membership(v_project_id,v_target_id) THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','already_member','该账号已是目标项目成员'
                    );
                END IF;
            ELSIF v_snapshot_target_id IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END IF;
            BEGIN
                FOR v_invitation IN
                    SELECT invitation.id FROM public.user_invitations AS invitation
                    WHERE invitation.email = v_args->>'email'
                      AND invitation.accepted_at IS NULL
                      AND (v_actor_role = 'super_admin' OR invitation.invited_by = v_actor_id)
                    ORDER BY invitation.id
                    FOR UPDATE NOWAIT
                LOOP
                    UPDATE public.user_invitations AS invitation
                    SET expires_at = v_now
                    WHERE invitation.id = v_invitation.id;
                END LOOP;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_identity_invitations_v1._failure('current_scope_busy');
            END;
            v_token := v_derived->>'token';
            INSERT INTO public.user_invitations (
                id,email,role,project_role,group_name,project_id,token,expires_at,invited_by
            ) VALUES (
                pg_catalog.gen_random_uuid(),v_args->>'email',v_args->>'role',
                v_args->>'project_member_role',v_args->>'group_name',v_project_id,
                v_token,v_now + pg_catalog.make_interval(days => (v_policy->>'invitation_ttl_days')::integer),
                v_actor_id
            ) RETURNING id INTO v_invitation_id;
            v_audit_id := aap_audit_v1.record_identity_invitation_event(
                'user.invite',
                pg_catalog.jsonb_build_object('actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role),
                pg_catalog.jsonb_build_object('type','user','id',v_args->>'email'),
                v_context,
                pg_catalog.jsonb_build_object(
                    'role',v_args->>'role','group_name',v_args->'group_name','project_id',v_args->'project_id'
                )
            );
            v_result := aap_identity_invitations_v1._empty_result()
                || pg_catalog.jsonb_build_object(
                    'invitation',aap_identity_invitations_v1._invitation_record(
                        v_invitation_id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
                    ),'frontend_base_url',v_policy->'frontend_base_url'
                );
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result,audit_id
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result,v_audit_id
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    IF v_operation IN ('revoke_invitation','resend_invitation') THEN
        IF v_actor_role = 'project_admin'
           AND v_initial.invited_by IS DISTINCT FROM v_actor_id THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        BEGIN
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_initial.id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT FOUND THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        SELECT * INTO v_receipt
        FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_result := v_receipt.result;
            v_replayed := true;
        ELSE
            IF v_invitation.accepted_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_accepted',CASE WHEN v_operation = 'revoke_invitation' THEN '该邀请已被接受，无法撤销' ELSE '该邀请已被接受，无法重发' END
                );
            END IF;
            IF v_operation = 'resend_invitation'
               AND v_actor_role = 'project_admin'
               AND v_invitation.role NOT IN ('employee','viewer') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','privileged_resend','项目管理员不能重发高权限邀请'
                );
            END IF;
            IF v_operation = 'revoke_invitation' THEN
                IF v_invitation.revoked_at IS NULL THEN
                    UPDATE public.user_invitations AS invitation
                    SET revoked_at = v_now, expires_at = v_now
                    WHERE invitation.id = v_invitation.id;
                END IF;
                v_detail := pg_catalog.jsonb_build_object(
                    'email',v_invitation.email,'role',v_invitation.role
                );
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'user.invite_revoke',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                    ),
                    pg_catalog.jsonb_build_object('type','invitation','id',v_invitation.id::text),
                    v_context,v_detail
                );
                v_result := aap_identity_invitations_v1._empty_result()
                    || pg_catalog.jsonb_build_object(
                        'invitation_id',v_invitation.id
                    );
            ELSE
                IF v_invitation.role IN ('annotator','reviewer') THEN
                    IF v_invitation.project_id IS NOT NULL AND v_invitation.project_role IS NULL THEN
                        v_invitation.project_role := v_invitation.role;
                    END IF;
                    v_invitation.role := 'employee';
                END IF;
                v_token := v_derived->>'token';
                UPDATE public.user_invitations AS invitation
                SET role = v_invitation.role,
                    project_role = v_invitation.project_role,
                    token = v_token,
                    expires_at = v_now + pg_catalog.make_interval(
                        days => (v_policy->>'invitation_ttl_days')::integer
                    ),
                    revoked_at = NULL
                WHERE invitation.id = v_invitation.id;
                v_invitation.role := v_invitation.role;
                v_invitation.token := v_token;
                v_invitation.expires_at := v_now + pg_catalog.make_interval(
                    days => (v_policy->>'invitation_ttl_days')::integer
                );
                v_invitation.revoked_at := NULL;
                v_detail := pg_catalog.jsonb_build_object(
                    'email',v_invitation.email,'role',v_invitation.role
                );
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'user.invite_resend',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                    ),
                    pg_catalog.jsonb_build_object('type','invitation','id',v_invitation.id::text),
                    v_context,v_detail
                );
                v_result := aap_identity_invitations_v1._empty_result()
                    || pg_catalog.jsonb_build_object(
                        'invitation',aap_identity_invitations_v1._invitation_record(v_invitation.id,NULL),
                        'frontend_base_url',v_derived->'frontend_base_url'
                    );
            END IF;
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result,audit_id
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result,v_audit_id
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    IF v_operation = 'send_invitation_email' THEN
        IF v_actor_role = 'project_admin'
           AND v_initial.invited_by IS DISTINCT FROM v_actor_id THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(v_initial.email,0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        IF v_project_id IS NOT NULL THEN
            v_workspace := aap_workspace_v1.lock_invitation_project(v_project_id,v_owner_id);
            IF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目已删除，无法接受邀请'
                );
            END IF;
            v_project := pg_catalog.jsonb_build_object(
                'id',v_project_id,'name',v_workspace #>> '{data,name}',
                'owner_id',v_owner_id
            );
            SELECT account.id,account.role,account.is_active INTO v_owner
            FROM public.users AS account WHERE account.id = v_owner_id;
            IF NOT FOUND OR NOT v_owner.is_active
               OR v_owner.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目当前没有可用的负责人'
                );
            END IF;
        END IF;
        BEGIN
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_initial.id
            FOR KEY SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT FOUND OR v_invitation.invited_by IS DISTINCT FROM v_initial.invited_by THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请不存在'
            );
        END IF;
        SELECT * INTO v_receipt
        FROM public.identity_invitation_operation_receipts AS receipt
        WHERE receipt.operation_id = v_operation_id;
        IF FOUND THEN
            IF v_receipt.operation IS DISTINCT FROM v_operation
               OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
               OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
               OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            SELECT outbox.* INTO v_outbox
            FROM public.identity_credential_mail_outbox AS outbox
            WHERE outbox.id = v_operation_id AND outbox.purpose = 'invitation'
              AND outbox.invitation_id = v_invitation.id;
            IF NOT FOUND OR v_outbox.token_hash IS DISTINCT FROM pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_invitation.token,'UTF8')),'hex'
            ) OR v_outbox.email IS DISTINCT FROM v_invitation.email
              OR v_outbox.actor_id IS DISTINCT FROM v_invitation.invited_by
              OR v_outbox.expires_at IS DISTINCT FROM v_invitation.expires_at
            THEN RETURN aap_identity_invitations_v1._failure('operation_conflict'); END IF;
            v_result := v_receipt.result || pg_catalog.jsonb_build_object(
                'mail_status',CASE WHEN v_outbox.status = 'delivered' THEN 'sent'
                    WHEN v_outbox.status IN ('rejected','cancelled','expired') THEN 'failed'
                    ELSE 'pending' END,
                'mail_failure_code',CASE WHEN v_outbox.status IN ('rejected','cancelled','expired')
                    THEN v_outbox.last_reason ELSE NULL END
            );
            v_replayed := true;
        ELSE
            IF v_invitation.accepted_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_used','该邀请已被使用'
                );
            ELSIF v_invitation.revoked_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_revoked','该邀请已撤销'
                );
            ELSIF v_invitation.expires_at <= v_now THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_expired','该邀请已过期'
                );
            END IF;
            SELECT account.id,account.role,account.is_active INTO v_inviter
            FROM public.users AS account WHERE account.id = v_invitation.invited_by;
            IF NOT FOUND OR NOT v_inviter.is_active
               OR v_inviter.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','issuer_inactive','邀请人已停用或不再具备管理权限'
                );
            END IF;
            IF v_invitation.role IN ('super_admin','project_admin') AND v_inviter.role <> 'super_admin' THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','issuer_role_invalid','该邀请权限已失效'
                );
            END IF;
            IF v_project_id IS NOT NULL AND v_inviter.role = 'project_admin'
               AND v_owner_id <> v_invitation.invited_by THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_admin_scope','邀请人已不再管理目标项目'
                );
            END IF;
            v_mail_hash := pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(v_invitation.token,'UTF8')),'hex'
            );
            IF v_derived->>'mail_token_hash' IS DISTINCT FROM v_mail_hash THEN
                RETURN aap_identity_invitations_v1._failure('operation_conflict');
            END IF;
            v_project_role := CASE WHEN v_project_id IS NULL THEN NULL
                ELSE COALESCE(v_invitation.project_role,
                    CASE WHEN v_invitation.role IN ('annotator','reviewer','viewer')
                         THEN v_invitation.role END) END;
            IF v_project_id IS NOT NULL AND v_project_role NOT IN ('annotator','reviewer','viewer') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invalid_project_role',
                    '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色'
                );
            END IF;
            v_mail_context := pg_catalog.jsonb_build_object(
                'project_name',CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END,
                'project_member_role',v_project_role,
                'sender_actor',pg_catalog.jsonb_build_object(
                    'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                ),
                'audit_context',v_context
            );
            INSERT INTO public.identity_credential_mail_outbox (
                id,actor_id,challenge_id,purpose,email,frontend_base_url,
                token_hash,token_ciphertext,encryption_id,expires_at,
                invitation_id,invitation_context
            ) VALUES (
                v_operation_id,v_invitation.invited_by,NULL,'invitation',v_invitation.email,
                v_derived->>'mail_frontend_url',v_mail_hash,
                v_derived->>'mail_token_ciphertext',v_derived->>'mail_encryption_id',
                v_invitation.expires_at,v_invitation.id,v_mail_context
            );
            v_result := aap_identity_invitations_v1._empty_result()
                || pg_catalog.jsonb_build_object(
                    'invitation',aap_identity_invitations_v1._invitation_record(
                        v_invitation.id,CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END
                    ),
                    'invitation_id',v_invitation.id,'email',v_invitation.email,
                    'mail_status','pending','mail_failure_code',NULL
                );
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    IF v_operation IN ('register_invitation','accept_invitation') THEN
        v_token := v_args->>'invitation_token';
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-identity-invitation-operation:' || v_operation_id::text,0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended(v_initial.email,0)
        ) THEN RETURN aap_identity_invitations_v1._failure('current_scope_busy'); END IF;
        IF v_project_id IS NOT NULL THEN
            v_workspace := aap_workspace_v1.lock_invitation_project(v_project_id,v_owner_id);
            IF v_workspace->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目已删除，无法接受邀请'
                );
            END IF;
            v_project := pg_catalog.jsonb_build_object(
                'id',v_project_id,'name',v_workspace #>> '{data,name}',
                'owner_id',v_owner_id
            );
            SELECT account.id,account.name,account.role,account.is_active INTO v_owner
            FROM public.users AS account WHERE account.id = v_owner_id;
            IF NOT FOUND OR NOT v_owner.is_active
               OR v_owner.role NOT IN ('super_admin','project_admin') THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','project_owner_unavailable','目标项目当前没有可用的负责人'
                );
            END IF;
        END IF;
        BEGIN
            SELECT invitation.* INTO v_invitation
            FROM public.user_invitations AS invitation
            WHERE invitation.id = v_initial.id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_invitations_v1._failure('current_scope_busy');
        END;
        IF NOT FOUND OR v_invitation.token IS DISTINCT FROM v_token THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','invitation_not_found','邀请链接无效'
            );
        END IF;
        SELECT account.id,account.name,account.email,account.role,account.is_active
        INTO v_inviter FROM public.users AS account WHERE account.id = v_invitation.invited_by;
        IF NOT FOUND OR NOT v_inviter.is_active
           OR v_inviter.role NOT IN ('super_admin','project_admin') THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','issuer_inactive','邀请人已停用或不再具备管理权限'
            );
        END IF;
        IF v_invitation.role IN ('super_admin','project_admin') AND v_inviter.role <> 'super_admin' THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','issuer_role_invalid','该邀请权限已失效'
            );
        END IF;
        IF v_project_id IS NOT NULL AND v_inviter.role = 'project_admin'
           AND v_owner_id <> v_invitation.invited_by THEN
            RETURN aap_identity_invitations_v1._failure(
                'policy_rejected','project_admin_scope','邀请人已不再管理目标项目'
            );
        END IF;
        IF v_invitation.accepted_at IS NOT NULL THEN
            IF v_operation = 'register_invitation'
               AND v_invitation.accepted_user_id = v_operation_id THEN
                SELECT * INTO v_receipt
                FROM public.identity_invitation_operation_receipts AS receipt
                WHERE receipt.operation_id = v_operation_id;
                IF NOT FOUND OR v_receipt.operation <> v_operation
                   OR v_receipt.actor_id IS NOT NULL
                   OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
                   OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                SELECT account.password_hash,account.credential_generation,account.role
                INTO v_target FROM public.users AS account
                WHERE account.id = v_operation_id AND account.is_active
                  AND account.email = v_invitation.email;
                IF NOT FOUND THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'operation_conflict'
                    );
                END IF;
                IF v_target.password_hash IS DISTINCT FROM v_derived->>'password_hash'
                   OR v_target.credential_generation IS DISTINCT FROM
                      (v_receipt.result #>> '{claims,gen}')::bigint
                   OR v_target.role IS DISTINCT FROM v_receipt.result #>> '{claims,role}'
                   OR v_receipt.result->'claims' IS DISTINCT FROM v_derived->'claims' THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                v_result := v_receipt.result;
                v_replayed := true;
            ELSIF v_operation = 'accept_invitation' THEN
                SELECT * INTO v_receipt
                FROM public.identity_invitation_operation_receipts AS receipt
                WHERE receipt.operation_id = v_operation_id;
                IF NOT FOUND OR v_receipt.operation <> v_operation
                   OR v_receipt.actor_id IS DISTINCT FROM v_actor_id
                   OR v_receipt.request_fingerprint IS DISTINCT FROM v_fingerprint
                   OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id
                   OR v_invitation.accepted_user_id IS DISTINCT FROM v_actor_id THEN
                    RETURN aap_identity_invitations_v1._failure('operation_conflict');
                END IF;
                v_result := v_receipt.result;
                v_replayed := true;
            ELSE
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_used','该邀请已被使用'
                );
            END IF;
        ELSE
            SELECT * INTO v_receipt
            FROM public.identity_invitation_operation_receipts AS receipt
            WHERE receipt.operation_id = v_operation_id;
            IF FOUND THEN RETURN aap_identity_invitations_v1._failure('operation_conflict'); END IF;
            IF v_invitation.revoked_at IS NOT NULL THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_revoked','该邀请已撤销'
                );
            END IF;
            IF v_invitation.expires_at <= v_now THEN
                RETURN aap_identity_invitations_v1._failure(
                    'policy_rejected','invitation_expired','该邀请已过期'
                );
            END IF;
            v_project_role := CASE WHEN v_project_id IS NULL THEN NULL
                ELSE COALESCE(v_invitation.project_role,
                    CASE WHEN v_invitation.role IN ('annotator','reviewer','viewer')
                         THEN v_invitation.role END) END;
            IF v_operation = 'register_invitation' THEN
                IF v_invitation.role NOT IN ('super_admin','project_admin','employee','viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','legacy_platform_role','该邀请包含已失效的账号角色，请联系管理员重发'
                    );
                END IF;
                IF v_project_id IS NOT NULL AND v_invitation.role IN ('super_admin','project_admin') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','platform_project_role_incompatible','账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目'
                    );
                END IF;
                IF pg_catalog.char_length(COALESCE(v_invitation.group_name,'')) > 100 THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','group_name_invalid','邀请中的数据组名称无效，请联系管理员重发邀请'
                    );
                END IF;
                IF EXISTS (SELECT 1 FROM public.users AS account
                           WHERE account.email = v_invitation.email) THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','email_exists','该邮箱已注册，请让该用户登录后确认项目邀请'
                    );
                END IF;
                v_password_hash := v_derived->>'password_hash';
                v_claims := v_derived->'claims';
                SELECT pg_catalog.count(*)::integer INTO v_count
                FROM pg_catalog.jsonb_object_keys(v_claims);
                IF v_count <> 6 OR NOT (v_claims ?& ARRAY['sub','role','exp','jti','gen','iss'])
                   OR EXISTS (
                       SELECT 1 FROM pg_catalog.jsonb_object_keys(v_claims) AS field(name)
                       WHERE field.name NOT IN ('sub','role','exp','jti','gen','iss')
                   ) OR pg_catalog.jsonb_typeof(v_claims->'sub') IS DISTINCT FROM 'string'
                   OR v_claims->>'sub' IS DISTINCT FROM v_operation_id::text
                   OR pg_catalog.jsonb_typeof(v_claims->'jti') IS DISTINCT FROM 'string'
                   OR v_claims->>'jti' IS DISTINCT FROM v_operation_id::text
                   OR pg_catalog.jsonb_typeof(v_claims->'role') IS DISTINCT FROM 'string'
                   OR v_claims->>'role' IS DISTINCT FROM v_invitation.role
                   OR pg_catalog.jsonb_typeof(v_claims->'exp') IS DISTINCT FROM 'number'
                   OR (v_claims->>'exp') !~ '^[1-9][0-9]*$'
                   OR (v_claims->>'exp')::numeric > 9223372036854775807
                   OR pg_catalog.jsonb_typeof(v_claims->'gen') IS DISTINCT FROM 'number'
                   OR v_claims->>'gen' IS DISTINCT FROM '0'
                   OR pg_catalog.jsonb_typeof(v_claims->'iss') IS DISTINCT FROM 'string'
                   OR v_claims->>'iss' IS DISTINCT FROM 'aap-identity/v1' THEN
                    RETURN aap_identity_invitations_v1._failure('invalid_request');
                END IF;
                BEGIN
                    IF pg_catalog.to_timestamp((v_claims->>'exp')::double precision) <= v_now THEN
                        RETURN aap_identity_invitations_v1._failure('token_expired');
                    END IF;
                EXCEPTION WHEN datetime_field_overflow OR numeric_value_out_of_range THEN
                    RETURN aap_identity_invitations_v1._failure('invalid_request');
                END;
                v_group_name := aap_identity_invitations_v1._strip_unicode_space(v_invitation.group_name);
                IF v_group_name = '' THEN v_group_name := NULL; END IF;
                IF v_group_name IS NOT NULL THEN
                    v_group := aap_identity_groups_v1.resolve_invitation_group(
                        v_invitation.id,v_invitation.token
                    );
                    IF pg_catalog.jsonb_typeof(v_group->'error') = 'object' THEN
                        IF v_group #>> '{error,code}' = 'invalid_group_name' THEN
                            RETURN aap_identity_invitations_v1._failure(
                                'policy_rejected','group_name_invalid','邀请中的数据组名称无效，请联系管理员重发邀请'
                            );
                        END IF;
                        RETURN aap_identity_invitations_v1._failure(
                            'policy_rejected','invitation_not_found','邀请链接无效'
                        );
                    END IF;
                    v_group_id := (v_group->>'id')::uuid;
                    v_group_name := v_group->>'name';
                    UPDATE public.user_invitations AS invitation
                    SET group_name = v_group_name WHERE invitation.id = v_invitation.id;
                END IF;
                INSERT INTO public.users (
                    id,email,name,password_hash,credential_generation,role,group_name,group_id,
                    status,is_active,email_verified_at
                ) VALUES (
                    v_operation_id,v_invitation.email,v_args->>'name',v_password_hash,0,
                    v_invitation.role,v_group_name,v_group_id,'online',true,v_now
                );
                v_user_id := v_operation_id;
                IF v_project_id IS NOT NULL THEN
                    v_member_reply := aap_workspace_v1.grant_invitation_membership(
                        v_invitation.invited_by,v_project_id,v_project_role,
                        v_user_id,v_owner_id
                    );
                    IF v_member_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'invitation membership scope changed';
                    END IF;
                END IF;
                UPDATE public.user_invitations AS invitation
                SET accepted_at = v_now, accepted_user_id = v_user_id
                WHERE invitation.id = v_invitation.id;
                v_acceptance := pg_catalog.jsonb_build_object(
                    'project_id',v_project_id,
                    'project_name',CASE WHEN v_project IS NULL THEN NULL ELSE v_project->>'name' END,
                    'project_member_role',v_project_role,
                    'next_action','wait_for_allocation',
                    'next_action_label',CASE WHEN v_project_id IS NULL THEN '等待管理员分配项目'
                                             ELSE '等待分派' END,
                    'responsible_person_name',CASE WHEN v_project_id IS NULL THEN NULL ELSE v_owner.name END,
                    'active_batch_count',0
                );
                IF v_project_id IS NOT NULL AND v_project_role IN ('annotator','reviewer') THEN
                    v_active_count := aap_labeling_v1.invitation_work_summary(
                        v_project_id,v_user_id,v_project_role
                    );
                    v_acceptance := v_acceptance || pg_catalog.jsonb_build_object(
                        'active_batch_count',v_active_count,
                        'next_action',CASE WHEN v_active_count > 0 THEN 'start_work' ELSE 'wait_for_allocation' END,
                        'next_action_label',CASE WHEN v_active_count > 0 THEN '开始工作' ELSE '等待分派' END
                    );
                END IF;
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'user.register',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_user_id,'email',v_invitation.email,'role',v_invitation.role
                    ),
                    pg_catalog.jsonb_build_object('type','user','id',v_user_id::text),
                    v_context,
                    pg_catalog.jsonb_build_object(
                        'email',v_invitation.email,'role',v_invitation.role,
                        'invitation_id',v_invitation.id,'project_id',v_project_id
                    )
                );
                v_result := aap_identity_invitations_v1._empty_result()
                    || pg_catalog.jsonb_build_object(
                        'user',aap_identity_invitations_v1._account_record(v_user_id),
                        'claims',v_claims,'acceptance',v_acceptance,
                        'email_verification_required',false
                    );
            ELSE
                IF v_project_id IS NULL THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','account_only_invitation',
                        '该邀请仅用于创建新账号，已有账号只能接受带目标项目的邀请'
                    );
                END IF;
                IF v_project_role NOT IN ('annotator','reviewer','viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','invalid_project_role',
                        '目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色'
                    );
                END IF;
                IF v_actor_native->>'email' IS DISTINCT FROM v_invitation.email THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','email_mismatch','当前登录邮箱与邀请邮箱不一致，请使用被邀请邮箱登录'
                    );
                END IF;
                IF v_actor_role NOT IN ('employee','viewer')
                   OR (v_actor_role = 'viewer' AND v_project_role <> 'viewer') THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','incompatible_account_role',
                        aap_identity_invitations_v1._incompatible_role_detail(
                            v_actor_role,v_project_role
                        )
                    );
                END IF;
                IF aap_workspace_v1.read_invitation_membership(v_project_id,v_actor_id) THEN
                    RETURN aap_identity_invitations_v1._failure(
                        'policy_rejected','already_member','该账号已是目标项目成员'
                    );
                END IF;
                v_member_reply := aap_workspace_v1.grant_invitation_membership(
                    v_invitation.invited_by,v_project_id,v_project_role,v_actor_id,v_owner_id
                );
                IF v_member_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    IF v_member_reply #>> '{error,code}' = 'membership_exists' THEN
                        RETURN aap_identity_invitations_v1._failure(
                            'policy_rejected','already_member','该账号已是目标项目成员'
                        );
                    END IF;
                    RETURN aap_identity_invitations_v1._failure('current_scope_busy');
                END IF;
                UPDATE public.user_invitations AS invitation
                SET accepted_at = v_now, accepted_user_id = v_actor_id
                WHERE invitation.id = v_invitation.id;
                v_active_count := 0;
                IF v_project_role IN ('annotator','reviewer') THEN
                    v_active_count := aap_labeling_v1.invitation_work_summary(
                        v_project_id,v_actor_id,v_project_role
                    );
                END IF;
                v_acceptance := pg_catalog.jsonb_build_object(
                    'project_id',v_project_id,'project_name',v_project->>'name',
                    'project_member_role',v_project_role,
                    'next_action',CASE WHEN v_active_count > 0 THEN 'start_work' ELSE 'wait_for_allocation' END,
                    'next_action_label',CASE WHEN v_active_count > 0 THEN '开始工作' ELSE '等待分派' END,
                    'responsible_person_name',v_owner.name,
                    'active_batch_count',v_active_count
                );
                v_audit_id := aap_audit_v1.record_identity_invitation_event(
                    'user.invite_accept_existing',
                    pg_catalog.jsonb_build_object(
                        'actor_id',v_actor_id,'email',v_principal->>'email','role',v_actor_role
                    ),
                    pg_catalog.jsonb_build_object('type','invitation','id',v_invitation.id::text),
                    v_context,
                    pg_catalog.jsonb_build_object('email',v_actor_native->>'email','project_id',v_project_id)
                );
                v_result := aap_identity_invitations_v1._empty_result()
                    || pg_catalog.jsonb_build_object(
                        'user',aap_identity_invitations_v1._account_record(v_actor_id),
                        'acceptance',v_acceptance
                    );
            END IF;
            INSERT INTO public.identity_invitation_operation_receipts (
                operation_id,actor_id,operation,request_fingerprint,derivation_id,result,audit_id
            ) VALUES (
                v_operation_id,v_actor_id,v_operation,v_fingerprint,v_derivation_id,v_result,v_audit_id
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-identity-invitations-native/v1','ok',true,
            'operation',v_operation,'operation_id',v_operation_id,
            'principal',v_principal,'data',pg_catalog.jsonb_build_object(
                'actor',v_actor_native,'replayed',v_replayed,'result',v_result
            )
        );
    END IF;

    -- Other transactional operations are handled below.
    RETURN aap_identity_invitations_v1._failure('unavailable');
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_identity_directory_v1._directory_user(p_user_id uuid)
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
CREATE OR REPLACE FUNCTION aap_identity_directory_v1.execute_directory_command(p_command jsonb)
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
CREATE OR REPLACE FUNCTION aap_identity_suspensions_v1.execute_suspension_command(
    p_command jsonb
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
    v_operation text;
    v_operation_id uuid;
    v_actor jsonb;
    v_actor_id uuid;
    v_expected_key_hash text;
    v_request_fingerprint text;
    v_derivation_id text;
    v_context jsonb;
    v_payload jsonb;
    v_target_user_id uuid;
    v_reason_present boolean := false;
    v_locked_user_id uuid;
    v_target_found boolean := false;
    v_target_email text;
    v_target_role text;
    v_target_active boolean;
    v_target_disabled_kind text;
    v_target_generation bigint;
    v_actor_reply jsonb;
    v_principal jsonb;
    v_actor_role text;
    v_scope_has_projects boolean;
    v_scope_all_projects_owned boolean;
    v_super_admin_status jsonb;
    v_other_unreserved bigint;
    v_retirement jsonb;
    v_receipt record;
    v_receipt_found boolean := false;
    v_audit_id bigint;
    v_user_record jsonb;
    v_result jsonb;
    v_project_failure jsonb := pg_catalog.jsonb_build_object(
        'reason', 'project_busy'
    );
BEGIN
    IF NOT aap_identity_suspensions_v1._has_exact_keys(
        p_command,
        ARRAY[
            'schema', 'operation', 'operation_id', 'actor',
            'expected_key_hash', 'request_fingerprint', 'derivation_id',
            'audit_context', 'payload'
        ]
    ) OR p_command->>'schema' IS DISTINCT FROM
            'aap-identity-suspensions-native/v1'
       OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
       OR p_command->>'operation' NOT IN (
           'suspend_account', 'reactivate_account'
       )
       OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR COALESCE(
           pg_catalog.jsonb_typeof(p_command->'expected_key_hash')
               NOT IN ('string', 'null'),
           true
       )
       OR (pg_catalog.jsonb_typeof(p_command->'expected_key_hash') = 'string'
           AND pg_catalog.octet_length(p_command->>'expected_key_hash') NOT BETWEEN 1 AND 255)
       OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR NOT aap_identity_suspensions_v1._has_exact_keys(
           p_command->'actor',
           ARRAY[
               'actor_id', 'name', 'email', 'role', 'is_active',
               'credential_type', 'credential_id', 'generation', 'scopes',
               'expires_at'
           ]
       ) OR pg_catalog.jsonb_typeof(p_command #> '{actor,actor_id}')
            IS DISTINCT FROM 'string'
       OR (p_command #>> '{actor,actor_id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR NOT aap_identity_suspensions_v1._has_exact_keys(
           p_command->'audit_context',
           ARRAY['method', 'path', 'ip', 'request_id']
       ) OR NOT aap_audit_v1.valid_request_context(p_command->'audit_context') THEN
        RETURN aap_identity_suspensions_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_operation := p_command->>'operation';
    v_operation_id := (p_command->>'operation_id')::uuid;
    v_actor := p_command->'actor';
    v_actor_id := (v_actor->>'actor_id')::uuid;
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_request_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_context := p_command->'audit_context';
    v_payload := p_command->'payload';

    IF v_operation = 'suspend_account' THEN
        IF NOT aap_identity_suspensions_v1._has_exact_keys(
            v_payload, ARRAY['target_user_id']
        ) OR pg_catalog.jsonb_typeof(v_payload->'target_user_id')
             IS DISTINCT FROM 'string'
          OR (v_payload->>'target_user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_suspensions_v1._failure(
                'invalid_request', 400, NULL
            );
        END IF;
    ELSE
        IF NOT aap_identity_suspensions_v1._has_exact_keys(
            v_payload, ARRAY['target_user_id', 'reason_present']
        ) OR pg_catalog.jsonb_typeof(v_payload->'target_user_id')
             IS DISTINCT FROM 'string'
          OR (v_payload->>'target_user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          OR pg_catalog.jsonb_typeof(v_payload->'reason_present')
                 IS DISTINCT FROM 'boolean' THEN
            RETURN aap_identity_suspensions_v1._failure(
                'invalid_request', 400, NULL
            );
        END IF;
        v_reason_present := (v_payload->>'reason_present')::boolean;
    END IF;
    v_target_user_id := (v_payload->>'target_user_id')::uuid;

    IF NOT aap_identity_v1.lock_super_admin_invariant() THEN
        RETURN aap_identity_suspensions_v1._failure(
            'account_busy', 409,
            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    END IF;

    BEGIN
        FOR v_locked_user_id IN
            SELECT account.id
            FROM public.users AS account
            WHERE account.id = ANY(
                ARRAY[v_actor_id, v_target_user_id]::uuid[]
            )
            ORDER BY account.id
            FOR UPDATE OF account NOWAIT
        LOOP
            NULL;
        END LOOP;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_suspensions_v1._failure(
            'account_busy', 409,
            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    END;

    SELECT account.email, account.role, account.is_active,
           account.disabled_kind, account.credential_generation
    INTO v_target_email, v_target_role, v_target_active,
         v_target_disabled_kind, v_target_generation
    FROM public.users AS account
    WHERE account.id = v_target_user_id;
    v_target_found := FOUND;
    IF v_operation = 'reactivate_account' AND NOT v_target_found THEN
        RETURN aap_identity_suspensions_v1._failure(
            'target_not_found', 404, pg_catalog.to_jsonb('用户不存在'::text)
        );
    END IF;

    v_actor_reply := aap_identity_accounts_v1._lock_current_actor(
        v_actor,
        v_expected_key_hash,
        'write_nowait'
    );
    IF v_actor_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_actor_reply #>> '{error,code}'
            WHEN 'invalid_request' THEN
                RETURN aap_identity_suspensions_v1._failure('invalid_request', 400, NULL);
            WHEN 'credential_invalid' THEN
                RETURN aap_identity_suspensions_v1._failure('credential_invalid', NULL, NULL);
            WHEN 'credential_revoked' THEN
                RETURN aap_identity_suspensions_v1._failure('credential_revoked', NULL, NULL);
            WHEN 'generation_outdated' THEN
                RETURN aap_identity_suspensions_v1._failure('generation_outdated', NULL, NULL);
            WHEN 'token_expired' THEN
                RETURN aap_identity_suspensions_v1._failure('token_expired', NULL, NULL);
            WHEN 'user_inactive' THEN
                RETURN aap_identity_suspensions_v1._failure('user_inactive', NULL, NULL);
            WHEN 'invalid_account_role' THEN
                RETURN aap_identity_suspensions_v1._failure('invalid_account_role', NULL, NULL);
            WHEN 'credential_busy' THEN
                RETURN aap_identity_suspensions_v1._failure('credential_busy', NULL, NULL);
            WHEN 'account_busy' THEN
                RETURN aap_identity_suspensions_v1._failure(
                    'account_busy', 409,
                    pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
                );
            ELSE
                RETURN aap_identity_suspensions_v1._failure('unavailable', 503, NULL);
        END CASE;
    END IF;
    v_principal := v_actor_reply->'data';
    v_actor_role := v_principal->>'role';
    IF v_actor_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_identity_suspensions_v1._failure(
            'manager_required', 403,
            pg_catalog.to_jsonb('当前账号已无管理权限'::text)
        );
    END IF;

    IF v_operation = 'suspend_account' AND NOT v_target_found THEN
        RETURN aap_identity_suspensions_v1._failure(
            'target_not_found', 404, pg_catalog.to_jsonb('用户不存在'::text)
        );
    END IF;
    IF v_actor_id = v_target_user_id THEN
        IF v_operation = 'suspend_account' THEN
            RETURN aap_identity_suspensions_v1._failure(
                'self_suspend', 400, pg_catalog.to_jsonb('不能停用自己'::text)
            );
        END IF;
        RETURN aap_identity_suspensions_v1._failure(
            'self_reactivate', 400, pg_catalog.to_jsonb('不能恢复自己'::text)
        );
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-identity-suspensions-operation/v1:' || v_operation_id::text,
            0
        )
    ) THEN
        RETURN aap_identity_suspensions_v1._failure(
            'account_busy', 409,
            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    END IF;

    SELECT receipt.actor_id, receipt.operation, receipt.target_user_id,
           receipt.request_fingerprint, receipt.derivation_id,
           receipt.target_generation, receipt.target_is_active,
           receipt.result
    INTO v_receipt
    FROM public.identity_suspension_operation_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    v_receipt_found := FOUND;
    IF v_receipt_found THEN
        IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.operation IS DISTINCT FROM v_operation
           OR v_receipt.target_user_id IS DISTINCT FROM v_target_user_id
           OR v_receipt.request_fingerprint IS DISTINCT FROM v_request_fingerprint
           OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
            RETURN aap_identity_suspensions_v1._failure(
                'operation_conflict', 409, NULL
            );
        END IF;
        IF v_receipt.target_generation IS DISTINCT FROM v_target_generation
           OR v_receipt.target_is_active IS DISTINCT FROM v_target_active THEN
            RETURN aap_identity_suspensions_v1._failure(
                'operation_conflict', 409, NULL
            );
        END IF;
    END IF;

    IF v_operation = 'suspend_account' THEN
        -- Preserve the legacy inactive no-op before PA role/scope checks. This
        -- also keeps its accepted replay from becoming a new suspension later.
        IF v_target_active THEN
            BEGIN
                SELECT scope.has_projects, scope.all_projects_owned
                INTO STRICT v_scope_has_projects, v_scope_all_projects_owned
                FROM aap_workspace_v1.lock_identity_suspension_scope(
                    v_actor_id, v_target_user_id, v_operation
                ) AS scope;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_identity_suspensions_v1._failure(
                    'project_busy', 409, v_project_failure
                );
            WHEN no_data_found OR too_many_rows THEN
                RETURN aap_identity_suspensions_v1._failure(
                    'unavailable', 503, NULL
                );
            END;
            IF v_scope_has_projects IS NULL
               OR v_scope_all_projects_owned IS NULL THEN
                RETURN aap_identity_suspensions_v1._failure('unavailable', 503, NULL);
            END IF;
            IF v_actor_role = 'project_admin' THEN
                IF v_target_role IS DISTINCT FROM 'employee' THEN
                    RETURN aap_identity_suspensions_v1._failure(
                        'suspend_role_forbidden', 403,
                        pg_catalog.to_jsonb('项目管理员仅能停用标注员/审核员账号'::text)
                    );
                END IF;
                IF NOT v_scope_all_projects_owned THEN
                    RETURN aap_identity_suspensions_v1._failure(
                        'suspend_scope_forbidden', 403,
                        pg_catalog.to_jsonb('该用户跨多个项目，须由超级管理员处理'::text)
                    );
                END IF;
            END IF;
        END IF;

        IF v_receipt_found THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-suspensions-native/v1',
                'ok', true,
                'operation', v_operation,
                'operation_id', v_operation_id,
                'principal', v_principal,
                'data', v_receipt.result,
                'replayed', true
            );
        END IF;

        IF NOT v_target_active THEN
            v_user_record := aap_identity_directory_v1._directory_user(
                v_target_user_id
            );
            IF v_user_record IS NULL
               OR (v_user_record->>'id')::uuid IS DISTINCT FROM v_target_user_id THEN
                RETURN aap_identity_suspensions_v1._failure('unavailable', 503, NULL);
            END IF;
            v_result := pg_catalog.jsonb_build_object('user', v_user_record);
            INSERT INTO public.identity_suspension_operation_receipts (
                operation_id, actor_id, operation, target_user_id,
                request_fingerprint, derivation_id, target_generation,
                target_is_active, result, audit_id
            ) VALUES (
                v_operation_id, v_actor_id, v_operation, v_target_user_id,
                v_request_fingerprint, v_derivation_id, v_target_generation,
                false, v_result, NULL
            );
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-suspensions-native/v1',
                'ok', true,
                'operation', v_operation,
                'operation_id', v_operation_id,
                'principal', v_principal,
                'data', v_result,
                'replayed', false
            );
        END IF;

        IF v_target_role = 'super_admin' THEN
            v_super_admin_status := aap_identity_v1.super_admin_status(
                v_target_user_id
            );
            v_other_unreserved :=
                (v_super_admin_status->>'other_unreserved')::bigint;
            IF v_other_unreserved = 0 THEN
                RETURN aap_identity_suspensions_v1._failure(
                    'last_super_admin', 400,
                    pg_catalog.to_jsonb('不能停用最后一名超级管理员'::text)
                );
            END IF;
        END IF;

        UPDATE public.users AS account
        SET is_active = false,
            status = 'offline',
            disabled_kind = 'suspended',
            disabled_at = pg_catalog.clock_timestamp(),
            disabled_by = v_actor_id,
            disabled_reason = '管理员停用账号'
        WHERE account.id = v_target_user_id;
        v_retirement := aap_identity_v1.retire_account_credentials(v_target_user_id);
        IF pg_catalog.jsonb_typeof(v_retirement) IS DISTINCT FROM 'object'
           OR v_retirement->>'schema' IS DISTINCT FROM 'aap-identity-retirement/v1'
           OR pg_catalog.jsonb_typeof(v_retirement->'generation') IS DISTINCT FROM 'number'
           OR (v_retirement->>'generation') !~ '^(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity credential retirement result';
        END IF;
        v_target_generation := (v_retirement->>'generation')::bigint;
        v_audit_id := aap_audit_v1.record_identity_suspension_event(
            'user.deactivate',
            pg_catalog.jsonb_build_object(
                'actor_id', v_principal->'actor_id',
                'email', v_principal->'email',
                'role', v_principal->'role'
            ),
            v_target_user_id,
            pg_catalog.jsonb_build_object('email', v_target_email),
            v_context
        );
    ELSE
        IF v_actor_role = 'project_admin' THEN
            IF v_target_role IS DISTINCT FROM 'employee' THEN
                RETURN aap_identity_suspensions_v1._failure(
                    'reactivate_role_forbidden', 403,
                    pg_catalog.to_jsonb('项目管理员不能恢复该角色'::text)
                );
            END IF;
            BEGIN
                SELECT scope.has_projects, scope.all_projects_owned
                INTO STRICT v_scope_has_projects, v_scope_all_projects_owned
                FROM aap_workspace_v1.lock_identity_suspension_scope(
                    v_actor_id, v_target_user_id, v_operation
                ) AS scope;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_identity_suspensions_v1._failure(
                    'account_busy', 409,
                    pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
                );
            WHEN no_data_found OR too_many_rows THEN
                RETURN aap_identity_suspensions_v1._failure(
                    'unavailable', 503, NULL
                );
            END;
            IF v_scope_has_projects IS NULL
               OR v_scope_all_projects_owned IS NULL THEN
                RETURN aap_identity_suspensions_v1._failure('unavailable', 503, NULL);
            END IF;
            IF NOT v_scope_has_projects THEN
                RETURN aap_identity_suspensions_v1._failure(
                    'reactivate_not_managed', 403,
                    pg_catalog.to_jsonb('该用户不在你管理的项目内'::text)
                );
            END IF;
            IF NOT v_scope_all_projects_owned THEN
                RETURN aap_identity_suspensions_v1._failure(
                    'reactivate_scope_forbidden', 403,
                    pg_catalog.to_jsonb('该用户存在不在你管理范围内的项目'::text)
                );
            END IF;
        END IF;

        IF v_receipt_found THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-identity-suspensions-native/v1',
                'ok', true,
                'operation', v_operation,
                'operation_id', v_operation_id,
                'principal', v_principal,
                'data', v_receipt.result,
                'replayed', true
            );
        END IF;

        IF v_target_active THEN
            RETURN aap_identity_suspensions_v1._failure(
                'already_active', 409,
                pg_catalog.to_jsonb('账号当前已启用'::text)
            );
        END IF;
        IF v_target_disabled_kind IS NULL
           OR v_target_disabled_kind NOT IN ('suspended', 'emergency_suspended') THEN
            RETURN aap_identity_suspensions_v1._failure(
                'not_reactivatable', 409,
                pg_catalog.to_jsonb('该停用状态不可恢复'::text)
            );
        END IF;

        v_retirement := aap_identity_v1.retire_account_credentials(v_target_user_id);
        IF pg_catalog.jsonb_typeof(v_retirement) IS DISTINCT FROM 'object'
           OR v_retirement->>'schema' IS DISTINCT FROM 'aap-identity-retirement/v1'
           OR pg_catalog.jsonb_typeof(v_retirement->'generation') IS DISTINCT FROM 'number'
           OR (v_retirement->>'generation') !~ '^(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity credential retirement result';
        END IF;
        v_target_generation := (v_retirement->>'generation')::bigint;
        UPDATE public.users AS account
        SET is_active = true,
            status = 'offline',
            disabled_kind = NULL,
            disabled_at = NULL,
            disabled_by = NULL,
            disabled_reason = NULL
        WHERE account.id = v_target_user_id;
        v_audit_id := aap_audit_v1.record_identity_suspension_event(
            'user.reactivate',
            pg_catalog.jsonb_build_object(
                'actor_id', v_principal->'actor_id',
                'email', v_principal->'email',
                'role', v_principal->'role'
            ),
            v_target_user_id,
            pg_catalog.jsonb_build_object('reason_present', v_reason_present),
            v_context
        );
    END IF;

    v_user_record := aap_identity_directory_v1._directory_user(v_target_user_id);
    IF v_user_record IS NULL
       OR (v_user_record->>'id')::uuid IS DISTINCT FROM v_target_user_id
       OR v_user_record->'is_active' IS DISTINCT FROM
            (CASE WHEN v_operation = 'suspend_account'
                  THEN 'false'::jsonb ELSE 'true'::jsonb END) THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid frozen Identity suspension user result';
    END IF;
    v_result := pg_catalog.jsonb_build_object('user', v_user_record);
    INSERT INTO public.identity_suspension_operation_receipts (
        operation_id, actor_id, operation, target_user_id,
        request_fingerprint, derivation_id, target_generation,
        target_is_active, result, audit_id
    ) VALUES (
        v_operation_id, v_actor_id, v_operation, v_target_user_id,
        v_request_fingerprint, v_derivation_id, v_target_generation,
        v_operation = 'reactivate_account', v_result, v_audit_id
    );
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-suspensions-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'principal', v_principal,
        'data', v_result,
        'replayed', false
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_suspensions_v1._failure(
            'account_busy', 409,
            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    WHEN OTHERS THEN
        RETURN aap_identity_suspensions_v1._failure('unavailable', 503, NULL);
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_identity_deletions_v1.execute_deletion_command(
    p_command jsonb
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
    v_operation text;
    v_operation_id uuid;
    v_actor jsonb;
    v_actor_id uuid;
    v_expected_key_hash text;
    v_request_fingerprint text;
    v_derivation_id text;
    v_context jsonb;
    v_payload jsonb;
    v_target_user_id uuid;
    v_transfer_to_user_id uuid;
    v_locked_user_id uuid;
    v_target_found boolean := false;
    v_target_email text;
    v_target_role text;
    v_target_active boolean;
    v_target_generation bigint;
    v_actor_reply jsonb;
    v_principal jsonb;
    v_actor_role text;
    v_scope_has_projects boolean;
    v_scope_all_projects_owned boolean;
    v_super_admin_status jsonb;
    v_other_unreserved bigint;
    v_pending_task_count bigint;
    v_locked_task_count bigint;
    v_sample_task_ids uuid[];
    v_pending_project_ids uuid[];
    v_receiver_found boolean := false;
    v_receiver_active boolean;
    v_receiver_role text;
    v_receiver_covered boolean;
    v_transferred_count bigint := 0;
    v_audit_id bigint;
    v_user_record jsonb;
    v_result jsonb;
    v_receipt record;
    v_receipt_found boolean := false;
    v_project_busy_detail jsonb := pg_catalog.jsonb_build_object(
        'reason', 'project_busy'
    );
BEGIN
    IF NOT aap_identity_deletions_v1._has_exact_keys(
        p_command,
        ARRAY[
            'schema', 'operation', 'operation_id', 'actor',
            'expected_key_hash', 'request_fingerprint', 'derivation_id',
            'audit_context', 'payload'
        ]
    ) OR p_command->>'schema' IS DISTINCT FROM
            'aap-identity-deletions-native/v1'
       OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
       OR p_command->>'operation' IS DISTINCT FROM 'delete_account'
       OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR COALESCE(
           pg_catalog.jsonb_typeof(p_command->'expected_key_hash')
               NOT IN ('string', 'null'),
           true
       )
       OR (pg_catalog.jsonb_typeof(p_command->'expected_key_hash') = 'string'
           AND pg_catalog.octet_length(p_command->>'expected_key_hash') NOT BETWEEN 1 AND 255)
       OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR NOT aap_identity_deletions_v1._has_exact_keys(
           p_command->'actor',
           ARRAY[
               'actor_id', 'name', 'email', 'role', 'is_active',
               'credential_type', 'credential_id', 'generation', 'scopes',
               'expires_at'
           ]
       ) OR pg_catalog.jsonb_typeof(p_command #> '{actor,actor_id}')
            IS DISTINCT FROM 'string'
       OR (p_command #>> '{actor,actor_id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR NOT aap_identity_deletions_v1._has_exact_keys(
           p_command->'audit_context',
           ARRAY['method', 'path', 'ip', 'request_id']
       ) OR NOT aap_audit_v1.valid_request_context(p_command->'audit_context') THEN
        RETURN aap_identity_deletions_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_operation := p_command->>'operation';
    v_operation_id := (p_command->>'operation_id')::uuid;
    v_actor := p_command->'actor';
    v_actor_id := (v_actor->>'actor_id')::uuid;
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_request_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_context := p_command->'audit_context';
    v_payload := p_command->'payload';

    IF NOT aap_identity_deletions_v1._has_exact_keys(
        v_payload, ARRAY['target_user_id', 'transfer_to_user_id']
    ) OR pg_catalog.jsonb_typeof(v_payload->'target_user_id')
             IS DISTINCT FROM 'string'
      OR (v_payload->>'target_user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      OR COALESCE(
          pg_catalog.jsonb_typeof(v_payload->'transfer_to_user_id')
              NOT IN ('string', 'null'),
          true
      )
      OR (pg_catalog.jsonb_typeof(v_payload->'transfer_to_user_id') = 'string'
          AND (v_payload->>'transfer_to_user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
        RETURN aap_identity_deletions_v1._failure('invalid_request', 400, NULL);
    END IF;
    v_target_user_id := (v_payload->>'target_user_id')::uuid;
    IF pg_catalog.jsonb_typeof(v_payload->'transfer_to_user_id') = 'string' THEN
        v_transfer_to_user_id := (v_payload->>'transfer_to_user_id')::uuid;
    END IF;

    IF NOT aap_identity_v1.lock_super_admin_invariant() THEN
        RETURN aap_identity_deletions_v1._failure(
            'account_busy', 409,
            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    END IF;

    BEGIN
        FOR v_locked_user_id IN
            SELECT account.id
            FROM public.users AS account
            WHERE account.id = ANY(
                ARRAY[v_actor_id, v_target_user_id, v_transfer_to_user_id]::uuid[]
            )
            ORDER BY account.id
            FOR UPDATE OF account NOWAIT
        LOOP
            NULL;
        END LOOP;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_deletions_v1._failure(
            'account_busy', 409,
            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    END;

    SELECT account.email, account.role, account.is_active,
           account.credential_generation
    INTO v_target_email, v_target_role, v_target_active,
         v_target_generation
    FROM public.users AS account
    WHERE account.id = v_target_user_id;
    v_target_found := FOUND;

    v_actor_reply := aap_identity_accounts_v1._lock_current_actor(
        v_actor, v_expected_key_hash, 'write_nowait'
    );
    IF v_actor_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_actor_reply #>> '{error,code}'
            WHEN 'invalid_request' THEN
                RETURN aap_identity_deletions_v1._failure('invalid_request', 400, NULL);
            WHEN 'credential_invalid' THEN
                RETURN aap_identity_deletions_v1._failure('credential_invalid', NULL, NULL);
            WHEN 'credential_revoked' THEN
                RETURN aap_identity_deletions_v1._failure('credential_revoked', NULL, NULL);
            WHEN 'generation_outdated' THEN
                RETURN aap_identity_deletions_v1._failure('generation_outdated', NULL, NULL);
            WHEN 'token_expired' THEN
                RETURN aap_identity_deletions_v1._failure('token_expired', NULL, NULL);
            WHEN 'user_inactive' THEN
                RETURN aap_identity_deletions_v1._failure('user_inactive', NULL, NULL);
            WHEN 'invalid_account_role' THEN
                RETURN aap_identity_deletions_v1._failure('invalid_account_role', NULL, NULL);
            WHEN 'credential_busy' THEN
                RETURN aap_identity_deletions_v1._failure('credential_busy', NULL, NULL);
            WHEN 'account_busy' THEN
                RETURN aap_identity_deletions_v1._failure(
                    'account_busy', 409,
                    pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
                );
            ELSE
                RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
        END CASE;
    END IF;
    v_principal := v_actor_reply->'data';
    v_actor_role := v_principal->>'role';
    IF v_actor_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_identity_deletions_v1._failure(
            'manager_required', 403,
            pg_catalog.to_jsonb('当前账号已无管理权限'::text)
        );
    END IF;
    IF NOT v_target_found THEN
        RETURN aap_identity_deletions_v1._failure(
            'target_not_found', 404, pg_catalog.to_jsonb('用户不存在'::text)
        );
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-identity-deletions-operation/v1:' || v_operation_id::text,
            0
        )
    ) THEN
        RETURN aap_identity_deletions_v1._failure(
            'account_busy', 409,
            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    END IF;

    SELECT receipt.actor_id, receipt.operation, receipt.target_user_id,
           receipt.transfer_to_user_id, receipt.request_fingerprint,
           receipt.derivation_id, receipt.target_generation,
           receipt.target_is_active, receipt.result
    INTO v_receipt
    FROM public.identity_deletion_operation_receipts AS receipt
    WHERE receipt.operation_id = v_operation_id;
    v_receipt_found := FOUND;
    IF v_receipt_found THEN
        IF v_receipt.actor_id IS DISTINCT FROM v_actor_id
           OR v_receipt.operation IS DISTINCT FROM v_operation
           OR v_receipt.target_user_id IS DISTINCT FROM v_target_user_id
           OR v_receipt.transfer_to_user_id IS DISTINCT FROM v_transfer_to_user_id
           OR v_receipt.request_fingerprint IS DISTINCT FROM v_request_fingerprint
           OR v_receipt.derivation_id IS DISTINCT FROM v_derivation_id THEN
            RETURN aap_identity_deletions_v1._failure(
                'operation_conflict', 409, NULL
            );
        END IF;
        IF v_receipt.target_generation IS DISTINCT FROM v_target_generation
           OR v_receipt.target_is_active IS DISTINCT FROM v_target_active THEN
            RETURN aap_identity_deletions_v1._failure(
                'operation_conflict', 409, NULL
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-deletions-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', v_receipt.result,
            'replayed', true
        );
    END IF;

    IF v_actor_id = v_target_user_id THEN
        RETURN aap_identity_deletions_v1._failure(
            'self_delete', 400,
            pg_catalog.to_jsonb('不能删除自己'::text)
        );
    END IF;

    -- Legacy DELETE deliberately accepts an inactive target before checking
    -- target role/scope, recipient state, task work or Audit/metadata effects.
    IF NOT v_target_active THEN
        v_user_record := aap_identity_directory_v1._directory_user(
            v_target_user_id
        );
        IF v_user_record IS NULL
           OR (v_user_record->>'id')::uuid IS DISTINCT FROM v_target_user_id
           OR v_user_record->'is_active' IS DISTINCT FROM 'false'::jsonb THEN
            RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
        END IF;
        v_result := pg_catalog.jsonb_build_object('user', v_user_record);
        INSERT INTO public.identity_deletion_operation_receipts (
            operation_id, actor_id, operation, target_user_id,
            transfer_to_user_id, request_fingerprint, derivation_id,
            target_generation, target_is_active, result, audit_id
        ) VALUES (
            v_operation_id, v_actor_id, v_operation, v_target_user_id,
            v_transfer_to_user_id, v_request_fingerprint, v_derivation_id,
            v_target_generation, false, v_result, NULL
        );
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-deletions-native/v1',
            'ok', true,
            'operation', v_operation,
            'operation_id', v_operation_id,
            'principal', v_principal,
            'data', v_result,
            'replayed', false
        );
    END IF;

    BEGIN
        SELECT scope.has_projects, scope.all_projects_owned
        INTO STRICT v_scope_has_projects, v_scope_all_projects_owned
        FROM aap_workspace_v1.lock_identity_deletion_scope(
            v_actor_id, v_target_user_id
        ) AS scope;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_deletions_v1._failure(
            'project_busy', 409, v_project_busy_detail
        );
    WHEN no_data_found OR too_many_rows THEN
        RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
    END;
    IF v_scope_has_projects IS NULL OR v_scope_all_projects_owned IS NULL THEN
        RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
    END IF;

    IF v_actor_role = 'project_admin' THEN
        IF v_target_role IS DISTINCT FROM 'employee' THEN
            RETURN aap_identity_deletions_v1._failure(
                'delete_role_forbidden', 403,
                pg_catalog.to_jsonb('项目管理员仅能删除员工账号'::text)
            );
        END IF;
        IF NOT v_scope_all_projects_owned THEN
            RETURN aap_identity_deletions_v1._failure(
                'delete_scope_forbidden', 403,
                pg_catalog.to_jsonb('该用户跨多个项目，须由超级管理员处理'::text)
            );
        END IF;
    END IF;

    IF v_target_role = 'super_admin' THEN
        v_super_admin_status := aap_identity_v1.super_admin_status(
            v_target_user_id
        );
        IF pg_catalog.jsonb_typeof(v_super_admin_status) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_super_admin_status->'other_unreserved')
                IS DISTINCT FROM 'number'
           OR (v_super_admin_status->>'other_unreserved') !~ '^(0|[1-9][0-9]*)$' THEN
            RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
        END IF;
        v_other_unreserved := (v_super_admin_status->>'other_unreserved')::bigint;
        IF v_other_unreserved = 0 THEN
            RETURN aap_identity_deletions_v1._failure(
                'last_super_admin', 400,
                pg_catalog.to_jsonb('不能删除最后一名超级管理员'::text)
            );
        END IF;
    END IF;

    BEGIN
        SELECT work.pending_task_count, work.locked_task_count,
               work.sample_task_ids, work.pending_project_ids
        INTO STRICT v_pending_task_count, v_locked_task_count,
                    v_sample_task_ids, v_pending_project_ids
        FROM aap_labeling_v1.identity_deletion_work(v_target_user_id) AS work;
    EXCEPTION WHEN no_data_found OR too_many_rows THEN
        RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
    END;
    IF v_pending_task_count IS NULL OR v_pending_task_count < 0
       OR v_locked_task_count IS NULL OR v_locked_task_count < 0
       OR v_sample_task_ids IS NULL
       OR pg_catalog.cardinality(v_sample_task_ids) > 5
       OR pg_catalog.array_position(v_sample_task_ids, NULL::uuid) IS NOT NULL
       OR v_pending_project_ids IS NULL
       OR pg_catalog.array_position(v_pending_project_ids, NULL::uuid) IS NOT NULL THEN
        RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
    END IF;

    IF v_transfer_to_user_id IS NULL
       AND (v_pending_task_count > 0 OR v_locked_task_count > 0) THEN
        RETURN aap_identity_deletions_v1._failure(
            'has_pending_tasks', 409,
            pg_catalog.jsonb_build_object(
                'reason', 'has_pending_tasks',
                'pending_task_count', v_pending_task_count,
                'locked_task_count', v_locked_task_count,
                'sample_task_ids', pg_catalog.to_jsonb(v_sample_task_ids),
                'message', pg_catalog.format(
                    '该用户当前有 %s 个未完成任务、%s 个锁定任务，需先转交',
                    v_pending_task_count, v_locked_task_count
                )
            )
        );
    END IF;

    IF v_transfer_to_user_id IS NOT NULL THEN
        SELECT account.is_active, account.role
        INTO v_receiver_active, v_receiver_role
        FROM public.users AS account
        WHERE account.id = v_transfer_to_user_id;
        v_receiver_found := FOUND;
        IF NOT v_receiver_found OR NOT v_receiver_active THEN
            RETURN aap_identity_deletions_v1._failure(
                'receiver_unavailable', 400,
                pg_catalog.to_jsonb('转交目标用户不存在或已禁用'::text)
            );
        END IF;
        IF v_transfer_to_user_id = v_target_user_id THEN
            RETURN aap_identity_deletions_v1._failure(
                'receiver_is_target', 400,
                pg_catalog.to_jsonb('转交目标不能与被删用户相同'::text)
            );
        END IF;
        IF v_receiver_role NOT IN ('employee', 'project_admin') THEN
            RETURN aap_identity_deletions_v1._failure(
                'receiver_role_invalid', 400,
                pg_catalog.to_jsonb('转交目标角色不合法'::text)
            );
        END IF;

        IF v_actor_role = 'project_admin'
           AND v_transfer_to_user_id <> v_actor_id THEN
            BEGIN
                SELECT aap_workspace_v1.identity_deletion_transfer_covered(
                    v_transfer_to_user_id, v_pending_project_ids
                )
                INTO STRICT v_receiver_covered;
            EXCEPTION WHEN no_data_found OR too_many_rows THEN
                RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
            END;
            IF v_receiver_covered IS NULL THEN
                RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
            END IF;
            IF NOT v_receiver_covered THEN
                RETURN aap_identity_deletions_v1._failure(
                    'receiver_scope_forbidden', 403,
                    pg_catalog.to_jsonb('转交目标必须是待转交任务所在项目的成员'::text)
                );
            END IF;
        END IF;

        BEGIN
            v_transferred_count := aap_labeling_v1.transfer_identity_deletion_work(
                v_target_user_id, v_transfer_to_user_id
            );
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_identity_deletions_v1._failure(
                'account_busy', 409,
                pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
            );
        END;
        IF v_transferred_count IS NULL OR v_transferred_count < 0 THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity deletion Labeling transfer result';
        END IF;
    END IF;

    UPDATE public.users AS account
    SET is_active = false,
        status = 'offline',
        disabled_kind = 'deleted',
        disabled_at = pg_catalog.clock_timestamp(),
        disabled_by = v_actor_id,
        disabled_reason = '管理员删除账号'
    WHERE account.id = v_target_user_id;

    v_audit_id := aap_audit_v1.record_identity_deletion_event(
        pg_catalog.jsonb_build_object(
            'actor_id', v_principal->'actor_id',
            'email', v_principal->'email',
            'role', v_principal->'role'
        ),
        v_target_user_id,
        pg_catalog.jsonb_build_object(
            'email', v_target_email,
            'role', v_target_role,
            'transferred_to', v_transfer_to_user_id,
            'transferred_count', v_transferred_count,
            'released_locks', CASE WHEN v_transfer_to_user_id IS NOT NULL
                                   THEN v_locked_task_count ELSE 0::bigint END
        ),
        v_context
    );
    IF v_audit_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity deletion Audit result';
    END IF;

    v_user_record := aap_identity_directory_v1._directory_user(v_target_user_id);
    IF v_user_record IS NULL
       OR (v_user_record->>'id')::uuid IS DISTINCT FROM v_target_user_id
       OR v_user_record->'is_active' IS DISTINCT FROM 'false'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid frozen Identity deletion user result';
    END IF;
    v_result := pg_catalog.jsonb_build_object('user', v_user_record);
    INSERT INTO public.identity_deletion_operation_receipts (
        operation_id, actor_id, operation, target_user_id,
        transfer_to_user_id, request_fingerprint, derivation_id,
        target_generation, target_is_active, result, audit_id
    ) VALUES (
        v_operation_id, v_actor_id, v_operation, v_target_user_id,
        v_transfer_to_user_id, v_request_fingerprint, v_derivation_id,
        v_target_generation, false, v_result, v_audit_id
    );
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-deletions-native/v1',
        'ok', true,
        'operation', v_operation,
        'operation_id', v_operation_id,
        'principal', v_principal,
        'data', v_result,
        'replayed', false
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_identity_deletions_v1._failure(
            'account_busy', 409,
            pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
        );
    WHEN OTHERS THEN
        RETURN aap_identity_deletions_v1._failure('unavailable', 503, NULL);
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_identity_offboarding_v1.apply_offboarding_command(
    p_command jsonb
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
    v_operation_id uuid;
    v_actor jsonb;
    v_actor_id uuid;
    v_expected_key_hash text;
    v_request_fingerprint text;
    v_derivation_id text;
    v_context jsonb;
    v_payload jsonb;
    v_target_user_id uuid;
    v_expected_generation bigint;
    v_expected_active boolean;
    v_mode text;
    v_normalized_reason text;
    v_reason_present boolean;
    v_scope_basis jsonb;
    v_scope_project_ids uuid[] := ARRAY[]::uuid[];
    v_owner_plan jsonb;
    v_labeling_plan jsonb;
    v_transfers jsonb;
    v_unresolved jsonb;
    v_owner_plan_item jsonb;
    v_labeling_plan_item jsonb;
    v_transfer_item jsonb;
    v_json_item jsonb;
    v_item_id uuid;
    v_previous_id uuid;
    v_owner_project_ids uuid[] := ARRAY[]::uuid[];
    v_actual_owner_ids uuid[] := ARRAY[]::uuid[];
    v_receiver_ids uuid[] := ARRAY[]::uuid[];
    v_locked_project_ids uuid[] := ARRAY[]::uuid[];
    v_actor_reply jsonb;
    v_principal jsonb;
    v_actor_role text;
    v_target_role text;
    v_target_disabled_kind text;
    v_target_active boolean;
    v_target_generation bigint;
    v_target_found boolean := false;
    v_current_target_role text;
    v_binding_text text;
    v_binding jsonb;
    v_binding_receiver_ids uuid[] := ARRAY[]::uuid[];
    v_binding_project_ids uuid[] := ARRAY[]::uuid[];
    v_scope_reply record;
    v_super_admin_status jsonb;
    v_other_unreserved bigint;
    v_owner_result jsonb;
    v_labeling_result jsonb;
    v_label_result_item jsonb;
    v_retirement jsonb;
    v_new_generation bigint;
    v_revoked_key_ids jsonb;
    v_released_lock_count bigint;
    v_audit_id bigint;
    v_user_record jsonb;
    v_result jsonb;
    v_expected_owner_count bigint;
    v_actual_owner_count bigint;
    v_expected_label_count bigint;
    v_actual_label_count bigint;
    v_match_count bigint;
    v_count bigint;
    v_index integer := 0;
    v_rows integer;
    v_role text;
    v_project_id uuid;
    v_receiver_id uuid;
    v_context_actor jsonb;
BEGIN
    IF NOT aap_identity_offboarding_v1._has_exact_keys(
        p_command,
        ARRAY[
            'schema', 'operation_id', 'operation', 'actor', 'expected_key_hash',
            'request_fingerprint', 'derivation_id', 'audit_context', 'phase', 'payload'
        ]
    ) OR p_command->>'schema' IS DISTINCT FROM
            'aap-identity-offboarding-native/v1'
       OR pg_catalog.jsonb_typeof(p_command->'phase') IS DISTINCT FROM 'string'
       OR p_command->>'phase' IS DISTINCT FROM 'apply'
       OR pg_catalog.jsonb_typeof(p_command->'operation') IS DISTINCT FROM 'string'
       OR p_command->>'operation' IS DISTINCT FROM 'commit_offboarding'
       OR pg_catalog.jsonb_typeof(p_command->'operation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'operation_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR COALESCE(
           pg_catalog.jsonb_typeof(p_command->'expected_key_hash')
               NOT IN ('string', 'null'),
           true
       )
       OR (pg_catalog.jsonb_typeof(p_command->'expected_key_hash') = 'string'
           AND pg_catalog.octet_length(p_command->>'expected_key_hash') NOT BETWEEN 1 AND 255)
       OR pg_catalog.jsonb_typeof(p_command->'request_fingerprint') IS DISTINCT FROM 'string'
       OR (p_command->>'request_fingerprint') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_command->'derivation_id') IS DISTINCT FROM 'string'
       OR (p_command->>'derivation_id') COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR NOT aap_identity_offboarding_v1._has_exact_keys(
           p_command->'actor',
           ARRAY[
               'actor_id', 'name', 'email', 'role', 'is_active',
               'credential_type', 'credential_id', 'generation', 'scopes',
               'expires_at'
           ]
       )
       OR pg_catalog.jsonb_typeof(p_command #> '{actor,actor_id}') IS DISTINCT FROM 'string'
       OR (p_command #>> '{actor,actor_id}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR NOT aap_identity_offboarding_v1._has_exact_keys(
           p_command->'audit_context',
           ARRAY['method', 'path', 'ip', 'request_id']
       )
       OR NOT aap_audit_v1.valid_request_context(p_command->'audit_context') THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_operation_id := (p_command->>'operation_id')::uuid;
    v_actor := p_command->'actor';
    v_actor_id := (v_actor->>'actor_id')::uuid;
    v_expected_key_hash := p_command->>'expected_key_hash';
    v_request_fingerprint := p_command->>'request_fingerprint';
    v_derivation_id := p_command->>'derivation_id';
    v_context := p_command->'audit_context';
    v_payload := p_command->'payload';

    IF NOT aap_identity_offboarding_v1._has_exact_keys(
        v_payload,
        ARRAY[
            'transaction_id', 'target_user_id', 'expected_target_generation',
            'expected_target_is_active', 'mode', 'normalized_reason',
            'reason_present', 'owner_plan', 'labeling_plan', 'transfers',
            'unresolved', 'scope_basis'
        ]
    )
       OR pg_catalog.jsonb_typeof(v_payload->'transaction_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_payload->'target_user_id') IS DISTINCT FROM 'string'
       OR (v_payload->>'target_user_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(v_payload->'expected_target_generation') IS DISTINCT FROM 'number'
       OR (v_payload->>'expected_target_generation') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(v_payload->'expected_target_is_active') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_payload->'mode') IS DISTINCT FROM 'string'
       OR v_payload->>'mode' NOT IN ('handoff', 'emergency_suspend')
       OR COALESCE(
           pg_catalog.jsonb_typeof(v_payload->'normalized_reason')
               NOT IN ('string', 'null'),
           true
       )
       OR (pg_catalog.jsonb_typeof(v_payload->'normalized_reason') = 'string'
           AND (
               pg_catalog.char_length(v_payload->>'normalized_reason') > 500
               OR v_payload->>'normalized_reason' = ''
           ))
       OR pg_catalog.jsonb_typeof(v_payload->'reason_present') IS DISTINCT FROM 'boolean'
       OR ((v_payload->'reason_present' = 'true'::jsonb)
           IS DISTINCT FROM (pg_catalog.jsonb_typeof(v_payload->'normalized_reason') = 'string'))
       OR pg_catalog.jsonb_typeof(v_payload->'owner_plan') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_payload->'labeling_plan') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_payload->'transfers') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_payload->'unresolved') IS DISTINCT FROM 'array'
       OR NOT aap_identity_offboarding_v1._has_exact_keys(
           v_payload->'scope_basis',
           ARRAY['target_role', 'target_was_active', 'project_ids']
       )
       OR pg_catalog.jsonb_typeof(v_payload #> '{scope_basis,target_role}') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_payload #> '{scope_basis,target_was_active}') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_payload #> '{scope_basis,project_ids}') IS DISTINCT FROM 'array' THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_target_user_id := (v_payload->>'target_user_id')::uuid;
    v_mode := v_payload->>'mode';
    v_reason_present := (v_payload->>'reason_present')::boolean;
    v_normalized_reason := v_payload->>'normalized_reason';
    v_scope_basis := v_payload->'scope_basis';
    v_owner_plan := v_payload->'owner_plan';
    v_labeling_plan := v_payload->'labeling_plan';
    v_transfers := v_payload->'transfers';
    v_unresolved := v_payload->'unresolved';
    BEGIN
        v_expected_generation := (v_payload->>'expected_target_generation')::bigint;
    EXCEPTION WHEN numeric_value_out_of_range THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END;
    v_expected_active := (v_payload->>'expected_target_is_active')::boolean;
    IF v_expected_active IS DISTINCT FROM
       ((v_scope_basis->'target_was_active') = 'true'::jsonb) THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_previous_id := NULL;
    FOR v_json_item IN
        SELECT item.value
        FROM pg_catalog.jsonb_array_elements(v_scope_basis->'project_ids') AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
           OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
        v_item_id := (v_json_item #>> '{}')::uuid;
        IF v_previous_id IS NOT NULL AND v_item_id <= v_previous_id THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
        v_scope_project_ids := pg_catalog.array_append(v_scope_project_ids, v_item_id);
        v_previous_id := v_item_id;
    END LOOP;

    v_binding_text := NULLIF(
        pg_catalog.current_setting('aap.identity_offboarding_prepare', true), ''
    );
    IF v_binding_text IS NULL THEN
        RETURN aap_identity_offboarding_v1._failure('operation_conflict', 409, NULL);
    END IF;
    BEGIN
        v_binding := v_binding_text::jsonb;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_identity_offboarding_v1._failure('operation_conflict', 409, NULL);
    END;
    IF NOT aap_identity_offboarding_v1._has_exact_keys(
        v_binding,
        ARRAY[
            'transaction_id', 'operation_id', 'operation', 'actor_id',
            'target_user_id', 'target_role', 'request_fingerprint',
            'derivation_id', 'target_generation', 'target_is_active',
            'receiver_ids', 'locked_project_ids'
        ]
    )
       OR v_binding->>'transaction_id' IS DISTINCT FROM pg_catalog.txid_current()::text
       OR v_binding->>'operation_id' IS DISTINCT FROM v_operation_id::text
       OR v_binding->>'operation' IS DISTINCT FROM 'commit_offboarding'
       OR v_binding->>'actor_id' IS DISTINCT FROM v_actor_id::text
       OR v_binding->>'target_user_id' IS DISTINCT FROM v_target_user_id::text
       OR v_binding->>'request_fingerprint' IS DISTINCT FROM v_request_fingerprint
       OR v_binding->>'derivation_id' IS DISTINCT FROM v_derivation_id
       OR v_binding->'target_generation' IS DISTINCT FROM
          v_payload->'expected_target_generation'
       OR v_binding->'target_is_active' IS DISTINCT FROM
          v_payload->'expected_target_is_active'
       OR v_binding->>'target_role' IS DISTINCT FROM
          v_scope_basis->>'target_role'
       OR pg_catalog.jsonb_typeof(v_binding->'receiver_ids') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_binding->'locked_project_ids') IS DISTINCT FROM 'array' THEN
        RETURN aap_identity_offboarding_v1._failure('operation_conflict', 409, NULL);
    END IF;

    FOR v_json_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_binding->'receiver_ids') AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
           OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_offboarding_v1._failure('operation_conflict', 409, NULL);
        END IF;
        v_receiver_ids := pg_catalog.array_append(
            v_receiver_ids, (v_json_item #>> '{}')::uuid
        );
    END LOOP;
    FOR v_json_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_binding->'locked_project_ids') AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
           OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_offboarding_v1._failure('operation_conflict', 409, NULL);
        END IF;
        v_locked_project_ids := pg_catalog.array_append(
            v_locked_project_ids, (v_json_item #>> '{}')::uuid
        );
    END LOOP;

    FOR v_owner_plan_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_owner_plan) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_owner_plan_item) IS DISTINCT FROM 'object'
           OR NOT aap_identity_offboarding_v1._has_exact_keys(
               v_owner_plan_item,
               ARRAY['project_id', 'expected_owner_id', 'receiver_id']
           )
           OR pg_catalog.jsonb_typeof(v_owner_plan_item->'project_id') IS DISTINCT FROM 'string'
           OR (v_owner_plan_item->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_owner_plan_item->'expected_owner_id') IS DISTINCT FROM 'string'
           OR (v_owner_plan_item->>'expected_owner_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_owner_plan_item->'receiver_id') IS DISTINCT FROM 'string'
           OR (v_owner_plan_item->>'receiver_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
        v_project_id := (v_owner_plan_item->>'project_id')::uuid;
        v_receiver_id := (v_owner_plan_item->>'receiver_id')::uuid;
        IF (v_owner_plan_item->>'expected_owner_id')::uuid IS DISTINCT FROM v_target_user_id
           OR v_receiver_id = v_target_user_id
           OR NOT (v_project_id = ANY(v_scope_project_ids))
           OR NOT (v_project_id = ANY(v_locked_project_ids))
           OR NOT (v_receiver_id = ANY(v_receiver_ids)) THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
    END LOOP;

    FOR v_labeling_plan_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_labeling_plan) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_labeling_plan_item) IS DISTINCT FROM 'object'
           OR NOT aap_identity_offboarding_v1._has_exact_keys(
               v_labeling_plan_item,
               ARRAY['project_id', 'role', 'receiver_id', 'batch_ids', 'task_ids']
           )
           OR pg_catalog.jsonb_typeof(v_labeling_plan_item->'project_id') IS DISTINCT FROM 'string'
           OR (v_labeling_plan_item->>'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_labeling_plan_item->'role') IS DISTINCT FROM 'string'
           OR v_labeling_plan_item->>'role' NOT IN ('annotator', 'reviewer')
           OR pg_catalog.jsonb_typeof(v_labeling_plan_item->'receiver_id') IS DISTINCT FROM 'string'
           OR (v_labeling_plan_item->>'receiver_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_labeling_plan_item->'batch_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(v_labeling_plan_item->'task_ids') IS DISTINCT FROM 'array' THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
        v_project_id := (v_labeling_plan_item->>'project_id')::uuid;
        v_receiver_id := (v_labeling_plan_item->>'receiver_id')::uuid;
        IF NOT (v_project_id = ANY(v_scope_project_ids))
           OR NOT (v_project_id = ANY(v_locked_project_ids))
           OR v_receiver_id = v_target_user_id
           OR NOT (v_receiver_id = ANY(v_receiver_ids)) THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;
    END LOOP;

    IF v_mode = 'emergency_suspend'
       AND (
           pg_catalog.jsonb_array_length(v_payload->'owner_plan') <> 0
           OR pg_catalog.jsonb_array_length(v_payload->'labeling_plan') <> 0
           OR pg_catalog.jsonb_array_length(v_transfers) <> 0
       ) THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END IF;
    IF v_mode = 'handoff'
       AND pg_catalog.jsonb_array_length(v_payload->'unresolved') <> 0 THEN
        RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
    END IF;

    v_actor_reply := aap_identity_accounts_v1._lock_current_actor(
        v_actor, v_expected_key_hash, 'write_nowait'
    );
    IF v_actor_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_actor_reply #>> '{error,code}'
            WHEN 'user_inactive' THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'manager_required', 403,
                    pg_catalog.to_jsonb('当前账号已无管理权限'::text)
                );
            WHEN 'account_busy' THEN
                RETURN aap_identity_offboarding_v1._failure(
                    'account_busy', 409,
                    pg_catalog.to_jsonb('账号正在更新，请刷新后重试'::text)
                );
            WHEN 'invalid_request' THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
            WHEN 'credential_invalid' THEN
                RETURN aap_identity_offboarding_v1._failure('credential_invalid', NULL, NULL);
            WHEN 'credential_revoked' THEN
                RETURN aap_identity_offboarding_v1._failure('credential_revoked', NULL, NULL);
            WHEN 'generation_outdated' THEN
                RETURN aap_identity_offboarding_v1._failure('generation_outdated', NULL, NULL);
            WHEN 'token_expired' THEN
                RETURN aap_identity_offboarding_v1._failure('token_expired', NULL, NULL);
            WHEN 'invalid_account_role' THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_account_role', NULL, NULL);
            WHEN 'credential_busy' THEN
                RETURN aap_identity_offboarding_v1._failure('credential_busy', NULL, NULL);
            ELSE
                RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
        END CASE;
    END IF;
    v_principal := v_actor_reply->'data';
    v_actor_role := v_principal->>'role';
    IF v_actor_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_identity_offboarding_v1._failure(
            'manager_required', 403,
            pg_catalog.to_jsonb('当前账号已无管理权限'::text)
        );
    END IF;

    SELECT account.role, account.is_active, account.credential_generation,
           account.disabled_kind
    INTO v_current_target_role, v_target_active, v_target_generation,
         v_target_disabled_kind
    FROM public.users AS account
    WHERE account.id = v_target_user_id;
    v_target_found := FOUND;
    IF NOT v_target_found
       OR v_target_generation IS DISTINCT FROM v_expected_generation
       OR v_target_active IS DISTINCT FROM v_expected_active
       OR v_current_target_role IS DISTINCT FROM v_scope_basis->>'target_role' THEN
        RETURN aap_identity_offboarding_v1._failure('operation_conflict', 409, NULL);
    END IF;
    IF v_actor_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_identity_offboarding_v1._failure(
            'manager_required', 403,
            pg_catalog.to_jsonb('当前账号已无管理权限'::text)
        );
    END IF;
    IF v_actor_id = v_target_user_id THEN
        RETURN aap_identity_offboarding_v1._failure(
            'self_offboard', 400,
            pg_catalog.to_jsonb('不能交接自己'::text)
        );
    END IF;
    IF v_target_active IS TRUE
       AND v_current_target_role = 'super_admin' THEN
        v_super_admin_status := aap_identity_v1.super_admin_status(
            v_target_user_id
        );
        IF pg_catalog.jsonb_typeof(v_super_admin_status) IS DISTINCT FROM 'object'
           OR v_super_admin_status->>'schema' IS DISTINCT FROM
              'aap-identity-super-admin-status/v1'
           OR pg_catalog.jsonb_typeof(v_super_admin_status->'other_unreserved') IS DISTINCT FROM 'number'
           OR (v_super_admin_status->>'other_unreserved') !~ '^(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity offboarding super-admin status';
        END IF;
        BEGIN
            v_other_unreserved :=
                (v_super_admin_status->>'other_unreserved')::bigint;
        EXCEPTION WHEN numeric_value_out_of_range THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity offboarding super-admin count';
        END;
        IF v_other_unreserved = 0 THEN
            RETURN aap_identity_offboarding_v1._failure(
                'last_super_admin', 400,
                pg_catalog.to_jsonb('不能停用最后一名超级管理员'::text)
            );
        END IF;
    END IF;
    IF v_actor_role = 'project_admin' THEN
        IF v_current_target_role IS DISTINCT FROM 'employee' THEN
            RETURN aap_identity_offboarding_v1._failure(
                'target_role_forbidden', 403,
                pg_catalog.to_jsonb(
                    '项目管理员仅能处理标注员/审核员账号'::text
                )
            );
        END IF;
        IF pg_catalog.cardinality(v_scope_project_ids) = 0
           AND v_target_active IS NOT TRUE THEN
            RETURN aap_identity_offboarding_v1._failure(
                'inactive_unmanaged', 403,
                pg_catalog.to_jsonb(
                    '该用户已停用且不在你管理的项目内，须由超级管理员处理'::text
                )
            );
        END IF;
        BEGIN
            SELECT scope.projects_exist, scope.all_projects_owned
            INTO STRICT v_scope_reply
            FROM aap_workspace_v1.identity_offboarding_replay_scope(
                v_actor_id, v_scope_project_ids
            ) AS scope;
        EXCEPTION WHEN no_data_found OR too_many_rows THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity offboarding scope reply';
        END;
        IF v_scope_reply.projects_exist IS DISTINCT FROM true
           OR v_scope_reply.all_projects_owned IS DISTINCT FROM true THEN
            RETURN aap_identity_offboarding_v1._failure(
                'scope_forbidden', 403,
                pg_catalog.to_jsonb('该用户存在不在你管理范围内的项目'::text)
            );
        END IF;
    END IF;

    IF NOT v_target_active
       AND v_target_disabled_kind IN ('deleted', 'historical_unknown') THEN
        RETURN aap_identity_offboarding_v1._failure(
            'historical_target', 409,
            pg_catalog.to_jsonb('历史未知或已删除账号不可再次交接'::text)
        );
    END IF;
    IF NOT v_target_active
       AND (
           v_target_disabled_kind IS NULL
           OR v_target_disabled_kind NOT IN ('suspended', 'emergency_suspended')
       ) THEN
        RETURN aap_identity_offboarding_v1._failure(
            'state_forbidden', 409,
            pg_catalog.to_jsonb('账号当前状态不可交接'::text)
        );
    END IF;

    v_owner_plan := v_payload->'owner_plan';
    v_labeling_plan := v_payload->'labeling_plan';
    v_transfers := v_payload->'transfers';
    v_unresolved := v_payload->'unresolved';
    SELECT pg_catalog.count(*)::bigint INTO v_expected_owner_count
    FROM pg_catalog.jsonb_array_elements(v_owner_plan) AS item(value);
    SELECT pg_catalog.count(*)::bigint INTO v_expected_label_count
    FROM pg_catalog.jsonb_array_elements(v_labeling_plan) AS item(value);

    IF v_mode = 'handoff' THEN
        SELECT pg_catalog.count(*) FILTER (
                   WHERE item.value->>'role' = 'owner'
               )::bigint,
               pg_catalog.count(*) FILTER (
                   WHERE item.value->>'role' IN ('annotator', 'reviewer')
               )::bigint
        INTO v_actual_owner_count, v_actual_label_count
        FROM pg_catalog.jsonb_array_elements(v_transfers) AS item(value);
        IF v_expected_owner_count <> v_actual_owner_count
           OR v_expected_label_count <> v_actual_label_count THEN
            RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
        END IF;

        FOR v_owner_plan_item IN
            SELECT item.value FROM pg_catalog.jsonb_array_elements(v_owner_plan) AS item(value)
        LOOP
            SELECT pg_catalog.count(*)::bigint INTO v_match_count
            FROM pg_catalog.jsonb_array_elements(v_transfers) AS item(value)
            WHERE item.value->>'role' = 'owner'
              AND item.value->>'project_id' = v_owner_plan_item->>'project_id'
              AND item.value->>'receiver_id' = v_owner_plan_item->>'receiver_id'
              AND item.value->'batch_ids' = '[]'::jsonb
              AND item.value->>'task_count' = '0'
              AND item.value->>'lock_count' = '0';
            IF v_match_count <> 1 THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
            END IF;
        END LOOP;
        FOR v_labeling_plan_item IN
            SELECT item.value FROM pg_catalog.jsonb_array_elements(v_labeling_plan) AS item(value)
        LOOP
            SELECT pg_catalog.count(*)::bigint INTO v_match_count
            FROM pg_catalog.jsonb_array_elements(v_transfers) AS item(value)
            WHERE item.value->>'role' = v_labeling_plan_item->>'role'
              AND item.value->>'project_id' = v_labeling_plan_item->>'project_id'
              AND item.value->>'receiver_id' = v_labeling_plan_item->>'receiver_id'
              AND item.value->'batch_ids' = v_labeling_plan_item->'batch_ids'
              AND item.value->>'task_count' =
                  pg_catalog.jsonb_array_length(v_labeling_plan_item->'task_ids')::text;
            IF v_match_count <> 1 THEN
                RETURN aap_identity_offboarding_v1._failure('invalid_request', 400, NULL);
            END IF;
        END LOOP;
    END IF;

    v_owner_result := pg_catalog.jsonb_build_object(
        'changed_project_ids', '[]'::jsonb,
        'changed_count', 0
    );
    IF pg_catalog.jsonb_array_length(v_owner_plan) > 0 THEN
        v_owner_result := aap_workspace_v1.transfer_identity_offboarding_owners(
            v_target_user_id, v_owner_plan
        );
    END IF;
    IF NOT aap_identity_offboarding_v1._has_exact_keys(
        v_owner_result, ARRAY['changed_project_ids', 'changed_count']
    )
       OR pg_catalog.jsonb_typeof(v_owner_result->'changed_project_ids') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_owner_result->'changed_count') IS DISTINCT FROM 'number'
       OR (v_owner_result->>'changed_count') !~ '^(0|[1-9][0-9]*)$' THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding Workspace transfer reply';
    END IF;
    v_previous_id := NULL;
    FOR v_json_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(
            v_owner_result->'changed_project_ids'
        ) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
           OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity offboarding Workspace project IDs';
        END IF;
        v_actual_owner_ids := pg_catalog.array_append(
            v_actual_owner_ids, (v_json_item #>> '{}')::uuid
        );
    END LOOP;
    FOR v_owner_plan_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_owner_plan) AS item(value)
    LOOP
        v_owner_project_ids := pg_catalog.array_append(
            v_owner_project_ids, (v_owner_plan_item->>'project_id')::uuid
        );
    END LOOP;
    IF v_actual_owner_ids IS DISTINCT FROM v_owner_project_ids
       OR (v_owner_result->>'changed_count')::bigint IS DISTINCT FROM
          pg_catalog.cardinality(v_owner_project_ids)::bigint THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'Identity offboarding Workspace transfer result mismatch';
    END IF;

    v_labeling_result := '[]'::jsonb;
    IF pg_catalog.jsonb_array_length(v_labeling_plan) > 0 THEN
        v_labeling_result := aap_labeling_v1.transfer_identity_offboarding_work(
            v_target_user_id, v_labeling_plan
        );
    END IF;
    IF pg_catalog.jsonb_typeof(v_labeling_result) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(v_labeling_result) <>
          pg_catalog.jsonb_array_length(v_labeling_plan) THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding Labeling transfer reply';
    END IF;
    v_index := 0;
    FOR v_labeling_plan_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_labeling_plan) AS item(value)
    LOOP
        v_label_result_item := v_labeling_result->v_index;
        IF NOT aap_identity_offboarding_v1._has_exact_keys(
            v_label_result_item,
            ARRAY['project_id', 'role', 'batch_ids', 'task_ids', 'task_count']
        )
           OR v_label_result_item->>'project_id' IS DISTINCT FROM
              v_labeling_plan_item->>'project_id'
           OR v_label_result_item->>'role' IS DISTINCT FROM
              v_labeling_plan_item->>'role'
           OR v_label_result_item->'batch_ids' IS DISTINCT FROM
              v_labeling_plan_item->'batch_ids'
           OR v_label_result_item->'task_ids' IS DISTINCT FROM
              v_labeling_plan_item->'task_ids'
           OR pg_catalog.jsonb_typeof(v_label_result_item->'task_count') IS DISTINCT FROM 'number'
           OR (v_label_result_item->>'task_count') !~ '^(0|[1-9][0-9]*)$'
           OR (v_label_result_item->>'task_count')::bigint IS DISTINCT FROM
              pg_catalog.jsonb_array_length(v_labeling_plan_item->'task_ids')::bigint THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Identity offboarding Labeling transfer result mismatch';
        END IF;
        v_index := v_index + 1;
    END LOOP;

    UPDATE public.users AS account
    SET is_active = false,
        status = 'offline',
        disabled_kind = CASE
            WHEN v_mode = 'handoff' THEN 'suspended'
            ELSE 'emergency_suspended'
        END,
        disabled_at = pg_catalog.clock_timestamp(),
        disabled_by = v_actor_id,
        disabled_reason = v_normalized_reason
    WHERE account.id = v_target_user_id
      AND account.credential_generation = v_expected_generation
      AND account.is_active = v_expected_active;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'Identity offboarding target update did not affect one row';
    END IF;

    v_retirement := aap_identity_v1.retire_account_credentials(v_target_user_id);
    IF pg_catalog.jsonb_typeof(v_retirement) IS DISTINCT FROM 'object'
       OR v_retirement->>'schema' IS DISTINCT FROM 'aap-identity-retirement/v1'
       OR pg_catalog.jsonb_typeof(v_retirement->'generation') IS DISTINCT FROM 'number'
       OR (v_retirement->>'generation') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(v_retirement->'revoked_key_ids') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding credential retirement reply';
    END IF;
    BEGIN
        v_new_generation := (v_retirement->>'generation')::bigint;
    EXCEPTION WHEN numeric_value_out_of_range THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding credential generation';
    END;
    IF v_new_generation IS DISTINCT FROM v_expected_generation + 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'Identity offboarding credential generation mismatch';
    END IF;
    v_revoked_key_ids := v_retirement->'revoked_key_ids';
    FOR v_json_item IN
        SELECT item.value FROM pg_catalog.jsonb_array_elements(v_revoked_key_ids) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_json_item) IS DISTINCT FROM 'string'
           OR (v_json_item #>> '{}') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid Identity offboarding revoked key IDs';
        END IF;
    END LOOP;

    v_released_lock_count := aap_labeling_v1.release_identity_offboarding_task_locks(
        v_target_user_id
    );
    IF v_released_lock_count IS NULL OR v_released_lock_count < 0 THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding task-lock release reply';
    END IF;

    v_context_actor := pg_catalog.jsonb_build_object(
        'actor_id', v_principal->'actor_id',
        'email', v_principal->'email',
        'role', v_principal->'role'
    );
    v_audit_id := aap_audit_v1.record_identity_offboarding_event(
        v_context_actor,
        v_target_user_id,
        pg_catalog.jsonb_build_object(
            'mode', v_mode,
            'reason_present', v_reason_present,
            'transfers', v_transfers,
            'unresolved', v_unresolved,
            'revoked_api_key_count', pg_catalog.jsonb_array_length(v_revoked_key_ids)
        ),
        v_context
    );
    IF v_audit_id IS NULL OR v_audit_id <= 0 THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding Audit ID';
    END IF;

    v_user_record := aap_identity_directory_v1._directory_user(v_target_user_id);
    IF v_user_record IS NULL
       OR pg_catalog.jsonb_typeof(v_user_record) IS DISTINCT FROM 'object'
       OR (v_user_record->>'id')::uuid IS DISTINCT FROM v_target_user_id
       OR v_user_record->'is_active' IS DISTINCT FROM 'false'::jsonb
       OR v_user_record->>'status' IS DISTINCT FROM 'offline'
       OR v_user_record->>'disabled_kind' IS DISTINCT FROM
          (CASE WHEN v_mode = 'handoff' THEN 'suspended' ELSE 'emergency_suspended' END)
       OR v_user_record->'disabled_reason' IS DISTINCT FROM
          COALESCE(pg_catalog.to_jsonb(v_normalized_reason), 'null'::jsonb) THEN
        RAISE EXCEPTION USING ERRCODE = 'XX000',
            MESSAGE = 'invalid Identity offboarding account reply';
    END IF;
    v_result := pg_catalog.jsonb_build_object(
        'user', v_user_record,
        'status', 'suspended',
        'mode', v_mode,
        'transfers', v_transfers,
        'unresolved', v_unresolved,
        'revoked_api_key_ids', v_revoked_key_ids,
        'audit_id', v_audit_id
    );

    INSERT INTO public.identity_offboarding_operation_receipts (
        operation_id, actor_id, operation, target_user_id,
        request_fingerprint, derivation_id, target_generation,
        target_is_active, result, scope_basis, audit_id
    ) VALUES (
        v_operation_id, v_actor_id, 'commit_offboarding', v_target_user_id,
        v_request_fingerprint, v_derivation_id, v_new_generation,
        false, v_result, v_scope_basis, v_audit_id
    );

    PERFORM pg_catalog.set_config('aap.identity_offboarding_prepare', '', true);

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-offboarding-native/v1',
        'ok', true,
        'operation', 'commit_offboarding',
        'operation_id', v_operation_id,
        'principal', v_principal,
        'phase', 'apply',
        'data', pg_catalog.jsonb_build_object('result', v_result),
        'replayed', false
    );
EXCEPTION
    WHEN OTHERS THEN
        RETURN aap_identity_offboarding_v1._failure('unavailable', 503, NULL);
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_identity_v1.create_provisioning_account(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_field_count integer;
    v_mode text;
    v_account_id uuid;
    v_email text;
    v_name text;
    v_password_hash text;
    v_now timestamptz;
    v_request_id text;
    v_audit_id bigint;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_field_count <> 8
       OR NOT (p_request ?& ARRAY[
           'schema', 'mode', 'account_id', 'email', 'name', 'password_hash',
           'now', 'request_id'
       ])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
           WHERE field.name NOT IN (
               'schema', 'mode', 'account_id', 'email', 'name', 'password_hash',
               'now', 'request_id'
           )
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    IF pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' IS DISTINCT FROM 'aap-identity-provisioning/v1'
       OR pg_catalog.jsonb_typeof(p_request->'mode') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'account_id') IS DISTINCT FROM 'string'
       OR (p_request->>'account_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'name') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'password_hash') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'now') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(p_request->'request_id') NOT IN ('string', 'null') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    v_mode := p_request->>'mode';
    v_email := p_request->>'email';
    v_name := p_request->>'name';
    v_password_hash := p_request->>'password_hash';
    v_request_id := p_request->>'request_id';
    BEGIN
        v_account_id := (p_request->>'account_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END;

    IF v_mode = 'bootstrap_admin' THEN
        IF pg_catalog.jsonb_typeof(p_request->'now') IS DISTINCT FROM 'string'
           OR (p_request->>'now') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
        END IF;
        BEGIN
            v_now := (p_request->>'now')::timestamptz;
        EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow
            OR invalid_time_zone_displacement_value THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
        END;
    ELSIF v_mode = 'import_admin' THEN
        IF v_email IS DISTINCT FROM 'admin'
           OR v_name IS DISTINCT FROM '超级管理员'
           OR pg_catalog.jsonb_typeof(p_request->'now') IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(p_request->'request_id') IS DISTINCT FROM 'null' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
        END IF;
    ELSE
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    IF v_mode = 'bootstrap_admin' THEN
        INSERT INTO public.users (
            id, email, name, password_hash, credential_generation, role,
            group_name, group_id, status, is_active, email_verified_at,
            preferences
        ) VALUES (
            v_account_id, v_email, v_name, v_password_hash, 0, 'super_admin',
            NULL, NULL, 'online', true, v_now, '{}'::jsonb
        );
        v_audit_id := aap_audit_v1.record_identity_bootstrap_admin(
            v_account_id, v_email, v_request_id
        );
        IF v_audit_id IS NULL OR v_audit_id <= 0 THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'invalid bootstrap Audit result';
        END IF;
    ELSE
        INSERT INTO public.users (
            id, email, name, password_hash, credential_generation, role,
            group_name, group_id, status, is_active, email_verified_at,
            preferences
        ) VALUES (
            v_account_id, 'admin', '超级管理员', v_password_hash, 0,
            'super_admin', NULL, NULL, 'offline', true, NULL, '{}'::jsonb
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-provisioning/v1',
        'email', v_email,
        'account_id', v_account_id
    );
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_identity_v1.create_seed_persona_account(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_field_count integer;
    v_account_id uuid;
    v_email text;
    v_name text;
    v_password_hash text;
    v_role text;
    v_group_name text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    SELECT pg_catalog.count(*)::integer INTO v_field_count
    FROM pg_catalog.jsonb_object_keys(p_request);
    IF v_field_count <> 7
       OR NOT (p_request ?& ARRAY[
           'schema', 'account_id', 'email', 'name', 'password_hash', 'role',
           'group_name'
       ])
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(p_request) AS field(name)
           WHERE field.name NOT IN (
               'schema', 'account_id', 'email', 'name', 'password_hash', 'role',
               'group_name'
           )
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    IF pg_catalog.jsonb_typeof(p_request->'schema') IS DISTINCT FROM 'string'
       OR p_request->>'schema' IS DISTINCT FROM 'aap-identity-seed-persona/v1'
       OR pg_catalog.jsonb_typeof(p_request->'account_id') IS DISTINCT FROM 'string'
       OR (p_request->>'account_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_request->'email') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'name') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'password_hash') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'role') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request->'group_name') NOT IN ('string', 'null') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    v_email := p_request->>'email';
    v_name := p_request->>'name';
    v_password_hash := p_request->>'password_hash';
    v_role := p_request->>'role';
    v_group_name := p_request->>'group_name';
    BEGIN
        v_account_id := (p_request->>'account_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END;

    IF v_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    INSERT INTO public.users (
        id, email, name, password_hash, credential_generation, role,
        group_name, group_id, status, is_active, email_verified_at, preferences
    ) VALUES (
        v_account_id, v_email, v_name, v_password_hash, 0, v_role,
        v_group_name, NULL, 'offline', true, NULL, '{}'::jsonb
    );

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-seed-persona/v1',
        'email', v_email,
        'account_id', v_account_id
    );
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_identity_fixture_v1.create_account(
    p_id uuid,
    p_email text,
    p_name text,
    p_password_hash text,
    p_role text
)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_account_id uuid;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;

    INSERT INTO public.users (
        id, email, name, password_hash, credential_generation, role,
        status, is_active, preferences
    ) VALUES (
        p_id, p_email, p_name, p_password_hash, 0, p_role,
        'offline', true, '{}'::jsonb
    )
    RETURNING id INTO v_account_id;
    RETURN v_account_id;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_identity_fixture_v1.mark_manual_inactive(
    p_id uuid,
    p_disabled_at timestamptz,
    p_disabled_by uuid
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_row_count integer;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'E2E fixture functions require a test database';
    END IF;

    UPDATE public.users
    SET is_active = false,
        status = 'offline',
        disabled_kind = 'manual',
        disabled_at = p_disabled_at,
        disabled_by = p_disabled_by
    WHERE id = p_id;
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
DROP TRIGGER initialize_account_presence ON public.users;
-- aap-statement-break
DROP FUNCTION aap_identity_v1._initialize_account_presence();
-- aap-statement-break
DROP TABLE public.identity_account_presence;
